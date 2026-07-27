"""客户端和 X-Plane 插件之间的本地通道。

插件为什么要单独一个进程：X-Plane 的绘图 API 只有插件够得着，而插件跑在
X-Plane 自己的 Python 里（XPPython3），装的包和我们客户端那套不是一回事——
把 PyQt6、pymumble、pyaudio 塞进 X-Plane 进程里既装不动也不该装。

所以分工是：

    客户端  收 FSD、算插值、匹配模型      ← 复杂的部分，在这里能测
    插件    照着给的数据画 + 写 TCAS      ← 薄，改动少，不用反复重启 X-Plane

协议是**每行一个 JSON**，UDP 发到本机。选 UDP 不选 TCP 是因为这是纯位置流：
丢一帧下一帧（200 ms 后）就补上了，为了可靠性去排队反而会积压出延迟。插件那
边永远只认最后收到的一帧。

一帧超过一个 UDP 包时按 seq/part/total 分片——64 架飞机的 JSON 能到十几 KB，
本机回环 MTU 通常够，但不能赌。
"""

import json
import logging
import socket

log = logging.getLogger("桥")

# 客户端 → 插件。49900 往上是 X-Plane 自己不用的区间。
PLUGIN_PORT = 49900
# 插件 → 客户端（握手和状态回报）
CLIENT_PORT = 49901
HOST = "127.0.0.1"

# 一个 UDP 包里放多少字节的负载。本机回环能扛更大，但 8 KB 是安全线。
MAX_PAYLOAD = 8000

PROTOCOL_VERSION = 1

# 一帧最多送多少架飞机。TCAS 数组是 64 个位置，0 号给本机，所以他机 63 架。
# 客户端按距离排序后截断——超了必须先扔远的，不能随便扔。
MAX_TRAFFIC = 63


def encode(message, sequence=0, max_payload=MAX_PAYLOAD):
    """把一条消息切成若干个待发的 UDP 包（bytes 列表）。

    每个包本身是一行 JSON，带 seq/part/total，插件那边靠这三个字段重组。
    """
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    raw = body.encode("utf-8")
    if len(raw) <= max_payload:
        chunks = [raw]
    else:
        chunks = [raw[i:i + max_payload] for i in range(0, len(raw), max_payload)]

    packets = []
    for index, chunk in enumerate(chunks):
        header = {"v": PROTOCOL_VERSION, "seq": sequence,
                  "part": index, "total": len(chunks),
                  "data": chunk.decode("utf-8", errors="surrogateescape")}
        packets.append(json.dumps(header, ensure_ascii=False).encode("utf-8"))
    return packets


class Reassembler:
    """插件侧：把分片拼回完整消息。

    只保留最新的那一帧。旧帧收不齐就直接扔——位置流里迟到的数据没有价值，
    留着反而会让飞机往回跳。
    """

    def __init__(self):
        self.sequence = None
        self.parts = {}
        self.total = 0

    def feed(self, packet):
        """喂一个 UDP 包。拼齐了返回消息，否则返回 None。"""
        try:
            header = json.loads(packet.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if header.get("v") != PROTOCOL_VERSION:
            return None

        sequence = header.get("seq", 0)
        if sequence != self.sequence:
            # 新的一帧，旧的没收齐就不要了
            self.sequence = sequence
            self.parts = {}
            self.total = header.get("total", 1)

        try:
            self.parts[int(header["part"])] = header["data"]
        except (KeyError, TypeError, ValueError):
            return None

        if len(self.parts) < self.total:
            return None

        body = "".join(self.parts[i] for i in range(self.total))
        self.parts = {}
        try:
            return json.loads(body)
        except ValueError as e:
            log.debug("重组后仍解析失败: %s", e)
            return None


class BridgeSender:
    """客户端侧：把他机数据推给插件。"""

    def __init__(self, host=HOST, port=PLUGIN_PORT):
        self.address = (host, port)
        self.sequence = 0
        self._socket = None

    def _ensure(self):
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setblocking(False)
        return self._socket

    def send(self, message):
        """发一帧。失败返回 False——插件没开是常态，不该当错误刷屏。"""
        self.sequence = (self.sequence + 1) & 0xFFFF
        try:
            sock = self._ensure()
            for packet in encode(message, self.sequence):
                sock.sendto(packet, self.address)
            return True
        except OSError as e:
            log.debug("发给插件失败（插件可能没开）: %s", e)
            return False

    def send_traffic(self, entries, own=None):
        return self.send({"type": "traffic", "aircraft": entries, "own": own})

    def close(self):
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
