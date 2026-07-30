"""MSFS 数据链路。

对应 xpc/xplane.py，换成 SimConnect。作用完全一样：把机位、姿态、速度、无线电
和应答机取出来，`snapshot()` 返回的字典和 xpc 那边**逐字段一致**——FSD 位置包和
语音频道那两层因此可以原样复用。

和 X-Plane 的区别：

    X-Plane   自己发 UDP，订阅 dataref，靠 BECN 信标发现
    MSFS      SimConnect（本机命名管道/共享内存），一问一答取 SimVar

SimConnect 只能连本机的模拟器，没有发现过程；模拟器没开时 `SimConnect()` 直接
抛异常，所以重连逻辑是"隔几秒再试一次构造"，而不是 xpc 那种超时判定。

单位：SimVar 里角度是**弧度**，高度已经是英尺，速度有专门的 knots 变量。频率
用 `COM ACTIVE FREQUENCY:1`，单位 MHz，直接可用——不像 X-Plane 要分两个精度的
dataref 讨论。
"""

import logging
import math
import threading
import time

log = logging.getLogger("sim")

# _poll 的三种结果。"没进飞行"和"连接断了"要分开——前者是常态，重开连接
# 只会在日志里刷一串 SIM OPEN。
OK = "ok"
NO_DATA = "no-data"
FAILED = "failed"

RETRY_INTERVAL = 3.0        # 连不上时隔多久再试
STALE_AFTER = 3.0           # 超过这么久取不到数据就认为断了
POLL_INTERVAL = 0.2         # 取数据的间隔，和位置包的 5 Hz 对齐

# SimVar 名字 -> 我们内部的键。值都通过 AircraftRequests.get() 取。
# 名字必须和 SimConnect 的 SimVar 完全一致，写错了 get() 返回 None。
# 单位不是猜的：Python-SimConnect 的 RequestList.py 为每个 SimVar 写死了请求
# 单位。经纬度它按 Degrees 要，姿态和航向按 Radians 要——名字里带 DEGREES 的
# 那几个反而是弧度。照着它来，不要照 SimVar 的原生单位来。
SIMVARS = {
    "latitude": "PLANE_LATITUDE",                  # 度（已经是度，不要再转）
    "longitude": "PLANE_LONGITUDE",                # 度（同上）
    "altitude": "PLANE_ALTITUDE",                  # 英尺
    "agl": "PLANE_ALT_ABOVE_GROUND",               # 英尺
    "groundspeed": "GROUND_VELOCITY",              # 节
    "pitch": "PLANE_PITCH_DEGREES",                # 弧度（名字骗人）
    "bank": "PLANE_BANK_DEGREES",                  # 弧度（名字同样骗人）
    "heading": "PLANE_HEADING_DEGREES_TRUE",       # 弧度
    "squawk": "TRANSPONDER_CODE:1",                # BCD
    "com1": "COM_ACTIVE_FREQUENCY:1",              # MHz
    "com2": "COM_ACTIVE_FREQUENCY:2",              # MHz
    "on_ground": "SIM_ON_GROUND",
    "engine_on": "GENERAL_ENG_COMBUSTION:1",
    "gear": "GEAR_HANDLE_POSITION",
    "flaps": "TRAILING_EDGE_FLAPS_LEFT_PERCENT",
    "spoilers": "SPOILERS_HANDLE_POSITION",
    "light_beacon": "LIGHT_BEACON",
    "light_landing": "LIGHT_LANDING",
    "light_taxi": "LIGHT_TAXI",
    "light_strobe": "LIGHT_STROBE",
    "light_nav": "LIGHT_NAV",
}


def bcd_to_squawk(raw):
    """应答机码在 SimVar 里是 BCD 编码的。

    0x1200 表示 1200。直接当十进制读会得到 4608，塞进位置包就是个乱码。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2000
    digits = 0
    for shift in (12, 8, 4, 0):
        digit = (value >> shift) & 0xF
        if digit > 7:            # 应答机是八进制，出现 8/9 说明这不是 BCD
            return value if 0 <= value <= 7777 else 2000
        digits = digits * 10 + digit
    return digits


class SimLink:
    """和 MSFS 的连接。

    on_state(connected, message) 在后台线程调用。
    """

    def __init__(self, on_state=None):
        self.on_state = on_state
        self.values = {}
        self.last_update = 0.0
        self.running = False

        self._sim = None
        self._requests = None
        self._thread = None
        self._lock = threading.Lock()
        self._connected = False

    # ---------- 状态 ----------
    def _state(self, connected, message):
        if connected != self._connected:
            self._connected = connected
            log.info("%s: %s", "connected" if connected else "disconnected", message)
            if self.on_state:
                try:
                    self.on_state(connected, message)
                except Exception as e:
                    log.warning("status callback raised: %s", e)

    @property
    def connected(self):
        return self._connected and (time.time() - self.last_update) < STALE_AFTER

    # ---------- 生命周期 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None
        self._close()

    def _close(self):
        if self._sim is not None:
            try:
                self._sim.exit()
            except Exception:
                pass
        self._sim = None
        self._requests = None

    @property
    def sim(self):
        """底层 SimConnect 句柄，他机注入那边要用。"""
        return self._sim

    def _open(self):
        """连模拟器。没开的话构造函数直接抛，这是正常情况不是错误。"""
        from SimConnect import AircraftRequests, SimConnect

        self._sim = SimConnect()
        # _time 是这个包的缓存窗口（毫秒）。位置一秒要读 5 次，缓存久了会发出
        # 过期的位置；设小一点让它每次都真的去问。
        self._requests = AircraftRequests(self._sim, _time=100)

    def _run(self):
        while self.running:
            if self._sim is None:
                try:
                    self._open()
                except Exception as e:
                    self._state(False, "连不上 MSFS（模拟器是否已启动？）")
                    log.debug("opening SimConnect failed: %s", e)
                    self._sleep(RETRY_INTERVAL)
                    continue
                log.info("connected to SimConnect")

            result = self._poll()
            if result is FAILED:
                # SimConnect 本身出错了，连接多半没了，重开
                self._state(False, "MSFS 连接中断")
                self._close()
                self._sleep(RETRY_INTERVAL)
                continue

            if result is NO_DATA:
                # 连接好好的，只是还没进飞行（在菜单里就是这样）。以前这里
                # 也走重开，日志里每隔几秒一条 "SIM OPEN"，白白反复建连接。
                self._state(False, "MSFS 没有数据（是否已进入飞行？）")
                self._sleep(POLL_INTERVAL)
                continue

            self._state(True, "已连接 MSFS")
            self._sleep(POLL_INTERVAL)

    def _sleep(self, seconds):
        """可被 stop() 打断的等待。"""
        deadline = time.time() + seconds
        while self.running and time.time() < deadline:
            time.sleep(0.05)

    def _poll(self):
        """取一轮数据。

        返回 OK / NO_DATA / FAILED。这三者必须分开：在主菜单里读不到经纬度是
        完全正常的，不该把整条 SimConnect 连接推倒重来。
        """
        values = {}
        try:
            for name, simvar in SIMVARS.items():
                value = self._requests.get(simvar)
                if value is not None:
                    values[name] = value
        except Exception as e:
            log.debug("reading a SimVar raised: %s", e)
            return FAILED

        # 经纬度是判断"有没有真的在飞"的最低要求
        if "latitude" not in values or "longitude" not in values:
            return NO_DATA

        with self._lock:
            self.values = values
            self.last_update = time.time()
        return OK

    # ---------- 取值 ----------
    def snapshot(self):
        """当前这一份数据，字段和 xpc/xplane.py 的 snapshot() 完全一致。"""
        with self._lock:
            raw = dict(self.values)
        if not raw:
            return None

        return {
            # 经纬度已经是度。以前这里又 math.degrees 了一次，31.14 变成
            # 1784.2，服务端每个位置包都回 "Invalid latitude/longitude"，
            # 90 秒后把连接掐掉。
            "latitude": raw.get("latitude", 0.0),
            "longitude": raw.get("longitude", 0.0),
            "altitude": int(round(raw.get("altitude", 0.0))),
            "agl": int(round(raw.get("agl", 0.0))),
            "groundspeed": int(round(raw.get("groundspeed", 0.0))),
            "pitch": -math.degrees(raw.get("pitch", 0.0)),
            "bank": -math.degrees(raw.get("bank", 0.0)),
            "heading": math.degrees(raw.get("heading", 0.0)) % 360.0,
            "squawk": bcd_to_squawk(raw.get("squawk", 0x2000)),
            "xpdr_mode": 2,          # MSFS 没有直接可读的模式，按"正常"报
            "com1": self._frequency(raw.get("com1")),
            "com2": self._frequency(raw.get("com2")),
            "com1_power": True,
            "on_ground": bool(raw.get("on_ground", 0)),
            # 下面这些是报给别人做动画用的
            "gear_down": bool(raw.get("gear", 1)),
            "flaps": max(0.0, min(1.0, raw.get("flaps", 0.0) / 100.0)),
            "spoilers": bool(raw.get("spoilers", 0)),
            "engines_on": bool(raw.get("engine_on", 1)),
            "lights": {
                "beacon_on": bool(raw.get("light_beacon", 0)),
                "landing_on": bool(raw.get("light_landing", 0)),
                "taxi_on": bool(raw.get("light_taxi", 0)),
                "strobe_on": bool(raw.get("light_strobe", 0)),
                "nav_on": bool(raw.get("light_nav", 0)),
            },
        }

    @staticmethod
    def _frequency(raw):
        """COM ACTIVE FREQUENCY 已经是 MHz，只要挡掉 0 和异常值。"""
        if not raw:
            return None
        value = round(float(raw), 3)
        # 民航 VHF 通信频段，超出范围的多半是没读到
        return value if 118.0 <= value <= 137.0 else None
