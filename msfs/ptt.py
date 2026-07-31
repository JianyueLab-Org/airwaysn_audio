"""PTT 的输入源：键盘、摇杆按钮、摇杆帽键、鼠标侧键。

controller / xpc / msfs 各存一份**逐字节相同**的副本，和 voice.py、applog.py、
mumblecompat.py 一样——这个仓库每个组件都从自己目录平铺导入，共享父模块要在
每份 spec 里做路径拼接。改一处就得三处一起改，test_ptt.py 的 SharedCopyTest
会在它们不一致时失败。

为什么是一串绑定，而不是三个字段
--------------------------------
原来 xpc/msfs 里是 `ptt_key` 一个字段加 `joystick_ptt` 一个字段，判断写在 GUI 的
轮询循环里，controller 干脆只有键盘。再加鼠标就是第三个字段、第三个分支，三个
客户端各写一遍。这里统一成一串绑定，**任意一个按住就算按住**，加一种输入源只是
多一个 kind。

三个源取法不一样，合不到一起
----------------------------
键盘和鼠标用 pynput 的全局监听器：事件驱动，按下就回调，没有轮询延迟。摇杆只能
轮询——SDL 要在自己的线程里 pump 事件队列，pygame 没有全局钩子可挂。所以键盘和
鼠标是即时的，摇杆是 POLL_INTERVAL 的粒度（20 ms，和一帧音频同长，听不出来）。

帽键（POV）为什么要单算一种
--------------------------
飞行摇杆和轭上的 PTT 十有八九在帽键上（Saitek/Logitech 的 Pro Flight Yoke、
Honeycomb Alpha 都是），而 SDL 把帽键报成 hat 而不是按钮——`get_button()` 一辈子
也看不见它。少了这一种，用户在"添加绑定"里按自己的 PTT 什么都不会发生，对话框
就一直停在"请按下…"，看起来像是这个界面坏了。帽键要 `get_hat()` 读，给出的是
一对 (x, y)，所以它是 HAT 这个 kind，不是 JOYSTICK 的一个按钮号。

两条硬规则
----------
- **鼠标只认侧键 X1 / X2**。监听是全局的：把左键绑成 PTT，用户在任何窗口点任何
  东西都会把语音发出去，而且自己完全看不出来——发话灯亮在被挡住的窗口里。左键、
  右键、中键在这里根本不接受（`mouse_name()` 返回空），不是"不推荐"。
- **绝不 suppress**。pynput 可以吞掉事件，吞了之后那个键在别的程序里就打不出来
  了；PTT 键在模拟器里往往另有用途，抢走它比没有 PTT 更糟。

回调在监听线程上跑（pynput 的线程，或摇杆轮询线程），碰 Qt 控件必须经 pyqtSignal
转回 GUI 线程。on_change 只在状态真的翻转时调一次，不是每帧都调。
"""

import logging
import os
import threading
import time

log = logging.getLogger("ptt")

# 摇杆轮询间隔。和音频帧一样是 20 ms——比这更密只是白烧一个核，更稀就能听出
# 话头被切掉一点。
POLL_INTERVAL = 0.02
# 摇杆打不开或者中途拔了，隔多久试一次。热插拔在飞行摇杆上很常见。
JOYSTICK_RETRY = 3.0
# 一直打不开时，每试这么多次再抱怨一遍。3 秒试一次、20 次一说，就是一分钟一行；
# 每次都说的话，真实日志里是四十多条一模一样的 WARNING，把别的线索全挤出去了。
OPEN_COMPLAIN_EVERY = 20

KEYBOARD = "keyboard"
MOUSE = "mouse"
JOYSTICK = "joystick"
HAT = "hat"
KINDS = (KEYBOARD, MOUSE, JOYSTICK, HAT)

# 能绑的鼠标键。左/中/右不在这里，理由见模块开头。
MOUSE_BUTTONS = ("x1", "x2")

# 帽键方向 → pygame get_hat() 给的 (x, y)。八个方向都收：八向帽键能稳稳停在
# 斜角上，只认四个正方向的话，那种位置根本录不进来。
HAT_DIRECTIONS = {
    "up": (0, 1),
    "down": (0, -1),
    "left": (-1, 0),
    "right": (1, 0),
    "up_left": (-1, 1),
    "up_right": (1, 1),
    "down_left": (-1, -1),
    "down_right": (1, -1),
}

# 界面上帽键方向怎么显示。用箭头而不是词：这个模块不产生界面文字（见 token()），
# 而箭头在两种语言里是同一个箭头，不用翻译。
HAT_ARROWS = {
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "up_left": "↖",
    "up_right": "↗",
    "down_left": "↙",
    "down_right": "↘",
}


def hat_direction(value):
    """pygame 的 (x, y) → 方向名。回中或读不懂就返回空字符串。"""
    try:
        pair = (int(value[0]), int(value[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return ""
    for name, vector in HAT_DIRECTIONS.items():
        if vector == pair:
            return name
    return ""


def hat_pressed(value, direction):
    """帽键现在的位置算不算按住了 direction。

    正方向只比对应的那个分量：绑了"上"的人把帽键推到右上，想说的还是话——正好
    卡在斜角上就哑掉是最难自查的一种故障，界面上一切正常。斜方向则要两个分量
    都对，否则"右上"会被"上"和"右"同时盖住，等于把一条斜向绑定拆成了两条。
    """
    vector = HAT_DIRECTIONS.get(direction)
    if vector is None:
        return False
    try:
        x, y = int(value[0]), int(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return False
    dx, dy = vector
    if dx and dy:
        return (x, y) == (dx, dy)
    if dx:
        return x == dx
    return y == dy


def key_name(key):
    """把 pynput 的按键对象规范成一个字符串。

    可打印字符取 char 并转小写：大小写对 PTT 没有意义，而按住 Shift 时同一个键
    给出的 char 是不一样的，不转小写就会出现"设置时录的是 v，用的时候按 Shift+V
    不响"。功能键取 name（`space`、`ctrl_l`）。两个都没有就退回虚拟键码——小键盘
    和多媒体键在某些布局上确实只有 vk。
    """
    char = getattr(key, "char", None)
    if char:
        return char.lower()
    name = getattr(key, "name", None)
    if name:
        return name.lower()
    vk = getattr(key, "vk", None)
    if vk is not None:
        return "vk%d" % vk
    return str(key).lower()


def mouse_name(button):
    """pynput 的鼠标键 → "x1" / "x2"，不是侧键就返回空字符串。

    按**名字**认而不是拿枚举比：Windows 和 macOS 上是 `Button.x1` / `Button.x2`，
    X11 上同一个物理键 pynput 叫 `button8` / `button9`。拿枚举比的话，代码在
    Windows 上能跑、在 Linux 上静默失效。
    """
    name = getattr(button, "name", str(button)).lower()
    if name in ("x1", "button8"):
        return "x1"
    if name in ("x2", "button9"):
        return "x2"
    return ""


class Binding:
    """一条 PTT 绑定。

    `to_dict()` 的结果直接进设置文件，所以这些字段名就是配置里的键名——改名等于
    让所有老配置里的绑定静默失效（程序照常起来，PTT 不响，用户以为是麦克风坏了）。
    """

    def __init__(self, kind, key="", button=None, device=0, device_name="",
                 hat=None, direction=""):
        self.kind = kind
        self.key = key or ""            # 键盘：规范化后的键名
        self.button = button            # 鼠标："x1"/"x2"；摇杆：按钮序号（int）
        self.device = int(device or 0)  # 摇杆/帽键：设备序号
        self.device_name = device_name or ""   # 摇杆：设备名，只用来在界面上认人
        self.hat = int(hat) if hat is not None else None   # 帽键：序号
        self.direction = direction or ""       # 帽键：方向，见 HAT_DIRECTIONS

    def __eq__(self, other):
        if not isinstance(other, Binding):
            return NotImplemented
        # device_name 不参与比较：同一个手柄换个 USB 口，SDL 给的名字可能带上
        # 不同的后缀，但绑定还是那一条。
        return self._identity() == other._identity()

    def __hash__(self):
        return hash(self._identity())

    def _identity(self):
        return (self.kind, self.key, self.button, self.device,
                self.hat, self.direction)

    def __repr__(self):
        return "<Binding %s %s>" % (self.kind, self.token())

    def token(self):
        """界面上显示的那个"键名"，**不含任何可翻译的词**。

        "鼠标侧键 1"这种说法由调用方拿 i18n 去拼——这个模块不产生界面文字，
        免得三份副本里各躺一套中文，翻译时漏掉其中两份。
        """
        if self.kind == KEYBOARD:
            return self.key.upper() if len(self.key) == 1 else self.key
        if self.kind == MOUSE:
            return str(self.button).upper()
        if self.kind == HAT:
            # "0↑"：帽键序号加一个箭头。箭头是符号不是词，两种语言通用。
            return "%s%s" % (self.hat if self.hat is not None else 0,
                             HAT_ARROWS.get(self.direction, ""))
        return str(self.button)

    def to_dict(self):
        if self.kind == KEYBOARD:
            return {"kind": KEYBOARD, "key": self.key}
        if self.kind == MOUSE:
            return {"kind": MOUSE, "button": self.button}
        if self.kind == HAT:
            return {"kind": HAT, "hat": self.hat, "direction": self.direction,
                    "device": self.device, "device_name": self.device_name}
        return {"kind": JOYSTICK, "button": self.button,
                "device": self.device, "device_name": self.device_name}

    @classmethod
    def from_dict(cls, data):
        """读一条绑定，读不懂就返回 None。

        配置文件是用户能手改的，也可能来自更新的版本。坏掉的一条应该被丢掉并
        记进日志，而不是让整个客户端起不来。
        """
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        try:
            if kind == KEYBOARD:
                key = str(data.get("key") or "").lower()
                return cls(KEYBOARD, key=key) if key else None
            if kind == MOUSE:
                button = str(data.get("button") or "").lower()
                return cls(MOUSE, button=button) if button in MOUSE_BUTTONS else None
            if kind == JOYSTICK:
                button = int(data["button"])
                if button < 0:
                    return None
                return cls(JOYSTICK, button=button,
                           device=int(data.get("device", 0)),
                           device_name=str(data.get("device_name") or ""))
            if kind == HAT:
                hat = int(data["hat"])
                direction = str(data.get("direction") or "").lower()
                if hat < 0 or direction not in HAT_DIRECTIONS:
                    return None
                return cls(HAT, hat=hat, direction=direction,
                           device=int(data.get("device", 0)),
                           device_name=str(data.get("device_name") or ""))
        except (TypeError, ValueError, KeyError) as e:
            log.warning("ignoring a malformed PTT binding %r: %s", data, e)
            return None
        log.warning("ignoring a PTT binding of unknown kind %r", kind)
        return None


def keyboard_binding(key):
    return Binding(KEYBOARD, key=str(key or "").lower())


def load(data, legacy_key=None, legacy_joystick=None):
    """从配置里读出绑定表。

    `data` 是配置里的 `ptt_bindings`。没有这一项时（第一次用新版本）就拿老的
    `ptt_key` / `joystick_ptt` 两个字段升上来，这样用户原来设的 PTT 键不会在
    升级之后悄悄失效——那是最难自查的一种故障，界面上一切正常，只是没人听得见。
    """
    if data is None:
        bindings = []
        if legacy_key:
            bindings.append(keyboard_binding(legacy_key))
        if legacy_joystick is not None:
            try:
                if int(legacy_joystick) >= 0:
                    bindings.append(Binding(JOYSTICK, button=int(legacy_joystick)))
            except (TypeError, ValueError):
                pass
        if bindings:
            log.info("upgraded the old PTT settings into %d binding(s)", len(bindings))
        return bindings
    if not isinstance(data, list):
        log.warning("the PTT bindings are not a list, ignoring them")
        return []
    out = []
    for item in data:
        binding = Binding.from_dict(item)
        if binding is not None and binding not in out:
            out.append(binding)
    return out


def dump(bindings):
    return [b.to_dict() for b in bindings]


def describe(bindings):
    """一串绑定在**日志**里的样子，英文。

    和 i18n.binding_label() 是两件事，故意分开：那个给界面，是中文；这个给日志，
    是英文（仓库的规矩是看字符串最后落到哪里，不是看它写在哪个模块）。

    有它是因为 "PTT watcher started with 1 binding(s)" 这句话等于什么都没说——
    真实日志里正是这一句，看不出那一条绑的是键盘还是摇杆、哪个设备、哪个按钮，
    而"PTT 不响"的排查第一步恰好就是这个。
    """
    if not bindings:
        return "none"
    out = []
    for b in bindings:
        where = ""
        if b.kind in (JOYSTICK, HAT):
            where = " on device %d%s" % (
                b.device, " (%s)" % b.device_name if b.device_name else "")
        if b.kind == KEYBOARD:
            out.append("key %s" % b.token())
        elif b.kind == MOUSE:
            out.append("mouse %s" % b.token())
        elif b.kind == HAT:
            out.append("hat %s%s" % (b.token(), where))
        else:
            out.append("button %s%s" % (b.token(), where))
    return ", ".join(out)


def describe_devices(pygame=None):
    """现在接着哪些摇杆，写成一行日志。

    绑定指着一个打不开的序号时，这是唯一能分清"设备换了序号"和"根本没插"的
    东西——两者的解法完全不同，而报错本身长得一模一样。

    **只在出错的路径上调。** 读名字要真的把每个设备开一下再关掉（pygame 没有
    "不打开就取名字"的接口），正常情况下没必要去碰用户正在飞的摇杆。顺利的时候
    _start_joystick() 只报一个数量。
    """
    if pygame is None:
        pygame = _import_pygame()
    if pygame is None:
        return "pygame is unavailable"
    try:
        count = pygame.joystick.get_count()
    except Exception as e:
        return "could not be counted (%s)" % e
    sticks = available_joysticks(pygame)
    if not sticks:
        if not count:
            return "none"
        return "%d connected, but none of them could be opened to read a name" % count
    return "%d connected: %s" % (
        count, ", ".join("#%d %s" % (index, name) for index, name in sticks))


def available_joysticks(pygame=None):
    """接着的摇杆：[(序号, 名字)]。摇杆不可用时返回空表，不抛异常。

    设置界面拿它填设备下拉框，日志那边经 describe_devices() 用它报设备清单。
    **调用前必须先把 PttWatcher 停掉**，理由见 PttCapture 的注释。

    `pygame` 传得进来是给轮询线程用的：那两个线程手里已经有初始化好的模块了，
    不能再自己去调 _import_pygame()（理由见那个函数）。
    """
    try:
        if pygame is None:
            pygame = _import_pygame()
        if pygame is None:
            return []
        if not pygame.joystick.get_init():
            pygame.joystick.init()
        out = []
        for index in range(pygame.joystick.get_count()):
            # 一个设备打不开不该让整张表作废——正要排查的往往就是"有一个开不了"，
            # 这时候更需要看见另外几个是什么
            try:
                stick = pygame.joystick.Joystick(index)
            except Exception as e:
                log.debug("joystick %d could not be opened to read its name: %s",
                          index, e)
                continue
            try:
                stick.init()
                out.append((index, stick.get_name()))
            except Exception as e:
                log.debug("joystick %d could not be read: %s", index, e)
            finally:
                # 只是列一下，别占着设备
                try:
                    stick.quit()
                except Exception:
                    pass
        return out
    except Exception as e:
        log.warning("could not list the joysticks: %s", e)
        return []


def _import_pygame():
    """惰性导入 pygame，失败返回 None。

    **必须从主线程（界面线程）调用，绝不能在轮询线程里调。** 这不是讲究，是
    Windows 上摇杆 PTT 会不会用的问题：SDL 的 DirectInput 后端要一个 HWND 才能
    调 SetCooperativeLevel，那个隐藏窗口是 `SDL_InitSubSystem(JOYSTICK)` 建的，
    按 Windows 的规矩归**建它的那个线程**所有；线程一结束，系统就把它名下的窗口
    全销毁，而 SDL 把句柄记在全局里，既不知道也不会重建。于是"在轮询线程里 init"
    的写法是这样一条曲线：第一次好用，等那个线程退出——用户打开一次设置就会——
    之后每一次打开摇杆都是

        could not open joystick 0: IDirectInputDevice8::SetCooperativeLevel()
        DirectX error 0x80070006

    （0x80070006 就是 E_HANDLE，句柄无效），一直到重启客户端为止。真实日志里的
    样子最能说明问题：用户在设置里录下自己摇杆上的 PTT，那一下是成功的，回头
    watcher 一起来就再也打不开了，试到最后只好改绑鼠标侧键。所以初始化放在
    PttWatcher._start_joystick() / PttCapture.start() 里做，两个都在界面线程上，
    活得和进程一样久；两个轮询线程只拿现成的模块用。

    惰性有两个理由。一是没插摇杆的用户占绝大多数，为他们初始化一遍 SDL 没意思；
    二是 pygame 在有些平台上装不上（比如新版本的 Python 还没有轮子），而没有摇杆
    不该让键盘 PTT 也一起没有——这个模块的三个源必须能各自失败。

    SDL 的两个后端必须在 import 之前关掉，否则它会和 PyQt6 抢视频、和 PyAudio
    抢音频。用 setdefault 而不是直接赋值：gui.py 在更早的地方已经设过一次，
    这里只是保证"就算调用方忘了，也不会错"。
    """
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    try:
        import pygame
    except Exception as e:
        log.info("pygame is unavailable, joystick PTT is off: %s", e)
        return None
    try:
        if not pygame.get_init():
            pygame.init()
        if not pygame.joystick.get_init():
            pygame.joystick.init()
    except Exception as e:
        log.warning("could not initialise pygame: %s", e)
        return None
    return pygame


def _import_pynput():
    """惰性导入 pynput。

    在 macOS 上第一次建监听器会弹辅助功能授权，在没有图形界面的地方会直接抛，
    所以这里也不能在模块导入时就做。
    """
    try:
        from pynput import keyboard as pynput_keyboard
        from pynput import mouse as pynput_mouse
        return pynput_keyboard, pynput_mouse
    except Exception as e:
        log.warning("pynput is unavailable, keyboard and mouse PTT are off: %s", e)
        return None, None


class PttWatcher:
    """把三个源合成一个"现在按住了没有"。

    `on_change(bool)` 只在状态**翻转**时调用，在监听线程上。三个源互不影响：
    摇杆打不开不会让键盘也失灵，pynput 没授权也不会让摇杆停摆。

    只启用真正绑到的源。这不只是省资源——在 macOS 上建一个全局键盘监听会弹
    辅助功能授权框，用户只绑了摇杆却被要求授权监听键盘，看起来像流氓软件。
    """

    def __init__(self, bindings=None, on_change=None):
        self.on_change = on_change
        self._lock = threading.Lock()
        self._bindings = list(bindings or [])
        # 各源当前按下的东西：键盘是键名，鼠标是 "x1"/"x2"，摇杆是 (设备, 按钮)，
        # 帽键是 (设备, 帽键号, 方向)
        self._down = {KEYBOARD: set(), MOUSE: set(),
                      JOYSTICK: set(), HAT: set()}
        self._state = False
        self._running = False
        self._keyboard_listener = None
        self._mouse_listener = None
        self._joystick_thread = None

    # ---------- 生命周期 ----------
    def start(self):
        if self._running:
            return
        self._running = True
        self._sync_sources()
        log.info("PTT watcher started with %d binding(s): %s",
                 len(self._bindings), describe(self._bindings))

    def stop(self):
        """停掉所有源，并且把 PTT 落下。

        落下这一步不能省：停的时候要是正按着，语音那边的发送线程会一直以为还按着
        （on_change 再也不会来了），麦克风就一直开着。
        """
        self._running = False
        self._stop_keyboard()
        self._stop_mouse()
        self._stop_joystick()
        with self._lock:
            for pressed in self._down.values():
                pressed.clear()
            was = self._state
            self._state = False
        if was:
            self._notify(False)
        log.info("PTT watcher stopped")

    def set_bindings(self, bindings):
        """换一套绑定。设置对话框保存之后调用，不用重启客户端。

        换完要重算一次：把正按着的那条绑定删掉时，得让 PTT 落下，否则麦克风就
        卡在开着的状态，而界面上已经没有任何东西显示它是被谁按住的。
        """
        with self._lock:
            self._bindings = list(bindings or [])
        if self._running:
            self._sync_sources()
        self._recompute()

    def bindings(self):
        with self._lock:
            return list(self._bindings)

    def is_down(self):
        with self._lock:
            return self._state

    def is_running(self):
        """监听开着没有。

        界面拿它决定"关掉设置对话框之后要不要把监听开回来"——PTT 只在连上之后
        才监听，没连的时候开设置再关掉，不该凭空多出一个监听器。
        """
        return self._running

    # ---------- 状态合成 ----------
    def _recompute(self):
        """按下的东西 ∩ 绑定表 → 新状态，翻转了才回调。"""
        with self._lock:
            state = False
            for b in self._bindings:
                if b.kind == KEYBOARD and b.key in self._down[KEYBOARD]:
                    state = True
                elif b.kind == MOUSE and b.button in self._down[MOUSE]:
                    state = True
                elif b.kind == JOYSTICK and (b.device, b.button) in self._down[JOYSTICK]:
                    state = True
                elif b.kind == HAT and \
                        (b.device, b.hat, b.direction) in self._down[HAT]:
                    state = True
                if state:
                    break
            if state == self._state:
                return
            self._state = state
        # 回调放在锁外面。它一路走到 voice.set_transmitting()，那里还会再拿别的锁；
        # 而且界面上的"重设 PTT"会回头调 set_bindings —— 在锁里回调就是死锁。
        self._notify(state)

    def _notify(self, state):
        """回调，并且吞掉它抛的异常。

        回调是别人的代码（GUI 的信号发射）。它抛异常时这里必须继续：`stop()` 里
        那次落下 PTT 的回调要是抛出去，停的过程就断在半路，麦克风留在开着的状态
        ——而客户端看上去已经关掉了。
        """
        if not self.on_change:
            return
        try:
            self.on_change(state)
        except Exception as e:
            log.warning("the PTT callback raised: %s", e)

    def _press(self, kind, token):
        with self._lock:
            if token in self._down[kind]:
                return                      # 键盘的自动重复，不算新的按下
            self._down[kind].add(token)
        self._recompute()

    def _release(self, kind, token):
        with self._lock:
            self._down[kind].discard(token)
        self._recompute()

    # ---------- 源的开关 ----------
    def _sync_sources(self):
        """按当前绑定表决定哪几个源该开着。"""
        with self._lock:
            kinds = {b.kind for b in self._bindings}
        if KEYBOARD in kinds:
            self._start_keyboard()
        else:
            self._stop_keyboard()
        if MOUSE in kinds:
            self._start_mouse()
        else:
            self._stop_mouse()
        # 按钮和帽键是同一个设备、同一个轮询线程，任意一种绑到了就得开着
        if JOYSTICK in kinds or HAT in kinds:
            self._start_joystick()
        else:
            self._stop_joystick()

    def _start_keyboard(self):
        if self._keyboard_listener is not None:
            return
        pynput_keyboard, _ = _import_pynput()
        if pynput_keyboard is None:
            return
        try:
            listener = pynput_keyboard.Listener(
                on_press=lambda key: self._press(KEYBOARD, key_name(key)),
                on_release=lambda key: self._release(KEYBOARD, key_name(key)))
            listener.daemon = True
            listener.start()
        except Exception as e:
            log.warning("could not start the keyboard listener: %s", e)
            return
        self._keyboard_listener = listener
        log.debug("keyboard PTT listener started")

    def _stop_keyboard(self):
        listener, self._keyboard_listener = self._keyboard_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:
                log.debug("stopping the keyboard listener raised: %s", e)

    def _start_mouse(self):
        if self._mouse_listener is not None:
            return
        _, pynput_mouse = _import_pynput()
        if pynput_mouse is None:
            return

        def on_click(x, y, button, pressed):
            name = mouse_name(button)
            if not name:
                return                      # 左/右/中键：不是我们的事，放过去
            if pressed:
                self._press(MOUSE, name)
            else:
                self._release(MOUSE, name)

        try:
            listener = pynput_mouse.Listener(on_click=on_click)
            listener.daemon = True
            listener.start()
        except Exception as e:
            log.warning("could not start the mouse listener: %s", e)
            return
        self._mouse_listener = listener
        log.debug("mouse PTT listener started")

    def _stop_mouse(self):
        listener, self._mouse_listener = self._mouse_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:
                log.debug("stopping the mouse listener raised: %s", e)

    def _start_joystick(self):
        if self._joystick_thread is not None:
            return
        # **pygame 一定要在这里初始化，不能挪进轮询线程。** 见 _import_pygame()
        # 开头那段——SDL 的那个隐藏窗口归建它的线程所有，轮询线程一退出，之后
        # 就再也打不开摇杆了。start() 是从界面线程调的，它和进程一样久。
        pygame = _import_pygame()
        if pygame is None:
            log.info("joystick PTT is off because pygame is unavailable")
            return
        # 顺利时只报个数量——读名字得把每个设备开一下，没必要去碰用户正在飞的
        # 摇杆。名字在打不开的时候才报（那条路上本来也开不了），见 describe_devices()
        try:
            count = pygame.joystick.get_count()
        except Exception as e:
            count = "an unknown number of (%s)" % e
        log.info("joystick PTT starting, %s device(s) connected; bound to %s",
                 count, describe([b for b in self.bindings()
                                  if b.kind in (JOYSTICK, HAT)]))
        thread = threading.Thread(target=self._joystick_loop, args=(pygame,),
                                  name="ptt-joystick", daemon=True)
        self._joystick_thread = thread
        thread.start()

    def _stop_joystick(self):
        thread, self._joystick_thread = self._joystick_thread, None
        if thread is not None and thread.is_alive():
            # 线程自己看 _joystick_thread 是不是还是自己，不是就退出。不用 join：
            # 最多再转一个 POLL_INTERVAL，而 join 会把界面线程卡住。
            pass

    def _clear_stick_state(self):
        """把摇杆这边按下的东西全部抹掉，并且重算一次。

        按钮和帽键要一起抹：设备没了以后留下任何一个"还按着"，麦克风就一直
        开着，而界面上已经没有任何东西显示是谁按住的。
        """
        with self._lock:
            self._down[JOYSTICK].clear()
            self._down[HAT].clear()
        self._recompute()

    def _joystick_loop(self, pygame):
        """轮询摇杆的按钮和帽键。

        pygame 是 _start_joystick() 在界面线程上初始化好之后传进来的，这个线程
        自己不碰初始化——理由见 _import_pygame()。

        掉线要能自己回来：飞行摇杆热插拔很常见，一次读失败就永远不再读的话，
        用户得重启客户端才有 PTT。读失败就关掉设备，隔 JOYSTICK_RETRY 再开。

        报错怎么记：**第一次说全，之后只在原因变了或者过了很久才再说一次。**
        每 JOYSTICK_RETRY 秒一条一模一样的 WARNING，在真实日志里就是四十多行
        同样的话，既没多说什么，又把别的线索挤出了滚动窗口。
        """
        me = threading.current_thread()
        sticks = {}
        last_open = 0.0
        failures = {}        # 设备序号 → (试了几次, 上一次的原因)
        complained = set()   # 已经报过"这条绑定超出设备范围"的，别每 20 ms 再报
        while self._running and self._joystick_thread is me:
            wanted = {b.device for b in self.bindings()
                      if b.kind in (JOYSTICK, HAT)}
            for index in list(sticks):
                if index not in wanted:
                    self._close_stick(sticks.pop(index))
            missing = wanted - set(sticks)
            now = time.monotonic()
            if missing and now - last_open >= JOYSTICK_RETRY:
                last_open = now
                for index in sorted(missing):
                    stick, error = self._open_stick(pygame, index)
                    if stick is not None:
                        attempts = failures.pop(index, (0, ""))[0]
                        if attempts:
                            # 自己好了这件事必须说：不然日志里只剩一串失败，
                            # 看不出后来到底恢复没有
                            log.info("joystick %d is back after %d failed "
                                     "attempt(s)", index, attempts)
                        sticks[index] = stick
                        complained = {c for c in complained if c[0] != index}
                    else:
                        attempts, previous = failures.get(index, (0, None))
                        attempts += 1
                        failures[index] = (attempts, error)
                        if attempts == 1 or error != previous:
                            log.warning("could not open joystick %d: %s; PTT is "
                                        "bound to %s, connected devices: %s",
                                        index, error, describe(self.bindings()),
                                        describe_devices(pygame))
                        elif attempts % OPEN_COMPLAIN_EVERY == 0:
                            log.warning("still cannot open joystick %d after %d "
                                        "attempts (%.0f s): %s",
                                        index, attempts,
                                        attempts * JOYSTICK_RETRY, error)
                        else:
                            log.debug("could not open joystick %d: %s", index, error)
            if sticks:
                try:
                    pygame.event.pump()
                    for index, stick in list(sticks.items()):
                        count = stick.get_numbuttons()
                        hats = stick.get_numhats()
                        for b in self.bindings():
                            if b.device != index:
                                continue
                            # 按钮号／帽键号超出这个设备的范围——多半是换了个规格
                            # 小一点的手柄。要的是"不响"，而不是让轮询线程带着
                            # IndexError 死掉，那样连别的绑定也一起没了。以前这里
                            # 是完全静默的：设备开得好好的、日志一个字没有、PTT
                            # 就是不响，从日志上根本看不出来。所以报一次，只报一次。
                            if b.kind == JOYSTICK:
                                token = (index, b.button)
                                if b.button >= count:
                                    if token not in complained:
                                        complained.add(token)
                                        log.warning(
                                            "PTT is bound to button %d but "
                                            "joystick %d (%s) only has %d "
                                            "button(s); this binding can never "
                                            "fire", b.button, index,
                                            stick.get_name(), count)
                                    self._release(JOYSTICK, token)
                                elif stick.get_button(b.button):
                                    self._press(JOYSTICK, token)
                                else:
                                    self._release(JOYSTICK, token)
                            elif b.kind == HAT:
                                token = (index, b.hat, b.direction)
                                if b.hat is None or b.hat >= hats:
                                    if token not in complained:
                                        complained.add(token)
                                        log.warning(
                                            "PTT is bound to hat %s but joystick "
                                            "%d (%s) only has %d hat(s); this "
                                            "binding can never fire", b.hat,
                                            index, stick.get_name(), hats)
                                    self._release(HAT, token)
                                elif hat_pressed(stick.get_hat(b.hat), b.direction):
                                    self._press(HAT, token)
                                else:
                                    self._release(HAT, token)
                except Exception as e:
                    log.warning("reading joystick %s failed (%s: %s), closing and "
                                "reopening in %.0f s",
                                ", ".join(str(i) for i in sorted(sticks)),
                                e.__class__.__name__, e, JOYSTICK_RETRY)
                    for stick in sticks.values():
                        self._close_stick(stick)
                    sticks.clear()
                    self._clear_stick_state()
            time.sleep(POLL_INTERVAL)
        for stick in sticks.values():
            self._close_stick(stick)
        self._clear_stick_state()

    @staticmethod
    def _open_stick(pygame, index):
        """打开一个设备，返回 (设备, 失败原因)。成功时原因是空串。

        原因由调用方去记，因为它才知道这是第几次失败——这里每次都记的话，就是
        每 JOYSTICK_RETRY 秒一条重复的话。"序号不存在"这条以前是静默 return
        None：绑定指着一个没插的设备时，日志里一个字都没有。
        """
        try:
            count = pygame.joystick.get_count()
            if index >= count:
                # 日志文字保持 ASCII：控制台在中文 Windows 上是 GBK，破折号之类
                # 的字符会变成乱码，甚至让 logging 自己报错
                return None, ("the system reports %d joystick(s), so there is "
                              "no #%d - was it unplugged, or did the devices "
                              "get renumbered?" % (count, index))
            stick = pygame.joystick.Joystick(index)
            stick.init()
            log.info("joystick %d opened: %s (%d buttons, %d hats)",
                     index, stick.get_name(), stick.get_numbuttons(),
                     stick.get_numhats())
            return stick, ""
        except Exception as e:
            return None, "%s: %s" % (e.__class__.__name__, e)

    @staticmethod
    def _close_stick(stick):
        try:
            stick.quit()
        except Exception as e:
            log.debug("closing a joystick raised: %s", e)


class PttCapture:
    """录一条绑定：三个源一起听，谁先动就是谁。

    设置对话框用这个做"按一下你想用的键"。和 PttWatcher 分开，是因为两者要的
    东西正好相反——watcher 问"这几条绑定按了没有"，capture 问"刚按的是什么"。

    **调用方必须先把 PttWatcher 停掉再 start()**：两个线程同时 pump SDL 的事件
    队列不是线程安全的，而且 watcher 还开着的话，用户为了录绑定按下的那一下会
    真的发出去一段语音。

    `on_captured(Binding)` 在监听线程上调用，只调一次，调完自己停。
    """

    def __init__(self, on_captured, joystick=True):
        self.on_captured = on_captured
        self._joystick = joystick
        self._done = threading.Event()
        self._keyboard_listener = None
        self._mouse_listener = None
        self._thread = None

    def start(self):
        pynput_keyboard, pynput_mouse = _import_pynput()
        if pynput_keyboard is not None:
            try:
                self._keyboard_listener = pynput_keyboard.Listener(
                    on_press=lambda key: self._fire(keyboard_binding(key_name(key))))
                self._keyboard_listener.daemon = True
                self._keyboard_listener.start()
            except Exception as e:
                log.warning("could not listen for a PTT key: %s", e)
        if pynput_mouse is not None:
            def on_click(x, y, button, pressed):
                if not pressed:
                    return
                name = mouse_name(button)
                if name:
                    self._fire(Binding(MOUSE, button=name))

            try:
                self._mouse_listener = pynput_mouse.Listener(on_click=on_click)
                self._mouse_listener.daemon = True
                self._mouse_listener.start()
            except Exception as e:
                log.warning("could not listen for a PTT mouse button: %s", e)
        if self._joystick:
            # 和 PttWatcher._start_joystick() 同一个理由：pygame 只能在这条界面
            # 线程上初始化，不能等到录制线程里去做。录制线程录完就退出，SDL 的
            # 那个隐藏窗口要是归它所有，一退出摇杆就再也打不开了。
            pygame = _import_pygame()
            if pygame is None:
                return
            self._thread = threading.Thread(target=self._joystick_loop,
                                            args=(pygame,),
                                            name="ptt-capture", daemon=True)
            self._thread.start()

    def stop(self):
        self._done.set()
        for listener in (self._keyboard_listener, self._mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception as e:
                    log.debug("stopping a capture listener raised: %s", e)
        self._keyboard_listener = None
        self._mouse_listener = None

    def _fire(self, binding):
        if self._done.is_set():
            return
        self._done.set()
        log.info("captured a PTT binding: %s", binding)
        try:
            if self.on_captured:
                self.on_captured(binding)
        except Exception as e:
            log.warning("the PTT capture callback raised: %s", e)
        # 自己停掉自己的监听器。放在回调之后，免得回调里又去开新的监听。
        self.stop()

    @staticmethod
    def _pressed_now(sticks):
        """录制这一刻已经按着／推着的东西，按钮和帽键都算。"""
        held = set()
        for index, stick in sticks:
            for button in range(stick.get_numbuttons()):
                if stick.get_button(button):
                    held.add((JOYSTICK, index, button))
            for hat in range(stick.get_numhats()):
                if hat_direction(stick.get_hat(hat)):
                    held.add((HAT, index, hat))
        return held

    def _joystick_loop(self, pygame):
        """所有接着的摇杆一起听，第一个动的按钮或帽键就是答案。

        和 watcher 那边不同，这里要先把每个设备的初始状态读一遍：录制开始时就
        按着的按钮、已经推在某个角上的帽键都不该算数（用户可能一直压着油门上的
        某个开关），只有"从没按到按"才算。帽键记的是"这个帽键动过没有"而不是
        某一个方向，所以一直推着上、不回中就直接改推右，也不会被录进来——回中
        再推一次就是了，比把常年压着的位置录成 PTT 好。

        pygame 是 start() 在界面线程上初始化好之后传进来的，理由见
        _import_pygame()。
        """
        sticks = []
        try:
            for index in range(pygame.joystick.get_count()):
                stick = pygame.joystick.Joystick(index)
                stick.init()
                sticks.append((index, stick))
        except Exception as e:
            log.warning("could not open the joysticks for capture (%s: %s); "
                        "connected devices: %s", e.__class__.__name__, e,
                        describe_devices(pygame))
        if not sticks:
            # 录不到摇杆时界面上只会一直停在"请按下…"，日志得说清是"没有设备"
            # 还是"有设备但打不开"——两件事的下一步完全不同
            log.info("no joystick is available for capture, only the keyboard "
                     "and the mouse can be recorded right now")
            return
        log.debug("listening for a PTT binding on %d joystick(s): %s",
                  len(sticks), ", ".join("#%d %s (%d buttons, %d hats)"
                                         % (i, s.get_name(), s.get_numbuttons(),
                                            s.get_numhats())
                                         for i, s in sticks))
        held = set()
        try:
            pygame.event.pump()
            held = self._pressed_now(sticks)
        except Exception as e:
            log.debug("reading the initial joystick state raised: %s", e)
        try:
            while not self._done.is_set():
                pygame.event.pump()
                for index, stick in sticks:
                    for button in range(stick.get_numbuttons()):
                        token = (JOYSTICK, index, button)
                        if not stick.get_button(button):
                            held.discard(token)
                        elif token not in held:
                            self._fire(Binding(JOYSTICK, button=button, device=index,
                                               device_name=stick.get_name()))
                            return
                    for hat in range(stick.get_numhats()):
                        token = (HAT, index, hat)
                        direction = hat_direction(stick.get_hat(hat))
                        if not direction:
                            held.discard(token)
                        elif token not in held:
                            self._fire(Binding(HAT, hat=hat, direction=direction,
                                               device=index,
                                               device_name=stick.get_name()))
                            return
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            log.warning("the joystick capture loop failed: %s", e)
        finally:
            for _, stick in sticks:
                try:
                    stick.quit()
                except Exception:
                    pass
