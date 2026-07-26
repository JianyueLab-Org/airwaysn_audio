import pymumble_py3 as pymumble
import pyaudio
import threading
import time
import numpy as np
from contextlib import contextmanager
from pymumble_py3.errors import ConnectionRejectedError

server = "hjdczy.top"

# ---------- 音频采样率候选列表 ----------
SUPPORTED_SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]

class AudioStreamError(Exception):
    pass

class ATCRadioClient:
    def __init__(self, server, user, password, frequency):
        self.frequency = frequency
        # 处理ATIS用户名格式
        if '_atis' in user:
            # 获取真实用户名和频率
            base_user = user.split('_atis')[0]
            freq_value = int(round(float(frequency) * 1000))
            # 构建ATIS格式用户名
            user = f"{base_user}_atis{str(freq_value).zfill(6)}"
        
        self.mumble = pymumble.Mumble(server, user, password=password, reconnect=True)
        # 将 pymumble 内部 ping 间隔从默认 10 秒改为 1 秒
        pymumble.mumble.PYMUMBLE_PING_DELAY = 1
        # 重连间隔从默认 10 秒改为 2 秒
        pymumble.mumble.PYMUMBLE_CONNECTION_RETRY_INTERVAL = 2
        self.mumble.set_receive_sound(True)  # 启用音频接收
        self.mumble.callbacks.set_callback(pymumble.constants.PYMUMBLE_CLBK_SOUNDRECEIVED, self.sound_received)  # 设置音频接收回调
        self.mumble.callbacks.set_callback("connected", self.on_connected)
        self.mumble.callbacks.set_callback(pymumble.constants.PYMUMBLE_CLBK_DISCONNECTED, self.on_disconnected)
        self.connected = False  # 初始化连接状态
        self.last_connected = False
        self.on_connection_change = None
        self.audio = pyaudio.PyAudio()
        self.input_stream = None
        self.output_stream = None
        self.speaking = False
        self.is_receiving = False
        self.on_ptt_change = None
        self.on_rx_change = None
        self._last_rx_time = 0
        self.current_channel = None  # 添加初始化
        self.CHUNK = 960  # 20ms @ 48000Hz
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = self._find_best_sample_rate()
        self.CHUNK = int(self.RATE * 0.02)
        self.mic_volume = 1.0
        self.speaker_volume = 1.0
        self._stream_lock = threading.Lock()
        self._running = True
        self._last_ping_rcv = time.time()

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

        for rate in candidates:
            if _test_rate(rate, None, True) and _test_rate(rate, None, False):
                print(f"[Audio] 使用采样率: {rate} Hz")
                return rate

        print(f"[Audio] 所有候选采样率均失败，使用 48000 Hz")
        return 48000

    @contextmanager
    def _safe_audio_stream(self, stream):
        """安全地处理音频流操作"""
        if stream is None or not stream.is_active():
            raise AudioStreamError("音频流未激活")
        try:
            with self._stream_lock:
                yield stream
        except Exception as e:
            print(f"音频流操作错误: {e}")
            self._try_restart_audio()
            raise

    def _try_restart_audio(self):
        """尝试重新启动音频设备"""
        try:
            print("尝试重新启动音频设备...")
            self.setup_audio()
        except Exception as e:
            print(f"重新启动音频设备失败: {e}")

    def start(self):
        try:
            self.mumble.start()
            # 等待连接完成或出现错误
            timeout = 10  # 10秒超时
            start_time = time.time()
            while not self.connected and time.time() - start_time < timeout:
                if hasattr(self.mumble, '_thread') and not self.mumble._thread.is_alive():
                    # 如果线程已经死掉，说明可能出现了错误
                    raise ConnectionRejectedError("连接被拒绝")
                time.sleep(0.1)
            
            if not self.connected:
                raise Exception("连接超时，可能是用户名或密码错误")
                
            # 同步连接监控的初始状态，避免首次循环时重复触发回调
            self.last_connected = self.connected

            self.setup_audio()
            # 启动 RX 状态监控线程
            self._rx_thread = threading.Thread(target=self._rx_monitor, daemon=True)
            self._rx_thread.start()
            # 启动连接状态监控线程
            self._monitor_thread = threading.Thread(target=self._connection_monitor, daemon=True)
            self._monitor_thread.start()
        except ConnectionRejectedError as e:
            self.stop()
            raise ConnectionRejectedError(str(e))
        except Exception as e:
            self.stop()
            raise Exception(f"连接失败: {str(e)}")

    def _rx_monitor(self):
        """监控 RX 接收状态，超时后关闭指示灯。"""
        while self._running and (self.connected or self.speaking):
            try:
                if self.is_receiving and time.time() - self._last_rx_time > 0.5:
                    self.is_receiving = False
                    if self.on_rx_change:
                        self.on_rx_change(False)
            except Exception:
                pass
            time.sleep(0.1)

    def on_connected(self):
        """当连接成功时被调用"""
        self.connected = True
        freq_value = int(round(float(self.frequency) * 1000))
        channel_name = f"FREQ_{str(freq_value).zfill(6)}"

        try:
            channel = self.mumble.channels.find_by_name(channel_name)
        except pymumble.errors.UnknownChannelError:
            # 创建新频道
            self.mumble.channels.new_channel(0, channel_name, temporary=True)
            time.sleep(0.1)  # 给服务器一点时间来创建频道
            try:
                channel = self.mumble.channels.find_by_name(channel_name)
            except:
                print(f"无法创建或找到频道: {channel_name}")
                return

        if channel:
            if not hasattr(self, 'current_channel') or self.current_channel != channel["channel_id"]:
                self.mumble.users.myself.move_in(channel["channel_id"])
                self.current_channel = channel["channel_id"]

        # 通知UI连接状态
        if self.on_connection_change:
            self.on_connection_change(True)

    def on_disconnected(self):
        """当连接断开时被调用"""
        self.connected = False
        print("[ATCRadioClient] 连接已断开")
        if self.on_connection_change:
            self.on_connection_change(False)

    def _sync_ping_heartbeat(self):
        """从 pymumble 内部 ping_stats 同步心跳"""
        try:
            last_rcv = self.mumble.ping_stats.get('last_rcv', 0)
            if last_rcv > 0:
                self._last_ping_rcv = last_rcv / 1000.0
        except Exception:
            pass

    def _connection_monitor(self):
        """监控连接状态，检测断线"""
        PING_TIMEOUT = 5  # 超过5秒无ping回复认为断线
        tick = 0
        while self._running:
            try:
                tick += 1
                self._sync_ping_heartbeat()

                mumble_connected = bool(self.mumble.connected)
                ping_ago = time.time() - self._last_ping_rcv
                ping_timeout = ping_ago > PING_TIMEOUT

                # ping超时且mumble还标记为已连接 → 认为断线
                if mumble_connected and ping_timeout:
                    mumble_connected = False
                    print(f"[连接监控 T{tick}] ping超时({ping_ago:.1f}s)，标记断线")

                # 连接状态变化时通知
                if mumble_connected != self.last_connected:
                    self.last_connected = mumble_connected
                    self.connected = mumble_connected
                    print(f"[连接监控 T{tick}] 连接状态变化: {not mumble_connected} -> {mumble_connected}")
                    if self.on_connection_change:
                        self.on_connection_change(mumble_connected)

            except Exception as e:
                if self._running:
                    print(f"[连接监控] 错误: {e}")
            time.sleep(1)

    def setup_audio(self, input_device=None, output_device=None):
        """设置音频设备"""
        with self._stream_lock:
            if self.input_stream:
                self.input_stream.stop_stream()
                self.input_stream.close()
            if self.output_stream:
                self.output_stream.stop_stream()
                self.output_stream.close()

            # 重新检测采样率（设备可能已更改）
            self.RATE = self._find_best_sample_rate()
            self.CHUNK = int(self.RATE * 0.02)
            print(f"[Audio] 重新初始化使用采样率: {self.RATE} Hz, CHUNK: {self.CHUNK}")

            try:
                self.input_stream = self.audio.open(
                    input=True,
                    input_device_index=input_device,
                    format=self.FORMAT,
                    channels=self.CHANNELS,
                    rate=self.RATE,
                    frames_per_buffer=self.CHUNK
                )

                self.output_stream = self.audio.open(
                    output=True,
                    output_device_index=output_device,
                    format=self.FORMAT,
                    channels=self.CHANNELS,
                    rate=self.RATE,
                    frames_per_buffer=self.CHUNK
                )
            except Exception as e:
                print(f"设置音频设备失败: {e}")
                raise

    def start_speaking(self):
        if not self.speaking:
            self.speaking = True
            if self.on_ptt_change:
                self.on_ptt_change(True)
            threading.Thread(target=self._audio_thread).start()

    def stop_speaking(self):
        self.speaking = False
        if self.on_ptt_change:
            self.on_ptt_change(False)

    def set_mic_volume(self, volume_percent):
        """设置麦克风音量 (0-200)"""
        self.mic_volume = max(0.0, min(2.0, volume_percent / 100.0))
        print(f"麦克风音量已设置为: {volume_percent}%")

    def set_speaker_volume(self, volume_percent):
        """设置扬声器音量 (0-200)"""
        self.speaker_volume = max(0.0, min(2.0, volume_percent / 100.0))
        print(f"扬声器音量已设置为: {volume_percent}%")

    def _audio_thread(self):
        while self.speaking:
            try:
                with self._safe_audio_stream(self.input_stream) as stream:
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                    if data:
                        # 使用numpy处理音频数据
                        audio_data = np.frombuffer(data, dtype=np.int16)
                        # 应用音量调节（添加限幅以防止溢出）
                        scaled_data = audio_data * self.mic_volume 
                        audio_data = np.clip(scaled_data, np.iinfo(np.int16).min, np.iinfo(np.int16).max).astype(np.int16)
                        if not self.connected:
                            continue
                        self.mumble.sound_output.add_sound(audio_data.tobytes())
            except AudioStreamError:
                time.sleep(0.1)  # 音频流错误时短暂等待
                continue
            except Exception as e:
                print(f"录音错误: {e}")
                time.sleep(0.1)
            time.sleep(0.001)  # 防止CPU过载

    def sound_received(self, user, soundchunk):
        """处理接收到的音频"""
        if not self.mumble.users.myself:
            return
        if user["name"] == self.mumble.users.myself["name"]:
            return  # 不处理自己的声音
            
        if not soundchunk or not hasattr(soundchunk, 'pcm') or soundchunk.pcm is None:
            return  # 忽略无效的音频数据

        # 标记正在接收，触发 RX 指示灯
        self._last_rx_time = time.time()
        if not self.is_receiving:
            self.is_receiving = True
            if self.on_rx_change:
                self.on_rx_change(True)

        try:
            with self._safe_audio_stream(self.output_stream) as stream:
                # 使用numpy处理接收到的音频
                audio_data = np.frombuffer(soundchunk.pcm, dtype=np.int16)
                if len(audio_data) == 0:
                    return  # 忽略空的音频数据
                    
                # 应用音量调节（添加限幅以防止溢出）
                scaled_data = audio_data * self.speaker_volume
                audio_data = np.clip(scaled_data, np.iinfo(np.int16).min, np.iinfo(np.int16).max).astype(np.int16)
                stream.write(audio_data.tobytes())
        except AudioStreamError:
            pass  # 忽略音频流错误，等待下一个音频块
        except Exception as e:
            print(f"处理接收音频时出错: {e}")

    def stop(self):
        self._running = False
        self.speaking = False
        self.connected = False
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
        if self.output_stream:
            self.output_stream.stop_stream()
            self.output_stream.close()
        self.audio.terminate()
        self.mumble.stop()

