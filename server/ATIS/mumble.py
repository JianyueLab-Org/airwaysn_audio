import request
import process
import pymumble_py3 as pymumble
import threading
import numpy as np
import time
import os
import wave
import subprocess
import io
import asyncio
import edge_tts
import re

class ATISBroadcaster(threading.Thread):
    def __init__(self, atis_id, frequency, atis_text):
        super().__init__()
        freq_value = int(round(float(frequency) * 1000))
        channel_name = f"FREQ_{str(freq_value).zfill(6)}"
        self.channel_name = channel_name
        self.user = f"900_atis{str(freq_value).zfill(6)}"
        self.password = "p@ssw0rd"
        self.running = False
        self.mumble = None
        
        # 处理ATIS文本
        print (f"原始ATIS文本: {atis_text}")
        if '|' in atis_text:
            self.english_text, self.chinese_text = [part.strip() for part in atis_text.split('|')]
            self.english_text = process.process_single_atis_text(self.english_text, is_chinese=False)
            self.chinese_text = process.process_single_atis_text(self.chinese_text, is_chinese=True)
        else:
            self.english_text = process.process_single_atis_text(atis_text.strip(), is_chinese=False)
            self.chinese_text = None
        print (f"处理后的ATIS文本: {self.english_text} ;; {self.chinese_text}")


        self.silence_duration = 1.0
        self.last_sound_time = time.time()
        self.chunk_size = 2000  # 20ms @ 48000Hz
        self.loop = asyncio.new_event_loop()

    def connect_to_server(self):
        try:
            # 如果频道是 FREQ_199998，跳过创建
            if self.channel_name == "FREQ_199998":
                print("跳过创建频道 FREQ_199998")
                return False
                
            self.mumble = pymumble.Mumble("hjdczy.top", self.user, password=self.password, reconnect=True)
            self.mumble.set_receive_sound(True)
            self.mumble.start()
            time.sleep(1)  # 等待连接建立
            
            try:
                channel = self.mumble.channels.find_by_name(self.channel_name)
            except pymumble.errors.UnknownChannelError:
                self.mumble.channels.new_channel(0, self.channel_name, temporary=True)
                time.sleep(0.1)
                channel = self.mumble.channels.find_by_name(self.channel_name)
            
            if channel:
                self.mumble.users.myself.move_in(channel["channel_id"])
                return True
        except Exception as e:
            print(f"连接错误: {e}")
            return False

    def check_channel_silence(self):
        """检查频道是否处于静音状态"""
        current_time = time.time()
        for user in self.mumble.users.values():
            if user["name"] != self.mumble.users.myself["name"]:
                sound = user.sound
                if sound and sound.is_sound():
                    self.last_sound_time = current_time
                    return False
        return (current_time - self.last_sound_time) >= self.silence_duration

    async def _text_to_audio_edge(self, text, voice="en-US-ChristopherNeural"):
        """使用edge-tts将文本转换为音频"""
        # 打印转换的文本内容
        # print (f"正在转换ATIS: {text.strip()}")
        communicate = edge_tts.Communicate(text, voice)
        temp_file = "temp_edge.mp3"
        try:
            await communicate.save(temp_file)
            
            # 使用ffmpeg将MP3转换为WAV格式
            subprocess.run([
                'ffmpeg', '-y',
                '-i', temp_file,
                '-ar', '48000',
                '-ac', '1',
                '-acodec', 'pcm_s16le',
                'temp_processed.wav'
            ], capture_output=True)
            
            with wave.open('temp_processed.wav', 'rb') as wf:
                audio_data = wf.readframes(wf.getnframes())
            
            return audio_data
        finally:
            # 清理临时文件
            if os.path.exists(temp_file):
                os.remove(temp_file)
            if os.path.exists('temp_processed.wav'):
                os.remove('temp_processed.wav')

    def text_to_audio(self, text):
        """将文本转换为音频数据"""
        print (f"正在转换ATIS: {text}")
        
        # 使用正则表达式检查是否包含中文字符
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', text))
        
        if has_chinese:  # 处理含中文的情况

            audio_data = bytearray()
            

            # 转换中文部分

            chinese_audio = self.loop.run_until_complete(
                self._text_to_audio_edge(text, "zh-CN-YunxiNeural"))
            audio_data.extend(chinese_audio)
            
            return bytes(audio_data)
        else:
            # 处理纯英文
            return self.loop.run_until_complete(
                self._text_to_audio_edge(text, "en-US-ChristopherNeural"))

    def broadcast_audio(self, audio_data):
        """广播音频数据"""
        if not audio_data:
            return

        position = 0
        total_size = len(audio_data)

        while position < total_size and self.running:
            if self.check_channel_silence():
                chunk_size = min(self.chunk_size, total_size - position)
                chunk = audio_data[position:position + chunk_size]
                if len(chunk) < chunk_size:
                    chunk += b'\x00' * (chunk_size - len(chunk))
                
                self.mumble.sound_output.add_sound(chunk)
                position += chunk_size
                time.sleep(0.02)
            else:
                time.sleep(0.5)

    def run(self):
        """线程主函数"""
        self.running = True
        if not self.connect_to_server():
            print(f"ATIS {self.user} 连接失败")
            return

        print(f"ATIS {self.user} 开始广播")
        self._broadcast_loop()

    def _broadcast_loop(self):
        """广播循环函数"""
        print("开始ATIS广播循环")
        while self.running:
            try:
                if self.check_channel_silence():
                    # 如果有中文ATIS，先播放中文
                    if self.chinese_text:
                        print("\n开始播放中文ATIS...")
                        chinese_audio = self.text_to_audio(self.chinese_text)
                        if chinese_audio:
                            self.broadcast_audio(chinese_audio)

                        if not self.running:
                            break

                        print("中文播放完成，等待检查频道状态...")

                    # 播放英文ATIS
                    if self.check_channel_silence() and self.running:
                        print("\n开始播放英文ATIS...")
                        english_audio = self.text_to_audio(self.english_text)
                        if english_audio:
                            self.broadcast_audio(english_audio)

                    print("本轮播放完成，等待下一轮...")
                    # time.sleep(30)  # 两轮播放之间的间隔
                else:
                    print("检测到频道有其他音频，等待...")
                    time.sleep(5)  # 检测到其他音频时的等待时间

            except Exception as e:
                print(f"ATIS播放循环错误: {str(e)}")
                time.sleep(5)  # 发生错误时的等待时间

            if not self.running:
                break

    def stop(self):
        """停止广播"""
        self.running = False
        if self.mumble:
            self.mumble.stop()

class ATISManager:
    def __init__(self):
        self.broadcasters = {}
        self.update_interval = 30
        self._stop_flag = False
        self.update_thread = None

    def start(self):
        """启动ATIS管理器"""
        self._stop_flag = False
        self.update_thread = threading.Thread(target=self._update_loop)
        self.update_thread.start()

    def stop(self):
        """停止所有ATIS广播"""
        self._stop_flag = True
        if self.update_thread:
            self.update_thread.join()
        for broadcaster in self.broadcasters.values():
            broadcaster.stop()
            broadcaster.join()
        self.broadcasters.clear()

    def _update_loop(self):
        """更新ATIS信息的循环"""
        while not self._stop_flag:
            try:
                data = request.get_airwaysn_data()
                if data and 'atis' in data:
                    current_atis = {atis['callsign']: atis for atis in data['atis']}
                    
                    # 停止不再活跃的ATIS
                    for callsign in list(self.broadcasters.keys()):
                        if callsign not in current_atis:
                            self.broadcasters[callsign].stop()
                            self.broadcasters[callsign].join()
                            del self.broadcasters[callsign]
                    
                    # 更新或启动新的ATIS
                    for callsign, atis in current_atis.items():
                        # 检查是否为199.998频率
                        if abs(float(atis.get('frequency', '0')) - 199.998) < 0.001:
                            print(f"跳过频率199.998的ATIS: {callsign}")
                            continue
                            
                        text = ' '.join(atis.get('text_atis', []))
                        if callsign not in self.broadcasters:
                            # 新的ATIS
                            broadcaster = ATISBroadcaster(
                                atis_id=callsign,
                                frequency=atis.get('frequency', '0'),
                                atis_text=text
                            )
                            broadcaster.start()
                            self.broadcasters[callsign] = broadcaster
                        else:
                            # 检查并更新现有ATIS的文本
                            current_broadcaster = self.broadcasters[callsign]
                            if text != current_broadcaster.english_text:  # 如果文本有变化
                                print(f"更新 {callsign} 的ATIS文本")
                                if '|' in text:
                                    english, chinese = [part.strip() for part in text.split('|')]
                                    current_broadcaster.english_text = process.process_single_atis_text(english, is_chinese=False)
                                    current_broadcaster.chinese_text = process.process_single_atis_text(chinese, is_chinese=True)
                                else:
                                    current_broadcaster.english_text = process.process_single_atis_text(text.strip(), is_chinese=False)
                                    current_broadcaster.chinese_text = None
            except Exception as e:
                print(f"更新ATIS信息时出错: {e}")
            
            # 等待下一次更新
            for _ in range(self.update_interval):
                if self._stop_flag:
                    break
                time.sleep(1)

if __name__ == "__main__":
    manager = ATISManager()
    try:
        manager.start()
        # 保持主程序运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止ATIS服务...")
        manager.stop()
        print("ATIS服务已停止")

