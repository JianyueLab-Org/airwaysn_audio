"""收到管制消息时的提示音。

飞行员盯着的是窗外和仪表，不是这个窗口。管制打一行字过来，消息区多一行、
标题不闪不响，人根本不知道——这个模块就是补这一下。

三个决定，都是踩过的坑换来的：

**声音走客户端自己选的那块输出设备**（`settings.output_device_index`），
不是系统默认设备。飞行员的耳机和系统默认输出常常不是同一个，提示音响在
桌面音箱里等于没响。这也是不用 `QSoundEffect` 的原因——PyQt 的多媒体模块
只认系统默认设备，还要给两个 spec 各塞一份 Qt 多媒体插件。

**波形是现场合成的，不带 wav 资源。** `opus.dll` 和 `SimConnect.dll` 那两个
"文件没跟着打包走、程序照样启动、功能静默失效"的坑已经够多了，能不加资源
就不加。合成一次缓存起来，之后每次响都是同一段 bytes。

**pyaudio 是真的要出声时才 import 的**（`_pyaudio()`）。没装 PortAudio 的
机器上这个模块照样导得进来，测试也能塞一个假的进来。

`wants_alert()` 是"这条该不该响"的全部判断，写成纯函数是为了能单测——
界面那边只管调它，然后 `play()`。日志是英文的，界面文字一个字都不在这里。
"""

import array
import logging
import math
import threading
import time

log = logging.getLogger("chime")

RATE = 48000
# 两声短促的上行音（A5 → E6）。上行听起来是"来消息了"，下行听起来像出错。
TONES = ((880.0, 0.085), (1318.5, 0.110))
GAP = 0.02                 # 两声之间的空隙
FADE = 0.006               # 6 ms 淡入淡出。直接切会带一声"啪"的爆音
GAIN = 0.22                # 相对满量程。无线电可能正在响，给它留够余量
MIN_INTERVAL = 1.0         # 两声之间的最短间隔
# 指定设备开不出来时按这个顺序退。蓝牙耳机常常只吃 44.1 kHz。
FALLBACK_RATES = (48000, 44100, 22050)

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _pyaudio():
    """惰性导入，方便测试替换整个模块。"""
    import pyaudio
    return pyaudio


def _clamp_volume(value, default=100):
    try:
        return max(0, min(200, int(value)))
    except (TypeError, ValueError):
        return default


def waveform(rate=RATE, volume=100):
    """合成那两声，返回 16 位单声道 PCM。

    音量按百分比给，和麦克风/扬声器那两根滑条一个量纲（0-200）。
    """
    volume = _clamp_volume(volume)
    key = (int(rate), volume)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached

    scale = GAIN * volume / 100.0
    samples = array.array("h")
    for frequency, seconds in TONES:
        count = max(1, int(rate * seconds))
        fade = max(1, int(rate * FADE))
        for i in range(count):
            # 两头各淡一段，中间满幅。淡出要按"离结尾还有多远"算
            envelope = min(1.0, i / fade, (count - i) / fade)
            value = math.sin(2 * math.pi * frequency * i / rate)
            value = max(-1.0, min(1.0, value * envelope * scale))
            samples.append(int(value * 32767))
        samples.extend([0] * int(rate * GAP))

    data = samples.tobytes()
    with _CACHE_LOCK:
        _CACHE[key] = data
    return data


def _mentions(callsign, body):
    """正文里点到这个呼号没有。

    前后不能再接字母数字，否则呼号 CCA150 会被 "CCA1501, descend" 点到——
    那是另一架飞机的指令，响一声比不响更坏。
    """
    if not callsign:
        return False
    text = (body or "").upper()
    start = 0
    while True:
        at = text.find(callsign, start)
        if at < 0:
            return False
        before = text[at - 1] if at else ""
        after = text[at + len(callsign):at + len(callsign) + 1]
        if not before.isalnum() and not after.isalnum():
            return True
        start = at + 1


def wants_alert(callsign, sender, recipient, body, every_message=False):
    """一条 #TM 该不该响。照 xPilot 的规矩：

    - **私聊**（收件人就是自己的呼号）一定响。管制找你、SUP 找你都走这条，
      这也是本网络上管制指令的主路径。
    - **频率消息**（收件人是 `@xxxxx`）默认只有正文里点到自己呼号的才响。
      一个频率上十几架飞机，每条都响的提示音会被用户当天就关掉；想要每条
      都响的人打开 `message_sound_all`。
    - **广播**（`*`、`*S` 之类）响：那是服务器或者 SUP 发的，条数很少。
    - **自己发出去的不响。** 服务端现在不回显，但这条判断很便宜。
    """
    callsign = (callsign or "").strip().upper()
    sender = (sender or "").strip().upper()
    recipient = (recipient or "").strip().upper()
    if sender and sender == callsign:
        return False
    if recipient.startswith("@"):
        return bool(every_message) or _mentions(callsign, body)
    return True


class Chime:
    """提示音播放器。

    `play()` 可以从任意线程调（FSD 的收包线程会直接调到），不阻塞、不抛异常。
    响不出来只写一行日志——设备被独占、耳机拔了、机器上根本没有 PortAudio，
    这些都不该影响收消息本身。
    """

    def __init__(self, settings=None, min_interval=MIN_INTERVAL):
        self.settings = settings
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._playing = False
        self._last = 0.0
        self._thread = None

    # ---------- 设置 ----------
    def enabled(self):
        return bool(getattr(self.settings, "message_sound", True))

    def volume(self):
        return _clamp_volume(getattr(self.settings, "message_sound_volume", 100))

    def every_message(self):
        return bool(getattr(self.settings, "message_sound_all", False))

    # ---------- 播放 ----------
    def play(self, force=False):
        """放一声，返回真表示这一下真的去放了。

        `force=True` 是设置里的"试听"：用户自己点的，既不看开关也不受最短
        间隔限制，否则连点两下第二下没反应，看着像按钮坏了。
        """
        if not force and not self.enabled():
            return False
        volume = self.volume()
        if volume <= 0:
            return False

        now = time.monotonic()
        with self._lock:
            if self._playing:
                # 上一声还没响完。排队没有意义：五条消息一起到的时候，用户要的
                # 是"有消息"这一个信息，不是连响五声。
                return False
            if not force and now - self._last < self.min_interval:
                return False
            self._playing = True
            self._last = now
            self._thread = threading.Thread(
                target=self._run, args=(volume,), daemon=True)
            self._thread.start()
        return True

    def wait(self, timeout=2.0):
        """等这一声响完。只给测试和退出时用。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, volume):
        try:
            self._write(volume)
        except Exception as e:
            log.info("could not play the message chime: %s", e)
        finally:
            with self._lock:
                self._playing = False

    def _write(self, volume):
        module = _pyaudio()
        audio = module.PyAudio()
        try:
            index = getattr(self.settings, "output_device_index", None)
            stream, rate = self._open(audio, module, index)
            try:
                stream.write(waveform(rate, volume))
            finally:
                try:
                    stream.stop_stream()
                finally:
                    stream.close()
        finally:
            audio.terminate()

    def _open(self, audio, module, index):
        """开一条输出流，返回 (流, 采样率)。

        指定的设备可能只吃 44.1 kHz，也可能在客户端启动之后被拔掉了——那种
        情况下退回系统默认设备总比一声不响强，无线电那条链路自己会报错。
        """
        devices = (index, None) if index is not None else (None,)
        errors = []
        for device in devices:
            for rate in FALLBACK_RATES:
                try:
                    stream = audio.open(format=module.paInt16, channels=1,
                                        rate=rate, output=True,
                                        output_device_index=device)
                except Exception as e:
                    errors.append(f"device={device} rate={rate}: {e}")
                    continue
                if device != index:
                    log.info("the chime fell back to the default output device")
                return stream, rate
        raise RuntimeError("; ".join(errors[-3:]) or "no output device")


class _Preview:
    """试听用的一次性设置：只有设备和音量两项。"""

    def __init__(self, output_device_index=None, volume=100):
        self.output_device_index = output_device_index
        self.message_sound = True
        self.message_sound_volume = volume


def preview(output_device_index=None, volume=100):
    """设置对话框里的"试听"。

    用**对话框里当前选的**设备和音量，而不是已经存下来的那份——用户多半正是
    刚换了耳机才来点这一下的。
    """
    return Chime(_Preview(output_device_index, volume)).play(force=True)
