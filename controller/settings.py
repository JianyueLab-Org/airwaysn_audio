import json
import os

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QSlider, QLineEdit, QComboBox)
from PyQt6.QtCore import Qt
from pynput import keyboard


class Settings:
    def __init__(self):
        self.config_file = "radio_settings.json"
        self.ptt_key = "v"
        self.mic_volume = 100
        self.speaker_volume = 100
        self.input_device_index = None
        self.output_device_index = None
        self.last_username = ""
        # 电台栈：上次用的那组频率，下次启动接着用
        self.radios = []
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
                    self.radios = data.get("radios", [])
        except Exception as e:
            print(f"加载设置失败: {e}")

    def save_settings(self):
        try:
            data = {
                "ptt_key": self.ptt_key,
                "mic_volume": self.mic_volume,
                "speaker_volume": self.speaker_volume,
                "input_device_index": self.input_device_index,
                "output_device_index": self.output_device_index,
                "last_username": self.last_username,
                "radios": self.radios,
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print(f"保存设置失败: {e}")


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.listening_for_key = False
        self.keyboard_listener = None
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()

        volume_label = QLabel("音量")
        volume_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(volume_label)

        mic_layout = QHBoxLayout()
        self.mic_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_slider.setRange(0, 200)
        self.mic_slider.setValue(self.settings.mic_volume)
        self.mic_value = QLabel(f"{self.settings.mic_volume}%")
        self.mic_slider.valueChanged.connect(lambda v: self.mic_value.setText(f"{v}%"))
        mic_layout.addWidget(QLabel("麦克风:"))
        mic_layout.addWidget(self.mic_slider)
        mic_layout.addWidget(self.mic_value)
        layout.addLayout(mic_layout)

        speaker_layout = QHBoxLayout()
        self.speaker_slider = QSlider(Qt.Orientation.Horizontal)
        self.speaker_slider.setRange(0, 200)
        self.speaker_slider.setValue(self.settings.speaker_volume)
        self.speaker_value = QLabel(f"{self.settings.speaker_volume}%")
        self.speaker_slider.valueChanged.connect(lambda v: self.speaker_value.setText(f"{v}%"))
        speaker_layout.addWidget(QLabel("主音量:"))
        speaker_layout.addWidget(self.speaker_slider)
        speaker_layout.addWidget(self.speaker_value)
        layout.addLayout(speaker_layout)

        ptt_layout = QHBoxLayout()
        self.ptt_input = QLineEdit(self.settings.ptt_key)
        self.ptt_input.setReadOnly(True)
        self.ptt_reset_btn = QPushButton("重设")
        self.ptt_reset_btn.clicked.connect(self.start_key_capture)
        ptt_layout.addWidget(QLabel("PTT 按键:"))
        ptt_layout.addWidget(self.ptt_input)
        ptt_layout.addWidget(self.ptt_reset_btn)
        layout.addLayout(ptt_layout)

        input_layout = QHBoxLayout()
        self.input_combo = QComboBox()
        self.populate_audio_devices(self.input_combo, True)
        input_layout.addWidget(QLabel("输入设备:"))
        input_layout.addWidget(self.input_combo)
        layout.addLayout(input_layout)

        output_layout = QHBoxLayout()
        self.output_combo = QComboBox()
        self.populate_audio_devices(self.output_combo, False)
        output_layout.addWidget(QLabel("输出设备:"))
        output_layout.addWidget(self.output_combo)
        layout.addLayout(output_layout)

        button_layout = QHBoxLayout()
        save_button = QPushButton("保存")
        save_button.clicked.connect(self.save_and_close)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)

        layout.addStretch()
        self.setLayout(layout)

    def populate_audio_devices(self, combo_box, is_input):
        import pyaudio
        p = pyaudio.PyAudio()
        combo_box.addItem("系统默认", None)
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
        self.ptt_input.setText("请按下按键...")
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
        self.settings.save_settings()
        self.accept()
