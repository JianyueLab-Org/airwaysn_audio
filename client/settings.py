import os
import logging
# 设置SDL环境变量
os.environ['SDL_VIDEODRIVER'] = 'dummy'

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                         QPushButton, QSlider, QLineEdit, QComboBox)
from PyQt6.QtCore import Qt, QTimer
import json
import pygame
import threading

log = logging.getLogger("设置")

class Settings:
    def __init__(self):
        self.config_file = "radio_settings.json"
        self.ptt_key = "v"
        self.joystick_ptt = None  # 新增摇杆PTT按键属性
        self.mic_volume = 100
        self.speaker_volume = 100
        self.input_device_index = None
        self.output_device_index = None
        # 新增：账号与密码
        self.username = ""
        self.password = ""
        self.load_settings()

    def load_settings(self):
        try:
            if os.path.exists(self.config_file):
                log.info("正在加载设置文件")
                with open(self.config_file, "r") as f:
                    data = json.load(f)
                    self.ptt_key = data.get("ptt_key", "v")
                    self.joystick_ptt = data.get("joystick_ptt", None)
                    self.mic_volume = data.get("mic_volume", 100)
                    self.speaker_volume = data.get("speaker_volume", 100)
                    self.input_device_index = data.get("input_device_index", None)
                    self.output_device_index = data.get("output_device_index", None)
                    # 新增：读取账号与密码
                    self.username = data.get("username", "")
                    self.password = data.get("password", "")
                # 调整调试输出，隐藏密码
                safe = dict(data)
                if "password" in safe:
                    safe["password"] = "***"
                log.debug("设置加载成功: %s", safe)
        except Exception as e:
            log.error("加载设置失败: %s", e)

    def save_settings(self):
        try:
            data = {
                "ptt_key": self.ptt_key,
                "joystick_ptt": self.joystick_ptt,
                "mic_volume": self.mic_volume,
                "speaker_volume": self.speaker_volume,
                "input_device_index": self.input_device_index,
                "output_device_index": self.output_device_index,
                # 新增：保存账号与密码
                "username": self.username,
                "password": self.password
            }
            # 调整调试输出，隐藏密码
            safe = dict(data)
            safe["password"] = "***" if safe.get("password") else ""
            log.debug("保存设置: %s", safe)
            with open(self.config_file, "w") as f:
                json.dump(data, f)
            log.info("设置保存成功")
        except Exception as e:
            log.error("保存设置失败: %s", e)

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.pygame_lock = threading.Lock()  # 添加pygame锁
        self.setup_ui()
        self.listening_for_key = False
        self.listening_for_joystick = False
        self.joy_timer = None

    def setup_ui(self):
        layout = QVBoxLayout()

        # PTT按键设置
        ptt_layout = QHBoxLayout()
        ptt_label = QLabel("键盘PTT按键:")
        self.ptt_input = QLineEdit(self.settings.ptt_key)
        self.ptt_input.setReadOnly(True)
        self.ptt_reset_btn = QPushButton("重设")
        self.ptt_reset_btn.clicked.connect(self.start_key_capture)
        ptt_layout.addWidget(ptt_label)
        ptt_layout.addWidget(self.ptt_input)
        ptt_layout.addWidget(self.ptt_reset_btn)

        # 摇杆PTT按键设置
        joy_ptt_layout = QHBoxLayout()
        joy_ptt_label = QLabel("摇杆PTT按键:")
        self.joy_ptt_input = QLineEdit()
        self.joy_ptt_input.setReadOnly(True)
        if self.settings.joystick_ptt is not None:
            self.joy_ptt_input.setText(f"按键 {self.settings.joystick_ptt}")
        self.joy_ptt_reset_btn = QPushButton("设置")
        self.joy_ptt_reset_btn.clicked.connect(self.start_joystick_capture)
        self.joy_ptt_clear_btn = QPushButton("清除")
        self.joy_ptt_clear_btn.clicked.connect(self.clear_joystick_ptt)
        joy_ptt_layout.addWidget(joy_ptt_label)
        joy_ptt_layout.addWidget(self.joy_ptt_input)
        joy_ptt_layout.addWidget(self.joy_ptt_reset_btn)
        joy_ptt_layout.addWidget(self.joy_ptt_clear_btn)

        # 音频输入设备选择
        input_layout = QHBoxLayout()
        input_label = QLabel("输入设备:")
        self.input_combo = QComboBox()
        self.populate_audio_devices(self.input_combo, True)
        input_layout.addWidget(input_label)
        input_layout.addWidget(self.input_combo)

        # 音频输出设备选择
        output_layout = QHBoxLayout()
        output_label = QLabel("输出设备:")
        self.output_combo = QComboBox()
        self.populate_audio_devices(self.output_combo, False)
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_combo)

        # 麦克风音量设置
        mic_layout = QHBoxLayout()
        mic_label = QLabel("麦克风音量:")
        self.mic_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_slider.setRange(0, 200)
        self.mic_slider.setValue(self.settings.mic_volume)
        self.mic_value = QLabel(f"{self.settings.mic_volume}%")
        mic_layout.addWidget(mic_label)
        mic_layout.addWidget(self.mic_slider)
        mic_layout.addWidget(self.mic_value)

        # 扬声器音量设置
        speaker_layout = QHBoxLayout()
        speaker_label = QLabel("收听音量:")
        self.speaker_slider = QSlider(Qt.Orientation.Horizontal)
        self.speaker_slider.setRange(0, 200)  # 设置范围为0-200%
        self.speaker_slider.setValue(self.settings.speaker_volume)
        self.speaker_value = QLabel(f"{self.settings.speaker_volume}%")
        speaker_layout.addWidget(speaker_label)
        speaker_layout.addWidget(self.speaker_slider)
        speaker_layout.addWidget(self.speaker_value)

        # 按钮
        button_layout = QHBoxLayout()
        save_button = QPushButton("保存")
        cancel_button = QPushButton("取消")
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)

        # 连接信号
        self.mic_slider.valueChanged.connect(
            lambda v: self.mic_value.setText(f"{v}%"))
        self.speaker_slider.valueChanged.connect(
            lambda v: self.speaker_value.setText(f"{v}%"))
        save_button.clicked.connect(self.save_and_close)
        cancel_button.clicked.connect(self.reject)

        # 添加所有布局
        layout.addLayout(ptt_layout)
        layout.addLayout(joy_ptt_layout)
        layout.addLayout(input_layout)
        layout.addLayout(output_layout)
        layout.addLayout(mic_layout)
        layout.addLayout(speaker_layout)
        layout.addLayout(button_layout)
        self.setLayout(layout)

    def populate_audio_devices(self, combo_box, is_input):
        """填充音频设备下拉列表"""
        import pyaudio
        p = pyaudio.PyAudio()
        
        # 添加"系统默认"选项
        combo_box.addItem("系统默认", None)
        
        # 遍历所有音频设备
        for i in range(p.get_device_count()):
            device_info = p.get_device_info_by_index(i)
            if is_input and device_info.get('maxInputChannels') > 0:
                combo_box.addItem(device_info.get('name'), i)
            elif not is_input and device_info.get('maxOutputChannels') > 0:
                combo_box.addItem(device_info.get('name'), i)
        
        # 设置当前选中的设备
        current_index = self.settings.input_device_index if is_input else self.settings.output_device_index
        if current_index is not None:
            index = combo_box.findData(current_index)
            if index >= 0:
                combo_box.setCurrentIndex(index)
        p.terminate()

    def start_key_capture(self):
        """开始捕获键盘按键"""
        log.debug("开始捕获键盘按键")
        self.listening_for_key = True
        self.ptt_input.setText("请按下按键...")
        self.ptt_reset_btn.setEnabled(False)
        
        import keyboard
        def on_key_pressed(event):
            if self.listening_for_key and event.name:
                log.debug("捕获到按键: %s", event.name)
                self.ptt_input.setText(event.name)
                self.listening_for_key = False
                self.ptt_reset_btn.setEnabled(True)
                keyboard.unhook_all()  # 停止监听

        keyboard.on_press(on_key_pressed)

    def start_joystick_capture(self):
        """开始捕获摇杆按键"""
        with self.pygame_lock:
            try:
                if not pygame.get_init():
                    log.debug("初始化pygame子系统")
                    pygame.init()
                if not pygame.display.get_init():
                    log.debug("初始化显示系统")
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    log.debug("初始化摇杆系统")
                    pygame.joystick.init()
                
                log.debug("检测到 %d 个摇杆", pygame.joystick.get_count())
                
                if pygame.joystick.get_count() == 0:
                    log.debug("未检测到摇杆设备")
                    self.joy_ptt_input.setText("未检测到摇杆")
                    return
                
                joystick = pygame.joystick.Joystick(0)
                joystick.init()
                log.debug("摇杆已初始化: %s", joystick.get_name())
                
                self.joy_ptt_input.setText("请按下摇杆按键...")
                self.joy_ptt_reset_btn.setEnabled(False)
                self.listening_for_joystick = True
                
                if self.joy_timer is None:
                    self.joy_timer = QTimer()
                    self.joy_timer.timeout.connect(self.check_joystick_button)
                self.joy_timer.start(50)  # 提高检查频率到50ms
                
            except Exception as e:
                log.debug("摇杆初始化失败: %s", e)
                self.joy_ptt_input.setText("初始化失败")
                if self.joy_timer:
                    self.joy_timer.stop()

    def check_joystick_button(self):
        """检查摇杆按键状态"""
        with self.pygame_lock:
            try:
                if not pygame.get_init():
                    pygame.init()
                if not pygame.display.get_init():
                    pygame.display.init()
                if not pygame.joystick.get_init():
                    pygame.joystick.init()
                    
                pygame.event.pump()
                
                if pygame.joystick.get_count() > 0:
                    joystick = pygame.joystick.Joystick(0)
                    joystick.init()
                    
                    for i in range(joystick.get_numbuttons()):
                        if joystick.get_button(i):
                            log.debug("检测到摇杆按键: %d", i)
                            # 先更新UI
                            self.joy_ptt_input.setText(f"按键 {i}")
                            self.listening_for_joystick = False
                            self.joy_ptt_reset_btn.setEnabled(True)
                            
                            # 然后更新设置
                            self.settings.joystick_ptt = i
                            # 立即保存设置到文件
                            self.settings.save_settings()
                            log.debug("新的摇杆按键设置已保存: %d", i)
                            
                            if self.joy_timer:
                                self.joy_timer.stop()
                            return
            except Exception as e:
                log.debug("检查摇杆按键状态时出错: %s", e)
                if self.joy_timer:
                    self.joy_timer.stop()
                self.joy_ptt_input.setText("摇杆读取失败")

    def clear_joystick_ptt(self):
        """清除摇杆PTT设置"""
        self.settings.joystick_ptt = None
        self.joy_ptt_input.setText("")

    def cleanup(self):
        """清理资源"""
        if self.joy_timer:
            self.joy_timer.stop()
            self.joy_timer = None
            
        import keyboard
        keyboard.unhook_all()
        
        # 不再在这里完全清理pygame，只清理当前摇杆实例
        try:
            if pygame.joystick.get_init():
                for i in range(pygame.joystick.get_count()):
                    try:
                        joy = pygame.joystick.Joystick(i)
                        joy.quit()
                    except:
                        pass
            log.debug("摇杆实例已清理")
        except Exception as e:
            log.debug("清理摇杆实例时出错: %s", e)
            pass

    def reject(self):
        """取消时清理资源"""
        if self.joy_timer:
            self.joy_timer.stop()
            self.joy_timer = None
        
        import keyboard
        keyboard.unhook_all()
        
        super().reject()

    def accept(self):
        """确认时清理资源"""
        if self.joy_timer:
            self.joy_timer.stop()
            self.joy_timer = None
        
        import keyboard
        keyboard.unhook_all()
        
        super().accept()

    def save_and_close(self):
        """保存并关闭设置对话框"""
        try:
            self.settings.ptt_key = self.ptt_input.text() or "v"
            # 确保摇杆按键设置被正确保存
            if self.joy_ptt_input.text().startswith("按键 "):
                try:
                    button_num = int(self.joy_ptt_input.text().split(" ")[1])
                    self.settings.joystick_ptt = button_num
                except:
                    self.settings.joystick_ptt = None
            self.settings.mic_volume = self.mic_slider.value()
            self.settings.speaker_volume = self.speaker_slider.value()
            self.settings.input_device_index = self.input_combo.currentData()
            self.settings.output_device_index = self.output_combo.currentData()
            
            # 保存设置到文件
            self.settings.save_settings()
            log.debug("保存设置: PTT键=%s, 摇杆按键=%s", self.settings.ptt_key, self.settings.joystick_ptt)
            
            self.accept()
        except Exception as e:
            log.error("保存设置时出错: %s", e)
            self.reject()