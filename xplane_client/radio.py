"""
radio.py — X-Plane 版本的 MumbleRadioClient

用 X-Plane UDP 协议替代 SimConnect 读取 COM1 频率。
其余功能（Mumble 连接、PTT、音频设备）与 SimConnect 版本一致。
"""

import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'

import socket
import struct
import sys
import pymumble_py3 as pymumble

import mumblecompat

# pymumble 建 TLS 用的 ssl.wrap_socket 在 Python 3.12 里已被删除，
# 不补上的话连接线程一起来就抛 AttributeError，界面只会显示成
# "连接被拒绝"，而实际上 TLS 握手根本没开始
mumblecompat.install()
import threading
import time
import keyboard
import pyaudio
import numpy as np
import functools
import pygame

# ---------- 配置 ----------
SERVER_HOST = "hjdczy.top"
USERNAME = ""
PASSWORD = ""
# ---------- 音频采样率候选列表 ----------
# 按优先级排序，自动检测设备支持的采样率
SUPPORTED_SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]
# ---------- X-Plane UDP 协议常量 ----------
MCAST_GRP = "239.255.1.1"
MCAST_PORT = 49707
DISCOVER_TIMEOUT = 10
RESPONSE_TIMEOUT = 3


def suppress_mumble_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            sys.stderr = open('nul', 'w')
            raise
    return wrapper


class XPlaneRadio:
    """负责通过 X-Plane UDP 协议读取 COM1 频率。"""

    def __init__(self):
        self._addr = None
        self._sock = None

    def discover(self):
        """通过多播信标发现 X-Plane，返回可用的数据地址列表。

        如果多播端口被占用（WinError 10013），自动回退到直接探测常见端口。
        """
        candidates = self._discover_multicast()
        if not candidates:
            print("[XPlane] 多播发现失败，回退到直接探测端口...", file=sys.stderr)
            candidates = self._discover_direct()
        return candidates

    def _discover_multicast(self):
        """通过多播信标发现 X-Plane。"""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", MCAST_PORT))
        except OSError as e:
            print(f"[XPlane] 多播端口 {MCAST_PORT} 绑定失败: {e}", file=sys.stderr)
            if sock:
                sock.close()
            return []

        mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(DISCOVER_TIMEOUT)

        print(f"[XPlane] 正在搜索 X-Plane（多播，超时 {DISCOVER_TIMEOUT}s）...", file=sys.stderr)

        try:
            data, addr = sock.recvfrom(1500)
        except socket.timeout:
            print(
                "[XPlane] 未发现 X-Plane。\n"
                "  请确认：\n"
                "  1. X-Plane 正在运行且已进入飞行\n"
                "  2. 设置 → Data Output → IPs for UDP network 中已添加本机 IP",
                file=sys.stderr,
            )
            sock.close()
            return []
        finally:
            sock.close()

        if data[:5] != b"BECN\x00":
            print(f"[XPlane] 收到未知信标 {data[:5]!r}", file=sys.stderr)
            return []

        _, main_ver, minor_ver, _, _, _, _port = struct.unpack_from("=5sBBiiIH", data)
        sender_ip = addr[0]

        print(f"[XPlane] 发现 X-Plane v{main_ver}.{minor_ver} @ {sender_ip}:{_port}", file=sys.stderr)

        candidates = set()
        for ip in {sender_ip, "127.0.0.1"}:
            for p in {_port, 49000}:
                candidates.add((ip, p))
        return sorted(candidates, key=lambda x: (x[1] != 49000 or x[0] != "127.0.0.1"))

    def _discover_direct(self):
        """直接探测常见的 X-Plane 数据输出端口，找到第一个即返回。"""
        candidates = []
        test_ports = [49000, 49001, 49002, 49003, 49004, 49005]
        for ip in ("127.0.0.1",):
            for port in test_ports:
                try:
                    freq = self.read_com1_freq((ip, port))
                    if freq is not None:
                        print(f"[XPlane] 直接探测成功 @ {ip}:{port}，频率 {freq:.3f} MHz", file=sys.stderr)
                        candidates.append((ip, port))
                        return candidates  # 找到即返回
                except Exception:
                    continue
        return candidates

    def _send_rref(self, addr, dataref, index=0, freq=1):
        """发送 RREF 请求，成功返回 (index, value)，失败返回 None。"""
        packet = struct.pack("=5sii", b"RREF\x00", freq, index)
        packet += dataref.encode() + b"\x00"
        packet = packet.ljust(413, b"\x00")

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
                if len(data) < 12 or data[:4] != b"RREF":
                    continue
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

    def read_com1_freq(self, addr):
        """读取 COM1 频率，返回 MHz 值；失败返回 None。

        使用 sim/cockpit2/radios/actuators/com1_frequency_hz_833，
        raw/1000 = MHz，精度 0.001 MHz（1 kHz），支持 8.33 kHz 步进。

        若该 dataref 不可用，回退到 sim/cockpit/radios/com1_freq_hz
        （精度 0.01 MHz）。
        """
        # 首选：_833 dataref（高精度，raw/1000 = MHz）
        result = self._send_rref(addr,
            "sim/cockpit2/radios/actuators/com1_frequency_hz_833", index=0)
        if result is not None:
            _, value = result
            if value > 0:
                return round(value / 1000.0, 3)

        # 回退：原始 dataref（0.01 MHz 精度）
        result = self._send_rref(addr,
            "sim/cockpit/radios/com1_freq_hz", index=0)
        if result is None:
            return None
        _, value = result
        return round(value / 100.0, 3)

    def find_and_read(self):
        """发现 X-Plane 并读取一次 COM1 频率。成功返回 (addr, freq_mhz)。"""
        candidates = self.discover()
        for addr in candidates:
            freq = self.read_com1_freq(addr)
            if freq is not None:
                self._addr = addr
                return (addr, freq)
        return (None, None)

    @property
    def addr(self):
        return self._addr


class MumbleRadioClient:
    """X-Plane 版本的 Mumble 无线电客户端。

    与 SimConnect 版本的区别仅在于：
    - 不依赖 SimConnect，改用 XPlaneRadio 读取频率
    - 频率读取通过 X-Plane UDP 协议完成
    """

    def __init__(self, server_host, username, password="", settings=None):
        # X-Plane 通信
        self.xplane = XPlaneRadio()

        # 音频配置
        self.CHUNK = 960
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.is_talking = False
        self.on_ptt_change = None
        self.is_receiving = False
        self.on_rx_change = None
        self.on_connection_change = None
        self._last_rx_time = 0

        # 设置
        if settings is not None:
            self.settings = settings
        else:
            from settings import Settings
            self.settings = Settings()
        try:
            self.settings.username = username or ""
            self.settings.password = password or ""
        except Exception as e:
            print(f"[DEBUG] 同步账号到设置失败: {e}")

        self.settings.mic_volume = max(0, min(200, self.settings.mic_volume))
        self.settings.speaker_volume = max(0, min(200, self.settings.speaker_volume))

        # 音频设备
        self.audio = pyaudio.PyAudio()
        self.RATE = self._find_best_sample_rate()
        # 根据实际采样率调整 CHUNK，保持约 20ms 缓冲区
        self.CHUNK = int(self.RATE * 0.02)
        print(f"[Audio] 使用采样率: {self.RATE} Hz, CHUNK: {self.CHUNK}")

        self.stream = self.audio.open(
            format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
            input=True, frames_per_buffer=self.CHUNK,
            input_device_index=self.settings.input_device_index,
        )
        self.output_stream = self.audio.open(
            format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
            output=True, frames_per_buffer=self.CHUNK,
            output_device_index=self.settings.output_device_index,
        )

        # Mumble 客户端
        self.mumble = pymumble.Mumble(
            server_host, username, password=password, reconnect=True,
        )
        self.mumble.set_receive_sound(True)
        self.mumble.callbacks.set_callback(
            pymumble.constants.PYMUMBLE_CLBK_SOUNDRECEIVED,
            self.handle_incoming_audio,
        )
        self.current_channel = None

        # ---- 独立连接状态标记 ----
        # 由 pymumble 的 connected/disconnected 回调管理，不依赖 mumble 内部线程状态
        self._connection_established = threading.Event()
        # 存储登录时发现的初始频率，用于首次连接后的频道切换
        self._initial_freq = None

        # 预初始化 connected 属性（pymumble 的 init_connection 在 start() 后才设置此属性）
        self.mumble.connected = 0

        self.update_volumes()

        # 将 pymumble 内部 ping 间隔从默认 10 秒改为 1 秒
        pymumble.mumble.PYMUMBLE_PING_DELAY = 1
        # 重连间隔从默认 10 秒改为 2 秒
        pymumble.mumble.PYMUMBLE_CONNECTION_RETRY_INTERVAL = 2

        # 心跳：记录最后一次收到 ping 回复的时间
        self._last_ping_rcv = time.time()

        # 线程管理
        self.monitor_thread = None
        self.voice_thread = None
        self.running = True

        # 摇杆
        self.pygame_lock = threading.Lock()
        self.pygame_initialized = False
        try:
            with self.pygame_lock:
                if not pygame.get_init():
                    pygame.init()
                if not pygame.display.get_init():
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    pygame.joystick.init()
                self.pygame_initialized = True
                print(f"[DEBUG] pygame初始化完成，检测到 {pygame.joystick.get_count()} 个摇杆")

                self.joystick = None
                if pygame.joystick.get_count() > 0:
                    self.joystick = pygame.joystick.Joystick(0)
                    self.joystick.init()
                    print(f"[DEBUG] 摇杆初始化完成: {self.joystick.get_name()}")
        except Exception as e:
            print(f"[DEBUG] 摇杆初始化失败: {e}")
            self.pygame_initialized = False
            self.joystick = None

    def _find_best_sample_rate(self):
        """自动检测音频设备支持的最佳采样率。"""
        candidates = list(SUPPORTED_SAMPLE_RATES)

        def _test_rate(rate, device_idx, is_input):
            try:
                test = self.audio.open(
                    format=self.FORMAT, channels=self.CHANNELS, rate=rate,
                    input=is_input, output=not is_input,
                    input_device_index=device_idx if is_input else None,
                    output_device_index=device_idx if not is_input else None,
                    frames_per_buffer=960,
                    start=False,
                )
                test.close()
                return True
            except Exception:
                return False

        input_idx = self.settings.input_device_index
        output_idx = self.settings.output_device_index

        for rate in candidates:
            input_ok = True
            output_ok = True
            if input_idx is not None:
                input_ok = _test_rate(rate, input_idx, True)
                if not input_ok:
                    input_ok = _test_rate(rate, None, True)
            if output_idx is not None:
                output_ok = _test_rate(rate, output_idx, False)
                if not output_ok:
                    output_ok = _test_rate(rate, None, False)
            if input_ok and output_ok:
                print(f"[Audio] 使用采样率: {rate} Hz")
                return rate

        print(f"[Audio] 所有候选采样率均失败，使用 48000 Hz")
        return 48000

    # ---------- 频率 / 频道 ----------
    @staticmethod
    def convert_frequency(frequency):
        return int(round(frequency * 1000))

    @staticmethod
    def get_channel_name(frequency):
        freq = MumbleRadioClient.convert_frequency(frequency)
        return f"FREQ_{str(freq).zfill(6)}"

    def set_connection_state(self, connected):
        """由外部（gui.py 回调）调用，设置独立连接标记。"""
        if connected:
            self._connection_established.set()
            print(f"[连接标记] 已设为 True | thread_alive={self.mumble.is_alive() if hasattr(self.mumble, 'is_alive') else 'N/A'}")
        else:
            self._connection_established.clear()
            print(f"[连接标记] 已设为 False")

    def switch_channel(self, frequency, caller="unknown"):
        """切换到指定频率对应的 Mumble 频道。

        参数:
            frequency: COM1 频率（MHz）
            caller:    调用来源名称（用于日志定位）
        """
        channel_name = self.get_channel_name(frequency)
        print(f"[频道切换] {caller}: 尝试切换到 {channel_name} (频率 {frequency:.3f} MHz)"
              f" | myself={'有' if self.mumble.users.myself else '无'}"
              f" | 线程存活={self.mumble.is_alive() if hasattr(self.mumble, 'is_alive') else 'N/A'}")
        try:
            channel = self.mumble.channels.find_by_name(channel_name)
            print(f"[频道切换] find_by_name 成功: channel_id={channel['channel_id']}")
        except pymumble.errors.UnknownChannelError:
            print(f"[频道切换] 频道 {channel_name} 不存在，尝试创建临时频道...")
            try:
                self.mumble.channels.new_channel(0, channel_name, temporary=True)
                print(f"[频道切换] 临时频道创建成功")
            except Exception as e:
                print(f"[频道切换] 创建临时频道失败: {type(e).__name__}: {e}")
                return
            try:
                channel = self.mumble.channels.find_by_name(channel_name)
                print(f"[频道切换] 创建后 find_by_name 成功: channel_id={channel['channel_id']}")
            except pymumble.errors.UnknownChannelError:
                print(f"[频道切换] 创建后仍找不到频道 {channel_name}，放弃切换")
                return

        if not channel:
            print(f"[频道切换] 频道对象为空，放弃切换")
            return

        try:
            if not self.mumble.users.myself:
                print(f"[频道切换] self.mumble.users.myself 为 None，无法获取当前频道，跳过切换")
                return
            current_id = self.mumble.users.myself["channel_id"]
            if current_id == channel["channel_id"]:
                print(f"[频道切换] 已在目标频道 {channel_name} 中，无需切换")
                return
            print(f"[频道切换] 当前频道 ID={current_id}，目标频道 ID={channel['channel_id']}，开始 move_in...")
            self.mumble.users.myself.move_in(channel["channel_id"])
            self.current_channel = channel["channel_id"]
            print(f"[频道切换] ✅ 成功切换到 {channel_name} (频率 {frequency:.3f} MHz)")
        except Exception as e:
            print(f"[频道切换] ❌ move_in 失败: {type(e).__name__}: {e}")

    def _sync_ping_heartbeat(self):
        """读取 pymumble 内部 ping_stats['last_rcv'] 刷新心跳。"""
        try:
            last_rcv = self.mumble.ping_stats.get('last_rcv', 0)
            if last_rcv > 0:
                self._last_ping_rcv = last_rcv / 1000.0
        except Exception:
            pass

    def _ensure_in_correct_channel(self, frequency):
        """确保用户在正确的频道中，如果不在则重新加入"""
        try:
            if not self._connection_established.is_set():
                print(f"[频道检查] 未连接，跳过")
                return
            if not self.mumble.users.myself:
                print(f"[频道检查] myself 尚未就绪，跳过")
                return
            channel_name = self.get_channel_name(frequency)
            current_channel_id = self.mumble.users.myself["channel_id"]
            print(f"[频道检查] 当前channel_id={current_channel_id!r}, 目标={channel_name}")

            # channel_id=0 => Root 频道，必须重新加入
            if current_channel_id == 0:
                print(f"[频道检查] ⚠️ 用户处于 Root 频道! 重新加入 {channel_name}")
                self.switch_channel(frequency, caller="频道检查-Root")
                return

            if current_channel_id is None:
                print(f"[频道检查] 未加入任何频道，重新加入 {channel_name}")
                self.switch_channel(frequency, caller="频道检查-无频道")
                return

            try:
                current_channel = self.mumble.channels[current_channel_id]
                current_name = current_channel["name"]
                if current_name != channel_name:
                    print(f"[频道检查] 频道不匹配 ({current_name} -> {channel_name})，重新加入")
                    self.switch_channel(frequency, caller="频道检查-不匹配")
                else:
                    print(f"[频道检查] ✅ 频道匹配 ({current_name})")
            except (KeyError, Exception) as e:
                print(f"[频道检查] 频道 ID {current_channel_id} 无效 ({e})，重新加入 {channel_name}")
                self.switch_channel(frequency, caller="频道检查-ID无效")
        except Exception as e:
            import traceback
            print(f"[频道检查] 错误: {e}")
            traceback.print_exc()

    def monitor_frequency(self):
        """监控 COM1 频率变化和连接/频道状态"""
        last_frequency = None
        last_connected = False
        tick = 0
        freq_read_fail_count = 0
        channel_switch_attempted = False  # 是否已尝试过首次频道切换
        while self.running:
            try:
                tick += 1
                self._sync_ping_heartbeat()

                # ★ 使用独立连接标记作为主要判断，不依赖 mumble.connected 或 is_alive()
                mumble_connected = self._connection_established.is_set()

                # 诊断信息
                pymumble_connected_raw = bool(self.mumble.connected)
                thread_alive = self.mumble.is_alive() if hasattr(self.mumble, 'is_alive') else True
                ping_ago = time.time() - self._last_ping_rcv
                ping_timeout = ping_ago > 3

                myself = self.mumble.users.myself
                my_channel_id = myself["channel_id"] if myself else None
                chan_name = ""
                if my_channel_id is not None:
                    try:
                        chan_name = self.mumble.channels[my_channel_id]["name"]
                    except:
                        chan_name = "<无效ID>"

                print(f"[监控 T{tick}] 连接标记={mumble_connected}"
                      f" | pymumble.connected={pymumble_connected_raw}"
                      f" | thread_alive={thread_alive}"
                      f" | channel_id={my_channel_id}"
                      f" | channel_name={chan_name!r}"
                      f" | ping_ago={ping_ago:.1f}s"
                      f"{' ⚠️线程死亡' if not thread_alive else ''}"
                      f"{' ⚠️超时' if ping_timeout else ''}")

                # ★ 不再根据 thread_alive 主动标记断连！仅记录日志供诊断
                # 连接状态变化由回调管理，通知 UI
                if mumble_connected != last_connected:
                    last_connected = mumble_connected
                    print(f"[监控 T{tick}] 连接状态变化: {not mumble_connected} -> {mumble_connected}")
                    if self.on_connection_change:
                        self.on_connection_change(mumble_connected)

                if mumble_connected:
                    freq = self.xplane.read_com1_freq(self.xplane.addr)
                    if freq is not None:
                        freq_read_fail_count = 0
                        if freq != last_frequency:
                            print(f"[监控 T{tick}] 频率变化: {last_frequency} -> {freq}")
                            self.switch_channel(freq, caller=f"监控-T{tick}-变")
                            last_frequency = freq
                        else:
                            self._ensure_in_correct_channel(freq)
                    else:
                        freq_read_fail_count += 1
                        print(f"[监控 T{tick}] ⚠️ read_com1_freq 返回 None (连续{freq_read_fail_count}次)")
                        # 即使读不到频率，也用 last_frequency 检查频道
                        if last_frequency is not None:
                            print(f"[监控 T{tick}] 用 last_frequency={last_frequency} 做频道检查")
                            self._ensure_in_correct_channel(last_frequency)
                        else:
                            print(f"[监控 T{tick}] 无 last_frequency 可用，跳过频道检查")

                    # 首次连接后确保至少尝试过一次频道切换
                    if not channel_switch_attempted and last_frequency is not None:
                        channel_switch_attempted = True
                        print(f"[监控 T{tick}] 首次连接后的频道切换已完成")
                else:
                    print(f"[监控 T{tick}] 未连接，跳过频率读取")

            except Exception as e:
                import traceback
                if self.running:
                    print(f"[监控 T{tick}] 错误: {e}")
                    traceback.print_exc()
            time.sleep(1)

    # ---------- 音量 ----------
    def update_volumes(self):
        try:
            if self.mumble and self.mumble.connected:
                self.mumble.sound_output.volume = self.settings.mic_volume / 100.0
            if hasattr(self, 'output_stream'):
                volume_scale = self.settings.speaker_volume / 100.0
                def volume_modifier(audio_data):
                    return (np.frombuffer(audio_data, dtype=np.int16) * volume_scale).astype(np.int16).tobytes()
                self.audio_processor = volume_modifier
        except Exception as e:
            print(f"更新音量设置时出错: {e}")

    # ---------- pygame / 摇杆 ----------
    def ensure_pygame_initialized(self):
        with self.pygame_lock:
            try:
                if not pygame.get_init():
                    pygame.init()
                if not pygame.display.get_init():
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    pygame.joystick.init()
                if pygame.joystick.get_count() > 0:
                    if not self.joystick or not self.joystick.get_init():
                        self.joystick = pygame.joystick.Joystick(0)
                        self.joystick.init()
                return True
            except Exception as e:
                print(f"[DEBUG] pygame重新初始化失败: {e}")
                return False

    def reinitialize_joystick(self):
        print("[DEBUG] 尝试重新初始化摇杆")
        with self.pygame_lock:
            try:
                if self.joystick:
                    try:
                        self.joystick.quit()
                    except Exception:
                        pass
                    self.joystick = None
                if not pygame.get_init():
                    pygame.init()
                if not pygame.display.get_init():
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    pygame.joystick.init()
                if pygame.joystick.get_count() > 0:
                    self.joystick = pygame.joystick.Joystick(0)
                    self.joystick.init()
                    print(f"[DEBUG] 摇杆重新初始化成功: {self.joystick.get_name()}")
                    return True
            except Exception as e:
                print(f"[DEBUG] 摇杆重新初始化失败: {e}")
                return False

    # ---------- 音频处理 ----------
    def handle_voice(self):
        print("[DEBUG] 开始语音处理线程")
        self.ensure_pygame_initialized()
        last_ptt_state = False

        while self.running:
            try:
                # RX 超时检查：超过 0.5 秒无接收则灭灯
                if self.is_receiving and time.time() - self._last_rx_time > 0.5:
                    self.is_receiving = False
                    if self.on_rx_change:
                        self.on_rx_change(False)

                keyboard_ptt = keyboard.is_pressed(self.settings.ptt_key)
                joystick_ptt = False

                if self.settings.joystick_ptt is not None:
                    try:
                        with self.pygame_lock:
                            if not pygame.get_init() or not pygame.joystick.get_init():
                                self.ensure_pygame_initialized()
                            pygame.event.pump()
                            if (self.joystick and self.joystick.get_init()
                                    and self.settings.joystick_ptt < self.joystick.get_numbuttons()):
                                joystick_ptt = self.joystick.get_button(self.settings.joystick_ptt)
                    except Exception as e:
                        print(f"[DEBUG] 摇杆读取错误: {e}")

                is_speaking = keyboard_ptt or joystick_ptt

                if is_speaking != last_ptt_state:
                    last_ptt_state = is_speaking
                    self.is_talking = is_speaking
                    if self.on_ptt_change:
                        self.on_ptt_change(self.is_talking)

                if self.is_talking:
                    # 下面这些 continue 会跳过循环末尾的 sleep：按住 PTT 而连接
                    # 还没就绪时，整个循环满速空转烧掉一个核，所以每处都要等一下
                    if not self.stream or not self.mumble:
                        time.sleep(0.05)
                        continue
                    try:
                        # ★ 先检查独立连接标记（比 mumble.connected 更可靠）
                        if not self._connection_established.is_set():
                            print("[DEBUG] 连接标记已清除，跳过音频发送")
                            time.sleep(0.05)
                            continue
                        if not self.mumble.connected > 0:
                            time.sleep(0.05)
                            continue
                        if not self.mumble.channels:
                            time.sleep(0.05)
                            continue
                        if not self.mumble.users.myself or not self.mumble.users.myself["channel_id"]:
                            time.sleep(0.05)
                            continue

                        data = self._safe_stream_read(self.CHUNK)
                        if data:
                            audio_data = np.frombuffer(data, dtype=np.int16)
                            audio_data = (audio_data * (self.settings.mic_volume / 100.0)).astype(np.int16)
                            self.mumble.sound_output.add_sound(audio_data.tobytes())
                    except Exception as e:
                        print(f"[DEBUG] 音频处理错误: {e}")

                time.sleep(0.01)
            except Exception as e:
                print(f"[DEBUG] 语音处理错误: {e}")
                time.sleep(0.1)

    def handle_incoming_audio(self, user, soundchunk):
        if not self.mumble.users.myself:
            return
        if user["name"] != self.mumble.users.myself["name"]:
            try:
                # 标记正在接收
                self._last_rx_time = time.time()
                if not self.is_receiving:
                    self.is_receiving = True
                    if self.on_rx_change:
                        self.on_rx_change(True)

                audio_data = np.frombuffer(soundchunk.pcm, dtype=np.int16)
                audio_data = (audio_data * (self.settings.speaker_volume / 100.0)).astype(np.int16)
                self._safe_stream_write(audio_data.tobytes())
            except Exception as e:
                print(f"音频输出错误: {e}")

    def _safe_stream_read(self, chunk):
        """线程安全地读取音频流，处理流被外部关闭的竞态。"""
        try:
            return self.stream.read(chunk, exception_on_overflow=False)
        except (OSError, IOError, Exception) as e:
            print(f"[DEBUG] 音频流读取错误（可能已被重初始化）: {e}")
            return None

    def _safe_stream_write(self, data):
        """线程安全地写入音频流，处理流被外部关闭的竞态。"""
        try:
            self.output_stream.write(data)
        except (OSError, IOError, Exception) as e:
            print(f"[DEBUG] 音频流写入错误（可能已被重初始化）: {e}")

    def reinitialize_audio(self):
        try:
            if hasattr(self, 'stream') and self.stream:
                self.stream.stop_stream()
                self.stream.close()
            if hasattr(self, 'output_stream') and self.output_stream:
                self.output_stream.stop_stream()
                self.output_stream.close()

            # 重新检测采样率（设备可能已更改）
            self.RATE = self._find_best_sample_rate()
            self.CHUNK = int(self.RATE * 0.02)
            print(f"[Audio] 重新初始化使用采样率: {self.RATE} Hz, CHUNK: {self.CHUNK}")

            self.stream = self.audio.open(
                format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
                input=True, frames_per_buffer=self.CHUNK,
                input_device_index=self.settings.input_device_index,
            )
            self.output_stream = self.audio.open(
                format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
                output=True, frames_per_buffer=self.CHUNK,
                output_device_index=self.settings.output_device_index,
            )
            self.update_volumes()
            print("音频设备重新初始化完成")
        except Exception as e:
            print(f"重新初始化音频设备失败: {e}")
            raise

    # ---------- 清理 ----------
    def cleanup(self):
        self.running = False

        # 先把工作线程收掉，再动音频设备。反过来的话，语音线程可能正卡在
        # stream.read() 里，而 PyAudio 在别的线程读的时候被 terminate 是 C 层
        # 崩溃，Python 的 try/except 接不住。
        for thread in (getattr(self, 'monitor_thread', None),
                       getattr(self, 'voice_thread', None)):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2.0)

        try:
            if hasattr(self, 'stream') and self.stream:
                self.stream.stop_stream()
                self.stream.close()
            if hasattr(self, 'output_stream') and self.output_stream:
                self.output_stream.stop_stream()
                self.output_stream.close()
            if hasattr(self, 'audio') and self.audio:
                self.audio.terminate()
            if hasattr(self, 'mumble') and self.mumble:
                try:
                    self.mumble.connected = 0
                except Exception:
                    pass
                self.mumble.stop()
            if hasattr(self, 'joystick') and self.joystick:
                try:
                    self.joystick.quit()
                except Exception:
                    pass
            pygame.quit()
        except Exception as e:
            print(f"清理资源时出错: {e}")


if __name__ == "__main__":
    client = None
    try:
        client = MumbleRadioClient(SERVER_HOST, USERNAME, PASSWORD)
        client.run()
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"\n程序发生错误: {e}")
    finally:
        if client:
            client.cleanup()
        print("\n按回车键退出...")
        try:
            input()
        except Exception:
            time.sleep(5)
