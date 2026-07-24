from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, 
                         QHBoxLayout, QLabel, QPushButton, QLineEdit, 
                         QStackedWidget, QFrame,  QMessageBox,
                         QDialog, QTextEdit, QCheckBox)
from PyQt6.QtCore import Qt
import re
from pynput import keyboard
from radio import ATCRadioClient, server
from settings import Settings, SettingsDialog
from ATIS import ATISBroadcaster
from PyQt6.QtGui import QIcon

icon_path = r".\favicon.ico"

class CircleIndicator(QFrame):
    """圆形状态指示灯，支持自定义颜色。"""
    def __init__(self, active_color="#ff0000", parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._active_color = active_color
        self._active = False
        self._update_style()

    def setActive(self, active):
        self._active = active
        self._update_style()

    def _update_style(self):
        color = self._active_color if self._active else "#808080"
        self.setStyleSheet(f"background-color: {color}; border-radius: 10px;")

PTTIndicator = CircleIndicator

class ATISDialog(QDialog):
    def __init__(self, atis_id, parent=None):
        super().__init__(parent)
        self.atis_id = atis_id
        self.initUI()

    def initUI(self):
        self.setWindowTitle(f'ATIS {self.atis_id} 设置')
        self.setGeometry(200, 200, 400, 500)

        layout = QVBoxLayout()

        # 频率输入框
        freq_layout = QHBoxLayout()
        self.freq_input = QLineEdit()
        self.freq_input.setPlaceholderText('频率 (例如: 118.000)')
        freq_layout.addWidget(QLabel('频率:'))
        freq_layout.addWidget(self.freq_input)
        layout.addLayout(freq_layout)

        # 中文通播选项
        self.chinese_checkbox = QCheckBox('启用中文通播')
        self.chinese_checkbox.stateChanged.connect(self.toggle_chinese_text)
        layout.addWidget(self.chinese_checkbox)

        # 中文通播输入框
        self.chinese_group = QWidget()
        chinese_layout = QVBoxLayout(self.chinese_group)
        chinese_layout.addWidget(QLabel('中文通播内容:'))
        self.chinese_text = QTextEdit()
        self.chinese_text.setPlaceholderText('所有输入的阿拉伯数字会被替换为无线电读法，所有空格+大写字母+空格的格式会被替换为无线电读法')
        chinese_layout.addWidget(self.chinese_text)
        layout.addWidget(self.chinese_group)
        self.chinese_group.setVisible(False)

        # 英文通播输入框
        layout.addWidget(QLabel('英文通播内容:'))
        self.english_text = QTextEdit()
        self.english_text.setPlaceholderText('all arabic numbers will be replaced with radio readout, and all spaces + capital letters + spaces will be replaced with radio readout')
        layout.addWidget(self.english_text)

        # 按钮
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton('开始播报')
        self.start_btn.clicked.connect(self.validate_and_accept)
        btn_layout.addWidget(self.start_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def toggle_chinese_text(self, state):
        self.chinese_group.setVisible(state == Qt.CheckState.Checked.value)

    def validate_and_accept(self):
        # 验证频率格式
        freq = self.freq_input.text()
        if not re.match(r'^\d{3}\.\d{3}$', freq):
            QMessageBox.warning(self, '输入错误', '请输入正确的频率格式 (例如: 118.000)')
            return

        # 预处理文本内容，去除多余空格
        chinese_text = " ".join(self.chinese_text.toPlainText().split()) if self.chinese_checkbox.isChecked() else ""
        english_text = " ".join(self.english_text.toPlainText().split())
        
        if not english_text:
            QMessageBox.warning(self, '输入错误', '英文通播内容不能为空')
            return

        # 内容验证通过，接受对话框
        self.accept()

class ATCWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon(icon_path))
        self.radio_client = None
        self.settings = Settings()
        self.server = server  # 添加服务器地址属性
        self.atis_clients = {}  # 存储ATIS客户端实例
        self.atis_status = {'1': False, '2': False}  # ATIS状态
        self.atis_broadcasters = {}  # 存储ATIS广播实例
        self.initUI()
        self.setup_keyboard_hook()

    def initUI(self):
        self.setWindowTitle('ATC Radio Client')
        self.setGeometry(100, 100, 400, 200)

        # 创建堆叠窗口部件
        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        # 创建登录页面
        self.login_page = QWidget()
        self.setup_login_page()
        
        # 创建通信页面
        self.comm_page = QWidget()
        self.setup_comm_page()

        # 将页面添加到堆叠窗口
        self.stacked_widget.addWidget(self.login_page)
        self.stacked_widget.addWidget(self.comm_page)

        # 加载上次的设置
        self.username_input.setText(self.settings.last_username)
        self.freq_input.setText(self.settings.last_frequency)

    def setup_login_page(self):
        layout = QVBoxLayout(self.login_page)

        # 用户名
        username_layout = QHBoxLayout()
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText('用户名')
        username_layout.addWidget(QLabel('用户名:'))
        username_layout.addWidget(self.username_input)

        # 密码
        password_layout = QHBoxLayout()
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText('密码')
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        password_layout.addWidget(QLabel('密码:'))
        password_layout.addWidget(self.password_input)

        # 频率
        freq_layout = QHBoxLayout()
        self.freq_input = QLineEdit()
        self.freq_input.setPlaceholderText('频率 (例如: 118.000)')
        freq_layout.addWidget(QLabel('频率:'))
        freq_layout.addWidget(self.freq_input)

        # 连接按钮
        self.connect_btn = QPushButton('连接')
        self.connect_btn.clicked.connect(self.connect_radio)

        # 状态标签
        self.login_status_label = QLabel('未连接')
        self.login_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 添加所有部件
        layout.addLayout(username_layout)
        layout.addLayout(password_layout)
        layout.addLayout(freq_layout)
        layout.addWidget(self.connect_btn)
        layout.addWidget(self.login_status_label)

    def setup_comm_page(self):
        layout = QVBoxLayout(self.comm_page)
        
        # 顶部工具栏（频率显示和设置按钮）
        top_bar = QHBoxLayout()
        self.freq_display = QLabel()
        self.freq_display.setAlignment(Qt.AlignmentFlag.AlignLeft)
        settings_btn = QPushButton("设置")
        settings_btn.clicked.connect(self.open_settings)
        top_bar.addWidget(self.freq_display)
        top_bar.addWidget(settings_btn)
        
        # 状态指示灯：PTT（红）和 RX（绿）
        indicator_layout = QHBoxLayout()
        
        self.ptt_indicator = CircleIndicator(active_color="#ff0000")
        ptt_label = QLabel("PTT")
        indicator_layout.addWidget(self.ptt_indicator)
        indicator_layout.addWidget(ptt_label)
        
        indicator_layout.addSpacing(15)
        
        self.rx_indicator = CircleIndicator(active_color="#00cc00")
        rx_label = QLabel("RX")
        indicator_layout.addWidget(self.rx_indicator)
        indicator_layout.addWidget(rx_label)
        
        indicator_layout.addStretch()
        
        # PTT按钮
        self.ptt_button = QPushButton('按住说话 (PTT)')
        self.ptt_button.pressed.connect(self.ptt_pressed)
        self.ptt_button.released.connect(self.ptt_released)
        
        # 状态标签
        self.comm_status_label = QLabel('就绪')
        self.comm_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # 断开连接按钮
        self.disconnect_btn = QPushButton('断开连接')
        self.disconnect_btn.clicked.connect(self.disconnect_radio)
        
        # ATIS按钮
        atis_layout = QHBoxLayout()
        self.atis_btn1 = QPushButton('添加情报通播1')
        self.atis_btn2 = QPushButton('添加情报通播2')
        self.atis_btn1.clicked.connect(lambda: self.toggle_atis('1'))
        self.atis_btn2.clicked.connect(lambda: self.toggle_atis('2'))
        atis_layout.addWidget(self.atis_btn1)
        atis_layout.addWidget(self.atis_btn2)
        
        # 添加所有部件
        layout.addLayout(top_bar)
        layout.addLayout(indicator_layout)
        layout.addWidget(self.ptt_button)
        layout.addWidget(self.comm_status_label)
        layout.addLayout(atis_layout)  # 添加ATIS按钮
        layout.addWidget(self.disconnect_btn)

    def setup_keyboard_hook(self):
        self.keyboard_listener = keyboard.Listener(
            on_press=lambda key: self.on_key_press(key),
            on_release=lambda key: self.on_key_release(key)
        )
        self.keyboard_listener.start()

    def on_key_press(self, key):
        try:
            # 将 pynput 的按键转换为字符串格式进行比较
            key_str = key.char if hasattr(key, 'char') else key.name if hasattr(key, 'name') else str(key)
            if key_str == self.settings.ptt_key:
                self.ptt_pressed()
        except AttributeError:
            pass

    def on_key_release(self, key):
        try:
            key_str = key.char if hasattr(key, 'char') else key.name if hasattr(key, 'name') else str(key)
            if key_str == self.settings.ptt_key:
                self.ptt_released()
        except AttributeError:
            pass

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            # 更新键盘钩子
            if hasattr(self, 'keyboard_listener'):
                self.keyboard_listener.stop()
            self.setup_keyboard_hook()
            # 更新音频设备
            if self.radio_client:
                self.radio_client.setup_audio(
                    self.settings.input_device_index,
                    self.settings.output_device_index
                )

    def validate_frequency(self, freq):
        pattern = r'^\d{3}\.\d{1,3}$'
        return bool(re.match(pattern, freq))

    def format_frequency(self, freq):
        base, decimal = freq.split('.')
        decimal = decimal.ljust(3, '0')
        return f"{base}.{decimal}"

    def connect_radio(self):
        username = self.username_input.text()
        password = self.password_input.text()
        frequency = self.freq_input.text()

        if not all([username, password, frequency]):
            self.login_status_label.setText('请填写所有字段')
            return

        if not self.validate_frequency(frequency):
            self.login_status_label.setText('频率格式无效')
            return

        frequency = self.format_frequency(frequency)

        try:
            self.radio_client = ATCRadioClient(self.server, username, password, frequency)
            self.radio_client.setup_audio(
                self.settings.input_device_index,
                self.settings.output_device_index
            )
            # 设置发射/接收状态回调
            def on_ptt_change(is_talking):
                self.ptt_indicator.setActive(is_talking)
            self.radio_client.on_ptt_change = on_ptt_change

            def on_rx_change(is_receiving):
                self.rx_indicator.setActive(is_receiving)
            self.radio_client.on_rx_change = on_rx_change

            # 设置初始音量
            self.radio_client.set_mic_volume(self.settings.mic_volume)
            self.radio_client.set_speaker_volume(self.settings.speaker_volume)
            self.radio_client.start()
            self.freq_display.setText(f'当前频率: {frequency}')
            self.stacked_widget.setCurrentIndex(1)
            
            # 保存最后使用的用户名和频率
            self.settings.last_username = username
            self.settings.last_frequency = frequency
            self.settings.save_settings()
            
        except Exception as e:
            error_message = str(e)
            QMessageBox.critical(self, '连接失败：', f'{error_message}')
            self.login_status_label.setText('未连接')

    def disconnect_radio(self):
        if self.radio_client:
            self.radio_client.stop()
            self.radio_client = None
        self.stacked_widget.setCurrentIndex(0)  # 返回登录页面
        self.login_status_label.setText('未连接')

    def ptt_pressed(self):
        if self.radio_client:
            self.radio_client.start_speaking()
            self.comm_status_label.setText('正在发送...')
            self.ptt_indicator.setActive(True)

    def ptt_released(self):
        if self.radio_client:
            self.radio_client.stop_speaking()
            self.comm_status_label.setText('就绪')
            self.ptt_indicator.setActive(False)

    def update_mic_volume(self, value):
        if self.radio_client:
            self.radio_client.set_mic_volume(value)
        self.settings.mic_volume = value
        self.settings.save_settings()

    def update_speaker_volume(self, value):
        if self.radio_client:
            self.radio_client.set_speaker_volume(value)
        self.settings.speaker_volume = value
        self.settings.save_settings()

    def toggle_atis(self, atis_id):
        if not self.atis_status[atis_id]:
            dialog = ATISDialog(atis_id, self)
            if dialog.exec():
                try:
                    freq = dialog.freq_input.text()
                    chinese_text = dialog.chinese_text.toPlainText().strip() if dialog.chinese_checkbox.isChecked() else ""
                    english_text = dialog.english_text.toPlainText().strip()

                    if not english_text:
                        raise ValueError("英文通播内容不能为空")

                    base_username = self.username_input.text()
                    atis_username = f"{base_username}_atis"
                    
                    client = ATCRadioClient(
                        self.server,
                        atis_username,
                        self.password_input.text(),
                        freq
                    )
                    
                    try:
                        client.start()
                    except Exception as e:
                        raise Exception(f"ATIS客户端启动失败: {str(e)}")

                    client.setup_audio(
                        self.settings.input_device_index,
                        self.settings.output_device_index
                    )
                    
                    self.atis_clients[atis_id] = client

                    try:
                        broadcaster = ATISBroadcaster(chinese_text, english_text, client)
                        broadcaster.start_broadcasting()
                        self.atis_broadcasters[atis_id] = broadcaster
                    except Exception as e:
                        client.stop()
                        del self.atis_clients[atis_id]
                        raise Exception(f"ATIS广播器启动失败: {str(e)}")
                    
                    self.atis_status[atis_id] = True
                    btn = self.atis_btn1 if atis_id == '1' else self.atis_btn2
                    btn.setText(f'停止情报通播{atis_id}')
                    
                except Exception as e:
                    QMessageBox.critical(self, '错误', f'启动ATIS失败: {str(e)}')
                    self.cleanup_atis(atis_id)
        else:
            self.cleanup_atis(atis_id)

    def cleanup_atis(self, atis_id):
        """清理ATIS相关资源"""
        try:
            if atis_id in self.atis_broadcasters:
                try:
                    self.atis_broadcasters[atis_id].stop_broadcasting()
                except Exception as e:
                    print(f"停止ATIS广播时出错: {e}")
                finally:
                    del self.atis_broadcasters[atis_id]
            
            if atis_id in self.atis_clients:
                try:
                    self.atis_clients[atis_id].stop()
                except Exception as e:
                    print(f"停止ATIS客户端时出错: {e}")
                finally:
                    del self.atis_clients[atis_id]
            
            self.atis_status[atis_id] = False
            
            btn = self.atis_btn1 if atis_id == '1' else self.atis_btn2
            btn.setText(f'添加情报通播{atis_id}')
        except Exception as e:
            print(f"清理ATIS资源时出错: {e}")

    def closeEvent(self, event):
        try:
            # 停止所有ATIS广播
            for broadcaster in self.atis_broadcasters.values():
                broadcaster.stop_broadcasting()
            # 停止所有ATIS客户端
            for client in self.atis_clients.values():
                client.stop()
            if hasattr(self, 'keyboard_listener'):
                self.keyboard_listener.stop()
            if self.radio_client:
                self.radio_client.stop()
        except Exception as e:
            print(f"关闭窗口时出错: {e}")
        finally:
            event.accept()

if __name__ == '__main__':
    import sys
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(icon_path))
    window = ATCWindow()
    window.show()
    sys.exit(app.exec())