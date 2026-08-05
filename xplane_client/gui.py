"""
gui.py — X-Plane 无线电客户端 GUI

与 SimConnect 版本功能一致：
- 登录界面（Mumble 账号密码）
- 主界面（COM1 频率显示、PTT 指示灯）
- 设置对话框（PTT 按键、音频设备、音量）

与 SimConnect 版本的区别：
- 使用 radio.py 中的 XPlaneRadio（UDP 协议）读取频率
- 启动时先自动发现 X-Plane 并获取初始频率
"""

import sys
import os
import logging
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QStackedWidget, QMessageBox, QDialog,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QPainter, QColor
from radio import MumbleRadioClient
import threading
import time
import keyboard
import pymumble_py3 as pymumble
import pygame
import applog
import version

ico_path = r".\favicon.ico"

log = logging.getLogger("GUI")


class CircleIndicator(QWidget):
    """圆形状态指示灯。"""

    def __init__(self, active_color=QColor(255, 0, 0), parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)
        self.is_active = False
        self._active_color = active_color

    def setActive(self, active):
        self.is_active = active
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self._active_color if self.is_active else QColor(128, 128, 128)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(5, 5, 20, 20)


class LoginWindow(QWidget):
    """登录界面。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()

        username_layout = QHBoxLayout()
        username_label = QLabel("用户名:")
        self.username_input = QLineEdit()
        username_layout.addWidget(username_label)
        username_layout.addWidget(self.username_input)

        password_layout = QHBoxLayout()
        password_label = QLabel("密码:")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        password_layout.addWidget(password_label)
        password_layout.addWidget(self.password_input)

        button_error_layout = QHBoxLayout()
        self.login_button = QPushButton("登录")
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: red")
        button_error_layout.addWidget(self.login_button)
        button_error_layout.addWidget(self.error_label)
        button_error_layout.addStretch()

        layout.addLayout(username_layout)
        layout.addLayout(password_layout)
        layout.addLayout(button_error_layout)
        self.setLayout(layout)

    def show_error(self, message):
        self.error_label.setText(message)

    def clear_error(self):
        self.error_label.setText("")


class MainWindow(QWidget):
    """主界面：显示 COM1 频率和 PTT 状态。"""

    def __init__(self, radio_client, parent=None):
        super().__init__(parent)
        self.radio_client = radio_client
        self._last_freq = None
        self.setup_ui()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frequency)
        self.timer.start(1000)

        self.settings_button.clicked.connect(self.show_settings)

    def setup_ui(self):
        layout = QVBoxLayout()

        top_layout = QHBoxLayout()
        self.connection_indicator = CircleIndicator(active_color=QColor(0, 255, 0))
        self.connection_label = QLabel("已连接")
        self.connection_label.setStyleSheet("color: green")
        self.freq_label = QLabel("COM1: -.--- MHz")
        self.settings_button = QPushButton("设置")
        top_layout.addWidget(self.connection_indicator)
        top_layout.addWidget(self.connection_label)
        top_layout.addStretch()
        top_layout.addWidget(self.freq_label)
        top_layout.addWidget(self.settings_button)

        middle_layout = QHBoxLayout()
        ptt_label = QLabel("PTT状态:")
        self.ptt_indicator = CircleIndicator(QColor(255, 0, 0))
        rx_label = QLabel("RX状态:")
        self.rx_indicator = CircleIndicator(QColor(0, 255, 0))
        middle_layout.addWidget(ptt_label)
        middle_layout.addWidget(self.ptt_indicator)
        middle_layout.addSpacing(15)
        middle_layout.addWidget(rx_label)
        middle_layout.addWidget(self.rx_indicator)
        middle_layout.addStretch()

        layout.addLayout(top_layout)
        layout.addLayout(middle_layout)
        layout.addStretch()
        self.setLayout(layout)

    def update_frequency(self):
        try:
            freq = self.radio_client.xplane.read_com1_freq(self.radio_client.xplane.addr)
            if freq is not None:
                self._last_freq = freq
                self.freq_label.setText(f"COM1: {freq:.3f} MHz")
            elif self._last_freq is not None:
                self.freq_label.setText(f"COM1: {self._last_freq:.3f} MHz")
        except Exception:
            if self._last_freq is not None:
                self.freq_label.setText(f"COM1: {self._last_freq:.3f} MHz")
            else:
                self.freq_label.setText("COM1: -.--- MHz")

    def update_connection_status(self, connected):
        if connected:
            self.connection_indicator.setActive(True)
            self.connection_label.setText("已连接")
            self.connection_label.setStyleSheet("color: green")
        else:
            self.connection_indicator.setActive(False)
            self.connection_label.setText("已断开")
            self.connection_label.setStyleSheet("color: red")

    def update_ptt_status(self, is_talking):
        self.ptt_indicator.setActive(is_talking)

    def update_rx_status(self, is_receiving):
        self.rx_indicator.setActive(is_receiving)

    def show_settings(self):
        from settings import SettingsDialog
        dialog = SettingsDialog(self.radio_client.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.radio_client.reinitialize_audio()


class ErrorSignal(QObject):
    error = pyqtSignal(str)


class ConnectionSignal(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()


class RadioGUI(QMainWindow):
    """X-Plane 无线电主窗口。"""

    def __init__(self):
        super().__init__()

        # 设置日志
        applog.setup()
        logging.getLogger("启动").info("X-Plane 飞行员客户端启动 %s", version.full())

        from PyQt6.QtGui import QIcon
        icon = QIcon(ico_path)
        self.setWindowIcon(icon)
        app = QApplication.instance()
        if app:
            app.setWindowIcon(icon)

        # 初始化 pygame
        try:
            pygame.init()
            pygame.display.init()
            pygame.joystick.init()
            log.debug("pygame初始化完成，检测到 %d 个摇杆", pygame.joystick.get_count())
        except Exception as e:
            log.error("Pygame初始化失败: %s", e)

        self.setWindowTitle(f"无线电-Airwaysn (X-Plane) {version.full()}")
        self.setMinimumSize(300, 200)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        self.login_window = LoginWindow()
        self.stacked_widget.addWidget(self.login_window)
        self.login_window.login_button.clicked.connect(self.handle_login)

        from settings import Settings
        self.settings = Settings()
        try:
            self.login_window.username_input.setText(self.settings.username or "")
            self.login_window.password_input.setText(self.settings.password or "")
        except Exception as e:
            log.error("自动填充账号失败: %s", e)

        self.radio_client = None
        self.main_window = None
        self.client_thread = None

        self.error_signal = ErrorSignal()
        self.error_signal.error.connect(self.show_error)
        self.connection_signal = ConnectionSignal()
        self.connection_signal.connected.connect(self.on_connected)
        self.connection_signal.disconnected.connect(self.on_disconnected)
    def show_error(self, message):
        if not self.main_window:
            QMessageBox.critical(self, "登录错误", message)

    def cleanup_client(self):
        log.info("开始清理客户端资源")
        if self.radio_client:
            self.radio_client.cleanup()
            self.radio_client = None
        if self.main_window:
            self.stacked_widget.removeWidget(self.main_window)
            self.main_window.deleteLater()
            self.main_window = None
        if self.client_thread and self.client_thread != threading.current_thread():
            if self.client_thread.is_alive():
                self.client_thread.join(timeout=1.0)
            self.client_thread = None
        log.info("客户端资源清理完成")

    def _switch_channel_async(self, frequency, caller):
        """在后台线程里切频道。

        on_connected 跑在 Qt 主线程上，而切频道要建频道、等服务器回
        ChannelState，是一次网络往返，最坏要等满 CHANNEL_TIMEOUT——在主线程上
        干这件事窗口会直接"未响应"。switch_channel 自己有锁，和监控线程同时
        进去也安全。
        """
        def work():
            try:
                self.radio_client.switch_channel(frequency, caller=caller)
            except Exception as e:
                log.error("%s 频道切换失败: %s", caller, e)
        threading.Thread(target=work, daemon=True).start()

    def on_connected(self):
        """Mumble 连接成功后，初始化主界面并启动后台线程。"""
        try:
            log.info("连接成功，正在初始化主窗口...")
            self.login_window.clear_error()

            try:
                if self.radio_client and self.radio_client.settings:
                    self.radio_client.settings.save_settings()
            except Exception as e:
                log.error("登录后保存设置失败: %s", e)

            if self.main_window:
                log.info("重连成功，更新主窗口状态")
                self.main_window.update_connection_status(True)
                # 重连时也切一次频道
                if self.radio_client and self.radio_client._initial_freq is not None:
                    self._switch_channel_async(
                        self.radio_client._initial_freq, "GUI-重连")
                return

            self.main_window = MainWindow(self.radio_client)
            self.stacked_widget.addWidget(self.main_window)
            self.stacked_widget.setCurrentWidget(self.main_window)
            self.main_window.update_connection_status(True)

            def on_ptt_change(is_talking):
                if self.main_window:
                    self.main_window.update_ptt_status(is_talking)
            self.radio_client.on_ptt_change = on_ptt_change

            def on_rx_change(is_receiving):
                if self.main_window:
                    self.main_window.update_rx_status(is_receiving)
            self.radio_client.on_rx_change = on_rx_change

            def on_connection_change(connected):
                if self.main_window:
                    self.main_window.update_connection_status(connected)
            self.radio_client.on_connection_change = on_connection_change

            # ★ 连接后立即切到初始频率（不等 monitor 线程第一次循环）
            initial_freq = self.radio_client._initial_freq
            if initial_freq is not None:
                log.info("连接成功，立即切换到频率 %.3f MHz", initial_freq)
                self._switch_channel_async(initial_freq, "GUI-on_connected")
            else:
                log.warning("无初始频率，跳过首次频道切换")

            # 启动监控和语音线程
            self.radio_client.monitor_thread = threading.Thread(
                target=self.radio_client.monitor_frequency, daemon=True,
            )
            self.radio_client.voice_thread = threading.Thread(
                target=self.radio_client.handle_voice, daemon=True,
            )
            self.radio_client.monitor_thread.start()
            self.radio_client.voice_thread.start()
            log.info("后台线程启动完成")
        except Exception as e:
            log.error("主窗口初始化失败: %s", e)
            self.login_window.show_error(f"初始化失败: {str(e)}")
            self.cleanup_client()

    def on_disconnected(self):
        log.warning("Mumble 连接断开")
        if self.main_window:
            self.main_window.update_connection_status(False)

    def handle_login(self):
        """处理登录按钮点击。"""
        log.info("开始登录流程")
        self.cleanup_client()

        username = self.login_window.username_input.text()
        password = self.login_window.password_input.text()

        try:
            self.settings.username = username or ""
            self.settings.password = password or ""

            # 先发现 X-Plane（在 UI 线程中同步完成）
            from radio import XPlaneRadio
            xplane_temp = XPlaneRadio()
            addr, freq = xplane_temp.find_and_read()
            if addr is None:
                self.login_window.show_error(
                    "未发现 X-Plane。\n"
                    "请确认：\n"
                    "  1. X-Plane 正在运行且已进入飞行\n"
                    "  2. 设置 → Data Output → IPs for UDP network 中已添加本机 IP"
                )
                return
            log.info("X-Plane 已发现 @ %s:%s，初始频率 %.3f MHz", addr[0], addr[1], freq)

            self.radio_client = MumbleRadioClient(
                "audio.airwaysn.org", username, password, settings=self.settings,
            )
            # 手动设置 X-Plane 地址和初始频率（避免重复发现）
            self.radio_client.xplane._addr = addr
            self.radio_client._initial_freq = freq
            log.info("保存初始频率: %.3f MHz", freq)

            # Mumble 连接回调：同步更新 radio_client 的独立连接标记
            self.radio_client.mumble.callbacks.set_callback(
                pymumble.constants.PYMUMBLE_CLBK_CONNECTED,
                lambda: (
                    self.radio_client.set_connection_state(True),
                    self.connection_signal.connected.emit(),
                ),
            )
            self.radio_client.mumble.callbacks.set_callback(
                pymumble.constants.PYMUMBLE_CLBK_DISCONNECTED,
                lambda: (
                    self.radio_client.set_connection_state(False),
                    self.connection_signal.disconnected.emit(),
                ),
            )

            def run_client():
                try:
                    self.radio_client.mumble.run()
                    self.radio_client.mumble.is_ready()
                    while self.radio_client and self.radio_client.mumble.is_alive():
                        time.sleep(1)
                except pymumble.errors.ConnectionRejectedError as e:
                    if "Wrong certificate or password" in str(e):
                        self.error_signal.error.emit("登录失败：用户名或密码错误")
                    else:
                        self.error_signal.error.emit(f"登录失败：{str(e)}")
                    self.cleanup_client()
                except Exception as e:
                    self.error_signal.error.emit(f"连接错误: {str(e)}")
                    self.cleanup_client()

            self.client_thread = threading.Thread(target=run_client, daemon=True)
            self.client_thread.start()
            log.info("客户端线程已启动")

        except Exception as e:
            log.error("初始化过程发生错误: %s", str(e))
            self.error_signal.error.emit(f"初始化失败: {str(e)}")
            self.cleanup_client()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RadioGUI()
    window.show()
    sys.exit(app.exec())
