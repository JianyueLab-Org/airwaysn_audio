"""管制员语音客户端。

界面按 TrackAudio 的电台栈组织：一个席位可以同时加多个频率，每个频率一行，各自
有 RX / TX / XC 开关和音量，按住 PTT 时对所有开了 TX 的频率一起发话。
"""

import sys
import time

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QPushButton, QLineEdit,
                             QStackedWidget, QFrame, QMessageBox, QSlider,
                             QScrollArea, QSizePolicy)
from pynput import keyboard

import radiostack
from radiostack import RadioStack
from settings import Settings, SettingsDialog
from voice import VoiceClient

icon_path = r".\favicon.ico"
SERVER = "hjdczy.top"


class VoiceSignals(QObject):
    """pymumble 的回调在库线程里跑，必须经信号回到界面线程。"""
    state = pyqtSignal(str, str)
    rx = pyqtSignal(int, bool, str)
    tx = pyqtSignal(bool)
    connection = pyqtSignal(bool)


class RadioRow(QFrame):
    """电台栈里的一行。"""

    def __init__(self, radio, window, parent=None):
        super().__init__(parent)
        self.khz = radio.frequency_khz
        self.window = window
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setup_ui(radio)

    def setup_ui(self, radio):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top.addWidget(self.title)

        self.rx_button = self._toggle("RX", lambda: self.window.toggle_rx(self.khz),
                                      "接收这个频率")
        self.tx_button = self._toggle("TX", lambda: self.window.toggle_tx(self.khz),
                                      "PTT 按下时对这个频率发话")
        self.xc_button = self._toggle("XC", lambda: self.window.toggle_xc(self.khz),
                                      "和其它开了 XC 的频率交叉耦合")
        for button in (self.rx_button, self.tx_button, self.xc_button):
            top.addWidget(button)

        self.remove_button = QPushButton("×")
        self.remove_button.setFixedWidth(28)
        self.remove_button.setToolTip("从电台栈移除")
        self.remove_button.clicked.connect(lambda: self.window.remove_radio(self.khz))
        top.addWidget(self.remove_button)
        layout.addLayout(top)

        bottom = QHBoxLayout()
        self.last_rx = QLabel("")
        self.last_rx.setStyleSheet("color: #666666;")
        self.last_rx.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bottom.addWidget(self.last_rx)

        self.mute_button = QPushButton("静音")
        self.mute_button.setCheckable(True)
        self.mute_button.setFixedWidth(52)
        self.mute_button.clicked.connect(
            lambda checked: self.window.set_muted(self.khz, checked))
        bottom.addWidget(self.mute_button)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(110)
        self.volume.valueChanged.connect(
            lambda value: self.window.set_volume(self.khz, value))
        bottom.addWidget(self.volume)
        layout.addLayout(bottom)

        self.refresh(radio)

    def _toggle(self, text, handler, tooltip):
        button = QPushButton(text)
        button.setCheckable(True)
        button.setFixedWidth(46)
        button.setToolTip(tooltip)
        button.clicked.connect(lambda: handler())
        return button

    def refresh(self, radio):
        selected = self.window.stack.selected_khz == radio.frequency_khz
        marker = "▸ " if selected else ""
        self.title.setText(f"{marker}{radio.label}")

        self.rx_button.setChecked(radio.rx)
        self.tx_button.setChecked(radio.tx)
        self.xc_button.setChecked(radio.xc)

        # 正在收/发的时候把按钮点亮，和 TrackAudio 一样一眼能看出哪个频率在响
        self.rx_button.setStyleSheet(self._style(radio.rx, radio.currently_rx, "#00cc00"))
        self.tx_button.setStyleSheet(self._style(radio.tx, radio.currently_tx, "#ff3b30"))
        self.xc_button.setStyleSheet(self._style(radio.xc, False, "#ff9500"))

        self.volume.blockSignals(True)
        self.volume.setValue(radio.volume)
        self.volume.blockSignals(False)
        self.mute_button.setChecked(radio.muted)

        if radio.last_received_callsign:
            stamp = time.strftime('%H:%M:%S', time.localtime(radio.last_received_at))
            self.last_rx.setText(f"最后通话: {radio.last_received_callsign}  {stamp}")
        else:
            self.last_rx.setText("最后通话: --")

        self.setStyleSheet("QFrame { border: 2px solid #1e90ff; border-radius: 4px; }"
                           if selected else "")

    @staticmethod
    def _style(enabled, active, color):
        if active:
            return f"background-color: {color}; color: white; font-weight: bold;"
        if enabled:
            return f"background-color: {color}; color: white;"
        return ""

    def mousePressEvent(self, event):
        # 点标题把这个频率设成主频率（真正进入的那个 Mumble 频道）
        self.window.select_radio(self.khz)
        super().mousePressEvent(event)


class ControllerWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon(icon_path))
        self.setWindowTitle('管制语音 - Airwaysn')
        self.setMinimumSize(520, 420)

        self.settings = Settings()
        self.voice = None
        self.rows = {}
        self.stack = RadioStack(on_change=self.on_stack_changed)

        self.signals = VoiceSignals()
        self.signals.state.connect(self.on_voice_state)
        self.signals.rx.connect(self.on_voice_rx)
        self.signals.tx.connect(self.on_voice_tx)
        self.signals.connection.connect(self.on_connection_change)

        self.setup_ui()
        self.setup_ptt()

        self.stack.load(self.settings.radios)

    # ---------- 界面 ----------
    def setup_ui(self):
        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)

        self.pages.addWidget(self.build_login_page())
        self.pages.addWidget(self.build_main_page())

        self.username_input.setText(self.settings.last_username)

    def build_login_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()

        for label, attr, echo in (('用户名:', 'username_input', False),
                                  ('密码:', 'password_input', True)):
            row = QHBoxLayout()
            field = QLineEdit()
            if echo:
                field.setEchoMode(QLineEdit.EchoMode.Password)
            setattr(self, attr, field)
            row.addWidget(QLabel(label))
            row.addWidget(field)
            layout.addLayout(row)

        self.connect_button = QPushButton('连接')
        self.connect_button.clicked.connect(self.connect_voice)
        layout.addWidget(self.connect_button)

        self.login_status = QLabel('未连接')
        self.login_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.login_status)
        layout.addStretch()
        return page

    def build_main_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        top = QHBoxLayout()
        # 连接状态指示灯：掉线要能一眼看出来，不然管制员会对着死掉的连接一直喊
        self.conn_indicator = QLabel()
        self.conn_indicator.setFixedSize(14, 14)
        self.conn_label = QLabel('已连接')
        self._set_connection_style(True)
        top.addWidget(self.conn_indicator)
        top.addWidget(self.conn_label)
        top.addSpacing(12)

        self.session_label = QLabel('未连接')
        self.session_label.setStyleSheet("font-weight: bold;")
        top.addWidget(self.session_label)
        top.addStretch()
        settings_button = QPushButton('设置')
        settings_button.clicked.connect(self.open_settings)
        top.addWidget(settings_button)
        disconnect_button = QPushButton('断开')
        disconnect_button.clicked.connect(self.disconnect_voice)
        top.addWidget(disconnect_button)
        layout.addLayout(top)

        add_row = QHBoxLayout()
        self.freq_input = QLineEdit()
        self.freq_input.setPlaceholderText('频率，例如 118.000')
        self.freq_input.returnPressed.connect(self.add_radio)
        self.callsign_input = QLineEdit()
        self.callsign_input.setPlaceholderText('呼号，例如 ZSPD_TWR（可留空）')
        add_button = QPushButton('添加频率')
        add_button.clicked.connect(self.add_radio)
        add_row.addWidget(self.freq_input)
        add_row.addWidget(self.callsign_input)
        add_row.addWidget(add_button)
        layout.addLayout(add_row)

        self.stack_area = QScrollArea()
        self.stack_area.setWidgetResizable(True)
        holder = QWidget()
        self.stack_layout = QVBoxLayout(holder)
        self.stack_layout.setSpacing(6)
        self.stack_layout.addStretch()
        self.stack_area.setWidget(holder)
        layout.addWidget(self.stack_area)

        self.empty_hint = QLabel('电台栈是空的，先在上面加一个频率')
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet("color: #888888;")
        layout.addWidget(self.empty_hint)

        bottom = QHBoxLayout()
        self.ptt_indicator = QLabel('PTT')
        self.ptt_indicator.setFixedWidth(44)
        self.ptt_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_ptt_style(False)
        bottom.addWidget(self.ptt_indicator)
        self.status_label = QLabel('就绪')
        self.status_label.setStyleSheet("color: #555555;")
        bottom.addWidget(self.status_label)
        bottom.addStretch()
        layout.addLayout(bottom)
        return page

    def _set_connection_style(self, connected):
        color = "#00cc00" if connected else "#cc0000"
        self.conn_indicator.setStyleSheet(
            f"background-color: {color}; border-radius: 7px;")
        self.conn_label.setText('已连接' if connected else '已断开')
        self.conn_label.setStyleSheet(f"color: {color};")

    def _set_ptt_style(self, active):
        color = "#ff3b30" if active else "#808080"
        self.ptt_indicator.setStyleSheet(
            f"background-color: {color}; color: white; border-radius: 4px; padding: 4px;")

    # ---------- 电台栈 ----------
    def on_stack_changed(self):
        """栈变了：重画界面、推给服务器、存盘。"""
        self.rebuild_rows()
        if self.voice:
            self.voice.sync(self.stack)
        self.settings.radios = self.stack.to_list()
        self.settings.save_settings()

    def rebuild_rows(self):
        current = {radio.frequency_khz for radio in self.stack}

        for khz in list(self.rows):
            if khz not in current:
                row = self.rows.pop(khz)
                self.stack_layout.removeWidget(row)
                row.deleteLater()

        for index, radio in enumerate(self.stack):
            row = self.rows.get(radio.frequency_khz)
            if row is None:
                row = RadioRow(radio, self)
                self.rows[radio.frequency_khz] = row
                self.stack_layout.insertWidget(index, row)
            else:
                row.refresh(radio)

        self.empty_hint.setVisible(len(self.stack) == 0)

    def add_radio(self):
        try:
            self.stack.add(self.freq_input.text(), self.callsign_input.text())
        except ValueError as e:
            QMessageBox.warning(self, '添加失败', str(e))
            return
        self.freq_input.clear()
        self.callsign_input.clear()

    def remove_radio(self, khz):
        self.stack.remove(khz)

    def select_radio(self, khz):
        self.stack.select(khz)

    def toggle_rx(self, khz):
        self.stack.toggle_rx(khz)

    def toggle_tx(self, khz):
        self.stack.toggle_tx(khz)

    def toggle_xc(self, khz):
        self.stack.toggle_xc(khz)

    def set_volume(self, khz, value):
        self.stack.set_volume(khz, value)

    def set_muted(self, khz, muted):
        self.stack.set_muted(khz, muted)

    # ---------- 连接 ----------
    def connect_voice(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        if not username or not password:
            self.login_status.setText('请填写用户名和密码')
            return

        self.connect_button.setEnabled(False)
        self.login_status.setText('正在连接…')
        QApplication.processEvents()

        self.voice = VoiceClient(
            SERVER, username, password,
            on_state=self.signals.state.emit,
            on_rx=self.signals.rx.emit,
            on_tx=self.signals.tx.emit,
            on_connection_change=self.signals.connection.emit)
        self.voice.set_mic_volume(self.settings.mic_volume)
        self.voice.set_speaker_volume(self.settings.speaker_volume)
        self.voice._input_device = self.settings.input_device_index
        self.voice._output_device = self.settings.output_device_index

        if not self.voice.connect():
            self.voice = None
            self.connect_button.setEnabled(True)
            return

        self.connect_button.setEnabled(True)
        self.settings.last_username = username
        self.settings.save_settings()
        self.session_label.setText(f'{username} · {SERVER}')
        self.pages.setCurrentIndex(1)
        self.voice.sync(self.stack)
        self.update_hint()

    def update_hint(self):
        """多频率接收要服务器支持频道监听（Mumble 1.4+）。

        不能靠"一段时间没收到声音"来判定不支持——频率上安静是常态，那样只会误报。
        这里只把前提讲清楚；真收到过非主频率的话音之后就不再提。
        """
        if not self.voice:
            return
        if len(self.stack.rx_frequencies()) > 1 and not self.voice.listeners_confirmed:
            self.status_label.setText(
                '多频率接收需要服务器支持频道监听（Mumble 1.4+）；'
                '若只听得到主频率（▸），说明服务器不支持')
        else:
            self.status_label.setText('就绪')

    def disconnect_voice(self):
        if self.voice:
            self.voice.disconnect()
            self.voice = None
        for radio in self.stack:
            radio.currently_rx = False
            radio.currently_tx = False
        self.rebuild_rows()
        self.pages.setCurrentIndex(0)
        self.login_status.setText('未连接')

    # ---------- 语音回调（已在界面线程） ----------
    def on_voice_state(self, state, message):
        if state == 'error':
            self.status_label.setText(message)
            self.login_status.setText(message)
            if self.pages.currentIndex() == 0:
                QMessageBox.critical(self, '连接失败', message)
        elif state == 'denied':
            # 服务器挡了某个动作（多半是监听频道的上限），红字留在状态栏
            self.status_label.setText(message)
            self.status_label.setStyleSheet("color: #cc0000;")
        else:
            self.status_label.setText(message)
            self.status_label.setStyleSheet("color: #555555;")

    def on_voice_rx(self, khz, active, callsign):
        self.stack.set_currently_rx(khz, active, callsign, time.time())
        if active:
            self.update_hint()

    def on_voice_tx(self, active):
        self._set_ptt_style(active)
        self.stack.set_currently_tx(active)

    def on_connection_change(self, connected):
        self._set_connection_style(connected)
        if not connected:
            # 掉线时把所有频率的收发状态灭掉，免得停在"正在通话"上
            for radio in self.stack:
                radio.currently_rx = False
                radio.currently_tx = False
            self.rebuild_rows()

    # ---------- PTT ----------
    def setup_ptt(self):
        self.ptt_listener = keyboard.Listener(
            on_press=self.on_key_press, on_release=self.on_key_release)
        self.ptt_listener.start()

    @staticmethod
    def _key_name(key):
        return key.char if hasattr(key, 'char') and key.char else getattr(key, 'name', str(key))

    def on_key_press(self, key):
        try:
            if self._key_name(key) == self.settings.ptt_key and self.voice:
                self.voice.start_transmit()
        except Exception:
            pass

    def on_key_release(self, key):
        try:
            if self._key_name(key) == self.settings.ptt_key and self.voice:
                self.voice.stop_transmit()
        except Exception:
            pass

    # ---------- 设置 ----------
    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            if hasattr(self, 'ptt_listener'):
                self.ptt_listener.stop()
            self.setup_ptt()
            if self.voice:
                self.voice.set_mic_volume(self.settings.mic_volume)
                self.voice.set_speaker_volume(self.settings.speaker_volume)
                try:
                    self.voice.setup_audio(self.settings.input_device_index,
                                           self.settings.output_device_index)
                except Exception as e:
                    QMessageBox.warning(self, '音频设备', f'切换设备失败: {e}')

    def closeEvent(self, event):
        try:
            if hasattr(self, 'ptt_listener'):
                self.ptt_listener.stop()
            if self.voice:
                self.voice.disconnect()
        except Exception as e:
            print(f"关闭时出错: {e}")
        finally:
            event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(icon_path))
    window = ControllerWindow()
    window.show()
    sys.exit(app.exec())
