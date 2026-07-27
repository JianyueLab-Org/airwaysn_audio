"""Mumble 语音。

对应 xPilot 的 audio 层。和 client/ xplane_client/ 里那两份最大的差别是：
**这里不自己去问频率**。XPlaneLink 已经在持续订阅 COM1 了，界面把频率喂进
set_frequency() 就行，省掉一整套轮询和"频道对不对"的反复检查。

频率到频道的换算是全网统一的约定，改了会同时打断飞行员、管制和 ATIS：

    freq_value   = int(round(频率 * 1000))        # 125.400 -> 125400
    channel_name = f"FREQ_{freq_value:06d}"       # -> FREQ_125400
"""

import logging
import threading
import time

import mumblecompat

# pymumble 建 TLS 用的 ssl.wrap_socket 在 Python 3.12 里已被删除。它自己那个
# except AttributeError 的兜底又调回同一个函数，异常从 pymumble 的线程里抛出
# 去，外面只看到线程死了，报成"服务器拒绝连接"。必须在 import 之前补。
mumblecompat.install()

import numpy as np
import pyaudio
import pymumble_py3 as pymumble
from pymumble_py3 import messages
from pymumble_py3.constants import (PYMUMBLE_CLBK_SOUNDRECEIVED,
                                    PYMUMBLE_CONN_STATE_CONNECTED)

log = logging.getLogger("语音")

SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]
FORMAT = pyaudio.paInt16
CHANNELS = 1
RX_TIMEOUT = 0.5          # 这么久没有新音频就把接收灯灭掉
# 建完临时频道到它出现在频道表里，要等服务器回一条 ChannelState。这是一次网络
# 往返，固定 sleep 赌不起——远程服务器上经常不够。
CHANNEL_TIMEOUT = 5.0
# 没切到目标频道时多久重试一次。切换要能自愈——刚上线那几秒 mumble 常常还没
# 就绪，一次失败就永远留在根频道是最难查的故障。
CHANNEL_RETRY_INTERVAL = 1.0
ROOT_CHANNEL = 0


def channel_name(frequency_mhz):
    """频率（MHz）对应的 Mumble 频道名。"""
    return f"FREQ_{int(round(float(frequency_mhz) * 1000)):06d}"


class Voice:
    """一条 Mumble 连接，跟着 COM1 走。

    回调都在后台线程触发：
        on_status(state, message)   connecting / online / error / stopped
        on_ptt(bool) / on_rx(bool)
        on_channel(frequency, channel_name)
    """

    def __init__(self, host, username, password="", settings=None,
                 on_status=None, on_ptt=None, on_rx=None, on_channel=None):
        self.host = host
        self.username = username
        self.password = password
        self.settings = settings

        self.on_status = on_status
        self.on_ptt = on_ptt
        self.on_rx = on_rx
        self.on_channel = on_channel

        self.mumble = None
        self.frequency = None
        self.channel = None
        self.transmitting = False
        self.receiving = False
        self.running = False

        self._audio = None
        self._input = None
        self._output = None
        self._rate = 48000
        self._chunk = 960
        self._last_rx = 0.0
        self._sent_frames = 0           # 本次 PTT 已发出的帧数
        self._received_frames = 0       # 本段接收已播放的帧数
        self._skip_reason = ""          # 明明按着 PTT 却没发的原因
        self._stuck_reason = ""         # 迟迟进不了频率频道的原因
        self._lock = threading.Lock()      # 一条连接一个发送队列，串行化
        self._channel_lock = threading.Lock()   # 频道切换不能并发
        self._channel_wanted = threading.Event()  # 有新频率要切
        self._channel_thread = None
        self._pending = None               # 还没切过去的频率
        self._thread = None

    # ---------- 状态 ----------
    def _status(self, state, message):
        log.info("%s: %s", state, message)
        if self.on_status:
            try:
                self.on_status(state, message)
            except Exception as e:
                log.warning("状态回调出错: %s", e)

    @property
    def connected(self):
        """真的连上了才算。

        pymumble 的 connected 是状态码不是布尔：0 未连接、1 认证中、2 已连接、
        3 失败。原来写 bool(...)，**失败的 3 也是真值**，于是连接被服务器拒绝
        之后我们照样当成连上了——实测日志里就是这样：Mumble 回了
        "Wrong certificate or password"，界面还报"语音已连接"。
        """
        if not self.mumble:
            return False
        return self.mumble.connected == PYMUMBLE_CONN_STATE_CONNECTED

    # ---------- 音频设备 ----------
    def _open_audio(self):
        self._audio = pyaudio.PyAudio()
        self._rate = self._best_rate()
        self._chunk = int(self._rate * 0.02)         # 20 ms 一帧
        if self._rate != 48000:
            log.warning("采样率退到 %d Hz。pymumble 按 48 kHz 送音频且这里不做重采样，"
                        "声音会变调", self._rate)

        input_index = getattr(self.settings, "input_device_index", None)
        output_index = getattr(self.settings, "output_device_index", None)
        self._input = self._audio.open(
            format=FORMAT, channels=CHANNELS, rate=self._rate, input=True,
            frames_per_buffer=self._chunk, input_device_index=input_index)
        self._output = self._audio.open(
            format=FORMAT, channels=CHANNELS, rate=self._rate, output=True,
            frames_per_buffer=self._chunk, output_device_index=output_index)
        # 把真正打开的设备名记下来。"听不到/说不出"最常见的原因就是选错了
        # 设备（比如麦克风指到了不存在的虚拟声卡），只报采样率看不出来。
        log.info("音频就绪：%d Hz，每帧 %d 采样；麦克风=%s，扬声器=%s",
                 self._rate, self._chunk,
                 self._device_name(input_index, True),
                 self._device_name(output_index, False))

    def _device_name(self, index, is_input):
        try:
            if index is None:
                info = (self._audio.get_default_input_device_info() if is_input
                        else self._audio.get_default_output_device_info())
                return f"[系统默认] {info.get('name', '?')}"
            return self._audio.get_device_info_by_index(index).get("name", "?")
        except Exception as e:
            return f"取不到设备信息（{e}）"

    def _best_rate(self):
        input_index = getattr(self.settings, "input_device_index", None)
        output_index = getattr(self.settings, "output_device_index", None)

        def works(rate, index, is_input):
            try:
                stream = self._audio.open(
                    format=FORMAT, channels=CHANNELS, rate=rate,
                    input=is_input, output=not is_input,
                    input_device_index=index if is_input else None,
                    output_device_index=None if is_input else index,
                    frames_per_buffer=960, start=False)
                stream.close()
                return True
            except Exception:
                return False

        for rate in SAMPLE_RATES:
            if works(rate, input_index, True) and works(rate, output_index, False):
                return rate
        log.warning("没有一个候选采样率能用，硬上 48000 Hz")
        return 48000

    def reopen_audio(self):
        """换了设备之后重开音频流。"""
        for stream in (self._input, self._output):
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
        self._input = self._output = None
        try:
            if self._audio:
                self._audio.terminate()
        except Exception:
            pass
        self._open_audio()

    # ---------- 生命周期 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._status('connecting', f"正在连接语音服务器 {self.host} …")
        try:
            self._open_audio()
        except Exception as e:
            self.running = False
            self._status('error', f"打不开音频设备: {e}")
            return

        try:
            self.mumble = pymumble.Mumble(self.host, self.username,
                                          password=self.password, reconnect=True)
            self.mumble.set_receive_sound(True)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED,
                                               self._on_sound)
            self.mumble.start()
            self.mumble.is_ready()
        except Exception as e:
            self.running = False
            self._status('error', f"语音服务器连接失败: {e}")
            return

        # is_ready() 返回不代表连上了：服务器拒绝时 pymumble 的连接线程会带着
        # ConnectionRejectedError 直接死掉，而 is_ready() 照样放行。实测里
        # 用户名填错，Mumble 回 "Wrong certificate or password"，界面却报
        # "语音已连接"，然后一切都莫名其妙地不工作。
        if not self.connected:
            self.running = False
            self._status('error',
                         f"语音服务器拒绝了 {self.username}（用户名或密码不对？）")
            return

        self._status('online', f"语音已连接（{self.username}）")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._channel_thread = threading.Thread(target=self._channel_loop,
                                                daemon=True)
        self._channel_thread.start()

        # 连上之前记下的频率现在可以切了
        if self._pending is not None:
            self._channel_wanted.set()

    def _channel_loop(self):
        """频道切换的工作线程。

        单独一条线程，不跟发送线程挤：切换要等服务器回 ChannelState，而发送
        线程是 20 ms 一帧的节奏，把切换放进去会把 PTT 一起卡住。

        **每轮都比对目标和当前，而不是等一个事件。** 原来是事件驱动的：
        set_frequency 置位、这里消费掉。只要那一次切换没成功——比如刚上线时
        mumble 还没就绪、或者建频道等超时——事件就没了，而 _pending 没变，
        set_frequency 又会直接 return，于是永远不再重试。实测就是这样：人一直
        留在根频道，对着没人的地方发，也收不到任何东西。
        """
        while self.running:
            # 事件只用来让新频率立刻生效，不作为唯一触发条件
            self._channel_wanted.wait(timeout=CHANNEL_RETRY_INTERVAL)
            self._channel_wanted.clear()
            if not self.running:
                break

            target = self._pending
            if target is None:
                self._note_stuck("还没有拿到 COM1 频率")
                continue
            if target == self.frequency:
                self._stuck_reason = ""      # 到位了
                continue
            if not self.connected:
                self._note_stuck("语音服务器还没连上")
                continue
            try:
                self._switch_channel(target)
            except Exception as e:
                log.warning("切换频道出错: %s", e)

    def _note_stuck(self, reason):
        """迟迟切不过去时说明原因。

        原来这两个分支是静默 continue 的：日志里既没有"建一个临时的"也没有
        任何错误，只剩下"PTT 一帧都没发"，完全看不出卡在哪一步。
        """
        if reason == self._stuck_reason:
            return
        self._stuck_reason = reason
        log.warning("还没能进入频率频道：%s", reason)

    def stop(self):
        self.running = False

        # 先收线程再动 PyAudio。反过来的话发送线程可能正卡在 stream.read()
        # 里，C 层被 terminate 掉是直接崩，Python 的 try/except 接不住。
        self._channel_wanted.set()      # 叫醒切换线程好让它看到 running=False
        for thread in (self._thread, self._channel_thread):
            if (thread and thread.is_alive()
                    and thread is not threading.current_thread()):
                thread.join(timeout=2)
        self._thread = self._channel_thread = None

        for stream in (self._input, self._output):
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
        self._input = self._output = None
        try:
            if self._audio:
                self._audio.terminate()
        except Exception:
            pass
        self._audio = None

        if self.mumble:
            try:
                self.mumble.stop()
            except Exception:
                pass
            self.mumble = None
        self._status('stopped', "语音已断开")

    # ---------- 频率 ----------
    def _find_channel(self, name):
        try:
            return self.mumble.channels.find_by_name(name)
        except pymumble.errors.UnknownChannelError:
            return None

    def _create_channel(self, name):
        """在根下建一个临时频道，**不要阻塞**。

        pymumble 的 channels.new_channel() 走 execute_command(blocking=True)，
        那个 acquire 没有任何超时——它自己的源码里就写着
        "TODO: manage a timeout for blocking commands"。命令一旦没被处理，这里
        就永远卡住，而且我们还握着 _channel_lock，整条切换链全死。

        实测就是这样：日志停在"建一个临时的"，之后既没有成功也没有任何错误，
        因为线程根本没从这一行返回。

        自己发命令、不等锁；频道有没有建出来由 _wait_for_channel 轮询判断，
        那本来就是更可靠的判据——服务器拒绝建频道时也不会干等。
        """
        command = messages.CreateChannel(0, name, True)
        self.mumble.execute_command(command, blocking=False)

    def _wait_until_in(self, channel_id):
        """等服务器确认我们真的进了这个频道。"""
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            myself = self.mumble.users.myself
            if myself and myself["channel_id"] == channel_id:
                return True
            time.sleep(0.1)
        return False

    def _wait_for_channel(self, name):
        """等服务器把新建的频道回报回来。

        new_channel() 只是发一条消息就返回，频道要等服务器回 ChannelState 才
        进本地表——这是一次网络往返。原来固定 sleep(0.3) 再找，连远程服务器时
        经常还没回来，日志里就是连着两条 "Channel FREQ_121700 does not exists"，
        看着像频道建不了，其实只是没等到。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            channel = self._find_channel(name)
            if channel is not None:
                return channel
            time.sleep(0.1)
        return self._find_channel(name)

    def set_frequency(self, frequency):
        """COM1 变了。**必须立刻返回**。

        这个方法是从 gui.py 的 tick() 调的，而 tick() 跑在 Qt 主线程上。真正
        的切换要建频道、等服务器回 ChannelState，是一次网络往返，最坏要等满
        CHANNEL_TIMEOUT——在主线程上干这件事窗口会直接"未响应"（实测过）。

        所以这里只记下目标频率并叫醒工作线程，切换在那边做。
        """
        if frequency is None:
            return
        frequency = round(float(frequency), 3)
        if frequency == self._pending:
            return
        self._pending = frequency
        self._channel_wanted.set()

    def _switch_channel(self, frequency):
        """真正的切换。只在工作线程里跑。

        整个过程上锁：start() 里的补切和工作线程会同时进来，两边各建一次
        频道、各报一次错（真实日志里就是这样）。
        """
        if not self.connected:
            return

        with self._channel_lock:
            if frequency == self.frequency:
                return          # 等锁的时候已经被另一个调用切过去了
            name = channel_name(frequency)
            try:
                channel = self._find_channel(name)
                if channel is None:
                    log.info("频道 %s 不存在，建一个临时的", name)
                    self._create_channel(name)
                    channel = self._wait_for_channel(name)
                if channel is None:
                    log.warning("建立频道 %s 后 %.0f 秒内没有出现，%.0f 秒后重试",
                                name, CHANNEL_TIMEOUT, CHANNEL_RETRY_INTERVAL)
                    return

                myself = self.mumble.users.myself
                if myself and myself["channel_id"] != channel["channel_id"]:
                    # move_in() 也走 execute_command(blocking=True)，和建频道
                    # 一样会无限期卡住，同样自己发命令
                    self.mumble.execute_command(
                        messages.MoveCmd(self.mumble.users.myself_session,
                                         channel["channel_id"]),
                        blocking=False)
                    # 命令是异步的，确认真的进去了再记账——否则收敛循环会以为
                    # 成功而不再重试，人却还留在原地
                    if not self._wait_until_in(channel["channel_id"]):
                        log.warning("发出了进入 %s 的请求，但 %.0f 秒内没有生效，"
                                    "稍后重试", name, CHANNEL_TIMEOUT)
                        return

                self.frequency = frequency
                self.channel = name
                self._stuck_reason = ""
                log.info("已切到 %s（%.3f MHz）", name, frequency)
                if self.on_channel:
                    self.on_channel(frequency, name)
            except Exception as e:
                log.warning("切换到 %s 失败: %s", name, e)

    # ---------- 收发 ----------
    def set_transmitting(self, value):
        """PTT。界面和快捷键都走这里。"""
        value = bool(value)
        if value == self.transmitting:
            return
        self.transmitting = value
        if value:
            self._sent_frames = 0
            self._skip_reason = ""
            log.info("PTT 按下")
        else:
            # 松开时把这一次到底发出去多少帧说清楚。"语音用不了"最常见的两种
            # 情况——根本没进发送分支、和发了但对方听不到——只有这个数能分开。
            if self._sent_frames:
                log.info("PTT 松开，本次发出 %d 帧（约 %.1f 秒）",
                         self._sent_frames, self._sent_frames * 0.02)
            else:
                log.warning("PTT 松开，一帧都没发出去：%s",
                            self._skip_reason or "原因不明")
        if self.on_ptt:
            self.on_ptt(value)

    def _skip(self, reason):
        """记下这一轮为什么没发。同一个原因只记一次，别刷屏。"""
        if reason != self._skip_reason:
            self._skip_reason = reason
            log.debug("暂时不发送: %s", reason)

    def _on_sound(self, user, chunk):
        """pymumble 的库线程调用。"""
        try:
            myself = self.mumble.users.myself if self.mumble else None
            if myself and user["name"] == myself["name"]:
                return
            self._last_rx = time.time()
            if not self.receiving:
                self.receiving = True
                self._received_frames = 0
                log.info("收到 %s 的语音", user.get("name", "?"))
                if self.on_rx:
                    self.on_rx(True)

            volume = getattr(self.settings, "speaker_volume", 100) / 100.0
            samples = np.frombuffer(chunk.pcm, dtype=np.int16)
            samples = (samples * volume).astype(np.int16)
            if not self._output:
                # 收到了但扬声器没开——"听不到别人"和"根本没人说话"是两回事
                log.warning("收到语音但扬声器没有打开，听不到")
                return
            self._output.write(samples.tobytes())
            self._received_frames += 1
        except Exception as e:
            log.warning("播放收到的音频出错: %s", e)

    def _run(self):
        """发送线程：按住 PTT 就把麦克风送上去，同时管接收灯的超时。"""
        while self.running:
            try:
                if self.receiving and time.time() - self._last_rx > RX_TIMEOUT:
                    self.receiving = False
                    log.info("接收结束，本段 %d 帧（约 %.1f 秒）",
                             self._received_frames, self._received_frames * 0.02)
                    if self.on_rx:
                        self.on_rx(False)

                if not self.transmitting:
                    time.sleep(0.02)
                    continue
                if not self.connected:
                    self._skip("语音服务器未连接")
                    time.sleep(0.02)
                    continue
                if not self._input:
                    self._skip("麦克风没有打开")
                    time.sleep(0.02)
                    continue

                myself = self.mumble.users.myself
                if not myself:
                    self._skip("服务器还没回报我们自己的用户信息")
                    time.sleep(0.05)
                    continue
                # 注意是 is None：根频道的 channel_id 就是 0，写成 not 会把
                # "在根频道"当成"没进频道"，PTT 于是一声不吭地什么都不做。
                if myself["channel_id"] is None:
                    self._skip("还没有进入任何频道")
                    time.sleep(0.05)
                    continue
                if myself["channel_id"] == ROOT_CHANNEL and self.channel is None:
                    # 还在根频道说明频道切换没成功。发出去也没人听得到，而且
                    # 会打扰根频道里的人——说清楚，别让它看起来像正常工作。
                    self._skip("还留在根频道（频率频道没切成功），发出去没人听得到")
                    time.sleep(0.05)
                    continue

                try:
                    data = self._input.read(self._chunk, exception_on_overflow=False)
                except Exception as e:
                    self._skip(f"读麦克风出错: {e}")
                    time.sleep(0.05)
                    continue

                volume = getattr(self.settings, "mic_volume", 100) / 100.0
                samples = (np.frombuffer(data, dtype=np.int16) * volume).astype(np.int16)
                with self._lock:
                    self.mumble.sound_output.add_sound(samples.tobytes())
                self._sent_frames += 1
            except Exception as e:
                log.debug("发送线程出错: %s", e)
                time.sleep(0.1)
