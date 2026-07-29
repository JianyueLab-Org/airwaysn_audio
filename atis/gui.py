"""情报通播客户端。

界面按 vATIS 组织：左边是席位列表，选中一个席位后可以挑预设、看生成出来的
文字通播和语音稿，然后连上语音服务器循环播出。

    席位 (Station)   一个机场的通播：ICAO、频率、类型、情报字母范围
    预设 (Preset)    一份模板 + 机场条件 + NOTAM，随跑道构型切换
    模板             [WIND] [CLOUDS] 这类变量，生成时替换成实际天气

天气来自 METAR，报文变了就自动推进一格情报字母。
"""

import logging
import os
import sys
import threading

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QPushButton, QLineEdit,
                             QListWidget, QListWidgetItem, QComboBox, QTextEdit,
                             QMessageBox, QDialog, QSplitter, QFileDialog)

import airports
import applog
import version
import chinese
import datafeed
import metar as metar_module
import template as template_module
import fsdclient
import vatis_import
import weather
from broadcast import Broadcaster
import profile as profile_module
from profile import (LANGUAGES, LANGUAGE_ENGLISH, Profile, Station,
                     TYPE_LABELS, TYPE_SUFFIX)
import settings as settings_module
from settings import Settings, SettingsDialog

log = logging.getLogger("界面")

# 语音（Mumble）服务器。FSD 是另一台，地址在设置里（fsd.airwaysn.org:6809）。
SERVER = "hjdczy.top"


def resource_path(name):
    """找随程序一起分发的资源。

    打包之后当前目录是用户双击时所在的目录，不是程序目录，用相对路径取图标
    会取不到（Qt 不会报错，只是默默用默认图标）。PyInstaller 把 datas 解到
    sys._MEIPASS。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


icon_path = resource_path("favicon.ico")


class AtisSignals(QObject):
    """后台线程 → 界面线程。"""
    state = pyqtSignal(str, str, str)      # callsign, state, message
    metar = pyqtSignal(str, object, str)   # callsign, Metar 或 None, 错误
    # 开播前的核对结果：callsign, cid, password, 数据源是否可达, 管制席位, 等级
    precheck = pyqtSignal(str, str, str, bool, object, int)


class StationDialog(QDialog):
    """新建 / 编辑一个通播席位。"""

    def __init__(self, station=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('通播席位')
        self.station = station
        self.setup_ui()
        if station:
            self.identifier.setText(station.identifier)
            self.name.setText(station.name)
            self.frequency.setText(station.frequency)
            self.atis_type.setCurrentIndex(
                list(TYPE_SUFFIX).index(station.atis_type))
            self.range_start.setText(station.code_range[0])
            self.range_end.setText(station.code_range[1])
            if station.latitude or station.longitude:
                self.latitude.setText(f"{station.latitude:.5f}")
                self.longitude.setText(f"{station.longitude:.5f}")
            index = self.language.findData(
                getattr(station, "voice_language", LANGUAGE_ENGLISH))
            self.language.setCurrentIndex(max(0, index))
            self.chinese_name.setText(getattr(station, "chinese_name", ""))
            self.chinese_runway.setText(getattr(station, "chinese_runway", ""))

    def setup_ui(self):
        layout = QVBoxLayout(self)

        self.identifier = QLineEdit()
        self.identifier.setPlaceholderText('机场 ICAO，例如 ZSPD')
        self.name = QLineEdit()
        self.name.setPlaceholderText('机场名称（可留空）')
        self.frequency = QLineEdit()
        self.frequency.setPlaceholderText('频率，例如 127.850')

        self.atis_type = QComboBox()
        for key in TYPE_SUFFIX:
            self.atis_type.addItem(f"{TYPE_LABELS[key]}  {TYPE_SUFFIX[key]}", key)

        self.language = QComboBox()
        for key, label in LANGUAGES.items():
            self.language.addItem(label, key)

        self.chinese_name = QLineEdit()
        self.chinese_name.setPlaceholderText('中文稿里念的机场名，例如 上海浦东')
        self.chinese_runway = QLineEdit()
        self.chinese_runway.setPlaceholderText('中文稿里念的跑道，例如 三六左')

        for label, widget in (('机场:', self.identifier), ('名称:', self.name),
                              ('频率:', self.frequency), ('类型:', self.atis_type),
                              ('语音:', self.language),
                              ('中文名:', self.chinese_name),
                              ('中文跑道:', self.chinese_runway)):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(widget)
            layout.addLayout(row)

        position_row = QHBoxLayout()
        self.latitude = QLineEdit()
        self.latitude.setPlaceholderText('纬度，例如 31.14340')
        self.longitude = QLineEdit()
        self.longitude.setPlaceholderText('经度，例如 121.80500')
        position_row.addWidget(QLabel('席位位置:'))
        position_row.addWidget(self.latitude)
        position_row.addWidget(self.longitude)
        layout.addLayout(position_row)

        # 输入机场代码时自动带出坐标，省得手查
        self.identifier.textChanged.connect(self.fill_position_from_airport)

        position_hint = QLabel('留空会按机场代码自动填。位置决定席位在雷达图上的位置。')
        position_hint.setStyleSheet("color: #777777;")
        position_hint.setWordWrap(True)
        layout.addWidget(position_hint)

        range_row = QHBoxLayout()
        self.range_start = QLineEdit('A')
        self.range_start.setFixedWidth(40)
        self.range_end = QLineEdit('Z')
        self.range_end.setFixedWidth(40)
        range_row.addWidget(QLabel('情报字母范围:'))
        range_row.addWidget(self.range_start)
        range_row.addWidget(QLabel('到'))
        range_row.addWidget(self.range_end)
        range_row.addStretch()
        layout.addLayout(range_row)

        hint = QLabel('离场和进场分别用不同字母段，飞行员就不会把两份通播搞混。')
        hint.setStyleSheet("color: #777777;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        ok = QPushButton('确定')
        ok.clicked.connect(self.validate_and_accept)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def fill_position_from_airport(self, text):
        """机场代码填完整了就自动带出坐标；用户已经手填过的不覆盖。"""
        if self.latitude.text().strip() or self.longitude.text().strip():
            return
        found = airports.coordinates(text)
        if found:
            self.latitude.setText(f"{found[0]:.5f}")
            self.longitude.setText(f"{found[1]:.5f}")

    def validate_and_accept(self):
        import re
        if not re.match(r'^[A-Za-z]{4}$', self.identifier.text().strip()):
            QMessageBox.warning(self, '输入错误', '机场代码要是 4 位 ICAO 代码')
            return
        try:
            khz = int(round(float(self.frequency.text().strip()) * 1000))
            if not 100000 <= khz <= 199999:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, '输入错误', '频率格式无效，例如 127.850')
            return
        self.accept()

    def build(self):
        def coordinate(widget):
            try:
                return float(widget.text().strip())
            except ValueError:
                return 0.0

        return Station(
            self.identifier.text().strip(),
            self.name.text().strip(),
            self.frequency.text().strip(),
            self.atis_type.currentData(),
            (self.range_start.text().strip().upper()[:1] or 'A',
             self.range_end.text().strip().upper()[:1] or 'Z'),
            self.station.presets if self.station else None,
            self.station.contractions if self.station else None,
            coordinate(self.latitude),
            coordinate(self.longitude),
            self.language.currentData(),
            self.chinese_name.text().strip(),
            self.chinese_runway.text().strip(),
        )


class AtisWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon(icon_path))
        self.setWindowTitle(f'情报通播 - Airwaysn {version.full()}')
        self.setMinimumSize(900, 600)

        self.settings = Settings()
        self.profile = Profile()
        self.broadcasters = {}          # callsign -> Broadcaster（语音）
        self.fsd_clients = {}           # callsign -> FSDClient（网络在线与文字通播）
        self.metars = {}                # callsign -> Metar
        self.raw_metars = {}            # callsign -> 原始电码，用来判断变没变
        self._weather_errors = {}       # callsign -> 还没恢复的取天气错误

        self.signals = AtisSignals()
        self.signals.state.connect(self.on_broadcast_state)
        self.signals.metar.connect(self.on_metar)
        self.signals.precheck.connect(self.on_precheck)

        self.setup_ui()
        self.refresh_stations()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all_metars)
        self.apply_refresh_interval()

    # ---------- 界面 ----------
    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        top = QHBoxLayout()
        self.cid_input = QLineEdit(self.settings.cid)
        self.cid_input.setPlaceholderText('用户名')
        self.cid_input.setFixedWidth(110)
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText('密码')
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setFixedWidth(140)
        top.addWidget(QLabel('账号:'))
        top.addWidget(self.cid_input)
        top.addWidget(self.password_input)
        top.addStretch()
        settings_button = QPushButton('设置')
        settings_button.clicked.connect(self.open_settings)
        top.addWidget(settings_button)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：席位列表
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel('通播席位'))
        self.station_list = QListWidget()
        self.station_list.currentItemChanged.connect(lambda *_: self.on_station_selected())
        left_layout.addWidget(self.station_list)

        buttons = QHBoxLayout()
        for text, handler in (('新建', self.add_station), ('编辑', self.edit_station),
                              ('删除', self.remove_station)):
            button = QPushButton(text)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        left_layout.addLayout(buttons)

        import_button = QPushButton('导入 vATIS 配置…')
        import_button.setToolTip('读取 vATIS 的 profile JSON，把里面的席位和预设导进来')
        import_button.clicked.connect(self.import_vatis)
        left_layout.addWidget(import_button)
        splitter.addWidget(left)

        # 右：预设与通播内容
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(lambda *_: self.regenerate())
        preset_row.addWidget(QLabel('预设:'))
        preset_row.addWidget(self.preset_combo, 1)
        edit_preset = QPushButton('编辑预设')
        edit_preset.clicked.connect(self.edit_preset)
        preset_row.addWidget(edit_preset)
        right_layout.addLayout(preset_row)

        letter_row = QHBoxLayout()
        self.letter_label = QLabel('情报字母: -')
        self.letter_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        letter_row.addWidget(self.letter_label)
        letter_row.addStretch()
        advance = QPushButton('推进字母')
        advance.clicked.connect(self.advance_letter)
        letter_row.addWidget(advance)
        fetch = QPushButton('刷新天气')
        fetch.clicked.connect(lambda: self.refresh_metar(self.current_station()))
        letter_row.addWidget(fetch)
        right_layout.addLayout(letter_row)

        self.metar_label = QLabel('METAR: --')
        self.metar_label.setWordWrap(True)
        self.metar_label.setStyleSheet("color: #555555; font-family: Consolas, monospace;")
        right_layout.addWidget(self.metar_label)

        right_layout.addWidget(QLabel('文字通播'))
        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setFixedHeight(90)
        right_layout.addWidget(self.text_preview)

        right_layout.addWidget(QLabel('语音稿'))
        self.voice_preview = QTextEdit()
        self.voice_preview.setReadOnly(True)
        right_layout.addWidget(self.voice_preview)

        broadcast_row = QHBoxLayout()
        self.broadcast_button = QPushButton('开始播出')
        self.broadcast_button.clicked.connect(self.toggle_broadcast)
        broadcast_row.addWidget(self.broadcast_button)
        self.status_label = QLabel('未播出')
        self.status_label.setStyleSheet("color: #555555;")
        broadcast_row.addWidget(self.status_label, 1)
        right_layout.addLayout(broadcast_row)

        splitter.addWidget(right)
        splitter.setSizes([260, 640])
        layout.addWidget(splitter)

    # ---------- 席位 ----------
    def current_station(self):
        item = self.station_list.currentItem()
        return self.profile.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def refresh_stations(self):
        selected = self.station_list.currentItem()
        wanted = selected.data(Qt.ItemDataRole.UserRole) if selected else None

        self.station_list.clear()
        for station in self.profile:
            item = QListWidgetItem(station.label)
            item.setData(Qt.ItemDataRole.UserRole, station.callsign)
            if station.callsign in self.broadcasters:
                item.setText(f"● {station.label}")
            self.station_list.addItem(item)
            if station.callsign == wanted:
                self.station_list.setCurrentItem(item)

        if self.station_list.currentRow() < 0 and self.station_list.count():
            self.station_list.setCurrentRow(0)
        self.on_station_selected()

    def on_station_selected(self):
        station = self.current_station()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        if station:
            for preset in station.presets:
                self.preset_combo.addItem(preset.name)
        self.preset_combo.blockSignals(False)

        broadcasting = bool(station and station.callsign in self.broadcasters)
        self.broadcast_button.setText('停止播出' if broadcasting else '开始播出')
        self.broadcast_button.setEnabled(station is not None)
        self.regenerate()

        if station and station.callsign not in self.metars:
            self.refresh_metar(station)

    def add_station(self):
        dialog = StationDialog(parent=self)
        if not dialog.exec():
            return
        try:
            station = self.profile.add(dialog.build())
        except ValueError as e:
            QMessageBox.warning(self, '添加失败', str(e))
            return
        self.profile.save()
        self.refresh_stations()
        self.refresh_metar(station)

    def edit_station(self):
        station = self.current_station()
        if not station:
            return
        dialog = StationDialog(station, parent=self)
        if not dialog.exec():
            return
        updated = dialog.build()
        if updated.callsign != station.callsign and self.profile.get(updated.callsign):
            QMessageBox.warning(self, '修改失败', f'{updated.callsign} 已经存在了')
            return
        updated.letter = station.letter
        self.profile.remove(station.callsign)
        self.profile.add(updated)
        self.profile.save()
        self.refresh_stations()

    def import_vatis(self):
        """从 vATIS 的 profile 里导入席位。"""
        path, _ = QFileDialog.getOpenFileName(
            self, '选择 vATIS 配置文件', '', 'vATIS 配置 (*.json);;所有文件 (*)')
        if not path:
            return

        try:
            profile_name, stations, notes = vatis_import.load_profile(path)
        except vatis_import.ImportError_ as e:
            QMessageBox.critical(self, '导入失败', str(e))
            return

        added, skipped = [], []
        for station in stations:
            if self.profile.get(station.callsign):
                skipped.append(station.callsign)
                continue
            self.profile.add(station)
            added.append(station.callsign)

        self.profile.save()
        self.refresh_stations()
        for station in stations:
            if station.callsign in added:
                self.refresh_metar(station)

        report = [f'从「{profile_name or path}」导入了 {len(added)} 个席位']
        if added:
            report.append('　' + '、'.join(added))
        if skipped:
            report.append(f'已存在、跳过的 {len(skipped)} 个：' + '、'.join(skipped))
        report.extend(notes)
        QMessageBox.information(self, '导入完成', '\n'.join(report))

    def remove_station(self):
        station = self.current_station()
        if not station:
            return
        if station.callsign in self.broadcasters:
            QMessageBox.warning(self, '无法删除', '请先停止这个席位的播出')
            return
        self.profile.remove(station.callsign)
        self.profile.save()
        self.refresh_stations()

    # ---------- 预设与生成 ----------
    def current_preset(self):
        station = self.current_station()
        if not station or not station.presets:
            return None
        index = max(0, self.preset_combo.currentIndex())
        return station.presets[min(index, len(station.presets) - 1)]

    def edit_preset(self):
        station = self.current_station()
        preset = self.current_preset()
        if not station or not preset:
            return
        dialog = PresetDialog(preset, parent=self)
        if dialog.exec():
            dialog.apply()
            self.profile.save()
            self.regenerate()

    def regenerate(self):
        """把当前席位 + 预设 + METAR 渲染成文字通播和语音稿。"""
        station = self.current_station()
        preset = self.current_preset()
        if not station or not preset:
            self.text_preview.setPlainText('')
            self.voice_preview.setPlainText('')
            self.letter_label.setText('情报字母: -')
            return

        self.letter_label.setText(f'情报字母: {station.letter}')
        parsed = self.metars.get(station.callsign)
        if parsed is None:
            self.text_preview.setPlainText('（还没有天气数据）')
            self.voice_preview.setPlainText('')
            return

        context = template_module.build_context(
            parsed, station.identifier, station.letter,
            preset.airport_conditions, preset.notams, preset.transition_level,
            # 语音念机场全名：念 "Z S P D" 听着像在拼写，真实通播念的是
            # "Shanghai Pudong International Airport"。席位上没填名称才退回代码。
            facility_voice=station.name or station.identifier,
            # 收尾语跟着预设：不同构型要交代的事不一样。留空用内置那句。
            closing=preset.closing or None)
        text, voice = template_module.render(preset.template, context,
                                             station.contractions)
        voice = self.voice_for(station, parsed, voice, preset)
        self.text_preview.setPlainText(text)
        self.voice_preview.setPlainText(voice)

        unknown = template_module.unknown_variables(preset.template)
        if unknown:
            self.status_label.setText('模板里有认不出的变量: ' + ', '.join(unknown))
        return text, voice

    # ---------- 天气 ----------
    def refresh_metar(self, station):
        if not station:
            return
        callsign, icao = station.callsign, station.identifier
        url = self.settings.metar_url
        signals = self.signals
        # 已经连上 FSD 就直接问自己的服务器要（$AX），它自带气象源和缓存
        fsd = self.fsd_clients.get(callsign)

        def worker():
            try:
                raw = None
                if fsd is not None and fsd.connected:
                    report = fsd.request_metar(icao)
                    raw = weather.normalize(report, icao) if report else None
                if raw is None:
                    raw = weather.fetch_metar(icao, url)
                signals.metar.emit(callsign, metar_module.Metar(raw), "")
            except weather.WeatherError as e:
                # 这条以前只进状态栏。打包是 console=False，用户报"取不到天气"
                # 时手里什么都没有，只能截图状态栏——日志里必须留一份
                log.warning("%s 取天气失败: %s", callsign, e)
                signals.metar.emit(callsign, None, str(e))
            except Exception as e:
                log.warning("%s 取天气出错: %s", callsign, e, exc_info=True)
                signals.metar.emit(callsign, None, f"取天气出错: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def apply_refresh_interval(self):
        """按设置重开定时器。设置改完要调一次，否则新间隔下次重启才生效。"""
        seconds = settings_module.clamp_refresh(
            getattr(self.settings, "metar_refresh",
                    settings_module.DEFAULT_METAR_REFRESH))
        self.timer.start(seconds * 1000)
        log.info("天气自动刷新间隔 %d 秒", seconds)
        return seconds

    def refresh_all_metars(self):
        for station in self.profile:
            self.refresh_metar(station)

    def on_metar(self, callsign, parsed, error):
        station = self.profile.get(callsign)
        if not station:
            return
        if error or parsed is None:
            message = error or '没有取到天气'
            self._weather_errors[callsign] = message
            self.status_label.setText(message)
            return

        # 这个席位取到了：把它之前那条错误撤掉。不撤的话，一次抖动留下的报错
        # 会一直挂在状态栏上——后面每次成功刷新都是静默的，没有谁会去改写它，
        # 于是天气早就正常了，界面还在喊失败。
        recovered = self._weather_errors.pop(callsign, None)

        changed = self.raw_metars.get(callsign) not in (None, parsed.raw)
        first = callsign not in self.raw_metars
        self.raw_metars[callsign] = parsed.raw
        self.metars[callsign] = parsed

        note = ''
        if changed:
            # 报文变了就换一格情报字母，这是 ATIS 的基本约定
            station.advance_letter()
            self.profile.save()
            note = f'{callsign} 天气更新，情报字母推进到 {station.letter}'
        elif recovered:
            note = f'{callsign} 天气已恢复'

        # 还没恢复的错误优先于流水账。反过来的话，一个席位取不到天气这件事会
        # 被另一个席位的"天气更新"盖掉，再也没人看得见。
        outstanding = next(iter(self._weather_errors.values()), None)
        if outstanding or note:
            self.status_label.setText(outstanding or note)

        if station is self.current_station():
            self.metar_label.setText(f'METAR: {parsed.raw}')
            self.regenerate()

        # 已经在播的席位，换稿
        if (changed or first) and callsign in self.broadcasters:
            self.push_update(station)

    def push_update(self, station):
        """把新的通播稿推给语音和 FSD 两边。"""
        rendered = self.render_for(station)
        if not rendered:
            return
        text, voice = rendered
        broadcaster = self.broadcasters.get(station.callsign)
        if broadcaster:
            broadcaster.update_text(voice)
        fsd = self.fsd_clients.get(station.callsign)
        if fsd:
            fsd.set_atis_lines(fsdclient.wrap_atis_text(text))

    def render_for(self, station):
        preset = station.presets[0] if station.presets else None
        if station is self.current_station():
            preset = self.current_preset() or preset
        parsed = self.metars.get(station.callsign)
        if not preset or parsed is None:
            return None
        context = template_module.build_context(
            parsed, station.identifier, station.letter,
            preset.airport_conditions, preset.notams, preset.transition_level,
            # 语音念机场全名：念 "Z S P D" 听着像在拼写，真实通播念的是
            # "Shanghai Pudong International Airport"。席位上没填名称才退回代码。
            facility_voice=station.name or station.identifier,
            # 收尾语跟着预设：不同构型要交代的事不一样。留空用内置那句。
            closing=preset.closing or None)
        text, voice = template_module.render(preset.template, context,
                                             station.contractions)
        return text, self.voice_for(station, parsed, voice, preset)

    @staticmethod
    def voice_for(station, parsed, english, preset=None):
        """按席位设置决定语音稿用哪种语言。

        中文稿不是英文的翻译，是 chinese.py 从 METAR 重新渲染的——语序和数字
        读法都不一样。双语时中文在后，因为中文飞行员听得懂英文的居多，反过来
        不一定。
        """
        language = getattr(station, "voice_language", profile_module.LANGUAGE_ENGLISH)
        if language == profile_module.LANGUAGE_ENGLISH:
            return english

        # 跑道优先取当前预设的：切到"北向"时英文稿的 ARR RWY 会变，中文稿
        # 要是还念着南向的跑道，同一份通播里两种语言互相矛盾。预设没填才回退
        # 到席位上那个。
        runway = getattr(preset, "chinese_runway", "") or station.chinese_runway
        script = chinese.render(
            parsed,
            facility=station.chinese_name or station.identifier,
            letter=station.letter,
            runway=runway,
            # 中文稿是从 METAR 独立渲染的，跑道构型、放行频率、应答机这些在
            # 中文侧没有对应字段，整段由预设提供，接在气象之后念
            extra=getattr(preset, "chinese_extra", "") or "")
        if language == profile_module.LANGUAGE_CHINESE:
            return script
        return f"{english} {script}"

    def advance_letter(self):
        station = self.current_station()
        if not station:
            return
        station.advance_letter()
        self.profile.save()
        self.regenerate()
        self.push_update(station)

    # ---------- 播出 ----------
    def toggle_broadcast(self):
        station = self.current_station()
        if not station:
            return
        if station.callsign in self.broadcasters:
            self.stop_broadcast(station.callsign)
            return

        cid = self.cid_input.text().strip()
        password = self.password_input.text()
        if not cid or not password:
            QMessageBox.warning(self, '错误', '请先填写用户名和密码')
            return

        # 语音账号是 {cid}_atis{频率} —— 同一频率上再开一个，用户名就撞了，
        # 服务端会把先连上的那个踢掉（server/login.py 的同名踢人逻辑）
        for other_callsign in self.broadcasters:
            other = self.profile.get(other_callsign)
            if other and other.frequency_khz == station.frequency_khz:
                QMessageBox.warning(
                    self, '频率冲突',
                    f'{other_callsign} 已经在 {other.frequency} 上播出了。\n'
                    f'两个通播用同一个频率会共用同一个语音账号，'
                    f'后连上的会把先连上的踢掉。')
                return

        rendered = self.render_for(station)
        if not rendered or not rendered[1].strip():
            QMessageBox.warning(self, '错误', '还没有可播的内容，先刷新天气')
            return

        # 开播前先查一次数据源：确认本人确实在管制，顺便拿到等级。
        # 放后台线程做，界面不卡；结果经信号回来后才真正连接。
        self.broadcast_button.setEnabled(False)
        self.status_label.setText('正在核对管制席位…')
        callsign = station.callsign
        url = self.settings.datafeed_url
        signals = self.signals

        def worker():
            data = datafeed.fetch(url)
            controller = datafeed.controller_for(cid, data=data) if data else None
            rating = datafeed.rating_for(cid, data=data) if data else None
            try:
                signals.precheck.emit(callsign, cid, password, data is not None,
                                      controller or {}, rating or 0)
            except RuntimeError:
                pass        # 窗口已经关了

        threading.Thread(target=worker, daemon=True).start()

    def on_precheck(self, callsign, cid, password, reachable, controller, rating):
        """数据源核对结果回来了（已在界面线程）。"""
        self.broadcast_button.setEnabled(True)
        self.status_label.setText('未播出')

        station = self.profile.get(callsign)
        if not station or callsign in self.broadcasters:
            return

        if not reachable:
            answer = QMessageBox.question(
                self, '无法核对管制席位',
                '连不上网络数据源，无法确认你此刻是否在管制。\n\n仍然开始播出吗？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif not controller:
            # 没在管制就不该挂通播——否则会留下无人值守的通播
            QMessageBox.warning(
                self, '你还没有在管制',
                f'网络上没有找到 CID {cid} 的管制席位，因此不能开通播。\n\n'
                f'请先用管制客户端上线（观察员不算），再回来开始播出。')
            return

        if controller:
            log.info("CID %s 正在管制 %s，允许开通播",
                     cid, controller.get('callsign', '?'))

        self.start_broadcast(station, cid, password, rating)

    def frequency_conflict(self, station):
        """同频率上已经有别的通播在播吗？

        语音账号是 {cid}_atis{频率}，同频率再开一个用户名就撞了，服务端会把
        先连上的踢掉（server/login.py 的同名踢人逻辑）。
        """
        for other_callsign in self.broadcasters:
            if other_callsign == station.callsign:
                continue
            other = self.profile.get(other_callsign)
            if other and other.frequency_khz == station.frequency_khz:
                return other
        return None

    def start_broadcast(self, station, cid, password, rating=0):
        """核对通过之后真正建立两条连接。"""
        # 核对期间隔了几秒，这段时间里可能又开了一个同频率的席位，所以这里
        # 要再查一次，不能只依赖点按钮那一刻的检查
        conflict = self.frequency_conflict(station)
        if conflict:
            QMessageBox.warning(
                self, '频率冲突',
                f'{conflict.callsign} 已经在 {conflict.frequency} 上播出了。\n'
                f'两个通播用同一个频率会共用同一个语音账号，'
                f'后连上的会把先连上的踢掉。')
            return

        rendered = self.render_for(station)
        if not rendered or not rendered[1].strip():
            QMessageBox.warning(self, '错误', '还没有可播的内容，先刷新天气')
            return

        callsign = station.callsign

        # FSD 那条连接：让席位出现在网络上、回答文字通播查询、提供气象
        if self.settings.connect_fsd:
            problem = fsdclient.callsign_problem(callsign)
            if problem:
                answer = QMessageBox.question(
                    self, '呼号不合服务端规则',
                    f'{problem}\n\n继续的话只播语音，席位不会出现在网络上。是否继续？',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            else:
                fsd = fsdclient.FSDClient(
                    self.settings.fsd_host, callsign, cid, password,
                    station.frequency,
                    real_name=self.settings.real_name or cid,
                    port=self.settings.fsd_port,
                    # 等级：设置里指定的优先，否则用刚才核对时查到的，
                    # 免得通播在雷达图上显示成观察员而管制席位是 C1
                    rating=self.settings.rating or rating,
                    latitude=station.latitude, longitude=station.longitude,
                    atis_lines=fsdclient.wrap_atis_text(rendered[0]),
                    # FSD 的错误单独标一个 state：它只是附加能力，登录失败
                    # 不该把频率上的语音通播一起停掉
                    on_status=lambda state, message, _c=callsign:
                        self.signals.state.emit(
                            _c, 'fsd-error' if state == 'error' else state,
                            f"[FSD] {message}"))
                self.fsd_clients[callsign] = fsd
                fsd.start()

        broadcaster = Broadcaster(
            SERVER, cid, password, station,
            on_state=lambda state, message, _c=callsign:
                self.signals.state.emit(_c, state, message))
        self.broadcasters[callsign] = broadcaster

        self.settings.cid = cid
        self.settings.save_settings()

        broadcaster.start(rendered[1])
        self.refresh_stations()

    def stop_broadcast(self, callsign):
        broadcaster = self.broadcasters.pop(callsign, None)
        if broadcaster:
            try:
                broadcaster.stop()
            except Exception as e:
                log.warning(f"停止播出出错: {e}")
        fsd = self.fsd_clients.pop(callsign, None)
        if fsd:
            try:
                fsd.stop()
            except Exception as e:
                log.warning(f"断开 FSD 出错: {e}")
        self.status_label.setText('未播出')
        self.refresh_stations()

    def on_broadcast_state(self, callsign, state, message):
        if callsign not in self.broadcasters:
            return
        station = self.current_station()
        if station and station.callsign == callsign:
            self.status_label.setText(message)
            self.status_label.setStyleSheet(
                "color: #cc0000;" if state == 'error' else "color: #555555;")
        if state == 'fsd-error':
            # 只收掉 FSD 这一条，语音继续播——席位不在网络上总比频率上没声音好
            fsd = self.fsd_clients.pop(callsign, None)
            if fsd:
                try:
                    fsd.stop()
                except Exception as e:
                    log.warning(f"停止 FSD 连接出错: {e}")
            self.status_label.setText(f'{message}（语音仍在播出）')
            self.status_label.setStyleSheet("color: #cc6600;")
            log.warning("%s 的 FSD 连接失败，语音继续: %s", callsign, message)
        elif state == 'error':
            QMessageBox.critical(self, f'{callsign} 播出错误', message)
            self.stop_broadcast(callsign)

    # ---------- 其它 ----------
    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        dialog.exec()
        # 刷新间隔要立刻生效，不然改了得重启才算数
        self.apply_refresh_interval()

    def closeEvent(self, event):
        try:
            # 两个字典取并集：万一 FSD 连上了而语音没起来，也要收干净
            for callsign in set(self.broadcasters) | set(self.fsd_clients):
                self.stop_broadcast(callsign)
        except Exception as e:
            log.warning(f"关闭时出错: {e}")
        finally:
            event.accept()


class PresetDialog(QDialog):
    """编辑一份预设：模板、机场条件、NOTAM。"""

    def __init__(self, preset, parent=None):
        super().__init__(parent)
        self.preset = preset
        self.setWindowTitle(f'预设 - {preset.name}')
        self.setMinimumSize(640, 520)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        name_row = QHBoxLayout()
        self.name = QLineEdit(self.preset.name)
        name_row.addWidget(QLabel('名称:'))
        name_row.addWidget(self.name)
        layout.addLayout(name_row)

        layout.addWidget(QLabel('模板'))
        self.template = QTextEdit(self.preset.template)
        layout.addWidget(self.template)

        variables = QLabel('可用变量: ' + '  '.join(
            f'[{name}]' for name in sorted(set(template_module.ALIASES))))
        variables.setWordWrap(True)
        variables.setStyleSheet("color: #777777; font-size: 11px;")
        layout.addWidget(variables)

        vox = QLabel('变量后面加 :VOX 表示在文字通播里也用语音说法，例如 [WX:VOX]。')
        vox.setStyleSheet("color: #777777;")
        vox.setWordWrap(True)
        layout.addWidget(vox)

        layout.addWidget(QLabel('机场条件  [ARPT_COND]'))
        self.conditions = QTextEdit(self.preset.airport_conditions)
        self.conditions.setFixedHeight(60)
        layout.addWidget(self.conditions)

        layout.addWidget(QLabel('NOTAM  [NOTAMS]'))
        self.notams = QTextEdit(self.preset.notams)
        self.notams.setFixedHeight(60)
        layout.addWidget(self.notams)

        tl_row = QHBoxLayout()
        self.transition_level = QLineEdit(self.preset.transition_level)
        self.transition_level.setPlaceholderText('例如 3600 米')
        tl_row.addWidget(QLabel('过渡高度层  [TL]:'))
        tl_row.addWidget(self.transition_level)
        layout.addLayout(tl_row)

        buttons = QHBoxLayout()
        ok = QPushButton('保存')
        ok.clicked.connect(self.accept)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def apply(self):
        self.preset.name = self.name.text().strip() or self.preset.name
        self.preset.template = self.template.toPlainText()
        self.preset.airport_conditions = self.conditions.toPlainText().strip()
        self.preset.notams = self.notams.toPlainText().strip()
        self.preset.transition_level = self.transition_level.text().strip()


def selftest():
    """检查这份构建能不能真的合成语音。

    pyttsx3 走 SAPI5，而 SAPI5 靠 comtypes 在运行时生成绑定代码——打包之后
    很容易因为找不到驱动或者写不了缓存目录而失灵，界面上却只表现为"通播没声
    音"。加个自检开关，装机之后先跑一次就能确认：

        airwaysn-atis.exe --selftest    退出码 0 表示语音可用
    """
    from broadcast import Synthesizer
    synth = Synthesizer()
    try:
        pcm = synth.synthesize("ZSPD information alpha, wind calm.")
        if not pcm:
            log.warning("自检失败: 语音合成没有产出音频")
            return 1
        seconds = len(pcm) / (2.0 * 48000)
        log.info(f"自检通过: 合成了 {seconds:.1f} 秒音频")
        return 0
    except Exception as e:
        log.warning(f"自检失败: {e}")
        return 1
    finally:
        synth.cleanup()


if __name__ == '__main__':
    # --debug 会把协议细节也记进日志（FSD 收发的每个包、频道操作）。
    # 设置里的开关是给拿不到命令行的用户准备的，两者任一打开即生效。
    applog.setup(debug='--debug' in sys.argv or Settings().debug)
    log.info("情报通播客户端启动 %s", version.full())

    if '--selftest' in sys.argv:
        sys.exit(selftest())

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(icon_path))
    window = AtisWindow()
    window.show()
    sys.exit(app.exec())
