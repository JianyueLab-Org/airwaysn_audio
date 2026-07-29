import json
import logging
import os

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QSlider, QLineEdit, QComboBox,
                             QCheckBox)
from PyQt6.QtCore import Qt
from pynput import keyboard

import applog
import i18n
import version
from i18n import t

log = logging.getLogger("设置")


class Settings:
    def __init__(self):
        self.config_file = "radio_settings.json"
        self.ptt_key = "v"
        self.mic_volume = 100
        self.speaker_volume = 100
        self.input_device_index = None
        self.output_device_index = None
        self.last_username = ""
        # 电台栈**不存**。频率该从数据源来：上了席位的自动加，别人的席位在
        # "在线频率"里点。留着上一场的频率反而危险——那些临时频道多半早就没
        # 人了，屏幕上却看起来一切正常。老配置里的 radios 键读到也直接忽略。
        # 窗口置顶。管制员多半把语音压在雷达/模拟器上面用，这个开关要能记住
        self.always_on_top = False
        # 精简模式：只留电台卡片。和置顶是一对，同样要记住
        self.compact = False
        # 界面语言。空字符串表示"还没选过"，第一次启动跟系统走
        self.language = ""
        self.debug = False
        self.load_settings()

    def load_settings(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.ptt_key = data.get("ptt_key", "v")
                    self.mic_volume = data.get("mic_volume", 100)
                    self.speaker_volume = data.get("speaker_volume", 100)
                    self.input_device_index = data.get("input_device_index", None)
                    self.output_device_index = data.get("output_device_index", None)
                    self.last_username = data.get("last_username", "")
                    self.always_on_top = bool(data.get("always_on_top", False))
                    self.compact = bool(data.get("compact", False))
                    self.language = data.get("language", "") or ""
                    self.debug = bool(data.get("debug", False))
        except Exception as e:
            log.warning(f"加载设置失败: {e}")

    def save_settings(self):
        try:
            data = {
                "ptt_key": self.ptt_key,
                "mic_volume": self.mic_volume,
                "speaker_volume": self.speaker_volume,
                "input_device_index": self.input_device_index,
                "output_device_index": self.output_device_index,
                "last_username": self.last_username,
                "always_on_top": self.always_on_top,
                "compact": self.compact,
                "language": self.language,
                "debug": self.debug,
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"保存设置失败: {e}")


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(t("settings.title"))
        self.listening_for_key = False
        self.keyboard_listener = None
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()

        volume_label = QLabel(t("settings.volume"))
        volume_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(volume_label)

        mic_layout = QHBoxLayout()
        self.mic_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_slider.setRange(0, 200)
        self.mic_slider.setValue(self.settings.mic_volume)
        self.mic_value = QLabel(f"{self.settings.mic_volume}%")
        self.mic_slider.valueChanged.connect(lambda v: self.mic_value.setText(f"{v}%"))
        mic_layout.addWidget(QLabel(t("settings.mic")))
        mic_layout.addWidget(self.mic_slider)
        mic_layout.addWidget(self.mic_value)
        layout.addLayout(mic_layout)

        speaker_layout = QHBoxLayout()
        self.speaker_slider = QSlider(Qt.Orientation.Horizontal)
        self.speaker_slider.setRange(0, 200)
        self.speaker_slider.setValue(self.settings.speaker_volume)
        self.speaker_value = QLabel(f"{self.settings.speaker_volume}%")
        self.speaker_slider.valueChanged.connect(lambda v: self.speaker_value.setText(f"{v}%"))
        speaker_layout.addWidget(QLabel(t("settings.speaker")))
        speaker_layout.addWidget(self.speaker_slider)
        speaker_layout.addWidget(self.speaker_value)
        layout.addLayout(speaker_layout)

        ptt_layout = QHBoxLayout()
        self.ptt_input = QLineEdit(self.settings.ptt_key)
        self.ptt_input.setReadOnly(True)
        self.ptt_reset_btn = QPushButton(t("settings.ptt_reset"))
        self.ptt_reset_btn.clicked.connect(self.start_key_capture)
        ptt_layout.addWidget(QLabel(t("settings.ptt_key")))
        ptt_layout.addWidget(self.ptt_input)
        ptt_layout.addWidget(self.ptt_reset_btn)
        layout.addLayout(ptt_layout)

        language_layout = QHBoxLayout()
        self.language_combo = QComboBox()
        for code, name in i18n.available().items():
            self.language_combo.addItem(name, code)
        index = self.language_combo.findData(i18n.current())
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        language_layout.addWidget(QLabel(t("settings.language")))
        language_layout.addWidget(self.language_combo)
        layout.addLayout(language_layout)

        input_layout = QHBoxLayout()
        self.input_combo = QComboBox()
        self.populate_audio_devices(self.input_combo, True)
        input_layout.addWidget(QLabel(t("settings.input")))
        input_layout.addWidget(self.input_combo)
        layout.addLayout(input_layout)

        output_layout = QHBoxLayout()
        self.output_combo = QComboBox()
        self.populate_audio_devices(self.output_combo, False)
        output_layout.addWidget(QLabel(t("settings.output")))
        output_layout.addWidget(self.output_combo)
        layout.addLayout(output_layout)

        # 日志：出问题时让用户能一键找到文件，而不是去解释路径
        log_layout = QHBoxLayout()
        self.debug_checkbox = QCheckBox(t("settings.debug"))
        self.debug_checkbox.setChecked(self.settings.debug)
        self.debug_checkbox.setToolTip(t("settings.debug_tip"))
        open_log = QPushButton(t("settings.open_log"))
        open_log.clicked.connect(lambda: applog.open_log_folder())
        log_layout.addWidget(self.debug_checkbox)
        log_layout.addWidget(open_log)
        layout.addLayout(log_layout)

        # 连上之后登录页就看不见了，版本号在这里再露一次
        version_label = QLabel(version.full())
        version_label.setStyleSheet("color: #808080;")
        layout.addWidget(version_label)

        button_layout = QHBoxLayout()
        save_button = QPushButton(t("settings.save"))
        save_button.clicked.connect(self.save_and_close)
        cancel_button = QPushButton(t("settings.cancel"))
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)

        layout.addStretch()
        self.setLayout(layout)

    def populate_audio_devices(self, combo_box, is_input):
        import pyaudio
        p = pyaudio.PyAudio()
        combo_box.addItem(t("settings.system_default"), None)
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            channels = 'maxInputChannels' if is_input else 'maxOutputChannels'
            if info.get(channels):
                combo_box.addItem(info.get('name'), i)
        current = self.settings.input_device_index if is_input else self.settings.output_device_index
        if current is not None:
            index = combo_box.findData(current)
            if index >= 0:
                combo_box.setCurrentIndex(index)
        p.terminate()

    def start_key_capture(self):
        self.listening_for_key = True
        self.ptt_input.setText(t("settings.ptt_press"))
        self.ptt_reset_btn.setEnabled(False)

        def on_press(key):
            if not self.listening_for_key:
                return
            name = key.char if hasattr(key, 'char') and key.char else getattr(key, 'name', str(key))
            self.ptt_input.setText(name)
            self.listening_for_key = False
            self.ptt_reset_btn.setEnabled(True)
            self.cleanup()

        self.keyboard_listener = keyboard.Listener(on_press=on_press)
        self.keyboard_listener.start()

    def cleanup(self):
        if self.keyboard_listener:
            self.keyboard_listener.stop()
            self.keyboard_listener = None

    def reject(self):
        self.cleanup()
        super().reject()

    def accept(self):
        self.cleanup()
        super().accept()

    def save_and_close(self):
        self.settings.ptt_key = self.ptt_input.text() or "v"
        self.settings.mic_volume = self.mic_slider.value()
        self.settings.speaker_volume = self.speaker_slider.value()
        self.settings.input_device_index = self.input_combo.currentData()
        self.settings.output_device_index = self.output_combo.currentData()
        self.settings.debug = self.debug_checkbox.isChecked()
        self.settings.language = self.language_combo.currentData()
        i18n.set_language(self.settings.language)
        self.settings.save_settings()
        self.accept()
