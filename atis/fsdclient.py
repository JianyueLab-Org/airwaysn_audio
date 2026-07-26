"""以 ATIS 席位的身份连接 FSD 服务端（can-fsd）。

通播因此同时做两件事：这条 FSD 连接让席位出现在在线列表和数据源里、回答飞行员
客户端的文字通播查询；broadcast.py 那条 Mumble 连接在同一频率上播语音。

协议要点，逐条对照 can-fsd 的解析代码（internal/fsd/conn.go、handler.go、
metar.go）和 docs/protocol.md：

    登录     $ID{呼号}:SERVER:{客户端ID}:{客户端名}:{主版本}:{次版本}:{CID}:{机器码}
             #AA{呼号}:SERVER:{真实姓名}:{CID}:{密码}:{等级}:{协议版本}
    位置     %{呼号}:{频率}:{席位类型}:{可视范围}:{等级}:{纬度}:{经度}:0
    通播回复 $CR{呼号}:{对方}:ATIS:T:{一行}  …  末行 :E:{行数}
    气象     $AX{呼号}:SERVER:METAR:{ICAO}  →  $ARserver:{呼号}:METAR:{报文}
    下线     #DA{呼号}:{CID}

$ID 的第 9 个字段（challenge）故意留空：填了服务端就会发起 VATSIM 客户端质询
（$ZC），那套算法只有官方客户端有密钥表。can-fsd 允许不参与质询
（internal/fsd/conn.go 的 authenticate）。

频率按协议压成 5 位：118.000 → "18000"，开头的 1 和小数点是隐含的。
"""

import logging
import socket
import threading
import time

log = logging.getLogger("FSD")

DEFAULT_PORT = 6809
PROTO_REVISION = 100          # ProtoRevisionClassic
FACILITY_ATIS = 7
RATING_OBSERVER = 1           # 通播不需要管制权限，用最低等级登录一定能通过
POSITION_INTERVAL = 15.0      # 服务端 150 秒收不到位置包就断线
LOGIN_TIMEOUT = 10.0
MAX_ATIS_LINES = 64           # can-fsd 每个席位最多收 64 行
ATIS_LINE_WIDTH = 70
MAX_CALLSIGN_LENGTH = 10      # can-fsd 的 IsValidCallsign 上限

CLIENT_ID = "0001"
CLIENT_NAME = "AirwaySN ATIS"
CLIENT_MAJOR = 1
CLIENT_MINOR = 0


def encode_frequency(frequency):
    """118.000 → "18000"（开头的 1 和小数点是协议隐含的）。"""
    value = int(round(float(frequency) * 1000))
    return f"{value:06d}"[1:]


def sanitize_line(line):
    """包是冒号分隔的，正文里的冒号和换行会破坏分帧。"""
    return line.replace(":", " ").replace("\r", " ").replace("\n", " ").strip()


def wrap_atis_text(text, width=ATIS_LINE_WIDTH):
    """把通播文字折成若干行。"""
    lines, current = [], ""
    for word in sanitize_line(text).split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:MAX_ATIS_LINES]


def callsign_problem(callsign):
    """呼号不合服务端规矩时返回说明，合规返回 None。

    规则来自 can-fsd 的 IsValidCallsign / IsATISCallsign。长度上限尤其容易踩：
    ZSPD_D_ATIS 有 11 个字符，超了。
    """
    callsign = (callsign or "").strip().upper()
    if not 2 <= len(callsign) <= MAX_CALLSIGN_LENGTH:
        return (f"呼号 {callsign} 有 {len(callsign)} 个字符，"
                f"服务端只接受 2-{MAX_CALLSIGN_LENGTH} 个")
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in callsign):
        return f"呼号 {callsign} 含有服务端不接受的字符"
    if not callsign.endswith("_ATIS"):
        return f"呼号 {callsign} 不是以 _ATIS 结尾，服务端不会把它算作通播席位"
    return None


class FSDClient:
    """一个通播席位的 FSD 连接。on_status(state, message) 在后台线程调用。"""

    def __init__(self, host, callsign, cid, password, frequency,
                 real_name="ATIS", port=DEFAULT_PORT, rating=None,
                 latitude=0.0, longitude=0.0, vis_range=50,
                 atis_lines=None, on_status=None, rating_lookup=None):
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.callsign = callsign.strip().upper()
        self.cid = str(cid).strip()
        self.password = password
        self.real_name = sanitize_line(real_name or "ATIS") or "ATIS"
        # rating 为 None 表示"自动"：登录前用 rating_lookup 查本人此刻的等级，
        # 查不到就退回观察员。查询放在连接线程里做，不卡界面。
        self.rating = int(rating) if rating else None
        self.rating_lookup = rating_lookup
        self.latitude = float(latitude or 0.0)
        self.longitude = float(longitude or 0.0)
        self.vis_range = int(vis_range)
        self.on_status = on_status

        self._frequency = str(frequency).strip()
        self._atis_lines = list(atis_lines or [])
        self._lock = threading.Lock()

        # 等待中的 METAR 请求：ICAO → [Event, 结果]
        self._metar_waiters = {}
        self._metar_lock = threading.Lock()

        self.running = False
        self.stop_event = threading.Event()
        self.thread = None
        self._sock = None
        self._buffer = b""
        self._logged_in = False

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

    def set_atis_lines(self, lines):
        with self._lock:
            self._atis_lines = list(lines or [])[:MAX_ATIS_LINES]

    def set_frequency(self, frequency):
        with self._lock:
            self._frequency = str(frequency).strip()

    @property
    def frequency(self):
        with self._lock:
            return self._frequency

    @property
    def atis_lines(self):
        with self._lock:
            return list(self._atis_lines)

    def request_metar(self, icao, timeout=20):
        """向服务端要一份 METAR（$AX）。拿不到返回 None。

        服务端自己有气象源和缓存（can-fsd internal/fsd/metar.go），走这条路
        就不用再去连外部气象接口。
        """
        icao = icao.strip().upper()
        if not self.connected:
            return None

        waiter = [threading.Event(), None]
        with self._metar_lock:
            self._metar_waiters[icao] = waiter
        try:
            if not self._send(f"$AX{self.callsign}:SERVER:METAR:{icao}"):
                return None
            if not waiter[0].wait(timeout):
                return None
            return waiter[1]
        finally:
            with self._metar_lock:
                self._metar_waiters.pop(icao, None)

    # ---------- 内部 ----------
    def _status(self, state, message):
        log.info("%s %s: %s", self.callsign, state, message)
        if self.on_status:
            try:
                self.on_status(state, message)
            except Exception as e:
                log.warning(f"状态回调失败: {e}")

    def _send(self, packet):
        if not self._sock:
            return False
        try:
            self._sock.sendall((packet + "\r\n").encode("utf-8", errors="replace"))
            # 登录包里带密码，日志里要遮掉
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

        if self.rating is None:
            found = None
            if self.rating_lookup:
                try:
                    found = self.rating_lookup()
                except Exception as e:
                    log.warning("查等级失败: %s", e)
            self.rating = found or RATING_OBSERVER

        self._status('connecting',
                     f"正在以 {self.callsign} 登录 FSD（等级 {self.rating}）…")
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=10)
            self._sock.settimeout(1.0)
        except Exception as e:
            self._status('error', f"无法连接 FSD 服务器 {self.host}:{self.port}（{e}）")
            return False

        greeting = self._read_packet(timeout=5)
        if greeting:
            log.info(f"服务端问候: {greeting}")

        machine_id = sum(ord(c) for c in self.callsign) * 7919
        self._send(f"$ID{self.callsign}:SERVER:{CLIENT_ID}:{CLIENT_NAME}:"
                   f"{CLIENT_MAJOR}:{CLIENT_MINOR}:{self.cid}:{machine_id}")
        self._send(f"#AA{self.callsign}:SERVER:{self.real_name}:{self.cid}:"
                   f"{self.password}:{self.rating}:{PROTO_REVISION}")
        # 登录没有专门的成功包，用一次 CAPS 查询换个明确回应
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
                self._send_position()
                self._status('online', f"已作为 {self.callsign} 登录 FSD")
                return True

        self._status('error', "FSD 登录超时，未收到服务器回应")
        return False

    def _loop(self):
        next_position = time.time() + POSITION_INTERVAL
        while self.running and not self.stop_event.is_set():
            packet = self._read_packet(timeout=1)
            if packet == "":
                self._status('error', "与 FSD 服务器的连接已断开")
                return
            if packet and self._handle_packet(packet) is False:
                return
            if time.time() >= next_position:
                if not self._send_position():
                    return
                next_position = time.time() + POSITION_INTERVAL

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

    def _send_position(self):
        try:
            frequency = encode_frequency(self.frequency)
        except (TypeError, ValueError):
            self._status('error', f"频率 {self.frequency} 无法编码")
            return False
        return self._send(
            f"%{self.callsign}:{frequency}:{FACILITY_ATIS}:{self.vis_range}:"
            f"{self.rating}:{self.latitude:.5f}:{self.longitude:.5f}:0")

    @staticmethod
    def _redact(packet):
        """#AA 的第 5 段是密码，日志里换成星号。"""
        if not packet.startswith("#AA"):
            return packet
        fields = packet.split(":")
        if len(fields) > 4:
            fields[4] = "***"
        return ":".join(fields)

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
            # 登录之后的 $ER 多半是某次查询失败（比如没有该机场的气象），
            # 不该把整条连接拆掉
            log.info(f"服务器返回错误（{code}）: {message}")
            self._fail_metar_waiters()
            return True

        if head.startswith("$AR") and len(fields) >= 4 and fields[2] == "METAR":
            self._resolve_metar(":".join(fields[3:]).strip())
            return True

        if head.startswith("$CQ") and len(fields) >= 3:
            sender, recipient, query = head[3:], fields[1], fields[2]
            if recipient == self.callsign:
                if query == "ATIS":
                    self._send_atis(sender)
                elif query == "CAPS":
                    self._send(f"$CR{self.callsign}:{sender}:CAPS:ATCINFO=1")
            return True

        if head.startswith("$CR") and len(fields) >= 3:
            if fields[1] == self.callsign and fields[2] == "CAPS":
                self._logged_in = True      # 登录成功的确认
            return True

        if head.startswith("$PI") and len(fields) >= 3:
            self._send(f"$PO{self.callsign}:{head[3:]}:{':'.join(fields[2:])}")
            return True

        if head.startswith("#TM"):
            log.info(f"服务器消息: {packet}")
            return True

        if head.startswith("#DL"):
            self._logged_in = True          # 服务端心跳
            return True

        return True

    def _send_atis(self, recipient):
        """回答一次通播查询：逐行 T，最后一行 E 带行数。

        飞行员客户端问的是 ATIS；服务端自己的轮询在旧 Python 服务端上收的是
        TEXTATIS（见 can-fsd handler.go 的 handleTextATISResponse），所以回给
        服务端时两种都发一遍，多余的那份会被忽略。
        """
        lines = self.atis_lines
        for query_type in (["ATIS", "TEXTATIS"] if recipient == "SERVER" else ["ATIS"]):
            for line in lines:
                if not self._send(f"$CR{self.callsign}:{recipient}:{query_type}:T:{line}"):
                    return
            self._send(f"$CR{self.callsign}:{recipient}:{query_type}:E:{len(lines)}")

    def _resolve_metar(self, report):
        icao = report.split()[0].upper() if report.split() else ""
        with self._metar_lock:
            waiter = self._metar_waiters.get(icao)
            if waiter is None and len(self._metar_waiters) == 1:
                waiter = next(iter(self._metar_waiters.values()))
        if waiter:
            waiter[1] = report
            waiter[0].set()

    def _fail_metar_waiters(self):
        with self._metar_lock:
            waiters = list(self._metar_waiters.values())
        for waiter in waiters:
            waiter[0].set()

    def _close(self):
        if self._sock:
            try:
                self._send(f"#DA{self.callsign}:{self.cid}")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._logged_in = False
        self._status('stopped', "已从 FSD 下线")
