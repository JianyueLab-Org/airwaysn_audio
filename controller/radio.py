import pymumble_py3 as pymumble
import pyaudio
import threading
import time
import numpy as np
from contextlib import contextmanager
from pymumble_py3.errors import ConnectionRejectedError

server = "hjdczy.top"

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
        self.mumble.set_receive_sound(True)  # 启用音频接收
        self.mumble.callbacks.set_callback(pymumble.constants.PYMUMBLE_CLBK_SOUNDRECEIVED, self.sound_received)  # 设置音频接收回调
        self.mumble.callbacks.set_callback("connected", self.on_connected)
        self.connected = False  # 初始化连接状态
        self.audio = pyaudio.PyAudio()
        self.input_stream = None
        self.output_stream = None
        self.speaking = False
        self.current_channel = None  # 添加初始化
        self.CHUNK = 960  # 20ms @ 48000Hz
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 48000
        self.mic_volume = 1.0
        self.speaker_volume = 1.0
        self._stream_lock = threading.Lock()

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
                
            self.setup_audio()
        except ConnectionRejectedError as e:
            self.stop()
            raise ConnectionRejectedError(str(e))
        except Exception as e:
            self.stop()
            raise Exception(f"连接失败: {str(e)}")

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

    def setup_audio(self, input_device=None, output_device=None):
        """设置音频设备"""
        with self._stream_lock:
            if self.input_stream:
                self.input_stream.stop_stream()
                self.input_stream.close()
            if self.output_stream:
                self.output_stream.stop_stream()
                self.output_stream.close()

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
            threading.Thread(target=self._audio_thread).start()

    def stop_speaking(self):
        self.speaking = False

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
        if user["name"] == self.mumble.users.myself["name"]:
            return  # 不处理自己的声音
            
        if not soundchunk or not hasattr(soundchunk, 'pcm') or soundchunk.pcm is None:
            return  # 忽略无效的音频数据

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
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
        if self.output_stream:
            self.output_stream.stop_stream()
            self.output_stream.close()
        self.audio.terminate()
        self.mumble.stop()

