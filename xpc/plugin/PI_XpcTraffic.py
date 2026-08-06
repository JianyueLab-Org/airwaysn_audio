"""XPC for CAN —— X-Plane 他机渲染插件。

装法：把这个文件放进

    <X-Plane>/Resources/plugins/PythonPlugins/PI_XpcTraffic.py

需要先装 XPPython3（https://xppython3.readthedocs.io）。**装哪个版本取决于模拟器**：

    X-Plane 12        XPPython3 v4.x
    X-Plane 11.52     XPPython3 v3.1.5   —— v4 是用 SDK 420 编的，不兼容 XP11

TCAS 接管（override_TCAS + sim/cockpit2/tcas/targets/*）是 X-Plane 11.50 引入
的。11.50 以前只有旧的 19 个多人机位 dataref，这里不去支持——XPPython3 v3.1.5
本来也是对着 11.52 发的。所以能力是**探测**出来的，不是按版本号写死的：找不到
TCAS 的 dataref 就只画飞机、不送 TCAS，并在日志里说清楚。

反过来不用担心：X-Plane 在接管 TCAS 时会自动把最近 19 架镜像回旧的
sim/multiplayer/position/plane#_* dataref，所以还在读那套的老插件照样能看到。

这个插件**故意做得很薄**。他机航迹、插值、机型匹配全在客户端算好，这里只做两
件 X-Plane 之外做不到的事：

    画       xp.createInstance + instanceSetPosition，模型是客户端指定的 .obj
    TCAS     acquirePlanes 之后写 sim/cockpit2/tcas/targets/*

这么分是因为插件跑在 X-Plane 进程里，改一行就得重启模拟器；而客户端那边有单
元测试，改起来是秒级的。

两件容易踩的事：

1. **CSL 模型的动画 dataref 必须先注册再加载模型。** OBJ8 里写的是
   `libxplanemp/controls/gear_ratio` 这类名字，X-Plane 在加载 .obj 时就要能
   解析它们，晚了模型出得来但不会动。所以 XPluginStart 里先注册。

2. **XPLMInstance 画出来的飞机不进 TCAS。** 座舱的 TCAS/ND 看的是
   `sim/cockpit2/tcas/targets/*`，那是另一套，得单独填。填之前要
   acquirePlanes()，否则 override_TCAS 写不进去。
"""

import base64
import json
import socket
import traceback
import zlib

try:
    import xp
except ImportError:      # 在 X-Plane 之外被导入（比如跑测试）时不炸
    xp = None

PLUGIN_PORT = 49900
# v2：分片按字节切、负载 base64。和 bridge.py 保持一致（有测试钉着）。
PROTOCOL_VERSION = 2

# TCAS 目标数组是 64 个位置（sim/cockpit2/tcas/targets/*，float[64]）。
# 第 0 位是本机，所以他机最多 63 架。
MAX_TCAS_TARGETS = 63

# 客户端停发多久之后清场。位置流是 5 Hz，2 秒足够判定它没了。
TIMEOUT = 2.0

# CSL 的 OBJ8 用这些 dataref 驱动动画。名字是 libxplanemp 的约定，
# Bluebell / X-CSL 等包都按这个写，顺序就是 instanceSetPosition 里 data 的顺序。
ANIMATION_DATAREFS = [
    "libxplanemp/controls/gear_ratio",
    "libxplanemp/controls/flap_ratio",
    "libxplanemp/controls/spoiler_ratio",
    "libxplanemp/controls/speed_brake_ratio",
    "libxplanemp/controls/slat_ratio",
    "libxplanemp/controls/wing_sweep_ratio",
    "libxplanemp/controls/thrust_ratio",
    "libxplanemp/controls/yoke_pitch_ratio",
    "libxplanemp/controls/yoke_heading_ratio",
    "libxplanemp/controls/yoke_roll_ratio",
    "libxplanemp/controls/thrust_revers",
    "libxplanemp/controls/taxi_lites_on",
    "libxplanemp/controls/landing_lites_on",
    "libxplanemp/controls/beacon_lites_on",
    "libxplanemp/controls/strobe_lites_on",
    "libxplanemp/controls/nav_lites_on",
]


class Reassembler:
    """把分片的 UDP 包拼回完整消息。和客户端 bridge.py 里那份对称。"""

    def __init__(self):
        self.sequence = None
        self.parts = {}
        self.total = 0

    def feed(self, packet):
        try:
            header = json.loads(packet.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if header.get("v") != PROTOCOL_VERSION:
            return None

        sequence = header.get("seq", 0)
        if sequence != self.sequence:
            # 16 位环回序号：只认往前走的新帧，迟到的旧分片直接扔
            if self.sequence is not None:
                delta = (sequence - self.sequence) & 0xFFFF
                if delta == 0 or delta > 0x8000:
                    return None
            self.sequence = sequence
            self.parts = {}
            self.total = max(1, int(header.get("total", 1) or 1))
        try:
            part = int(header["part"])
            if not 0 <= part < self.total:
                return None
            self.parts[part] = base64.b64decode(header["data"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(self.parts) < self.total:
            return None

        body = b"".join(self.parts[i] for i in range(self.total))
        self.parts = {}
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


class RenderedAircraft:
    """一架正在画的飞机。"""

    def __init__(self, callsign):
        self.callsign = callsign
        self.object_path = ""
        self.object_ref = None
        self.instance = None
        self.loading = False
        # 每发起一次异步加载加一。回调带着发起时的号回来，对不上就说明这次
        # 加载已经被更新的一次顶掉了——直接卸掉，别覆盖 instance（否则旧
        # instance 没人销毁，一架冻住的重影永远挂在天上）。
        self.load_generation = 0

    def destroy(self):
        if self.instance is not None and xp:
            try:
                xp.destroyInstance(self.instance)
            except Exception:
                pass
        self.instance = None
        if self.object_ref is not None and xp:
            try:
                xp.unloadObject(self.object_ref)
            except Exception:
                pass
        self.object_ref = None


class PythonInterface:
    def XPluginStart(self):
        self.Name = "XPC for CAN Traffic"
        self.Sig = "org.airwaysn.xpc.traffic"
        self.Desc = "把 Cerulean 网络上的其他飞机画进 X-Plane，并送进 TCAS"

        self.socket = None
        self.reassembler = Reassembler()
        self.aircraft = {}          # 呼号 -> RenderedAircraft
        self.last_message = 0.0
        self.tcas_written = 0       # 上一帧写了多少个 TCAS 槽位
        self.have_planes = False
        self.accessors = []
        self.own_callsign = ""

        # 动画 dataref 必须在任何 CSL 模型加载之前注册好，
        # 否则 X-Plane 解析 OBJ8 时找不到它们，模型能出来但不会动。
        self._register_animation_datarefs()

        self._find_tcas_datarefs()

        xp.registerFlightLoopCallback(self.flight_loop, -1, 0)
        xp.log(f"XPC traffic plugin started ({self._version_note()})")
        return self.Name, self.Sig, self.Desc

    @staticmethod
    def _version_note():
        """把模拟器和 SDK 版本记进日志——用户报问题时第一件要知道的事。"""
        try:
            sim, xplm, _ = xp.getVersions()
            return f"X-Plane {sim}, XPLM {xplm}"
        except Exception:
            return "版本未知"

    def _find_tcas_datarefs(self):
        """探测 TCAS 接管能力。

        override_TCAS 和 tcas/targets 是 X-Plane 11.50 才有的。找不到就只画飞
        机不送 TCAS——按版本号写死不如直接问 X-Plane 有没有这个 dataref。
        """
        self.override_tcas = xp.findDataRef("sim/operation/override/override_TCAS")
        self.tcas = {
            name: xp.findDataRef(f"sim/cockpit2/tcas/targets/{path}")
            for name, path in (
                ("x", "position/x"), ("y", "position/y"), ("z", "position/z"),
                ("psi", "position/psi"), ("the", "position/the"),
                ("phi", "position/phi"),
                ("vertical_speed", "position/vertical_speed"),
                ("weight_on_wheels", "position/weight_on_wheels"),
                ("modeC", "modeC_code"), ("modeS", "modeS_id"),
                ("flight_id", "flight_id"), ("icao_type", "icao_type"),
            )}

        missing = [name for name, ref in self.tcas.items() if ref is None]
        self.tcas_available = bool(self.override_tcas) and not missing
        if not self.tcas_available:
            xp.log("this X-Plane build has no TCAS override (11.50+ required); "
                   f"traffic will be drawn but will not reach TCAS. "
                   f"missing: {missing or 'override_TCAS'}")

    def _register_animation_datarefs(self):
        """注册 libxplanemp 那套动画 dataref。

        如果 LiveTraffic 之类的插件已经注册过同名 dataref，findDataRef 能找到
        它——那说明另一套 XPMP2 正在跑。同时开两套会抢 AI 机位，这里只记一条
        日志让用户知道，不去抢。
        """
        existing = xp.findDataRef(ANIMATION_DATAREFS[0])
        if existing is not None:
            xp.log("warning: another plugin already registered the libxplanemp "
                   "animation datarefs (LiveTraffic / XPMP2?); running two "
                   "traffic systems at once makes them fight each other")
            return

        for name in ANIMATION_DATAREFS:
            # 只读访问器就够：值是通过 instanceSetPosition 的 data 传的，
            # 不走 dataref 本身。这里注册只是让 OBJ8 能解析到名字。
            accessor = xp.registerDataAccessor(name, readFloat=lambda ref=None: 0.0)
            self.accessors.append(accessor)

    def XPluginEnable(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.bind(("127.0.0.1", PLUGIN_PORT))
            self.socket.setblocking(False)
        except OSError as e:
            xp.log(f"could not bind {PLUGIN_PORT}: {e}")
            return 0

        # 只有要送 TCAS 才需要抢 AI 机位。没这个能力就别抢——白占着会挡住
        # LiveTraffic 之类真正用得上的插件。
        if not self.tcas_available:
            return 1

        # acquirePlanes 是写 override_TCAS 的前提（X-Plane 的 dataref 文档里
        # 明写 "Only writeable by the plugin that has the AI planes acquired"）
        try:
            self.have_planes = bool(xp.acquirePlanes())
        except Exception as e:
            xp.log(f"acquirePlanes failed: {e}")
            self.have_planes = False

        if self.have_planes:
            xp.setDatai(self.override_tcas, 1)
            xp.log("acquired the AI planes, TCAS override is on")
        else:
            try:
                _, _, who = xp.countAircraft()
            except Exception:
                who = "?"
            xp.log(f"could not acquire the AI planes (plugin {who} holds them), "
                   f"traffic will not reach TCAS")
        return 1

    def XPluginDisable(self):
        self._clear_all()
        if self.have_planes:
            try:
                xp.setDatai(self.override_tcas, 0)
                xp.releasePlanes()
            except Exception:
                pass
            self.have_planes = False
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None

    def XPluginStop(self):
        xp.unregisterFlightLoopCallback(self.flight_loop, 0)
        for accessor in self.accessors:
            try:
                xp.unregisterDataAccessor(accessor)
            except Exception:
                pass
        self.accessors = []

    def XPluginReceiveMessage(self, who, message, param):
        pass

    # ---------- 主循环 ----------
    def flight_loop(self, lastCall, elapsedSim, counter, refcon):
        try:
            self._pump()
        except Exception:
            xp.log("traffic plugin error:\n" + traceback.format_exc())
        return -1        # 每帧都跑

    def _pump(self):
        message = self._receive_latest()
        now = xp.getElapsedTime()

        if message is not None:
            self.last_message = now
            self._apply(message)
        elif self.last_message and now - self.last_message > TIMEOUT:
            # 客户端断了，把天上清空，别留一堆冻住的飞机
            self.last_message = 0.0
            self._clear_all()

    def _receive_latest(self):
        """把收到的包都读干净，只保留最后一条完整消息。

        位置流里迟到的帧没有价值——留着反而让飞机往回跳。
        """
        latest = None
        while True:
            try:
                packet, _ = self.socket.recvfrom(65535)
            except (BlockingIOError, OSError):
                break
            message = self.reassembler.feed(packet)
            if message is not None:
                latest = message
        return latest

    def _apply(self, message):
        if message.get("type") != "traffic":
            return
        entries = message.get("aircraft") or []

        seen = set()
        for index, entry in enumerate(entries):
            callsign = entry.get("callsign")
            if not callsign:
                continue
            seen.add(callsign)
            self._draw(callsign, entry)

        for callsign in [c for c in self.aircraft if c not in seen]:
            self.aircraft.pop(callsign).destroy()

        self._write_tcas(entries[:MAX_TCAS_TARGETS])

    def _draw(self, callsign, entry):
        aircraft = self.aircraft.get(callsign)
        if aircraft is None:
            aircraft = RenderedAircraft(callsign)
            self.aircraft[callsign] = aircraft

        wanted = entry.get("object") or ""
        if wanted and wanted != aircraft.object_path:
            # 客户端换了匹配结果（多半是机型问到了），换模型
            aircraft.destroy()
            aircraft.object_path = wanted
            aircraft.loading = True
            aircraft.load_generation += 1
            xp.loadObjectAsync(self._object_path(wanted), self._object_loaded,
                               (callsign, aircraft.load_generation))

        if aircraft.instance is None:
            return

        x, y, z = xp.worldToLocal(entry["latitude"], entry["longitude"],
                                  entry["altitude"] / 3.280839895)
        # 注意顺序是 (x, y, z, pitch, heading, roll)——heading 在 roll 前面
        xp.instanceSetPosition(
            aircraft.instance,
            (x, y, z, entry.get("pitch", 0.0), entry.get("heading", 0.0),
             entry.get("bank", 0.0)),
            self._animation_values(entry))

    @staticmethod
    def _object_path(path):
        """把客户端给的路径整理成 XPLM 能吃的样子。

        客户端发来的是绝对路径（用户选的 CSL 目录），Windows 上还带反斜杠。
        XPLM 的对象加载习惯 X-System 根目录的相对路径（XPMP2 专门有个
        RemoveXPSysDir 干这个）——在 X-Plane 树里的就转成相对的，不在的原样
        交上去碰运气，失败会在 _object_loaded 里说清楚。
        """
        clean = (path or "").replace("\\", "/")
        try:
            system = xp.getSystemPath().replace("\\", "/")
            if system and clean.lower().startswith(system.lower()):
                clean = clean[len(system):].lstrip("/")
        except Exception:
            pass
        return clean

    def _object_loaded(self, object_ref, refcon):
        """loadObjectAsync 的回调。加载期间飞机可能已经走了，或又换过模型。"""
        callsign, generation = refcon
        aircraft = self.aircraft.get(callsign)
        if (aircraft is None or object_ref is None
                or generation != aircraft.load_generation):
            if object_ref is not None:
                xp.unloadObject(object_ref)
            if object_ref is None and aircraft is not None:
                # 不留这行日志的话，"TCAS 有目标、窗外没飞机"完全没法查
                xp.log(f"could not load the CSL object for {callsign}: "
                       f"{aircraft.object_path}")
                aircraft.loading = False
            return
        aircraft.loading = False
        aircraft.object_ref = object_ref
        aircraft.instance = xp.createInstance(object_ref, ANIMATION_DATAREFS)

    @staticmethod
    def _animation_values(entry):
        """按 ANIMATION_DATAREFS 的顺序给值。顺序错了动画就会串。"""
        lights = entry.get("lights") or {}
        gear = entry.get("gear_down")
        if gear is None:
            # 对方没报配置就按状态猜：在地上或低速就放起落架
            gear = bool(entry.get("on_ground")) or entry.get("groundspeed", 0) < 150
        thrust = 0.0 if entry.get("on_ground") and entry.get("groundspeed", 0) < 1 else 0.7
        return [
            1.0 if gear else 0.0,
            float(entry.get("flaps", 0.0)),
            1.0 if entry.get("spoilers") else 0.0,
            0.0,                       # speed brake
            0.0,                       # slat
            0.0,                       # wing sweep
            thrust if entry.get("engines_on", True) else 0.0,
            0.0, 0.0, 0.0,             # yoke
            0.0,                       # reverser
            1.0 if lights.get("taxi_on") else 0.0,
            1.0 if lights.get("landing_on") else 0.0,
            1.0 if lights.get("beacon_on") else 0.0,
            1.0 if lights.get("strobe_on") else 0.0,
            1.0 if lights.get("nav_on") else 0.0,
        ]

    # ---------- TCAS ----------
    def _write_tcas(self, entries):
        """填 sim/cockpit2/tcas/targets/*。

        第 0 位是本机，他机从 1 开始。带上 flight_id 和 icao_type，ND 上显示
        的呼号和机型才是对的。X-Plane 会自动把最近 19 架镜像回旧的
        sim/multiplayer/position/plane#_*，所以老插件也能看到。
        """
        if not (self.tcas_available and self.have_planes):
            return

        count = len(entries)
        # 数量变少时要把多出来的槽位清掉。X-Plane 靠 modeS_id 非零判断目标
        # 存在，不清的话走掉的飞机会以最后的位置永远留在 ND/TCAS 上。
        if count < self.tcas_written:
            stale = self.tcas_written - count
            try:
                xp.setDatavi(self.tcas["modeS"], [0] * stale, 1 + count, stale)
            except Exception:
                pass
        self.tcas_written = count
        if count == 0:
            return
        xs, ys, zs, psis, thes, phis, vss, wows = [], [], [], [], [], [], [], []
        modes, ids = [], []
        flight_ids, icao_types = bytearray(), bytearray()

        for entry in entries:
            x, y, z = xp.worldToLocal(entry["latitude"], entry["longitude"],
                                      entry["altitude"] / 3.280839895)
            xs.append(x)
            ys.append(y)
            zs.append(z)
            psis.append(entry.get("heading", 0.0))
            thes.append(entry.get("pitch", 0.0))
            phis.append(entry.get("bank", 0.0))
            vss.append(entry.get("vertical_speed", 0.0))
            wows.append(1 if entry.get("on_ground") else 0)
            modes.append(int(entry.get("squawk", 0)))
            # modeS_id 要求 1..0xFFFFFF 唯一，用呼号哈希凑一个稳定的。
            # 不能用 hash()：它按进程随机加盐，重启一次 X-Plane 同一架飞机
            # 就换了个 id。
            ids.append((zlib.crc32(entry["callsign"].encode("utf-8"))
                        & 0xFFFFFF) or 1)
            flight_ids.extend(self._fixed_string(entry["callsign"], 8))
            icao_types.extend(self._fixed_string(entry.get("equipment", ""), 8))

        # 从下标 1 开始写，0 号位留给本机
        xp.setDatavf(self.tcas["x"], xs, 1, count)
        xp.setDatavf(self.tcas["y"], ys, 1, count)
        xp.setDatavf(self.tcas["z"], zs, 1, count)
        xp.setDatavf(self.tcas["psi"], psis, 1, count)
        xp.setDatavf(self.tcas["the"], thes, 1, count)
        xp.setDatavf(self.tcas["phi"], phis, 1, count)
        xp.setDatavf(self.tcas["vertical_speed"], vss, 1, count)
        xp.setDatavi(self.tcas["weight_on_wheels"], wows, 1, count)
        xp.setDatavi(self.tcas["modeC"], modes, 1, count)
        xp.setDatavi(self.tcas["modeS"], ids, 1, count)
        xp.setDatab(self.tcas["flight_id"], bytes(flight_ids), 8, len(flight_ids))
        xp.setDatab(self.tcas["icao_type"], bytes(icao_types), 8, len(icao_types))

    @staticmethod
    def _fixed_string(text, width):
        """定长、以 0 结尾的字段。TCAS 的字符串数组是按固定跨度排的。"""
        raw = (text or "").encode("ascii", errors="replace")[:width - 1]
        return raw + b"\x00" * (width - len(raw))

    def _clear_all(self):
        for aircraft in self.aircraft.values():
            aircraft.destroy()
        self.aircraft.clear()
        self.tcas_written = 0
        if self.tcas_available and self.have_planes:
            try:
                # 目标数清零，否则 ND 上会留下一圈不动的光点
                xp.setDatavi(self.tcas["modeS"], [0] * MAX_TCAS_TARGETS,
                             1, MAX_TCAS_TARGETS)
            except Exception:
                pass
