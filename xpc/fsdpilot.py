"""以飞行员身份连接 FSD 服务端（can-fsd）。

对应 xPilot 的 fsd 层。协议逐条对照 can-fsd 的解析代码
（internal/fsd/conn.go、handler.go、packet.go）和 docs/protocol.md：

    登录   $ID{呼号}:SERVER:{客户端ID}:{客户端名}:{主}:{次}:{CID}:{机器码}
           #AP{呼号}:SERVER:{CID}:{密码}:{等级}:{协议版本}:{模拟器}:{真实姓名}
    位置   @{应答机模式}:{呼号}:{squawk}:{等级}:{纬度}:{经度}:{高度}:{地速}:{PBH}:{气压差}
    计划   $FP{呼号}:*A:{规则}:{机型}:{巡航速度}:{起飞地}:{预计起飞}:{实际起飞}
           :{巡航高度}:{目的地}:{备降小时}:{备降分钟}:{备降场}:{备注}:{航路}
    文字   #TM{呼号}:{收件人}:{正文}
    下线   #DP{呼号}:{CID}

$ID 的第 9 个字段（challenge）留空，服务端就不会发起 VATSIM 客户端质询——
那套算法只有官方客户端有密钥表，can-fsd 允许第三方客户端不参与
（internal/fsd/conn.go 的 authenticate）。

位置包里的姿态压在一个 32 位整数里（PBH），编码必须和服务端的解码严丝合缝，
错了会让别人看到飞机以奇怪的姿态飞行。
"""

import logging
import socket
import threading
import time

log = logging.getLogger("FSD")

DEFAULT_PORT = 6809
PROTO_REVISION = 100          # ProtoRevisionClassic
RATING_OBSERVER = 1
POSITION_INTERVAL = 0.2       # 每秒 5 次，和 VATSIM 客户端一致
SLOW_POSITION_INTERVAL = 5.0  # 停在地面上没动时降频
LOGIN_TIMEOUT = 10.0
MAX_CALLSIGN_LENGTH = 10      # can-fsd 的 IsValidCallsign 上限

CLIENT_ID = "0001"
CLIENT_NAME = "XPC for CAN"
CLIENT_MAJOR = 1
CLIENT_MINOR = 0

SIMULATOR_XPLANE = 8          # X-Plane 在 FSD 里的模拟器编号

# 应答机模式对应位置包的第一个字符
XPDR_STANDBY = "S"            # 待机 / 仅 mode A
XPDR_MODE_C = "N"             # 正常
XPDR_IDENT = "Y"              # 识别


def pack_pbh(pitch, bank, heading, on_ground=False):
    """把俯仰/坡度/航向压成 32 位整数。

    can-fsd 的 PitchBankHeading 是这样拆的（internal/fsd/packet.go）：

        pitch   = 位 22-31，乘 360/1024，再折到 -180..180
        bank    = 位 12-21，同上
        heading = 位 2-11，乘 360/1024
        位 1    = 是否在地面

    所以这里按同样的比例反着编。角度先折到 0..360 再量化，否则负角度会溢出。
    """
    ratio = 1024.0 / 360.0

    def quantise(value):
        return int(round((value % 360.0) * ratio)) & 0x3FF

    packed = (quantise(pitch) << 22) | (quantise(bank) << 12) | (quantise(heading) << 2)
    if on_ground:
        packed |= 0x2
    return packed & 0xFFFFFFFF


def unpack_pbh(packed):
    """pack_pbh 的逆运算，用来还原别人的姿态。

    位宽和比例必须和 pack_pbh 对称。test_xpc.py 里另有一份从 can-fsd 的 Go
    代码转写来的独立实现当参照物——那份才是判定标准，这里改了要能对上它。
    """
    ratio = 360.0 / 1024.0
    mask = 0x3FF

    def normalise(value):
        return value - 360.0 if value > 180.0 else value

    return {
        "pitch": normalise((packed >> 22 & mask) * ratio),
        "bank": normalise((packed >> 12 & mask) * ratio),
        "heading": (packed >> 2 & mask) * ratio,
        "on_ground": bool(packed & 0x2),
    }


def callsign_problem(callsign):
    """呼号不合服务端规矩时返回说明。规则来自 can-fsd 的 IsValidCallsign。"""
    callsign = (callsign or "").strip().upper()
    if not 2 <= len(callsign) <= MAX_CALLSIGN_LENGTH:
        return (f"呼号 {callsign} 有 {len(callsign)} 个字符，"
                f"服务端只接受 2-{MAX_CALLSIGN_LENGTH} 个")
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in callsign):
        return f"呼号 {callsign} 含有服务端不接受的字符（只能是字母、数字、_ 和 -）"
    return None


def sanitize(text):
    """包是冒号分隔的，正文里的冒号和换行会破坏分帧。"""
    return (text or "").replace(":", " ").replace("\r", " ").replace("\n", " ").strip()


class FSDPilot:
    """飞行员的 FSD 连接。

    回调都在后台线程触发：
        on_status(state, message)     connecting / online / error / stopped
        on_text(sender, recipient, message)
        on_controllers(list)          附近的管制席位
    """

    def __init__(self, host, callsign, cid, password, real_name="",
                 port=DEFAULT_PORT, rating=RATING_OBSERVER, aircraft="",
                 on_status=None, on_text=None, on_controllers=None,
                 traffic=None):
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.callsign = (callsign or "").strip().upper()
        self.cid = str(cid).strip()
        self.password = password
        self.real_name = sanitize(real_name) or self.cid
        self.rating = int(rating)
        self.aircraft = sanitize(aircraft).upper()

        self.on_status = on_status
        self.on_text = on_text
        self.on_controllers = on_controllers

        # 他机表。给了就解析别人的位置包并参与机型交换；不给就只当语音+上报用。
        self.traffic = traffic
        if traffic is not None and traffic.on_request_info is None:
            traffic.on_request_info = self.request_plane_info
        # 航司码取呼号前三位字母，CCA1501 -> CCA。用于模型匹配的涂装选择。
        prefix = self.callsign[:3]
        self.airline = prefix if prefix.isalpha() else ""

        self.running = False
        self.stop_event = threading.Event()
        self.thread = None
        self._sock = None
        self._buffer = b""
        self._logged_in = False

        self._lock = threading.Lock()
        self._position = None       # 最近一次从模拟器拿到的快照
        self._squawk = 2000
        self._xpdr_mode = XPDR_STANDBY
        self._ident_until = 0.0
        self.controllers = {}       # 呼号 -> {frequency, ...}

    # ---------- 对外 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.thread = None

    @property
    def connected(self):
        return bool(self._logged_in and self._sock)

    def update_position(self, snapshot):
        """模拟器那边有新数据了。只存下来，实际发包由连接线程按节奏做。"""
        with self._lock:
            self._position = snapshot
            if snapshot:
                self._squawk = snapshot.get("squawk", self._squawk)
                mode = snapshot.get("xpdr_mode", 0)
                # X-Plane 的 transponder_mode：0 关，1 待机，2 开，3 测试/C
                self._xpdr_mode = XPDR_MODE_C if mode >= 2 else XPDR_STANDBY

    def ident(self, seconds=8.0):
        """按下 IDENT，位置包的模式字符临时变成 Y。"""
        self._ident_until = time.time() + seconds
        log.info("IDENT")

    def send_text(self, recipient, message):
        """给某个呼号发文字消息。recipient 用 @频率 可以发到频率上。"""
        message = sanitize(message)
        if not message:
            return False
        return self._send(f"#TM{self.callsign}:{sanitize(recipient)}:{message}")

    def file_flight_plan(self, plan):
        """提交飞行计划。plan 是 gui 那边攒好的字典。"""
        fields = [
            sanitize(plan.get("rules", "I"))[:1] or "I",
            sanitize(plan.get("aircraft", self.aircraft)),
            sanitize(plan.get("cruise_speed", "")),
            sanitize(plan.get("departure", "")).upper(),
            sanitize(plan.get("departure_time", "")),
            sanitize(plan.get("actual_time", "")),
            sanitize(plan.get("cruise_altitude", "")),
            sanitize(plan.get("arrival", "")).upper(),
            sanitize(plan.get("alternate_hours", "0")),
            sanitize(plan.get("alternate_minutes", "0")),
            sanitize(plan.get("alternate", "")).upper(),
            sanitize(plan.get("remarks", "")),
            sanitize(plan.get("route", "")).upper(),
        ]
        return self._send(f"$FP{self.callsign}:*A:" + ":".join(fields))

    def request_atis(self, callsign):
        """问某个管制席位要文字通播。"""
        return self._send(f"$CQ{self.callsign}:{sanitize(callsign).upper()}:ATIS")

    def request_metar(self, icao, timeout=20):
        """向服务端要 METAR（$AX）。拿不到返回 None。"""
        icao = (icao or "").strip().upper()
        if not self.connected:
            return None
        waiter = [threading.Event(), None]
        with self._lock:
            self._metar_waiter = waiter
        if not self._send(f"$AX{self.callsign}:SERVER:METAR:{icao}"):
            return None
        return waiter[1] if waiter[0].wait(timeout) else None

    # ---------- 内部 ----------
    def _status(self, state, message):
        log.info("%s %s: %s", self.callsign, state, message)
        if self.on_status:
            try:
                self.on_status(state, message)
            except Exception as e:
                log.warning("状态回调失败: %s", e)

    @staticmethod
    def _redact(packet):
        """#AP 的第 4 段是密码，日志里换成星号。"""
        if not packet.startswith("#AP"):
            return packet
        fields = packet.split(":")
        if len(fields) > 3:
            fields[3] = "***"
        return ":".join(fields)

    def _send(self, packet):
        if not self._sock:
            return False
        try:
            self._sock.sendall((packet + "\r\n").encode("utf-8", errors="replace"))
            log.debug("→ %s", self._redact(packet))
            return True
        except Exception as e:
            self._status('error', f"发送失败: {e}")
            return False

    def _run(self):
        try:
            if not self._connect():
                return
            self._loop()
        except Exception as e:
            self._status('error', f"FSD 连接异常: {e}")
        finally:
            self._close()

    def _connect(self):
        problem = callsign_problem(self.callsign)
        if problem:
            self._status('error', problem)
            return False

        self._status('connecting', f"正在以 {self.callsign} 登录 {self.host} …")
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=10)
            self._sock.settimeout(1.0)
        except Exception as e:
            self._status('error', f"无法连接 FSD 服务器 {self.host}:{self.port}（{e}）")
            return False

        greeting = self._read_packet(timeout=5)
        if greeting:
            log.info("服务端问候: %s", greeting)

        machine_id = sum(ord(c) for c in self.callsign) * 7919
        self._send(f"$ID{self.callsign}:SERVER:{CLIENT_ID}:{CLIENT_NAME}:"
                   f"{CLIENT_MAJOR}:{CLIENT_MINOR}:{self.cid}:{machine_id}")
        self._send(f"#AP{self.callsign}:SERVER:{self.cid}:{self.password}:"
                   f"{self.rating}:{PROTO_REVISION}:{SIMULATOR_XPLANE}:{self.real_name}")
        self._send(f"$CQ{self.callsign}:SERVER:CAPS")

        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            if not self.running or self.stop_event.is_set():
                return False
            packet = self._read_packet(timeout=1)
            if packet is None:
                continue
            if packet == "":
                self._status('error', "FSD 服务器关闭了连接")
                return False
            if self._handle_packet(packet) is False:
                return False
            if self._logged_in:
                self._status('online', f"已作为 {self.callsign} 上线")
                # 上线先问一遍在线管制，好把附近频率列出来
                self._send(f"$CQ{self.callsign}:SERVER:ATC")
                return True

        self._status('error', "FSD 登录超时，未收到服务器回应")
        return False

    def _loop(self):
        next_position = 0.0
        while self.running and not self.stop_event.is_set():
            packet = self._read_packet(timeout=0.2)
            if packet == "":
                self._status('error', "与 FSD 服务器的连接已断开")
                return
            if packet and self._handle_packet(packet) is False:
                return

            now = time.time()
            if now >= next_position:
                interval = self._send_position()
                next_position = now + interval

    def _send_position(self):
        """发一个位置包，返回下一次的间隔。"""
        with self._lock:
            snapshot = self._position
            squawk = self._squawk
            mode = self._xpdr_mode

        if not snapshot:
            return POSITION_INTERVAL

        if time.time() < self._ident_until:
            mode = XPDR_IDENT

        pbh = pack_pbh(snapshot["pitch"], snapshot["bank"], snapshot["heading"],
                       snapshot.get("on_ground", False))
        self._send(
            f"@{mode}:{self.callsign}:{squawk:04d}:{self.rating}:"
            f"{snapshot['latitude']:.5f}:{snapshot['longitude']:.5f}:"
            f"{snapshot['altitude']}:{snapshot['groundspeed']}:{pbh}:0")

        # 停在地面上没动就降频，省得刷屏
        if snapshot.get("on_ground") and snapshot.get("groundspeed", 0) < 1:
            return SLOW_POSITION_INTERVAL
        return POSITION_INTERVAL

    def _read_packet(self, timeout=1):
        """读一个包。超时返回 None，连接关闭返回 ""，空行跳过。"""
        while True:
            while b"\n" not in self._buffer:
                try:
                    self._sock.settimeout(timeout)
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    return None
                except Exception:
                    return ""
                if not chunk:
                    return ""
                self._buffer += chunk

            line, self._buffer = self._buffer.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip("\r").strip()
            if text:
                return text

    def _handle_packet(self, packet):
        """返回 False 表示应当结束连接。"""
        log.debug("← %s", packet)
        fields = packet.split(":")
        head = fields[0]

        if head.startswith("$ER"):
            code = fields[2] if len(fields) > 2 else "?"
            message = fields[4] if len(fields) > 4 else packet
            if not self._logged_in:
                self._status('error', f"FSD 拒绝登录（{code}）: {message}")
                return False
            log.warning("服务器返回错误（%s）: %s", code, message)
            return True

        if head.startswith("$AR") and len(fields) >= 4 and fields[2] == "METAR":
            waiter = getattr(self, "_metar_waiter", None)
            if waiter:
                waiter[1] = ":".join(fields[3:]).strip()
                waiter[0].set()
            return True

        if head.startswith("#TM") and len(fields) >= 3:
            sender = head[3:]
            recipient = fields[1]
            body = ":".join(fields[2:])
            if self.on_text:
                try:
                    self.on_text(sender, recipient, body)
                except Exception as e:
                    log.warning("文字消息回调出错: %s", e)
            return True

        if head.startswith("%") and len(fields) >= 3:
            # 管制席位的位置包：%呼号:频率:席位类型:可视范围:等级:纬度:经度:0
            self._note_controller(head[1:], fields)
            return True

        if head.startswith("@") and len(fields) >= 9:
            self._note_traffic(fields)
            return True

        if head.startswith("#SB") and len(fields) >= 3:
            self._handle_plane_info(head[3:], fields)
            return True

        if head.startswith("#DP"):
            # 注意是 is not None：TrafficTable 有 __len__，空表本身是假值
            if self.traffic is not None:
                self.traffic.remove(head[3:])
            return True

        if head.startswith("$CR") and len(fields) >= 3:
            if fields[1] == self.callsign and fields[2] == "CAPS":
                self._logged_in = True
            return True

        if head.startswith("$CQ") and len(fields) >= 3:
            sender, recipient, query = head[3:], fields[1], fields[2]
            if recipient == self.callsign:
                if query == "CAPS":
                    self._send(f"$CR{self.callsign}:{sender}:CAPS:ATCINFO=0:MODELDESC=1")
                elif query == "RN":
                    self._send(f"$CR{self.callsign}:{sender}:RN:{self.real_name}::{self.rating}")
                elif query == "ACC":
                    self._send(f"$CR{self.callsign}:{sender}:ACC:{self.aircraft}")
            return True

        if head.startswith("$PI") and len(fields) >= 3:
            self._send(f"$PO{self.callsign}:{head[3:]}:{':'.join(fields[2:])}")
            return True

        if head.startswith("#DA") or head.startswith("#DL"):
            if head.startswith("#DA"):
                self._forget_controller(head[3:])
            self._logged_in = True
            return True

        return True

    def _note_traffic(self, fields):
        """别人的位置包。字段顺序和我们自己发的那个一样。

        `@` 包里没有机型，所以第一次见到某架飞机时 TrafficTable 会回调
        request_plane_info() 去问对方要。
        """
        # 必须写 is None：TrafficTable 有 __len__，空表本身是假值，
        # 写成 `if not self.traffic` 会让第一架飞机永远进不来。
        if self.traffic is None:
            return
        callsign = fields[1]
        if callsign == self.callsign:
            return          # 服务器回显了我们自己的包
        try:
            attitude = unpack_pbh(int(fields[8]))
            self.traffic.update_position(
                callsign,
                latitude=float(fields[4]), longitude=float(fields[5]),
                altitude=int(fields[6]), groundspeed=int(fields[7]),
                pitch=attitude["pitch"], bank=attitude["bank"],
                heading=attitude["heading"], on_ground=attitude["on_ground"],
                squawk=int(fields[2]), mode=fields[0][1:] or "S")
        except (IndexError, ValueError) as e:
            log.debug("位置包解析失败 %s: %s", fields[:2], e)

    def request_plane_info(self, callsign):
        """问对方的机型，用于模型匹配。"""
        return self._send(f"#SB{self.callsign}:{callsign}:PIR")

    def _handle_plane_info(self, sender, fields):
        """#SB。别人问我们机型要答，别人报机型要记下来。"""
        kind = fields[2] if len(fields) > 2 else ""

        if kind == "PIR":
            # 别人问我们。不答的话对方只能拿通用模型画我们。
            reply = f"#SB{self.callsign}:{sender}:PI:GEN:EQUIPMENT={self.aircraft or 'B738'}"
            if self.airline:
                reply += f":AIRLINE={self.airline}"
            self._send(reply)
            return

        if self.traffic is None:
            return

        if kind == "PI" and len(fields) > 3 and fields[3] == "GEN":
            # 键值对顺序不保证，出现与否也不保证（protocol.md 明说了）
            info = {}
            for field in fields[4:]:
                key, _, value = field.partition("=")
                name = {"EQUIPMENT": "equipment", "AIRLINE": "airline",
                        "LIVERY": "livery", "CSL": "csl"}.get(key.upper())
                if name and value:
                    info[name] = value
            if info:
                self.traffic.set_plane_info(sender, **info)
            return

        if kind == "PI" and len(fields) > 3 and fields[3] == "X":
            # 老式：#SB发方:收方:PI:X:0:发动机类型:CSL=名字（有的客户端写成 ~名字）
            for field in fields[4:]:
                if field.upper().startswith("CSL="):
                    self.traffic.set_plane_info(sender, csl=field[4:])
                elif field.startswith("~"):
                    self.traffic.set_plane_info(sender, csl=field[1:])

    def _note_controller(self, callsign, fields):
        """记下一个在线管制席位，界面用来列附近频率。"""
        try:
            frequency = fields[1]
            # 协议里频率是 5 位，开头的 1 和小数点是隐含的：28500 → 128.500
            display = f"1{frequency[:2]}.{frequency[2:]}" if len(frequency) == 5 else frequency
            entry = {"callsign": callsign, "frequency": display,
                     "facility": int(fields[2]) if len(fields) > 2 else 0,
                     "seen": time.time()}
        except (IndexError, ValueError):
            return
        self.controllers[callsign] = entry
        if self.on_controllers:
            try:
                self.on_controllers(list(self.controllers.values()))
            except Exception as e:
                log.warning("管制列表回调出错: %s", e)

    def _forget_controller(self, callsign):
        if self.controllers.pop(callsign, None) and self.on_controllers:
            try:
                self.on_controllers(list(self.controllers.values()))
            except Exception as e:
                log.warning("管制列表回调出错: %s", e)

    def _close(self):
        if self._sock:
            try:
                self._send(f"#DP{self.callsign}:{self.cid}")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._logged_in = False
        self._status('stopped', "已从 FSD 下线")
