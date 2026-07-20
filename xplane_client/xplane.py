"""
xplane.py — 读取 X-Plane COM1 频率（MHz）并退出

用法:
    python xplane.py

依赖: 无（仅用标准库）

协议说明:
    1. 监听多播组 239.255.1.1:49707 接收 X-Plane 信标 BECN
    2. 从信标解析出 X-Plane 的数据端口
    3. 发送 RREF 请求 sim/cockpit/radios/com1_freq_hz
    4. 接收响应，解析出频率值（单位：百 MHz，12540 = 125.40 MHz）
    5. 打印到控制台后退出
"""

import socket
import struct
import sys
MCAST_GRP = "239.255.1.1"
MCAST_PORT = 49707
DISCOVER_TIMEOUT = 10   # 发现 X-Plane 超时（秒）
RESPONSE_TIMEOUT = 3     # 等待数据响应超时（秒）


def discover_xplane():
    """通过多播信标发现 X-Plane，返回 (ip, port)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Windows 需要绑定到 0.0.0.0（所有接口）
    sock.bind(("0.0.0.0", MCAST_PORT))
    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(DISCOVER_TIMEOUT)

    print(f"正在搜索 X-Plane（超时 {DISCOVER_TIMEOUT}s）...", file=sys.stderr)

    try:
        data, addr = sock.recvfrom(1500)
    except socket.timeout:
        print(
            "错误：未发现 X-Plane。\n"
            "请确认：\n"
            "  1. X-Plane 正在运行且已进入飞行\n"
            "  2. 设置 → Data Output → IPs for UDP network 中已添加本机 IP",
            file=sys.stderr,
        )
        sock.close()
        sys.exit(1)
    finally:
        sock.close()

    if data[:5] != b"BECN\x00":
        print(f"错误：收到未知信标 {data[:5]!r}", file=sys.stderr)
        sys.exit(1)

    # 解析信标: BECN\x00 + mainVer(u8) + minorVer(u8) + softwareID(s32) + xpVer(s32) + role(u32) + port(u16)
    # 注意：必须用 "=" 前缀禁用 struct 对齐填充，否则本地对齐会在 B,B 后的 i 前插入填充字节
    _, main_ver, minor_ver, _, _, _, _port = struct.unpack_from("=5sBBiiIH", data)
    sender_ip = addr[0]

    print(f"发现 X-Plane v{main_ver}.{minor_ver} @ {sender_ip}:{_port}", file=sys.stderr)

    # 生成候选地址：信标地址 + 本地回环 + 默认端口 49000
    candidates = set()
    for ip in {sender_ip, "127.0.0.1"}:
        for p in {_port, 49000}:
            candidates.add((ip, p))

    # 把 127.0.0.1:49000 移到最前面（最可能有效）
    sorted_candidates = sorted(candidates, key=lambda x: (x[1] != 49000 or x[0] != "127.0.0.1"))
    return sorted_candidates


def send_rref(addr, dataref, index=0, freq=1):
    """发送 RREF 请求，成功返回 (index, value)，失败返回 None。"""
    packet = struct.pack("=5sii", b"RREF\x00", freq, index)
    packet += dataref.encode() + b"\x00"
    packet = packet.ljust(413, b"\x00")  # C++ 的 sendData(buffer, 413)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(RESPONSE_TIMEOUT)

    try:
        sock.sendto(packet, addr)
    except OSError:
        sock.close()
        return None

    try:
        while True:
            try:
                data, _ = sock.recvfrom(1500)
            except ConnectionResetError:
                continue
            # C++ 的 compareHead 只检查前 4 字节 "RREF"
            if len(data) < 12 or data[:4] != b"RREF":
                continue
            # 数据对从偏移 5（HEADER_LENGTH=5）开始：int32 index + float32 value
            remaining = len(data) - 5
            if remaining % 8 != 0:
                continue
            for i in range(remaining // 8):
                off = 5 + i * 8
                ridx, rval = struct.unpack_from("=if", data, off)
                if ridx == index:
                    sock.close()
                    return (ridx, rval)
    except socket.timeout:
        sock.close()
        return None


def read_com1_freq(addr):
    """读取 COM1 频率并返回 MHz 值。"""
    result = send_rref(addr, "sim/cockpit/radios/com1_freq_hz", index=0)
    if result is None:
        return None
    _, value = result
    return value / 100.0


def main():
    candidates = discover_xplane()
    for addr in candidates:
        result = read_com1_freq(addr)
        if result is not None:
            print(f"{result:.2f}")
            return

    print("错误：所有地址均无法获取 COM1 频率。", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
