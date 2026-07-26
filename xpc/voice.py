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
from pymumble_py3.constants import PYMUMBLE_CLBK_SOUNDRECEIVED

log = logging.getLogger("语音")

SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]
FORMAT = pyaudio.paInt16
CHANNELS = 1
RX_TIMEOUT = 0.5          # 这么久没有新音频就把接收灯灭掉


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
        self._lock = threading.Lock()      # 一条连接一个发送队列，串行化
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
        return bool(self.mumble and self.mumble.connected)

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
        log.info("音频就绪：%d Hz，每帧 %d 采样", self._rate, self._chunk)

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

        self._status('online', f"语音已连接（{self.username}）")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        if self._pending is not None:
            self.set_frequency(self._pending)

    def stop(self):
        self.running = False

        # 先收线程再动 PyAudio。反过来的话发送线程可能正卡在 stream.read()
        # 里，C 层被 terminate 掉是直接崩，Python 的 try/except 接不住。
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

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
    def set_frequency(self, frequency):
        """COM1 变了。频率一样就什么都不做。"""
        if frequency is None:
            return
        frequency = round(float(frequency), 3)
        if frequency == self.frequency:
            return
        self._pending = frequency
        if not self.connected:
            return

        name = channel_name(frequency)
        try:
            try:
                channel = self.mumble.channels.find_by_name(name)
            except pymumble.errors.UnknownChannelError:
                log.info("频道 %s 不存在，建一个临时的", name)
                self.mumble.channels.new_channel(0, name, temporary=True)
                time.sleep(0.3)
                channel = self.mumble.channels.find_by_name(name)

            myself = self.mumble.users.myself
            if myself and myself["channel_id"] != channel["channel_id"]:
                myself.move_in(channel["channel_id"])
            self.frequency = frequency
            self.channel = name
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
        log.debug("PTT %s", "按下" if value else "松开")
        if self.on_ptt:
            self.on_ptt(value)

    def _on_sound(self, user, chunk):
        """pymumble 的库线程调用。"""
        try:
            myself = self.mumble.users.myself if self.mumble else None
            if myself and user["name"] == myself["name"]:
                return
            self._last_rx = time.time()
            if not self.receiving:
                self.receiving = True
                if self.on_rx:
                    self.on_rx(True)

            volume = getattr(self.settings, "speaker_volume", 100) / 100.0
            samples = np.frombuffer(chunk.pcm, dtype=np.int16)
            samples = (samples * volume).astype(np.int16)
            if self._output:
                self._output.write(samples.tobytes())
        except Exception as e:
            log.debug("播放收到的音频出错: %s", e)

    def _run(self):
        """发送线程：按住 PTT 就把麦克风送上去，同时管接收灯的超时。"""
        while self.running:
            try:
                if self.receiving and time.time() - self._last_rx > RX_TIMEOUT:
                    self.receiving = False
                    if self.on_rx:
                        self.on_rx(False)

                if not (self.transmitting and self.connected and self._input):
                    time.sleep(0.02)
                    continue

                myself = self.mumble.users.myself
                if not myself or not myself["channel_id"]:
                    time.sleep(0.05)
                    continue

                try:
                    data = self._input.read(self._chunk, exception_on_overflow=False)
                except Exception as e:
                    log.debug("读麦克风出错: %s", e)
                    time.sleep(0.05)
                    continue

                volume = getattr(self.settings, "mic_volume", 100) / 100.0
                samples = (np.frombuffer(data, dtype=np.int16) * volume).astype(np.int16)
                with self._lock:
                    self.mumble.sound_output.add_sound(samples.tobytes())
            except Exception as e:
                log.debug("发送线程出错: %s", e)
                time.sleep(0.1)
