"""PTT 的输入源：键盘、摇杆按钮、鼠标侧键。

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

KEYBOARD = "keyboard"
MOUSE = "mouse"
JOYSTICK = "joystick"
KINDS = (KEYBOARD, MOUSE, JOYSTICK)

# 能绑的鼠标键。左/中/右不在这里，理由见模块开头。
MOUSE_BUTTONS = ("x1", "x2")


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

    def __init__(self, kind, key="", button=None, device=0, device_name=""):
        self.kind = kind
        self.key = key or ""            # 键盘：规范化后的键名
        self.button = button            # 鼠标："x1"/"x2"；摇杆：按钮序号（int）
        self.device = int(device or 0)  # 摇杆：设备序号
        self.device_name = device_name or ""   # 摇杆：设备名，只用来在界面上认人

    def __eq__(self, other):
        if not isinstance(other, Binding):
            return NotImplemented
        # device_name 不参与比较：同一个手柄换个 USB 口，SDL 给的名字可能带上
        # 不同的后缀，但绑定还是那一条。
        return (self.kind, self.key, self.button, self.device) == \
               (other.kind, other.key, other.button, other.device)

    def __hash__(self):
        return hash((self.kind, self.key, self.button, self.device))

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
        return str(self.button)

    def to_dict(self):
        if self.kind == KEYBOARD:
            return {"kind": KEYBOARD, "key": self.key}
        if self.kind == MOUSE:
            return {"kind": MOUSE, "button": self.button}
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


def available_joysticks():
    """接着的摇杆：[(序号, 名字)]。摇杆不可用时返回空表，不抛异常。

    设置界面拿它填设备下拉框。**调用前必须先把 PttWatcher 停掉**，理由见
    PttCapture 的注释。
    """
    try:
        pygame = _import_pygame()
        if pygame is None:
            return []
        if not pygame.joystick.get_init():
            pygame.joystick.init()
        out = []
        for index in range(pygame.joystick.get_count()):
            stick = pygame.joystick.Joystick(index)
            try:
                stick.init()
                out.append((index, stick.get_name()))
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
        # 各源当前按下的东西：键盘是键名，鼠标是 "x1"/"x2"，摇杆是 (设备, 按钮)
        self._down = {KEYBOARD: set(), MOUSE: set(), JOYSTICK: set()}
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
        log.info("PTT watcher started with %d binding(s)", len(self._bindings))

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
        if JOYSTICK in kinds:
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
        thread = threading.Thread(target=self._joystick_loop,
                                  name="ptt-joystick", daemon=True)
        self._joystick_thread = thread
        thread.start()

    def _stop_joystick(self):
        thread, self._joystick_thread = self._joystick_thread, None
        if thread is not None and thread.is_alive():
            # 线程自己看 _joystick_thread 是不是还是自己，不是就退出。不用 join：
            # 最多再转一个 POLL_INTERVAL，而 join 会把界面线程卡住。
            pass

    def _joystick_loop(self):
        """轮询摇杆按钮。

        掉线要能自己回来：飞行摇杆热插拔很常见，一次读失败就永远不再读的话，
        用户得重启客户端才有 PTT。读失败就关掉设备，隔 JOYSTICK_RETRY 再开。
        """
        me = threading.current_thread()
        # pygame 只取一次。放进循环的话，每 20 ms 就要走一遍 import 和 get_init()
        # 的检查；而且它要么一开始就没有，要么一直都在，没有中途出现的情况。
        pygame = _import_pygame()
        if pygame is None:
            log.info("joystick PTT is off because pygame is unavailable")
            return
        sticks = {}
        last_open = 0.0
        while self._running and self._joystick_thread is me:
            wanted = {b.device for b in self.bindings() if b.kind == JOYSTICK}
            for index in list(sticks):
                if index not in wanted:
                    self._close_stick(sticks.pop(index))
            missing = wanted - set(sticks)
            now = time.monotonic()
            if missing and now - last_open >= JOYSTICK_RETRY:
                last_open = now
                for index in sorted(missing):
                    stick = self._open_stick(pygame, index)
                    if stick is not None:
                        sticks[index] = stick
            if sticks:
                try:
                    pygame.event.pump()
                    for index, stick in list(sticks.items()):
                        count = stick.get_numbuttons()
                        for b in self.bindings():
                            if b.kind != JOYSTICK or b.device != index:
                                continue
                            token = (index, b.button)
                            if b.button >= count:
                                # 按钮号超出这个设备的范围——多半是换了个按钮更少的
                                # 手柄。要的是"不响"，而不是让轮询线程带着 IndexError
                                # 死掉，那样连别的摇杆绑定也一起没了。
                                self._release(JOYSTICK, token)
                                continue
                            if stick.get_button(b.button):
                                self._press(JOYSTICK, token)
                            else:
                                self._release(JOYSTICK, token)
                except Exception as e:
                    log.warning("reading the joystick failed, will reopen: %s", e)
                    for stick in sticks.values():
                        self._close_stick(stick)
                    sticks.clear()
                    with self._lock:
                        self._down[JOYSTICK].clear()
                    self._recompute()
            time.sleep(POLL_INTERVAL)
        for stick in sticks.values():
            self._close_stick(stick)
        with self._lock:
            self._down[JOYSTICK].clear()
        self._recompute()

    @staticmethod
    def _open_stick(pygame, index):
        try:
            if index >= pygame.joystick.get_count():
                return None
            stick = pygame.joystick.Joystick(index)
            stick.init()
            log.info("joystick %d opened: %s (%d buttons)",
                     index, stick.get_name(), stick.get_numbuttons())
            return stick
        except Exception as e:
            log.warning("could not open joystick %d: %s", index, e)
            return None

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
            self._thread = threading.Thread(target=self._joystick_loop,
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

    def _joystick_loop(self):
        """所有接着的摇杆一起听，第一个按下的按钮就是答案。

        和 watcher 那边不同，这里要先把每个设备的初始状态读一遍：录制开始时就
        按着的按钮不该算数（用户可能一直压着油门上的某个开关），只有"从没按到
        按"才算。
        """
        pygame = _import_pygame()
        if pygame is None:
            return
        sticks = []
        try:
            for index in range(pygame.joystick.get_count()):
                stick = pygame.joystick.Joystick(index)
                stick.init()
                sticks.append((index, stick))
        except Exception as e:
            log.warning("could not open the joysticks for capture: %s", e)
        if not sticks:
            return
        held = set()
        try:
            pygame.event.pump()
            for index, stick in sticks:
                for button in range(stick.get_numbuttons()):
                    if stick.get_button(button):
                        held.add((index, button))
        except Exception as e:
            log.debug("reading the initial joystick state raised: %s", e)
        try:
            while not self._done.is_set():
                pygame.event.pump()
                for index, stick in sticks:
                    for button in range(stick.get_numbuttons()):
                        pressed = bool(stick.get_button(button))
                        if pressed and (index, button) not in held:
                            self._fire(Binding(JOYSTICK, button=button, device=index,
                                               device_name=stick.get_name()))
                            return
                        if not pressed:
                            held.discard((index, button))
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            log.warning("the joystick capture loop failed: %s", e)
        finally:
            for _, stick in sticks:
                try:
                    stick.quit()
                except Exception:
                    pass
