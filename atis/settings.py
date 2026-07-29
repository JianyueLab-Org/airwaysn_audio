import logging
import json
import os

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QLineEdit, QCheckBox, QComboBox)

import applog
import datafeed
import weather

log = logging.getLogger("设置")

DEFAULT_FSD_HOST = "fsd.airwaysn.org"
DEFAULT_FSD_PORT = 6809

# 早期版本把 FSD 主机错填成了语音服务器的地址。那台机器上没有 FSD，
# 留着只会一直连不上，所以读配置时直接换掉。
WRONG_FSD_HOSTS = {"hjdczy.top"}

# 自动刷新天气的间隔（秒）
DEFAULT_METAR_REFRESH = 300
MIN_METAR_REFRESH = 60
MAX_METAR_REFRESH = 3600


def clamp_refresh(value):
    """刷新间隔夹到合理范围。

    配置被改成 0 或负数会让定时器疯转，把气象源打死然后被封；改成一天一次又
    等于没有自动更新。两头都夹住。
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_METAR_REFRESH
    return max(MIN_METAR_REFRESH, min(MAX_METAR_REFRESH, seconds))


class Settings:
    def __init__(self):
        self.config_file = "atis_settings.json"
        self.cid = ""
        self.metar_url = weather.DEFAULT_METAR_URL
        # FSD 服务端：席位靠它出现在网络上，气象也从它那里要。
        # 和语音服务器（hjdczy.top:64738）不是同一台。
        self.fsd_host = DEFAULT_FSD_HOST
        self.fsd_port = DEFAULT_FSD_PORT
        self.real_name = ""
        self.connect_fsd = True
        # FSD 登录用的等级。0 表示自动：登录前从数据源查本人此刻的等级，
        # 这样通播和管制席位显示的等级一致；查不到就退回 OBS。
        self.rating = 0
        self.datafeed_url = datafeed.DEFAULT_DATAFEED_URL
        # 自动刷新天气的间隔（秒）。METAR 半小时一发，5 分钟查一次足够及时，
        # 又不至于把气象源打太狠。夹在 1 分钟到 1 小时之间。
        self.metar_refresh = DEFAULT_METAR_REFRESH
        # 精简模式：只留席位列表。值班时窗口压在别的东西旁边用，要记住
        self.compact = False
        # 窗口置顶。和精简是一对，同样要记住
        self.always_on_top = False
        self.debug = False
        self.load_settings()

    def load_settings(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.cid = data.get("cid", "")
                self.metar_url = data.get("metar_url") or weather.DEFAULT_METAR_URL
                self.fsd_host = data.get("fsd_host") or DEFAULT_FSD_HOST
                if self.fsd_host in WRONG_FSD_HOSTS:
                    log.warning("配置里的 FSD 地址 %s 是语音服务器，已改为 %s",
                                self.fsd_host, DEFAULT_FSD_HOST)
                    self.fsd_host = DEFAULT_FSD_HOST
                self.fsd_port = int(data.get("fsd_port") or DEFAULT_FSD_PORT)
                self.real_name = data.get("real_name", "")
                self.connect_fsd = bool(data.get("connect_fsd", True))
                self.rating = int(data.get("rating") or 0)
                self.datafeed_url = (data.get("datafeed_url")
                                     or datafeed.DEFAULT_DATAFEED_URL)
                self.metar_refresh = clamp_refresh(
                    data.get("metar_refresh", DEFAULT_METAR_REFRESH))
                self.compact = bool(data.get("compact", False))
                self.always_on_top = bool(data.get("always_on_top", False))
                self.debug = bool(data.get("debug", False))
        except Exception as e:
            log.warning(f"加载设置失败: {e}")

    def save_settings(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({
                    "cid": self.cid,
                    "metar_url": self.metar_url,
                    "fsd_host": self.fsd_host,
                    "fsd_port": self.fsd_port,
                    "real_name": self.real_name,
                    "connect_fsd": self.connect_fsd,
                    "rating": self.rating,
                    "datafeed_url": self.datafeed_url,
                    "metar_refresh": self.metar_refresh,
                    "compact": self.compact,
                    "always_on_top": self.always_on_top,
                    "debug": self.debug,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"保存设置失败: {e}")


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        self.connect_fsd = QCheckBox("播出时同时登录 FSD"
                                     "（席位出现在在线列表，并回答文字通播查询）")
        self.connect_fsd.setChecked(self.settings.connect_fsd)
        layout.addWidget(self.connect_fsd)

        fsd_row = QHBoxLayout()
        self.fsd_host_input = QLineEdit(self.settings.fsd_host)
        self.fsd_port_input = QLineEdit(str(self.settings.fsd_port))
        self.fsd_port_input.setFixedWidth(70)
        fsd_row.addWidget(QLabel("FSD 服务器:"))
        fsd_row.addWidget(self.fsd_host_input)
        fsd_row.addWidget(QLabel("端口:"))
        fsd_row.addWidget(self.fsd_port_input)
        layout.addLayout(fsd_row)

        refresh_row = QHBoxLayout()
        self.refresh_input = QComboBox()
        for seconds, label in ((60, "1 分钟"), (120, "2 分钟"), (300, "5 分钟"),
                               (600, "10 分钟"), (900, "15 分钟"),
                               (1800, "30 分钟"), (3600, "1 小时")):
            self.refresh_input.addItem(label, seconds)
        index = self.refresh_input.findData(
            clamp_refresh(getattr(self.settings, "metar_refresh",
                                  DEFAULT_METAR_REFRESH)))
        self.refresh_input.setCurrentIndex(index if index >= 0 else 2)
        refresh_row.addWidget(QLabel("天气自动刷新:"))
        refresh_row.addWidget(self.refresh_input)
        refresh_row.addWidget(QLabel("（报文变化时自动推进情报字母并换稿）"))
        refresh_row.addStretch()
        layout.addLayout(refresh_row)

        rating_row = QHBoxLayout()
        self.rating_input = QComboBox()
        for value, label in ((0, "自动（按 CID 从数据源获取）"),
                             (1, "OBS 观察员"), (2, "S1"), (3, "S2"), (4, "S3"),
                             (5, "C1"), (7, "C3"), (8, "I1"), (10, "I3")):
            self.rating_input.addItem(label if value == 0 else f"{value}  {label}",
                                      value)
        index = self.rating_input.findData(self.settings.rating)
        self.rating_input.setCurrentIndex(index if index >= 0 else 0)
        rating_row.addWidget(QLabel("登录等级:"))
        rating_row.addWidget(self.rating_input)
        rating_row.addWidget(QLabel("（不能高于本人实际等级）"))
        layout.addLayout(rating_row)

        name_row = QHBoxLayout()
        self.real_name_input = QLineEdit(self.settings.real_name)
        self.real_name_input.setPlaceholderText("登录 FSD 时显示的姓名")
        name_row.addWidget(QLabel("真实姓名:"))
        name_row.addWidget(self.real_name_input)
        layout.addLayout(name_row)

        row = QHBoxLayout()
        self.metar_input = QLineEdit(self.settings.metar_url)
        self.metar_input.setPlaceholderText(weather.DEFAULT_METAR_URL)
        row.addWidget(QLabel("备用气象源:"))
        row.addWidget(self.metar_input)
        layout.addLayout(row)

        hint = QLabel("登录 FSD 之后天气直接向自己的服务器要（$AX）；这个地址只在"
                      "还没连上 FSD 时用来预览，机场代码会直接拼在后面。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777777;")
        layout.addWidget(hint)

        # 日志：出问题时让用户能一键找到文件，而不是去解释路径
        log_row = QHBoxLayout()
        self.debug_checkbox = QCheckBox("记录调试信息（重启后生效）")
        self.debug_checkbox.setChecked(self.settings.debug)
        self.debug_checkbox.setToolTip("打开后连 FSD 收发的每个包都会记下来")
        open_log = QPushButton("打开日志")
        open_log.clicked.connect(lambda: applog.open_log_folder())
        log_row.addWidget(self.debug_checkbox)
        log_row.addWidget(open_log)
        layout.addLayout(log_row)

        buttons = QHBoxLayout()
        save = QPushButton("保存")
        save.clicked.connect(self.save_and_close)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def save_and_close(self):
        self.settings.metar_url = (self.metar_input.text().strip()
                                   or weather.DEFAULT_METAR_URL)
        self.settings.metar_refresh = clamp_refresh(
            self.refresh_input.currentData())
        self.settings.connect_fsd = self.connect_fsd.isChecked()
        self.settings.fsd_host = self.fsd_host_input.text().strip() or DEFAULT_FSD_HOST
        try:
            self.settings.fsd_port = int(self.fsd_port_input.text().strip())
        except ValueError:
            self.settings.fsd_port = DEFAULT_FSD_PORT
        self.settings.real_name = self.real_name_input.text().strip()
        self.settings.rating = self.rating_input.currentData()
        self.settings.debug = self.debug_checkbox.isChecked()
        self.settings.save_settings()
        self.accept()
