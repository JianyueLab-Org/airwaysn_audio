"""语音连接：一条 Mumble 连接同时守听/发话多个频率。

TrackAudio 靠 AFV 天然支持多频率，我们的后端是 Mumble——一个用户只能待在一个
频道里，所以用两个机制拼出同样的效果：

    收：频道监听（Mumble 1.4 起的 listening_channel），一次可以监听多个频道
    发：VoiceTarget（whisper），一条目标里可以带多个频道

两个都要绕开 pymumble 的封装直接发 protobuf：pymumble 没有监听频道的接口，
whisper 那边 `id==1` 时只取第一个频道（mumble.py 里写死的），也带不了多个。

服务端如果是 Mumble 1.3，监听频道会被忽略——那种情况下只有主频率（真正进入的
那个频道）能听到声音，靠 listeners_confirmed 暴露给界面提示用户。
"""

import threading
import time

import numpy as np
import pyaudio
import pymumble_py3 as pymumble
from pymumble_py3 import mumble_pb2
from pymumble_py3.constants import (
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_DISCONNECTED,
    PYMUMBLE_CLBK_PERMISSIONDENIED,
    PYMUMBLE_CLBK_SOUNDRECEIVED,
    PYMUMBLE_MSG_TYPES_USERSTATE,
    PYMUMBLE_MSG_TYPES_VOICETARGET,
)

import radiostack

SUPPORTED_SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]
WHISPER_TARGET_ID = 1          # VoiceTarget 的编号，1-30 任选
RX_TIMEOUT = 0.5               # 多久没收到话音就认为对方松开了
CONNECT_TIMEOUT = 15.0
PING_TIMEOUT = 5.0             # 超过这么久没有 ping 回复就认为断线

# pymumble 默认 10 秒 ping 一次、断线后 10 秒才重连，掉线要很久才能被发现。
# 调快之后配合下面的 _connection_monitor，断线基本能在几秒内反映到界面上。
pymumble.mumble.PYMUMBLE_PING_DELAY = 1
pymumble.mumble.PYMUMBLE_CONNECTION_RETRY_INTERVAL = 2


class VoiceClient:
    """管制席位的语音连接。

    回调都在后台线程触发，界面那边要自己转到 Qt 线程：
        on_state(state, message)          connecting / online / error / stopped
        on_rx(frequency_khz, active, callsign)
        on_tx(active)
    """

    def __init__(self, server, cid, password, on_state=None, on_rx=None, on_tx=None,
                 on_connection_change=None):
        self.server = server
        self.cid = str(cid).strip()
        self.password = password
        self.on_state = on_state
        self.on_rx = on_rx
        self.on_tx = on_tx
        self.on_connection_change = on_connection_change

        self.mumble = None
        self.connected = False
        # 收到过监听频道的话音才算证实服务端支持频道监听。安静不能当作不支持——
        # 频率上没人说话是常态，据此报警只会天天误报。
        self.listeners_confirmed = False
        self.running = False

        # 音频
        self.audio = None
        self.input_stream = None
        self.output_stream = None
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 48000
        self.CHUNK = 960
        self._input_device = None
        self._output_device = None
        self._stream_lock = threading.Lock()

        self.mic_volume = 100
        self.speaker_volume = 100
        self.transmitting = False

        # 频率 ↔ 频道
        self._channel_ids = {}           # frequency_khz -> channel_id
        self._channel_to_khz = {}        # channel_id -> frequency_khz
        self._listening = set()
        self._tx_channels = []
        self._xc_channels = []
        self._sent_target = None         # 上次发出去的发话目标，避免重复发
        self._volumes = {}               # frequency_khz -> 0-100
        self._last_rx = {}               # frequency_khz -> 最后收到话音的时间

        self._tx_thread = None
        self._rx_monitor = None
        self._connection_thread = None
        self._last_connected = False
        self._last_ping_rcv = time.time()

    # ---------- 状态回报 ----------
    def _state(self, state, message):
        print(f"[语音] {state}: {message}")
        if self.on_state:
            try:
                self.on_state(state, message)
            except Exception as e:
                print(f"[语音] 状态回调出错: {e}")

    # ---------- 连接 ----------
    def connect(self):
        """阻塞式连接。成功返回 True。"""
        self._state('connecting', f"正在连接 {self.server} …")
        try:
            self.audio = pyaudio.PyAudio()
            self.RATE = self._find_best_sample_rate()
            self.CHUNK = int(self.RATE * 0.02)

            self.mumble = pymumble.Mumble(self.server, self.cid,
                                          password=self.password, reconnect=True)
            self.mumble.set_receive_sound(True)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, self._on_connected)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED, self._on_disconnected)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_PERMISSIONDENIED,
                                               self._on_permission_denied)
            self.mumble.start()
        except Exception as e:
            self._state('error', f"连接失败: {e}")
            return False

        deadline = time.time() + CONNECT_TIMEOUT
        while not self.connected and time.time() < deadline:
            if not self.mumble.is_alive():
                self._state('error', "连接被拒绝，请检查用户名或密码")
                return False
            time.sleep(0.1)

        if not self.connected:
            self._state('error', "连接超时，可能是用户名或密码错误")
            return False

        try:
            self.setup_audio()
        except Exception as e:
            self._state('error', f"音频设备打开失败: {e}")
            return False

        self.running = True
        # 同步初始状态，避免监控线程第一圈就误报一次状态变化
        self._last_connected = True
        self._last_ping_rcv = time.time()

        self._rx_monitor = threading.Thread(target=self._rx_monitor_loop, daemon=True)
        self._rx_monitor.start()
        self._connection_thread = threading.Thread(target=self._connection_monitor, daemon=True)
        self._connection_thread.start()

        self._state('online', f"已连接，账号 {self.cid}")
        if self.on_connection_change:
            self.on_connection_change(True)
        return True

    def _on_connected(self):
        self.connected = True

    def _on_disconnected(self):
        if self.running:
            self._state('error', "与语音服务器的连接已断开")
        self.connected = False
        if self.on_connection_change:
            self.on_connection_change(False)

    def _connection_monitor(self):
        """盯着连接是否还活着。

        光看 pymumble 的 connected 标志不够——网线拔掉之后它还会长时间停在
        "已连接"。所以同时看 ping_stats 里最后一次收到 ping 回复的时间，超过
        PING_TIMEOUT 就判定断线。
        """
        while self.running:
            try:
                try:
                    last_rcv = self.mumble.ping_stats.get('last_rcv', 0)
                    if last_rcv:
                        self._last_ping_rcv = last_rcv / 1000.0
                except Exception:
                    pass

                alive = bool(self.mumble and self.mumble.connected)
                if alive and time.time() - self._last_ping_rcv > PING_TIMEOUT:
                    alive = False
                    print(f"[语音] ping 超时 {time.time() - self._last_ping_rcv:.1f}s，判定断线")

                if alive != self._last_connected:
                    self._last_connected = alive
                    self.connected = alive
                    print(f"[语音] 连接状态变化: {alive}")
                    if self.on_connection_change:
                        self.on_connection_change(alive)
            except Exception as e:
                if self.running:
                    print(f"[语音] 连接监控出错: {e}")
            time.sleep(1)

    def _on_permission_denied(self, event):
        """服务器拒绝某个动作时说明白原因。

        频道监听有两个服务端上限（mumble-server.ini 的 listenersperuser /
        listenersperchannel，默认不限），超了服务器会明确回 ChannelListenerLimit
        或 UserListenerLimit——没有这条回报的话，管制员只会看到某些频率一直安静，
        根本猜不到是被服务器挡了。
        """
        try:
            kind = self.mumble.denial_type(event.type)
        except Exception:
            kind = str(getattr(event, "type", "?"))

        messages = {
            "UserListenerLimit": "服务器限制了每个用户能监听的频道数，部分频率收不到",
            "ChannelListenerLimit": "服务器限制了单个频道的监听人数，这个频率收不到",
            "Permission": "没有权限（频道监听需要 Listen 权限）",
        }
        reason = messages.get(kind, f"服务器拒绝了操作: {kind}")
        if getattr(event, "reason", ""):
            reason += f"（{event.reason}）"
        self._state('denied', reason)

    def disconnect(self):
        self.running = False
        self.transmitting = False
        self.connected = False

        for thread in (self._tx_thread, self._rx_monitor, self._connection_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)
        self._tx_thread = None
        self._rx_monitor = None
        self._connection_thread = None

        self._close_streams()
        if self.audio:
            try:
                self.audio.terminate()
            except Exception as e:
                print(f"[语音] 释放音频设备出错: {e}")
            self.audio = None
        if self.mumble:
            try:
                self.mumble.stop()
            except Exception as e:
                print(f"[语音] 断开连接出错: {e}")
            self.mumble = None
        self._state('stopped', "已断开")

    # ---------- 音频设备 ----------
    def _find_best_sample_rate(self):
        def works(rate):
            try:
                probe = self.audio.open(
                    format=self.FORMAT, channels=self.CHANNELS, rate=rate,
                    input=True, frames_per_buffer=960, start=False,
                    input_device_index=self._input_device)
                probe.close()
                return True
            except Exception:
                return False

        for rate in SUPPORTED_SAMPLE_RATES:
            if works(rate):
                if rate != 48000:
                    print(f"[语音] 设备不支持 48kHz，退到 {rate} Hz（音调会有偏差）")
                return rate
        return 48000

    def setup_audio(self, input_device=None, output_device=None):
        """打开输入输出流。不传设备时沿用上次选定的。"""
        if input_device is not None:
            self._input_device = input_device
        if output_device is not None:
            self._output_device = output_device

        with self._stream_lock:
            self._close_streams_locked()
            self.input_stream = self.audio.open(
                format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
                input=True, frames_per_buffer=self.CHUNK,
                input_device_index=self._input_device)
            self.output_stream = self.audio.open(
                format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
                output=True, frames_per_buffer=self.CHUNK,
                output_device_index=self._output_device)

    def _close_streams(self):
        with self._stream_lock:
            self._close_streams_locked()

    def _close_streams_locked(self):
        for name in ("input_stream", "output_stream"):
            stream = getattr(self, name, None)
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception as e:
                    print(f"[语音] 关闭{name}出错: {e}")
                setattr(self, name, None)

    def set_mic_volume(self, percent):
        self.mic_volume = max(0, min(200, int(percent)))

    def set_speaker_volume(self, percent):
        self.speaker_volume = max(0, min(200, int(percent)))

    # ---------- 频道 ----------
    def _resolve_channel(self, khz):
        """拿到频率对应的频道 id，没有就建一个临时频道。"""
        if khz in self._channel_ids:
            return self._channel_ids[khz]

        name = radiostack.channel_name(khz)
        try:
            channel = self.mumble.channels.find_by_name(name)
        except pymumble.errors.UnknownChannelError:
            try:
                self.mumble.channels.new_channel(0, name, temporary=True)
                time.sleep(0.2)
                channel = self.mumble.channels.find_by_name(name)
            except Exception as e:
                print(f"[语音] 无法创建频道 {name}: {e}")
                return None

        channel_id = channel["channel_id"]
        self._channel_ids[khz] = channel_id
        self._channel_to_khz[channel_id] = khz
        return channel_id

    def sync(self, stack):
        """把电台栈的状态推到服务器：进主频道、监听其余 RX、设好 TX 目标。"""
        if not self.connected or not self.mumble:
            return

        self._volumes = {r.frequency_khz: r.effective_volume() for r in stack}

        rx_channels = {}
        for khz in stack.rx_frequencies():
            channel_id = self._resolve_channel(khz)
            if channel_id is not None:
                rx_channels[khz] = channel_id

        # 主频率：真正进入的那个频道。服务端不支持监听时，至少这个频率能听到
        primary = stack.selected_khz if stack.selected_khz in rx_channels else None
        if primary is None and rx_channels:
            primary = next(iter(rx_channels))
        if primary is not None:
            self._join(rx_channels[primary])

        # 其余频率用频道监听
        wanted = {cid for khz, cid in rx_channels.items() if khz != primary}
        self._set_listening(wanted)

        # 发话目标
        self._tx_channels = [self._resolve_channel(khz) for khz in stack.tx_frequencies()]
        self._tx_channels = [c for c in self._tx_channels if c is not None]
        self._xc_channels = [self._resolve_channel(khz) for khz in stack.xc_frequencies()]
        self._xc_channels = [c for c in self._xc_channels if c is not None]
        self._set_voice_target(self._tx_channels)

    def _join(self, channel_id):
        try:
            if self.mumble.users.myself.get("channel_id") != channel_id:
                self.mumble.users.myself.move_in(channel_id)
        except Exception as e:
            print(f"[语音] 进入频道失败: {e}")

    def _set_listening(self, channel_ids):
        """频道监听。pymumble 没封装，直接发 UserState。"""
        add = channel_ids - self._listening
        remove = self._listening - channel_ids
        if not add and not remove:
            return

        try:
            state = mumble_pb2.UserState()
            state.session = self.mumble.users.myself_session
            for channel_id in add:
                state.listening_channel_add.append(channel_id)
            for channel_id in remove:
                state.listening_channel_remove.append(channel_id)
            self.mumble.send_message(PYMUMBLE_MSG_TYPES_USERSTATE, state)
            self._listening = set(channel_ids)
        except Exception as e:
            print(f"[语音] 设置频道监听失败: {e}")

    def _set_voice_target(self, channel_ids, force=False):
        """VoiceTarget：一次带上所有要发话的频道。

        sync() 在任何状态变化后都会被调用（包括拖动音量条），目标没变就别再发一遍。
        """
        if not self.mumble:
            return
        key = tuple(channel_ids)
        if not force and key == self._sent_target:
            return
        self._sent_target = key
        try:
            target = mumble_pb2.VoiceTarget()
            target.id = WHISPER_TARGET_ID
            for channel_id in channel_ids:
                entry = target.targets.add()
                entry.channel_id = channel_id
            self.mumble.send_message(PYMUMBLE_MSG_TYPES_VOICETARGET, target)
        except Exception as e:
            print(f"[语音] 设置发话目标失败: {e}")

    # ---------- 收 ----------
    def _on_sound(self, user, soundchunk):
        # 断线重连的空档里 myself 可能还没建立，早期版本在这里崩过
        if not self.mumble or not self.mumble.users.myself:
            return
        try:
            if user["name"] == self.mumble.users.myself["name"]:
                return
        except Exception:
            pass
        if not soundchunk or getattr(soundchunk, "pcm", None) is None:
            return

        channel_id = user.get("channel_id")
        khz = self._channel_to_khz.get(channel_id)
        if khz is None:
            return                      # 不是我们关心的频率

        if channel_id != self.mumble.users.myself.get("channel_id"):
            self.listeners_confirmed = True   # 收到了监听频道的话音，服务端确实支持

        now = time.time()
        first = khz not in self._last_rx or now - self._last_rx[khz] > RX_TIMEOUT
        self._last_rx[khz] = now
        if first and self.on_rx:
            try:
                self.on_rx(khz, True, user["name"])
            except Exception as e:
                print(f"[语音] RX 回调出错: {e}")

        try:
            audio = np.frombuffer(soundchunk.pcm, dtype=np.int16)
            if not len(audio):
                return
            scale = (self.speaker_volume / 100.0) * (self._volumes.get(khz, 100) / 100.0)
            audio = np.clip(audio * scale, np.iinfo(np.int16).min,
                            np.iinfo(np.int16).max).astype(np.int16)
            with self._stream_lock:
                if self.output_stream:
                    self.output_stream.write(audio.tobytes())
        except Exception as e:
            print(f"[语音] 播放收到的话音出错: {e}")

        self._forward_cross_couple(khz, soundchunk)

    def _forward_cross_couple(self, khz, soundchunk):
        """交叉耦合：在一个 XC 频率上收到的话音，转发到其它 XC 频率。"""
        if len(self._xc_channels) < 2:
            return
        source = self._channel_ids.get(khz)
        if source not in self._xc_channels:
            return
        others = [c for c in self._xc_channels if c != source]
        if not others:
            return
        try:
            self._set_voice_target(others, force=True)
            self.mumble.sound_output.target = WHISPER_TARGET_ID
            self.mumble.sound_output.add_sound(soundchunk.pcm)
        except Exception as e:
            print(f"[语音] 交叉耦合转发失败: {e}")
        finally:
            # 必须恢复，否则管制员下一次 PTT 会发到错误的频率上
            self._set_voice_target(self._tx_channels, force=True)
            if not self.transmitting:
                self.mumble.sound_output.target = 0

    def _rx_monitor_loop(self):
        while self.running:
            now = time.time()
            for khz, last in list(self._last_rx.items()):
                if now - last > RX_TIMEOUT:
                    del self._last_rx[khz]
                    if self.on_rx:
                        try:
                            self.on_rx(khz, False, "")
                        except Exception as e:
                            print(f"[语音] RX 回调出错: {e}")
            time.sleep(0.1)

    # ---------- 发 ----------
    def start_transmit(self):
        if self.transmitting or not self.connected:
            return
        if not self._tx_channels:
            return                       # 没有任何频率开了 TX
        self.transmitting = True
        if self.on_tx:
            self.on_tx(True)
        self._tx_thread = threading.Thread(target=self._transmit_loop, daemon=True)
        self._tx_thread.start()

    def stop_transmit(self):
        if not self.transmitting:
            return
        self.transmitting = False
        if self.on_tx:
            self.on_tx(False)

    def _transmit_loop(self):
        try:
            self.mumble.sound_output.target = WHISPER_TARGET_ID
        except Exception as e:
            print(f"[语音] 切换发话目标失败: {e}")

        while self.transmitting and self.running:
            try:
                with self._stream_lock:
                    stream = self.input_stream
                    if not stream:
                        break
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                if data:
                    audio = np.frombuffer(data, dtype=np.int16)
                    audio = np.clip(audio * (self.mic_volume / 100.0),
                                    np.iinfo(np.int16).min,
                                    np.iinfo(np.int16).max).astype(np.int16)
                    # 断线期间不要往外灌音频，否则会在缓冲里堆积
                    if not self.connected:
                        continue
                    self.mumble.sound_output.add_sound(audio.tobytes())
            except Exception as e:
                print(f"[语音] 录音出错: {e}")
                time.sleep(0.1)
            time.sleep(0.001)

        try:
            self.mumble.sound_output.target = 0
        except Exception:
            pass
