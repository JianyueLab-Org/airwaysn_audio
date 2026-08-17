import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'

import logging
from SimConnect import *
import pymumble_py3 as pymumble
from pymumble_py3 import messages

import mumblecompat

# pymumble 建 TLS 用的 ssl.wrap_socket 在 Python 3.12 里已被删除，
# 不补上的话连接线程一起来就抛 AttributeError，界面只会显示成
# "连接被拒绝"，而实际上 TLS 握手根本没开始
mumblecompat.install()
import threading
import time
import keyboard
import pyaudio
import wave
import numpy as np  # 确保numpy被导入
import sys
import functools
import pygame  # pygame导入必须在设置环境变量之后

log = logging.getLogger("无线电")

# 配置服务器信息
SERVER_HOST = "audio.ceruleanavi.net"  # Mumble服务器地址
USERNAME = ""    # 用户名
PASSWORD = ""             # 密码（如果需要）

# ---------- 音频采样率候选列表 ----------
# 按优先级排序，自动检测设备支持的采样率
SUPPORTED_SAMPLE_RATES = [48000, 44100, 32000, 24000, 16000]

# 建完临时频道到它出现在频道表里、以及进频道到服务器确认，都要等一次网络往返。
# 固定 sleep 赌不起——远程服务器上经常不够。
CHANNEL_TIMEOUT = 5.0

# 根频道的 channel_id 就是 0。判断"有没有进频道"必须用 is None，写成 not 会
# 把在根频道当成没进频道。
ROOT_CHANNEL = 0

def suppress_mumble_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            # 抑制异常输出
            sys.stderr = open('nul', 'w')
            raise
    return wrapper

class MumbleRadioClient:
    def __init__(self, server_host, username, password="", settings=None):
        # SimConnect 初始化
        self.simconnect = SimConnect()
        self.aq = AircraftRequests(self.simconnect, _time=2000)
        
        # 音频配置
        self.CHUNK = 960  # 20ms @ 48000Hz
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 48000
        self.is_talking = False
        self.on_ptt_change = None
        self.is_receiving = False
        self.on_rx_change = None
        self.on_connection_change = None
        self._last_rx_time = 0
        
        # 添加设置支持（改为可注入同一份 Settings）
        if settings is not None:
            self.settings = settings
        else:
            from settings import Settings
            self.settings = Settings()
        # 同步当前登录账号和密码到设置
        try:
            self.settings.username = username or ""
            self.settings.password = password or ""
        except Exception as e:
            log.error("同步账号到设置失败: %s", e)

        # 确保音量在合理范围内
        self.settings.mic_volume = max(0, min(200, self.settings.mic_volume))
        self.settings.speaker_volume = max(0, min(200, self.settings.speaker_volume))
        
        # 初始化音频设备
        self.audio = pyaudio.PyAudio()
        self.RATE = self._find_best_sample_rate()
        self.CHUNK = int(self.RATE * 0.02)
        log.info("使用采样率: %s Hz, CHUNK: %s", self.RATE, self.CHUNK)
        self.stream = self.audio.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            frames_per_buffer=self.CHUNK,
            input_device_index=self.settings.input_device_index
        )
        self.output_stream = self.audio.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            output=True,
            frames_per_buffer=self.CHUNK,
            output_device_index=self.settings.output_device_index
        )

        # 初始化 Mumble 客户端
        self.mumble = pymumble.Mumble(
            server_host, 
            username,
            password=password,
            reconnect=True
        )
        
        self.mumble.set_receive_sound(True)
        self.mumble.callbacks.set_callback(pymumble.constants.PYMUMBLE_CLBK_SOUNDRECEIVED, self.handle_incoming_audio)
        self.mumble.callbacks.set_callback(
            pymumble.constants.PYMUMBLE_CLBK_PERMISSIONDENIED,
            self.handle_permission_denied)
        self.current_channel = None

        # ---- 独立连接状态标记 ----
        # 由 pymumble 的 connected/disconnected 回调管理，不依赖 mumble 内部线程状态
        self._connection_established = threading.Event()
        # 频道切换不能并发：GUI 线程（登录成功、重连）和监控线程都会切，两边
        # 一起进去会各建一次频道、各报一次错
        self._channel_lock = threading.Lock()
        # 初始频率（登录时无发现阶段，在 monitor_frequency 中读取）
        self._initial_freq = None

        # 预初始化 connected 属性（pymumble 的 init_connection 在 start() 后才设置此属性）
        self.mumble.connected = 0
        
        # 初始应用音量设置
        self.update_volumes()
        
        # 将 pymumble 内部 ping 间隔从默认 10 秒改为 1 秒
        # 注意：from .constants import * 是值拷贝，不能只改 constants，要改 mumble 模块的命名空间
        pymumble.mumble.PYMUMBLE_PING_DELAY = 1
        # 重连间隔从默认 10 秒改为 2 秒
        pymumble.mumble.PYMUMBLE_CONNECTION_RETRY_INTERVAL = 2
        
        # 心跳：记录最后一次收到 ping 回复的时间
        # 初始化为当前时间，避免启动时误报
        self._last_ping_rcv = time.time()

        # 线程管理
        self.monitor_thread = None
        self.voice_thread = None
        self.running = True

        self.pygame_lock = threading.Lock()  # 添加pygame锁
        self.pygame_initialized = False
        try:
            with self.pygame_lock:
                log.debug("开始初始化 pygame 子系统")
                if not pygame.get_init():
                    pygame.init()
                if not pygame.display.get_init():
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    pygame.joystick.init()
                self.pygame_initialized = True
                log.debug("pygame初始化完成，检测到 %d 个摇杆", pygame.joystick.get_count())
                
                self.joystick = None
                if pygame.joystick.get_count() > 0:
                    self.joystick = pygame.joystick.Joystick(0)
                    self.joystick.init()
                    log.debug("摇杆初始化完成: %s", self.joystick.get_name())
        except Exception as e:
            log.debug("摇杆初始化失败: %s", e)
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
                log.info("使用采样率: %s Hz", rate)
                return rate

        log.info("所有候选采样率均失败，使用 48000 Hz")
        return 48000

    def convert_frequency(self, frequency):
        """将频率转换为标准格式"""
        return int(round(frequency * 1000))
    
    def get_channel_name(self, frequency):
        """根据频率生成频道名称"""
        freq = self.convert_frequency(frequency)
        return f"FREQ_{str(freq).zfill(6)}"
    
    def set_connection_state(self, connected):
        """由外部（gui.py 回调）调用，设置独立连接标记。"""
        if connected:
            self._connection_established.set()
            log.info("连接标记已设为 True | thread_alive=%s", self.mumble.is_alive() if hasattr(self.mumble, 'is_alive') else 'N/A')
        else:
            self._connection_established.clear()
            log.info("连接标记已设为 False")

    def _find_channel(self, name):
        try:
            return self.mumble.channels.find_by_name(name)
        except pymumble.errors.UnknownChannelError:
            return None

    def _create_channel(self, name):
        """在根下建一个临时频道，**不要阻塞**。

        pymumble 的 channels.new_channel() 走 execute_command(blocking=True)，
        那个 acquire 没有任何超时——pymumble 自己的源码里就写着
        "TODO: manage a timeout for blocking commands"。命令一旦没被服务器处理，
        这里就永远卡住，调用线程整个死掉：日志停在"尝试创建临时频道"，之后既
        没有成功也没有任何错误，因为线程根本没从这一行返回。实测的后果就是
        飞行员一直留在根频道——发出去没人听得到，也收不到任何东西。

        自己发命令、不等锁；频道有没有建出来由 _wait_for_channel 轮询判断，
        那本来就是更可靠的判据。
        """
        self.mumble.execute_command(messages.CreateChannel(0, name, True),
                                    blocking=False)

    def _move_in(self, channel_id):
        """进频道，同样不能用 move_in()——它也走 blocking=True。"""
        self.mumble.execute_command(
            messages.MoveCmd(self.mumble.users.myself_session, channel_id),
            blocking=False)

    def _wait_for_channel(self, name):
        """等服务器把新建的频道回报回来。

        建频道只是发一条消息，频道要等服务器回 ChannelState 才进本地表——这是
        一次网络往返，不是本地操作。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            channel = self._find_channel(name)
            if channel is not None:
                return channel
            time.sleep(0.1)
        return self._find_channel(name)

    def _wait_until_in(self, channel_id):
        """等服务器确认我们真的进了这个频道。

        move 是异步的，命令发出去不等于进去了。不确认就记 current_channel，
        _ensure_in_correct_channel 会以为已经到位，人却还留在原地。
        """
        deadline = time.time() + CHANNEL_TIMEOUT
        while time.time() < deadline and self.running:
            myself = self.mumble.users.myself
            if myself is not None and myself["channel_id"] == channel_id:
                return True
            time.sleep(0.1)
        myself = self.mumble.users.myself
        return myself is not None and myself["channel_id"] == channel_id

    def switch_channel(self, frequency, caller="unknown"):
        """切换到对应频率的频道

        参数:
            frequency: COM1 频率（MHz）
            caller:    调用来源名称（用于日志定位）

        返回 True 表示确实已经在目标频道里了。整个过程有界：最坏各等一个
        CHANNEL_TIMEOUT，不会像以前那样把调用线程永久挂住。
        """
        channel_name = self.get_channel_name(frequency)
        log.info("频道切换 %s: 尝试切换到 %s (频率 %.3f MHz) | myself=%s | 线程存活=%s",
                 caller, channel_name, frequency,
                 '有' if self.mumble.users.myself else '无',
                 self.mumble.is_alive() if hasattr(self.mumble, 'is_alive') else 'N/A')
        with self._channel_lock:
            try:
                channel = self._find_channel(channel_name)
                if channel is None:
                    log.info("频道切换 频道 %s 不存在，尝试创建临时频道...", channel_name)
                    self._create_channel(channel_name)
                    channel = self._wait_for_channel(channel_name)
                if channel is None:
                    log.warning("频道切换 建立频道 %s 后 %.0f 秒内没有出现，放弃这一次",
                                channel_name, CHANNEL_TIMEOUT)
                    return False

                if not self.mumble.users.myself:
                    log.info("频道切换 self.mumble.users.myself 为 None，无法获取当前频道，跳过切换")
                    return False
                current_id = self.mumble.users.myself["channel_id"]
                if current_id == channel["channel_id"]:
                    log.info("频道切换 已在目标频道 %s 中，无需切换", channel_name)
                    self.current_channel = channel["channel_id"]
                    return True
                log.info("频道切换 当前频道 ID=%s，目标频道 ID=%s，开始进入...",
                         current_id, channel['channel_id'])
                self._move_in(channel["channel_id"])
                # 命令是异步的，确认真的进去了再记账
                if not self._wait_until_in(channel["channel_id"]):
                    log.warning("频道切换 发出了进入 %s 的请求，但 %.0f 秒内没有生效，稍后重试",
                                channel_name, CHANNEL_TIMEOUT)
                    return False
                self.current_channel = channel["channel_id"]
                log.info("成功切换到 %s (频率 %.3f MHz)", channel_name, frequency)
                return True
            except Exception as e:
                log.warning("频道切换 失败: %s: %s", type(e).__name__, e)
                return False


    def _sync_ping_heartbeat(self):
        """读取 pymumble 内部 ping_stats['last_rcv'] 刷新心跳。
        
        不主动发 ping（避免多线程 socket 竞态），而是靠 pymumble 内部自动 ping。
        在 __init__ 中将 PYMUMBLE_PING_DELAY 置为 1 秒，使自动 ping 每秒一次。
        PING_TIMEOUT=3 意味着最多 3 秒即可发现断连。
        """
        try:
            # ping_stats['last_rcv'] 是毫秒级时间戳，由 ping_response() 在 pymumble 线程中更新
            last_rcv = self.mumble.ping_stats.get('last_rcv', 0)
            if last_rcv > 0:
                self._last_ping_rcv = last_rcv / 1000.0
        except Exception:
            pass

    def _ensure_in_correct_channel(self, frequency):
        """确保用户在正确的频道中，如果不在则重新加入"""
        try:
            if not self._connection_established.is_set():
                log.info("频道检查 未连接，跳过")
                return
            if not self.mumble.users.myself:
                log.info("频道检查 myself 尚未就绪，跳过")
                return
            channel_name = self.get_channel_name(frequency)
            current_channel_id = self.mumble.users.myself["channel_id"]
            log.info("频道检查 当前channel_id=%r, 目标=%s", current_channel_id, channel_name)

            # channel_id=0 => Root 频道，必须重新加入
            if current_channel_id == 0:
                log.info("频道检查 用户处于 Root 频道! 重新加入 %s", channel_name)
                self.switch_channel(frequency, caller="频道检查-Root")
                return

            if current_channel_id is None:
                log.info("频道检查 未加入任何频道，重新加入 %s", channel_name)
                self.switch_channel(frequency, caller="频道检查-无频道")
                return

            try:
                current_channel = self.mumble.channels[current_channel_id]
                current_name = current_channel["name"]
                if current_name != channel_name:
                    log.info("频道检查 频道不匹配 (%s -> %s)，重新加入", current_name, channel_name)
                    self.switch_channel(frequency, caller="频道检查-不匹配")
                else:
                    log.info("频道检查 频道匹配 (%s)", current_name)
            except (KeyError, Exception) as e:
                log.info("频道检查 频道 ID %s 无效 (%s)，重新加入 %s", current_channel_id, e, channel_name)
                self.switch_channel(frequency, caller="频道检查-ID无效")
        except Exception as e:
            log.error("频道检查 错误: %s", e, exc_info=True)

    def monitor_frequency(self):
        """监控COM1频率变化和连接/频道状态"""
        last_frequency = None
        last_connected = False
        tick = 0
        freq_read_fail_count = 0
        channel_switch_attempted = False

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

                log.info("监控 T%d 连接标记=%s"
                      " | pymumble.connected=%s"
                      " | thread_alive=%s"
                      " | channel_id=%s"
                      " | channel_name=%r"
                      " | ping_ago=%.1fs"
                      "%s%s",
                      tick, mumble_connected,
                      pymumble_connected_raw,
                      thread_alive,
                      my_channel_id,
                      chan_name,
                      ping_ago,
                      ' ⚠️线程死亡' if not thread_alive else '',
                      ' ⚠️超时' if ping_timeout else '')

                # ★ 不再根据 thread_alive 主动标记断连！仅记录日志供诊断
                # 连接状态变化由回调管理，通知 UI
                if mumble_connected != last_connected:
                    last_connected = mumble_connected
                    log.info("监控 T%d 连接状态变化: %s -> %s", tick, not mumble_connected, mumble_connected)
                    if self.on_connection_change:
                        self.on_connection_change(mumble_connected)

                if mumble_connected:
                    com1_active = self.aq.get("COM_ACTIVE_FREQUENCY:1")
                    if com1_active is not None:
                        freq_read_fail_count = 0
                        if com1_active != last_frequency:
                            log.info("监控 T%d 频率变化: %s -> %s", tick, last_frequency, com1_active)
                            self.switch_channel(com1_active, caller=f"监控-T{tick}-变")
                            last_frequency = com1_active
                        else:
                            self._ensure_in_correct_channel(com1_active)
                    else:
                        freq_read_fail_count += 1
                        log.info("监控 T%d 读频率返回 None (连续%d次)", tick, freq_read_fail_count)
                        if last_frequency is not None:
                            log.info("监控 T%d 用 last_frequency=%s 做频道检查", tick, last_frequency)
                            self._ensure_in_correct_channel(last_frequency)
                        else:
                            log.info("监控 T%d 无 last_frequency 可用，跳过频道检查", tick)

                    if not channel_switch_attempted and last_frequency is not None:
                        channel_switch_attempted = True
                        log.info("监控 T%d 首次连接后的频道切换已完成", tick)
                else:
                    log.info("监控 T%d 未连接，跳过频率读取", tick)

            except Exception as e:
                if self.running:
                    log.error("监控 T%d 错误: %s", tick, e, exc_info=True)
            time.sleep(1)
    
    def update_volumes(self):
        """更新麦克风和扬声器音量"""
        try:
            if self.mumble and self.mumble.connected:
                # 麦克风音量 0-200% 映射到 0-2.0
                self.mumble.sound_output.volume = self.settings.mic_volume / 100.0
            if hasattr(self, 'output_stream'):
                # 扬声器音量 0-200% 映射到音频数据缩放
                volume_scale = self.settings.speaker_volume / 100.0
                def volume_modifier(audio_data):
                    return (np.frombuffer(audio_data, dtype=np.int16) * volume_scale).astype(np.int16).tobytes()
                self.audio_processor = volume_modifier
        except Exception as e:
            log.error("更新音量设置时出错: %s", e)

    def ensure_pygame_initialized(self):
        """确保pygame在当前线程中正确初始化"""
        with self.pygame_lock:
            try:
                if not pygame.get_init():
                    log.debug("重新初始化pygame")
                    pygame.init()
                if not pygame.display.get_init():
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    pygame.joystick.init()
                
                if pygame.joystick.get_count() > 0:
                    if not self.joystick or not self.joystick.get_init():
                        self.joystick = pygame.joystick.Joystick(0)
                        self.joystick.init()
                        log.debug("摇杆重新初始化: %s", self.joystick.get_name())
                return True
            except Exception as e:
                log.debug("pygame重新初始化失败: %s", e)
                return False

    def reinitialize_joystick(self):
        """重新初始化摇杆"""
        log.debug("尝试重新初始化摇杆")
        with self.pygame_lock:
            try:
                # 关闭现有摇杆
                if self.joystick:
                    try:
                        self.joystick.quit()
                    except:
                        pass
                    self.joystick = None

                # 确保pygame已初始化
                if not pygame.get_init():
                    pygame.init()
                if not pygame.display.get_init():
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    pygame.joystick.init()

                # 重新初始化摇杆
                if pygame.joystick.get_count() > 0:
                    self.joystick = pygame.joystick.Joystick(0)
                    self.joystick.init()
                    log.debug("摇杆重新初始化成功: %s", self.joystick.get_name())
                    return True
            except Exception as e:
                log.debug("摇杆重新初始化失败: %s", e)
                return False

    def handle_voice(self):
        """处理按键说话功能"""
        log.debug("开始语音处理线程")
        self.ensure_pygame_initialized()
        last_ptt_state = False
        
        while self.running:
            try:
                # RX 超时检查：超过 0.5 秒无接收则灭灯
                if self.is_receiving and time.time() - self._last_rx_time > 0.5:
                    self.is_receiving = False
                    if self.on_rx_change:
                        self.on_rx_change(False)

                # 检查键盘和摇杆PTT状态
                keyboard_ptt = keyboard.is_pressed(self.settings.ptt_key)
                joystick_ptt = False
                
                if self.settings.joystick_ptt is not None:
                    try:
                        with self.pygame_lock:
                            if not pygame.get_init() or not pygame.joystick.get_init():
                                self.ensure_pygame_initialized()
                            pygame.event.pump()
                            if (self.joystick and self.joystick.get_init() and 
                                self.settings.joystick_ptt < self.joystick.get_numbuttons()):
                                joystick_ptt = self.joystick.get_button(self.settings.joystick_ptt)
                    except Exception as e:
                        log.debug("摇杆读取错误: %s", e)
                
                is_speaking = keyboard_ptt or joystick_ptt
                
                # 状态改变时更新和打印
                if is_speaking != last_ptt_state:
                    log.debug("PTT状态改变: %s (键盘: %s, 摇杆: %s)", is_speaking, keyboard_ptt, joystick_ptt)
                    last_ptt_state = is_speaking
                    self.is_talking = is_speaking
                    if self.on_ptt_change:
                        self.on_ptt_change(self.is_talking)
                
                # 如果PTT被按下，检查是否可以发送音频
                if self.is_talking:
                    if not self.stream or not self.mumble:
                        log.debug("音频发送失败：设备未就绪")
                        time.sleep(0.05)
                        continue
                    
                    try:
                        # ★ 先检查独立连接标记（比 mumble.connected 更可靠）
                        if not self._connection_established.is_set():
                            log.debug("连接标记已清除，跳过音频发送")
                            time.sleep(0.05)
                            continue

                        if not self.mumble.connected > 0:
                            log.debug("Mumble未连接")
                            time.sleep(0.05)
                            continue
                            
                        if not self.mumble.channels:
                            log.debug("Mumble频道列表为空")
                            time.sleep(0.05)
                            continue
                            
                        # 注意是 is None：根频道的 channel_id 就是 0，写成
                        # not 会把"在根频道"当成"没进频道"，PTT 于是一声不吭
                        # 地什么都不做
                        if not self.mumble.users.myself:
                            log.debug("服务器还没回报我们自己的用户信息")
                            time.sleep(0.05)
                            continue
                        if self.mumble.users.myself["channel_id"] is None:
                            log.debug("未加入任何频道")
                            time.sleep(0.05)
                            continue
                        if self.mumble.users.myself["channel_id"] == ROOT_CHANNEL:
                            # 频率频道永远是根的子频道，人在根里就一定是切换没
                            # 成功。这里不能附带判断 current_channel——掉线重连
                            # 之后它停在旧值，条件不成立，话音就真的被发进根频
                            # 道：自己频率上没人听得到，根频道里的人全听见了
                            log.warning("还留在根频道（频率频道没切成功），发出去没人听得到")
                            time.sleep(0.05)
                            continue

                        data = self._safe_stream_read(self.CHUNK)
                        if data:
                            audio_data = np.frombuffer(data, dtype=np.int16)
                            audio_data = (audio_data * (self.settings.mic_volume / 100.0)).astype(np.int16)
                            if not any(audio_data):
                                log.debug("检测到静音数据")
                            else:
                                self.mumble.sound_output.add_sound(audio_data.tobytes())
                                
                    except Exception as e:
                        log.debug("音频处理错误: %s", e)
                
                time.sleep(0.01)
            except Exception as e:
                log.debug("语音处理错误: %s", e)
                time.sleep(0.1)

    def handle_permission_denied(self, event):
        """服务器拒绝了某个动作，把原因说出来。

        不接这条回报的代价，实测日志长这样：

            频道 FREQ_127100 不存在，建一个临时的
            建立频道 FREQ_127100 后 5 秒内没有出现，1 秒后重试
            （无限重复，没有任何错误）

        建频率频道要根频道的 MakeTempChannel（0x400）、进频道要 Enter（0x4）。
        服务器缺哪一条都只是**默默不照做**：命令发出去了、没有报错、频道就是不
        出现。看上去像网络慢，实际上再等一万年也不会成功，而真正的原因服务器早
        就用 PermissionDenied 说了。管制端和 ATIS 一直接着这条，飞行员端漏了。
        """
        try:
            kind = self.mumble.denial_type(event.type)
        except Exception:
            kind = str(getattr(event, "type", "?"))
        reasons = {
            "Permission": "没有权限（建频率频道要根频道的 MakeTempChannel，"
                          "进频道要 Enter）",
            "ChannelName": "频道名不合服务器的规矩",
            "NestingLimit": "频道层级超过了服务器上限",
            "ChannelCountLimit": "服务器上的频道数已达上限",
        }
        reason = reasons.get(kind, "服务器拒绝了操作: %s" % kind)
        if getattr(event, "reason", ""):
            reason += "（%s）" % event.reason
        log.warning("服务器拒绝: %s", reason)

    def handle_incoming_audio(self, user, soundchunk):
        """处理接收到的音频"""
        if not self.mumble.users.myself:
            return
        if user["name"] != self.mumble.users.myself["name"]:  # 不播放自己的声音
            try:
                # 标记正在接收
                self._last_rx_time = time.time()
                if not self.is_receiving:
                    self.is_receiving = True
                    if self.on_rx_change:
                        self.on_rx_change(True)

                # 调整收听音量
                audio_data = np.frombuffer(soundchunk.pcm, dtype=np.int16)
                audio_data = (audio_data * (self.settings.speaker_volume / 100.0)).astype(np.int16)
                self._safe_stream_write(audio_data.tobytes())
            except Exception as e:
                log.error("音频输出错误: %s", e)

    def _safe_stream_read(self, chunk):
        """线程安全地读取音频流，处理流被外部关闭的竞态。"""
        try:
            return self.stream.read(chunk, exception_on_overflow=False)
        except (OSError, IOError, Exception) as e:
            log.debug("音频流读取错误（可能已被重初始化）: %s", e)
            return None

    def _safe_stream_write(self, data):
        """线程安全地写入音频流，处理流被外部关闭的竞态。"""
        try:
            self.output_stream.write(data)
        except (OSError, IOError, Exception) as e:
            log.debug("音频流写入错误（可能已被重初始化）: %s", e)

    def reinitialize_audio(self):
        """重新初始化音频设备"""
        try:
            # 关闭现有的音频流
            if hasattr(self, 'stream') and self.stream:
                self.stream.stop_stream()
                self.stream.close()
            if hasattr(self, 'output_stream') and self.output_stream:
                self.output_stream.stop_stream()
                self.output_stream.close()
            
            # 重新检测采样率（设备可能已更改）
            self.RATE = self._find_best_sample_rate()
            self.CHUNK = int(self.RATE * 0.02)
            log.info("重新初始化使用采样率: %s Hz, CHUNK: %s", self.RATE, self.CHUNK)

            # 重新创建音频流
            self.stream = self.audio.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                frames_per_buffer=self.CHUNK,
                input_device_index=self.settings.input_device_index
            )
            self.output_stream = self.audio.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                output=True,
                frames_per_buffer=self.CHUNK,
                output_device_index=self.settings.output_device_index
            )
            
            # 更新音量设置
            self.update_volumes()
            log.info("音频设备重新初始化完成")
        except Exception as e:
            log.error("重新初始化音频设备失败: %s", e)
            raise

    def show_settings(self):
        """显示设置对话框"""
        from settings import SettingsDialog
        dialog = SettingsDialog(self.settings)
        if dialog.exec():
            # 如果用户点击了保存，则重新初始化音频设备
            self.reinitialize_audio()

    @suppress_mumble_errors
    def run(self):
        """启动客户端主循环"""
        try:
            self.mumble.start()
            time.sleep(1)  # 给予足够时间让连接完成或失败
            
            # 尝试执行一个操作来检查连接状态
            try:
                self.mumble.is_ready()
            except pymumble.errors.ConnectionRejectedError:
                raise pymumble.errors.ConnectionRejectedError("用户名或密码错误")
                
            while self.running:
                try:
                    self.mumble.is_ready()
                    time.sleep(1)
                except:
                    break
        except Exception as e:
            raise e from None
        finally:
            self.cleanup()
    
    def cleanup(self):
        """清理资源"""
        self.running = False  # 停止所有线程的运行

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
                except:
                    pass
                self.mumble.stop()
                
            if hasattr(self, 'simconnect') and self.simconnect:
                self.simconnect.exit()
                
            if hasattr(self, 'joystick') and self.joystick:
                try:
                    self.joystick.quit()
                except:
                    pass
            pygame.quit()
                
        except Exception as e:
            log.error("清理资源时出错: %s", e)
            pass  # 确保清理过程中的错误不会影响程序


if __name__ == "__main__":
    client = None
    try:
        client = MumbleRadioClient(SERVER_HOST, USERNAME, PASSWORD)
        client.run()
    except KeyboardInterrupt:
        log.info("程序被用户中断")
    except Exception as e:
        log.error("程序发生错误: %s", e)
    finally:
        if client:
            client.cleanup()
        log.info("按回车键退出...")
        try:
            input()
        except:
            # 如果input()失败，使用time.sleep作为备选
            import time
            time.sleep(5)