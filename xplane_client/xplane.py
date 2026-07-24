"""
xplane.py — 读取 X-Plane COM1 频率（MHz）并退出

用法:
    python xplane.py                    # RREF 模式（0.01 MHz 精度，2 位小数）
    python xplane.py --raw              # 打印原始 RREF float32 值
    python xplane.py --data             # DATA 输出行 3（可能支持 Hz 级精度）

精度说明:
    sim/cockpit/radios/com1_freq_hz 通过 RREF 只返回整数编码的 0.01 MHz 值
    （如 12541 = 125.41 MHz）。
    X-Plane 内部以 Hz 级精度存储，但 RREF/UDP 不暴露该精度。
    DATA 输出行 3 可尝试以 Hz 为单位获取（需在 X-Plane 设置中勾选）。
"""

import socket
import struct
import sys
import time

MCAST_GRP = "239.255.1.1"
MCAST_PORT = 49707
DISCOVER_TIMEOUT = 10
RESPONSE_TIMEOUT = 3
DATA_TIMEOUT = 2


def discover_xplane():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", MCAST_PORT))
    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(DISCOVER_TIMEOUT)

    print(f"正在搜索 X-Plane（超时 {DISCOVER_TIMEOUT}s）...", file=sys.stderr)
    try:
        data, addr = sock.recvfrom(1500)
    except socket.timeout:
        print("错误：未发现 X-Plane。", file=sys.stderr)
        sock.close()
        sys.exit(1)
    finally:
        sock.close()

    if data[:5] != b"BECN\x00":
        print(f"错误：收到未知信标 {data[:5]!r}", file=sys.stderr)
        sys.exit(1)

    _, main_ver, minor_ver, _, _, _, _port = struct.unpack_from("=5sBBiiIH", data)
    sender_ip = addr[0]
    print(f"发现 X-Plane v{main_ver}.{minor_ver} @ {sender_ip}:{_port}", file=sys.stderr)
    return sender_ip, _port


def send_rref(addr, dataref, index=0, freq=1):
    packet = struct.pack("=5sii", b"RREF\x00", freq, index)
    packet += dataref.encode() + b"\x00"
    packet = packet.ljust(413, b"\x00")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(RESPONSE_TIMEOUT)
    try:
        sock.sendto(packet, addr)
        while True:
            try:
                data, _ = sock.recvfrom(1500)
            except ConnectionResetError:
                continue
            if len(data) < 12 or data[:4] != b"RREF":
                continue
            remaining = len(data) - 5
            if remaining % 8 != 0:
                continue
            for i in range(remaining // 8):
                off = 5 + i * 8
                ridx, rval = struct.unpack_from("=if", data, off)
                if ridx == index:
                    return rval
    except socket.timeout:
        return None
    finally:
        sock.close()


def request_data_row(addr, row):
    """请求 DATA 输出行。返回最新 DATA 包原始字节。"""
    packet = struct.pack("=5sii", b"DATA\x00", 0, 1)
    packet += struct.pack("=i", row)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(DATA_TIMEOUT)
    try:
        sock.sendto(packet, addr)
        last_data = None
        deadline = time.time() + DATA_TIMEOUT
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(1500)
            except socket.timeout:
                break
            except ConnectionResetError:
                continue
            if data[:5] == b"DATA\x00":
                last_data = data
        return last_data
    finally:
        sock.close()


def parse_data_row3(data):
    """解析 DATA 行 3：8 个 float32 [COM1, COM1_stdby, COM2, COM2_stdby, ...] (Hz)。"""
    if len(data) < 5 + 4 + 8 * 4:
        return None
    row_num = struct.unpack_from("=i", data, 5)[0]
    if row_num != 3:
        return None
    return struct.unpack_from("=8f", data, 9)


# 探测候选 dataref 列表: (名称, 除数, 说明)
PROBE_LIST = [
    ("sim/cockpit/radios/com1_freq_hz",                      100.0,       "默认 0.01 MHz"),
    ("sim/cockpit2/radios/actuators/com1_frequency_Mhz",     1.0,         "Mhz 直接 MHz"),
    ("sim/cockpit2/radios/actuators/com1_frequency_hz",      1_000_000.0, "Hz 全拼"),
    ("sim/cockpit2/radios/actuators/com1_frequency_hz_833",  1000.0,      "Hz_833 /1000=MHz → 3位小数✓"),
    ("sim/cockpit2/radios/actuators/com1_frequency_khz",     1_000.0,     "kHz"),
    ("sim/cockpit2/radios/actuators/com1_left_frequency_hz",  1_000_000.0, "left Hz"),
    ("sim/cockpit2/radios/actuators/com1_left_frequency_hz_833", 1000.0,  "left Hz_833 /1000=MHz → 3位小数✓"),
]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--data", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--833", action="store_true", dest="hz833", help="使用 _833 dataref（0.001 MHz 精度）")
    args = parser.parse_args()

    ip, port = discover_xplane()

    if args.probe:
        print(f"\n{'Dataref':55s} {'raw':>15s}  {'MHz':>12s}", file=sys.stderr)
        print("-" * 85, file=sys.stderr)
        for dr, div, label in PROBE_LIST:
            for addr in [(ip, port), ("127.0.0.1", 49000)]:
                raw = send_rref(addr, dr, index=0)
                if raw is not None:
                    mhz = raw / div
                    print(f"{dr:55s} {raw!r:>15s}  {mhz:>12.6f}")
                    break
            else:
                print(f"{dr:55s} {'<超时>':>15s}")
        return

    if args.data:
        for addr in [(ip, port), ("127.0.0.1", 49000)]:
            data = request_data_row(addr, 3)
            if data is None:
                continue
            values = parse_data_row3(data)
            if values is None:
                continue
            hz = values[0]
            if hz <= 0:
                continue
            if args.raw:
                print(f"raw_Hz={hz:.0f} /1e6={hz/1_000_000:.6f} MHz")
            else:
                print(f"{hz / 1_000_000:.3f}")
            return
        print("错误：DATA 行 3 无响应。请确保 X-Plane 设置中勾选了行 3。",
              file=sys.stderr)
        sys.exit(1)

    if args.hz833:
        # 使用 _833 dataref: raw/1000 = MHz（0.001 MHz 精度！）
        for addr in [(ip, port), ("127.0.0.1", 49000)]:
            raw = send_rref(addr, "sim/cockpit2/radios/actuators/com1_frequency_hz_833", index=0)
            if raw is not None:
                mhz = raw / 1000.0
                if args.raw:
                    print(f"raw={raw!r} /1000={mhz:.6f} MHz")
                else:
                    print(f"{mhz:.3f}")
                return

    # RREF 默认模式 (0.01 MHz)
    for addr in [(ip, port), ("127.0.0.1", 49000)]:
        raw = send_rref(addr, "sim/cockpit/radios/com1_freq_hz", index=0)
        if raw is not None:
            if args.raw:
                print(f"raw={raw!r} /100={raw/100.0:.6f} MHz")
            else:
                print(f"{raw/100.0:.2f}")
            return
    print("错误：无法获取 COM1 频率。", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
