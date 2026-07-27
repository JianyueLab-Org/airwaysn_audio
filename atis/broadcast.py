"""把通播播到 Mumble 上。

一个席位一条独立的 Mumble 连接，用户名 {cid}_atis{频率}——服务端
server/login.py 认这个格式，并把频率的 6 位数字当成 Murmur 用户 id，所以同一
个账号开多个不同频率的通播不会互相踢掉。

这条连接不打开任何本地音频设备：通播只发合成出来的语音，不碰麦克风，也不该和
管制端抢输入输出设备。
"""

import logging
import os
import re
import tempfile
import threading
import time
import wave

import numpy as np
import pymumble_py3 as pymumble
import pyttsx3
from pymumble_py3 import mumble_pb2
from pymumble_py3.constants import (
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_PERMISSIONDENIED,
    PYMUMBLE_CLBK_SOUNDRECEIVED,
    PYMUMBLE_MSG_TYPES_REJECT,
)

import mumblecompat

# pymumble 用的 ssl.wrap_socket 在 Python 3.12 里已被删除，导入时先补上，
# 否则连接线程一起来就抛 AttributeError
mumblecompat.install()

log = logging.getLogger("通播")

# Mumble 服务器拒绝时给的类型，逐条翻译成人能看懂的原因。
# 全都笼统说成"用户名或密码"会把人引到错误的方向——比如认证器挂了的时候，
# 用户会一直去改密码。
REJECT_REASONS = {
    "WrongUserPW": "密码错误",
    "WrongServerPW": "服务器密码错误",
    "InvalidUsername": "用户名不符合服务器的规则",
    "UsernameInUse": "这个用户名已经在线了",
    "ServerFull": "服务器已满",
    "NoCertificate": "服务器要求证书",
    "AuthenticatorFail": "服务端认证器故障（服务器上的 login.py 可能没在运行）",
    "WrongVersion": "客户端版本不被服务器接受",
}

class RejectAwareMumble(pymumble.Mumble):
    """截下服务器的 Reject 消息，把拒绝类型留下来。

    pymumble 处理 Reject 时只把 reason 字段带进异常（mumble.py 的
    dispatch_control_message），而 Murmur 经常只填 type 不填 reason——于是外面
    拿到一个空字符串，只能说"没有给出原因"。这里在分发之前先把整条消息读出来，
    type 才是真正有用的那个字段。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reject_type = None
        self.reject_reason = None

    def dispatch_control_message(self, type, message):
        if type == PYMUMBLE_MSG_TYPES_REJECT:
            try:
                reject = mumble_pb2.Reject()
                reject.ParseFromString(message)
                self.reject_type = mumble_pb2.Reject.RejectType.Name(reject.type)
                self.reject_reason = reject.reason or ""
                log.warning("服务器拒绝连接: type=%s reason=%r",
                            self.reject_type, self.reject_reason)
            except Exception as e:
                log.warning("解析 Reject 消息失败: %s", e)
        return super().dispatch_control_message(type, message)

    def rejection(self):
        """翻译成人能看懂的原因，没有被拒绝则返回 None。"""
        if not self.reject_type and not self.reject_reason:
            return None
        reason = REJECT_REASONS.get(self.reject_type)
        if reason and self.reject_reason:
            return f"{reason}（服务器附言：{self.reject_reason}）"
        if reason:
            return reason
        if self.reject_reason:
            return self.reject_reason
        return self.reject_type

TARGET_RATE = 48000                          # pymumble 要求 48kHz 单声道 16bit
FRAME_BYTES = int(TARGET_RATE * 0.02) * 2    # 一帧 20ms
PREBUFFER_SECONDS = 0.5                      # 发送时保持的缓冲长度
SILENCE_HOLD = 0.8                           # 频道里静默多久才算空闲
QUIET_WAIT_TIMEOUT = 60.0
CONNECT_TIMEOUT = 15.0
# 建完临时频道到它出现在频道表里，要等服务器回一条 ChannelState。这是一次网络
# 往返，固定 sleep 赌不起——远程服务器上 0.2 秒经常不够，表现出来就是
# "Channel FREQ_xxxxxx does not exists"。
CHANNEL_TIMEOUT = 5.0
REPEAT_GAP = 5.0                             # 两轮播报之间的间隔

# pyttsx3 在同一进程里共享同一个 SAPI 引擎，多个席位同时合成会互相打断
_TTS_LOCK = threading.Lock()

_CHINESE = re.compile(r'[一-鿿]')


def resample(audio, source_rate, target_rate):
    """线性插值重采样到 48kHz。

    SAPI 出来的语音一般是 22050Hz，往上采样不会产生混叠，线性插值对通播语音
    足够。用它换掉 scipy.signal.resample，打包体积能少一半（scipy 光自己就
    七八十兆），也省掉几条容易漏的隐式导入。
    """
    if source_rate == target_rate or len(audio) == 0:
        return audio
    target_length = int(round(len(audio) * target_rate / float(source_rate)))
    if target_length <= 0:
        return audio[:0]
    return np.interp(
        np.linspace(0, len(audio) - 1, target_length),
        np.arange(len(audio)),
        audio,
    )


def _pick_voice(engine, chinese):
    hints = (("chinese", "zh_", "zh-", "huihui", "yaoyao", "kangkang") if chinese
             else ("english", "en_", "en-", "zira", "david", "mark", "hazel"))
    try:
        voices = engine.getProperty('voices')
    except Exception:
        return None
    for voice in voices or []:
        blob = " ".join(str(getattr(voice, attr, ""))
                        for attr in ("id", "name", "languages")).lower()
        if any(hint in blob for hint in hints):
            return voice.id
    return None


class Synthesizer:
    """文本 → 48kHz 单声道 PCM，按文本缓存。"""

    def __init__(self):
        self._engine = None
        self._temp_dir = tempfile.mkdtemp(prefix="atis_")
        self._cache = {}

    def _ready(self):
        if self._engine is None:
            try:
                # SAPI5 走 COM，工作线程要自己初始化
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            self._engine = pyttsx3.init()
            self._engine.setProperty('rate', 150)
            self._engine.setProperty('volume', 0.9)
        return self._engine

    def synthesize(self, text):
        if not text or not text.strip():
            return None
        if text in self._cache:
            return self._cache[text]
        with _TTS_LOCK:
            pcm = self._render(text)
        if pcm:
            self._cache[text] = pcm
        return pcm

    def _render(self, text):
        try:
            engine = self._ready()
            voice_id = _pick_voice(engine, bool(_CHINESE.search(text)))
            if voice_id:
                engine.setProperty('voice', voice_id)

            path = os.path.join(self._temp_dir, "atis.wav")
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

            engine.save_to_file(text, path)
            engine.runAndWait()

            # save_to_file 是异步落盘的，runAndWait 返回后文件可能还没写完
            for _ in range(25):
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    break
                time.sleep(0.2)
            else:
                raise FileNotFoundError("语音文件未生成")

            with wave.open(path, 'rb') as wav:
                channels, width = wav.getnchannels(), wav.getsampwidth()
                rate = wav.getframerate()
                frames = wav.readframes(wav.getnframes())

            if width != 2:
                raise ValueError(f"不支持的采样宽度: {width * 8} bit")
            if not frames:
                raise ValueError("语音文件为空")

            audio = np.frombuffer(frames, dtype=np.int16)
            if channels > 1:
                audio = audio.reshape(-1, channels).mean(axis=1)
            audio = resample(audio, rate, TARGET_RATE)
            audio = np.clip(audio, np.iinfo(np.int16).min,
                            np.iinfo(np.int16).max).astype(np.int16)

            # 结尾补 0.2 秒静音，免得最后一个音节被截掉
            tail = np.zeros(int(TARGET_RATE * 0.2), dtype=np.int16)
            pcm = np.concatenate([audio, tail]).tobytes()

            try:
                os.remove(path)
            except Exception:
                pass
            log.info(f"合成完成 {len(pcm) / (2.0 * TARGET_RATE):.1f} 秒")
            return pcm
        except Exception as e:
            log.warning(f"语音合成失败: {e}")
            return None

    def cleanup(self):
        self._engine = None
        try:
            for name in os.listdir(self._temp_dir):
                try:
                    os.remove(os.path.join(self._temp_dir, name))
                except Exception:
                    pass
            os.rmdir(self._temp_dir)
        except Exception:
            pass


class Broadcaster:
    """一个席位的语音播出。on_state(state, message) 在后台线程调用。"""

    def __init__(self, server, cid, password, station, on_state=None):
        self.server = server
        self.cid = str(cid).strip()
        self.password = password
        self.station = station
        self.on_state = on_state

        self.user = f"{self.cid}_atis{str(station.frequency_khz).zfill(6)}"
        self.running = False
        self.stop_event = threading.Event()
        self.mumble = None
        self.thread = None

        self._synth = Synthesizer()
        self._connected = False
        self._denial = None          # 服务器拒绝某个动作时的说明
        self._last_other_sound = 0.0
        self._text_lock = threading.Lock()
        self._voice_text = ""
        self._pending_text = None

    # ---------- 状态 ----------
    def _state(self, state, message):
        log.info("%s %s: %s", self.user, state, message)
        if self.on_state:
            try:
                self.on_state(state, message)
            except Exception as e:
                log.warning(f"状态回调出错: {e}")

    # ---------- 生命周期 ----------
    def start(self, voice_text):
        if self.running:
            return
        with self._text_lock:
            self._voice_text = voice_text
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def update_text(self, voice_text):
        """换稿。下一轮播报时生效，不打断正在播的这一遍。"""
        with self._text_lock:
            self._pending_text = voice_text

    def stop(self):
        self.running = False
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.thread = None
        self._disconnect()
        self._synth.cleanup()

    def _run(self):
        try:
            if not self._connect():
                return
            self._loop()
        except Exception as e:
            self._state('error', f"播报异常: {e}")
        finally:
            self._disconnect()

    # ---------- 连接 ----------
    def _on_connected(self):
        self._connected = True

    def _on_sound(self, user, soundchunk):
        # 开了接收就必须及时取走，否则 pymumble 会一直堆内存
        try:
            user.sound.get_sound()
        except Exception:
            pass
        try:
            name = user["name"]
        except Exception:
            name = ""
        if name and name != self.user:
            self._last_other_sound = time.time()

    def _connect(self):
        self._state('connecting', f"正在以 {self.user} 连接 {self.server} …")
        try:
            self.mumble = RejectAwareMumble(self.server, self.user,
                                            password=self.password, reconnect=True)
            self.mumble.set_receive_sound(True)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, self._on_connected)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound)
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_PERMISSIONDENIED,
                                               self._on_permission_denied)
            self.mumble.start()
        except Exception as e:
            self._state('error', f"连接失败: {e}")
            return False

        deadline = time.time() + CONNECT_TIMEOUT
        while not self._connected and time.time() < deadline:
            if not self.running or self.stop_event.is_set():
                return False
            if not self.mumble.is_alive():
                # 线程没了：要么服务器发了 Reject，要么客户端自己出错了。
                # 后者以前会被一律说成"服务器拒绝"，把人往密码上引。
                reason = self.mumble.rejection()
                self._state('error',
                            f"语音服务器拒绝了 {self.user}：{reason}" if reason else
                            f"到 {self.server} 的连接意外中断，"
                            f"服务器没有说明原因（详见日志）")
                return False
            time.sleep(0.1)

        if not self._connected:
            reason = self.mumble.rejection()
            self._state('error', f"连接 {self.server} 超时" +
                        (f"：{reason}" if reason else "，服务器没有响应"))
            return False

        return self._join_channel()

    def _find_channel(self, name):
        try:
            return self.mumble.channels.find_by_name(name)
        except pymumble.errors.UnknownChannelError:
            return None

    def _join_channel(self):
        name = self.station.channel
        try:
            channel = self._find_channel(name)
            if channel is None:
                self._denial = None
                log.info("频道 %s 不存在，建一个临时的", name)
                self.mumble.channels.new_channel(0, name, temporary=True)
                channel = self._wait_for_channel(name)

            if channel is None:
                # 分清"服务器拒绝"和"没等到"。以前两种都报成频道不存在，
                # 而前者再等多久也不会有。
                if self._denial:
                    self._state('error',
                                f"服务器不允许建立频道 {name}：{self._denial}")
                else:
                    self._state('error',
                                f"建立频道 {name} 后 {CHANNEL_TIMEOUT:.0f} 秒内"
                                f"没有出现，服务器没有说明原因")
                return False

            self.mumble.users.myself.move_in(channel["channel_id"])
            self._state('online', f"已在 {self.station.frequency} 播出")
            return True
        except Exception as e:
            self._state('error', f"进入频道失败: {e}")
            return False

    def _wait_for_channel(self, name):
        """等服务器把新建的频道回报回来。

        建频道是发一条消息就返回，频道要等服务器回 ChannelState 才会进本地表。
        服务器拒绝的话（缺 MakeTempChannel 权限）不会有 ChannelState，只有一条
        PermissionDenied——那种情况下等下去没有意义，收到就立刻退出。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline:
            if not self.running or self.stop_event.is_set():
                return None
            if self._denial:
                return None
            channel = self._find_channel(name)
            if channel is not None:
                return channel
            time.sleep(0.1)
        return self._find_channel(name)

    def _on_permission_denied(self, event):
        """服务器拒绝了某个动作。

        建频道要根频道的 MakeTempChannel 权限（ACL 里的 0x400），Mumble 默认
        ACL 不一定给。没有这条回报的话，只会看到"频道不存在"，完全猜不到是被
        权限挡了。
        """
        try:
            kind = self.mumble.denial_type(event.type)
        except Exception:
            kind = str(getattr(event, "type", "?"))
        messages = {
            "Permission": "没有权限（建立频率频道需要根频道的 MakeTempChannel 权限）",
            "ChannelName": "频道名不合服务器的规矩",
            "NestingLimit": "频道层级超过了服务器上限",
            "ChannelCountLimit": "服务器上的频道数已达上限",
        }
        self._denial = messages.get(kind, f"服务器拒绝了操作: {kind}")
        if getattr(event, "reason", ""):
            self._denial += f"（{event.reason}）"
        log.warning("%s: %s", self.station.callsign, self._denial)

    def _disconnect(self):
        if self.mumble:
            try:
                self.mumble.stop()
            except Exception as e:
                log.warning(f"断开出错: {e}")
            self.mumble = None
        self._connected = False

    # ---------- 播报 ----------
    def _channel_is_quiet(self):
        return (time.time() - self._last_other_sound) >= SILENCE_HOLD

    def _wait_for_quiet(self):
        deadline = time.time() + QUIET_WAIT_TIMEOUT
        announced = False
        while self.running and not self.stop_event.is_set():
            if self._channel_is_quiet():
                return True
            if not announced:
                self._state('online', "频率上有通话，等待中…")
                announced = True
            if self.stop_event.wait(0.2) or time.time() > deadline:
                return False
        return False

    def _transmit(self, pcm):
        """按 20ms 帧节流发送；中途有人讲话就让路。"""
        position, total = 0, len(pcm)
        while position < total and self.running and not self.stop_event.is_set():
            if not self._channel_is_quiet():
                self._state('online', "有人讲话，中止本轮播报")
                return False
            try:
                buffered = self.mumble.sound_output.get_buffer_size()
            except Exception:
                buffered = 0
            if buffered > PREBUFFER_SECONDS:
                if self.stop_event.wait(0.05):
                    return False
                continue

            chunk = pcm[position:position + FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:
                chunk += b'\x00' * (FRAME_BYTES - len(chunk))
            try:
                self.mumble.sound_output.add_sound(chunk)
            except Exception as e:
                self._state('error', f"发送音频失败: {e}")
                return False
            position += FRAME_BYTES

        # 等缓冲排空，免得下一轮的静默判断被自己的尾音干扰
        deadline = time.time() + 5
        while self.running and not self.stop_event.is_set() and time.time() < deadline:
            try:
                if self.mumble.sound_output.get_buffer_size() <= 0:
                    break
            except Exception:
                break
            if self.stop_event.wait(0.05):
                break
        return position >= total

    def _loop(self):
        while self.running and not self.stop_event.is_set():
            with self._text_lock:
                if self._pending_text is not None:
                    self._voice_text = self._pending_text
                    self._pending_text = None
                    self._state('online', "已换用新的通播稿")
                text = self._voice_text

            pcm = self._synth.synthesize(text)
            if pcm is None:
                self._state('error', "语音合成失败，请检查系统 TTS 语音")
                return

            if not self._wait_for_quiet():
                break
            self._state('online', f"正在播报 {self.station.letter}")
            spoken = self._transmit(pcm)

            if not self.running or self.stop_event.is_set():
                break
            if self.stop_event.wait(REPEAT_GAP if spoken else 1.0):
                break

        self._state('stopped', "通播已停止")
