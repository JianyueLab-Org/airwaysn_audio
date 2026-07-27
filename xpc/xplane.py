"""X-Plane 数据链路。

对应 xPilot 的 simulator 层：把机位、姿态、速度、无线电和应答机从 X-Plane 取
出来，供 FSD 位置包和语音频率使用。

和 xplane_client 里那个一问一答的读法不同，这里是**订阅**：一次 RREF 请求把
所有需要的 dataref 按频率订上，之后 X-Plane 会持续推过来，本地只保留最新值。
飞行员客户端每秒要发好几次位置包，一次一问一答的往返扛不住。

    发现   监听多播 239.255.1.1:49707 的 BECN 信标，拿到 X-Plane 的地址和端口
    订阅   RREF 请求，freq 指定每秒推送次数，index 是我们自己编的号
    接收   RREF 回包里是 (index, float32) 对，按 index 对回 dataref
"""

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("模拟器")

MCAST_GROUP = "239.255.1.1"
MCAST_PORT = 49707
DISCOVER_TIMEOUT = 10
DEFAULT_PORT = 49000
UPDATE_RATE = 5                 # 每个 dataref 每秒推送次数
STALE_AFTER = 3.0               # 超过这么久没有新数据就认为断了
REDISCOVER_AFTER = 15.0         # 还是没有的话，重新去找一次 X-Plane

# 我们订阅的 dataref。索引是发给 X-Plane 的编号，回包按它对应回来。
DATAREFS = {
    "latitude":     "sim/flightmodel/position/latitude",
    "longitude":    "sim/flightmodel/position/longitude",
    # 真高（米），FSD 要英尺
    "elevation":    "sim/flightmodel/position/elevation",
    "agl":          "sim/flightmodel/position/y_agl",
    "groundspeed":  "sim/flightmodel/position/groundspeed",
    "pitch":        "sim/flightmodel/position/theta",
    "bank":         "sim/flightmodel/position/phi",
    "heading_true": "sim/flightmodel/position/psi",
    "squawk":       "sim/cockpit/radios/transponder_code",
    "xpdr_mode":    "sim/cockpit/radios/transponder_mode",
    # 0.001 MHz 精度，支持 8.33 kHz 间隔。X-Plane 11.30 起才有。
    "com1":         "sim/cockpit2/radios/actuators/com1_frequency_hz_833",
    "com2":         "sim/cockpit2/radios/actuators/com2_frequency_hz_833",
    # 老的 dataref，0.01 MHz 精度。两个一起订，谁回就用谁——不存在的 dataref
    # X-Plane 只是不推送，不会报错，所以不需要按版本分支。
    "com1_legacy":  "sim/cockpit/radios/com1_freq_hz",
    "com2_legacy":  "sim/cockpit/radios/com2_freq_hz",
    "com1_power":   "sim/cockpit2/radios/actuators/com1_power",
    "on_ground":    "sim/flightmodel/failures/onground_any",
}

INDEX_TO_NAME = {index: name for index, name in enumerate(DATAREFS)}
NAME_TO_INDEX = {name: index for index, name in INDEX_TO_NAME.items()}

METRES_PER_FOOT = 0.3048
KNOTS_PER_MPS = 1.9438444924406


class XPlaneLink:
    """和 X-Plane 的一条 UDP 链路。

    on_state(connected, message) 在后台线程调用。
    """

    def __init__(self, on_state=None):
        self.on_state = on_state
        self.address = None
        self.values = {}            # dataref 名 -> 最新值
        self.last_update = 0.0
        self.running = False

        self._socket = None
        self._thread = None
        self._lock = threading.Lock()
        self._connected = False

    # ---------- 状态 ----------
    def _state(self, connected, message):
        if connected != self._connected:
            self._connected = connected
            log.info("%s: %s", "已连接" if connected else "已断开", message)
            if self.on_state:
                try:
                    self.on_state(connected, message)
                except Exception as e:
                    log.warning("状态回调出错: %s", e)

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
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    # ---------- 发现 ----------
    def discover(self, timeout=DISCOVER_TIMEOUT):
        """等 X-Plane 的多播信标。返回 (ip, port) 或 None。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", MCAST_PORT))
            mreq = struct.pack("4sl", socket.inet_aton(MCAST_GROUP), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(timeout)
            data, addr = sock.recvfrom(1500)
        except (socket.timeout, OSError):
            return None
        finally:
            sock.close()

        if data[:5] != b"BECN\x00":
            log.warning("收到未知信标 %r", data[:5])
            return None
        try:
            _, major, minor, _, _, _, port = struct.unpack_from("=5sBBiiIH", data)
        except struct.error:
            return None
        log.info("发现 X-Plane v%s.%s @ %s:%s", major, minor, addr[0], port)
        return (addr[0], port)

    # ---------- 订阅 ----------
    def _subscribe(self, sock, address, rate=UPDATE_RATE):
        """把所有 dataref 订上。rate=0 表示退订。"""
        for name, dataref in DATAREFS.items():
            packet = struct.pack("=5sii", b"RREF\x00", rate, NAME_TO_INDEX[name])
            packet += dataref.encode() + b"\x00"
            packet = packet.ljust(413, b"\x00")
            try:
                sock.sendto(packet, address)
            except OSError as e:
                log.warning("订阅 %s 失败: %s", dataref, e)

    def _run(self):
        while self.running:
            address = self.address or self.discover(timeout=5)
            if not address:
                # 有的机器上多播收不到，直接试本机默认端口
                address = ("127.0.0.1", DEFAULT_PORT)

            self._close()
            try:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._socket.bind(("0.0.0.0", 0))
                self._socket.settimeout(1.0)
            except OSError as e:
                self._state(False, f"打不开 UDP 端口: {e}")
                time.sleep(2)
                continue

            self._subscribe(self._socket, address)
            self.address = address
            log.info("已向 %s:%s 订阅 %d 个 dataref", address[0], address[1], len(DATAREFS))

            got_data = False
            silent_since = time.time()
            while self.running:
                try:
                    data, _ = self._socket.recvfrom(2048)
                except socket.timeout:
                    if not self._still_waiting(silent_since):
                        break
                    continue
                except ConnectionResetError:
                    # Windows 上向没人监听的端口发 UDP 会回一个 ICMP 不可达，
                    # 下一次 recvfrom 就抛这个。X-Plane 还没起来时就是这种情况，
                    # 当成超时接着等——按 OSError 处理会一秒重订一次。
                    if not self._still_waiting(silent_since):
                        break
                    time.sleep(0.2)
                    continue
                except OSError:
                    break

                if self._handle(data):
                    silent_since = time.time()
                    if not got_data:
                        got_data = True
                        self._state(True, f"已连接 X-Plane @ {address[0]}")

            self._close()
            if self.running:
                time.sleep(1)

    def _still_waiting(self, silent_since):
        """一直没数据的时候该继续等还是换个地址重来。"""
        silent = time.time() - silent_since
        if silent > STALE_AFTER:
            self._state(False, "X-Plane 没有数据（是否已进入飞行？）")
        if silent > REDISCOVER_AFTER:
            self.address = None          # 重新发现一次
            return False
        return True

    def _handle(self, data):
        """解析一个 RREF 回包，返回是否有有效数据。"""
        if len(data) < 13 or data[:4] != b"RREF":
            return False
        body = len(data) - 5
        if body % 8:
            return False

        updates = {}
        for i in range(body // 8):
            index, value = struct.unpack_from("=if", data, 5 + i * 8)
            name = INDEX_TO_NAME.get(index)
            if name:
                updates[name] = value
        if not updates:
            return False

        with self._lock:
            self.values.update(updates)
            self.last_update = time.time()
        return True

    # ---------- 取值 ----------
    def snapshot(self):
        """当前这一份数据，已经换算成 FSD 要的单位。"""
        with self._lock:
            raw = dict(self.values)
        if not raw:
            return None

        elevation = raw.get("elevation", 0.0)
        return {
            "latitude": raw.get("latitude", 0.0),
            "longitude": raw.get("longitude", 0.0),
            "altitude": int(round(elevation / METRES_PER_FOOT)),
            "agl": int(round(raw.get("agl", 0.0) / METRES_PER_FOOT)),
            "groundspeed": int(round(raw.get("groundspeed", 0.0) * KNOTS_PER_MPS)),
            "pitch": raw.get("pitch", 0.0),
            "bank": raw.get("bank", 0.0),
            "heading": raw.get("heading_true", 0.0) % 360.0,
            "squawk": int(raw.get("squawk", 2000)),
            "xpdr_mode": int(raw.get("xpdr_mode", 0)),
            "com1": self._frequency(raw.get("com1"), raw.get("com1_legacy")),
            "com2": self._frequency(raw.get("com2"), raw.get("com2_legacy")),
            "com1_power": bool(raw.get("com1_power", 1)),
            "on_ground": bool(raw.get("on_ground", 0)),
        }

    @staticmethod
    def _frequency(precise, legacy=None):
        """COM 频率（MHz）。优先 8.33 那个，没有就用老的。

        _833 的单位是 kHz，除以 1000 得兆赫，能表示 8.33 间隔（132.005）。
        老的那个单位是 10 kHz，除以 100 得兆赫，只有 0.01 MHz 精度——X-Plane
        11.30 以前只有它，8.33 的频道会被舍到最近的 25 kHz。
        """
        if precise and precise > 0:
            return round(float(precise) / 1000.0, 3)
        if legacy and legacy > 0:
            return round(float(legacy) / 100.0, 3)
        return None
