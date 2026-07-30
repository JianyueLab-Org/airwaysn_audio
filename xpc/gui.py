"""XPC for CAN — X-Plane 飞行员客户端。

参考 xpilot（https://github.com/xpilot-project/xpilot）的布局：上面一条连接栏，
中间是消息区，右边是附近的管制席位，底下是无线电状态和 PTT。

三条链路各自独立，一条断了不影响另外两条：

    xplane.XPlaneLink    从 X-Plane 订阅位置、姿态、应答机和 COM1
    fsdpilot.FSDPilot    以飞行员身份连 FSD，把位置报上去，收发文字
    voice.Voice          Mumble 语音，频道跟着 COM1 走

外观用 qfluentwidgets（深色 Fluent），和管制端、通播端同一套——配色常量全在
theme.py 里，这里不再出现任何写死的 #rrggbb。界面文字全部走 i18n.t()，源码里
不留界面字面量，test_i18n.py 会扫源码兜底。

pymumble 和 FSD 的回调都在各自的后台线程上，碰 Qt 控件必须经 pyqtSignal 转到
GUI 线程——直接改控件在 Qt 里是未定义行为，表现出来是随机崩溃。
"""

import os

# 这两行必须在 import pygame 之前。pygame 只用来读摇杆（现在由 ptt.py 惰性导入），
# SDL 的视频和音频后端一起来会和 PyQt6、PyAudio 抢设备。
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'

import logging
import sys
import threading
import time

import applog
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QFont, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QListWidgetItem, QMainWindow, QMessageBox,
    QStackedWidget, QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CheckBox, ComboBox,
                            FluentIcon, HeaderCardWidget, LineEdit, ListWidget,
                            PasswordLineEdit, PlainTextEdit, PrimaryPushButton,
                            PushButton, SegmentedWidget, Slider, SpinBox,
                            StrongBodyLabel, TextEdit)

import bridge
import cslmatch
import fsdpilot
import i18n
import ptt
import theme
import traffic as traffic_module
import version
import voice as voice_module
import xpinstall
import xplane
from i18n import t
from settings import Settings

log = logging.getLogger("gui")

APP_NAME = "XPC for CAN"
# 版本号和 build 号统一在 version.py 里。build 号是打包时由 gui.spec 固化的
# ——打包之后程序里没有 .git，运行时问不出来。
VERSION = version.VERSION


class Signals(QObject):
    """后台线程 → GUI 线程的唯一通道。"""
    sim_state = pyqtSignal(bool, str)
    fsd_status = pyqtSignal(str, str)
    voice_status = pyqtSignal(str, str)
    text_message = pyqtSignal(str, str, str)
    controllers = pyqtSignal(list)
    ptt = pyqtSignal(bool)
    rx = pyqtSignal(bool)
    channel = pyqtSignal(float, str)


class Indicator(QLabel):
    """TX / RX 那种小灯。"""

    def __init__(self, text, colour):
        super().__init__(text)
        self._colour = colour
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(46, 26)
        self.setFont(QFont(theme.MONO_FONT, 10, QFont.Weight.Bold))
        self.set_lit(False)

    def set_lit(self, lit):
        colour = self._colour if lit else theme.OFF_COLOR
        text = "#ffffff" if lit else theme.IDLE_COLOR
        self.setStyleSheet(
            f"background-color: {colour}; color: {text};"
            "border-radius: 4px; padding: 2px;")


class Card(HeaderCardWidget):
    """带标题的卡片，用来代替 QGroupBox。

    QGroupBox 不吃 Fluent 主题，深色底上会画出一圈亮边框和一个亮标题；
    HeaderCardWidget 是 qfluentwidgets 里对应的东西，标题、圆角、底色都跟主题走。
    """

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setTitle(title)

    def body(self, layout):
        """把内容塞进卡片的正文区。"""
        self.viewLayout.addLayout(layout)
        return self


class XpcWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        i18n.set_language(self.settings.language or i18n.system_language())
        self.signals = Signals()

        self.sim = xplane.XPlaneLink(on_state=self.signals.sim_state.emit)
        self.fsd = None
        self.voice = None
        self.snapshot = None

        # 他机：FSD 线程往表里写，tick() 读出来插值好推给插件
        self.traffic = traffic_module.TrafficTable()
        self.bridge = bridge.BridgeSender()
        self.models = cslmatch.ModelSet()
        self._model_cache = {}          # 呼号 -> 匹配到的 .obj 路径
        self._load_models()
        # 键盘 / 摇杆 / 鼠标侧键三合一。连上之后才开始监听。
        self.ptt_watcher = ptt.PttWatcher(self.settings.ptt_bindings,
                                          on_change=self.on_ptt_change)

        self.setWindowTitle(f"{APP_NAME} v{VERSION}")
        self.resize(980, 660)
        self.setStyleSheet(theme.window_qss())
        if os.path.exists("favicon.ico"):
            self.setWindowIcon(QIcon("favicon.ico"))

        self._build_ui()
        self._connect_signals()

        self.sim.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)

    # ---------- 界面 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        layout.addWidget(self._build_connect_bar())

        middle = QHBoxLayout()
        middle.addWidget(self._build_messages(), 3)
        middle.addWidget(self._build_controllers(), 1)
        layout.addLayout(middle)

        layout.addWidget(self._build_radio_bar())

        self._build_menu()
        self.statusBar().showMessage(t("status.ready"))

    def _build_menu(self):
        menu = self.menuBar()

        file_menu = menu.addMenu(t("menu.file"))
        plan_action = QAction(t("menu.flight_plan"), self)
        plan_action.triggered.connect(self.open_flight_plan)
        file_menu.addAction(plan_action)
        settings_action = QAction(t("menu.settings"), self)
        settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction(t("menu.quit"), self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = menu.addMenu(t("menu.help"))
        log_action = QAction(t("menu.open_log"), self)
        log_action.triggered.connect(lambda: applog.open_log_folder())
        help_menu.addAction(log_action)
        about_action = QAction(t("menu.about"), self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _build_connect_bar(self):
        card = Card(t("connect.title"))
        grid = QGridLayout()

        self.callsign_input = LineEdit()
        self.callsign_input.setText(self.settings.callsign)
        self.callsign_input.setPlaceholderText(t("connect.callsign_hint"))
        self.callsign_input.setMaxLength(fsdpilot.MAX_CALLSIGN_LENGTH)
        self.cid_input = LineEdit()
        self.cid_input.setText(self.settings.cid)
        self.cid_input.setPlaceholderText(t("connect.cid"))
        self.password_input = PasswordLineEdit()
        self.password_input.setText(self.settings.password)
        self.aircraft_input = LineEdit()
        self.aircraft_input.setText(self.settings.aircraft)
        self.aircraft_input.setPlaceholderText(t("connect.aircraft_hint"))

        grid.addWidget(BodyLabel(t("connect.callsign")), 0, 0)
        grid.addWidget(self.callsign_input, 0, 1)
        grid.addWidget(BodyLabel(t("connect.cid")), 0, 2)
        grid.addWidget(self.cid_input, 0, 3)
        grid.addWidget(BodyLabel(t("connect.password")), 0, 4)
        grid.addWidget(self.password_input, 0, 5)
        grid.addWidget(BodyLabel(t("connect.aircraft")), 0, 6)
        grid.addWidget(self.aircraft_input, 0, 7)

        self.connect_button = PrimaryPushButton(t("connect.connect"))
        self.connect_button.setMinimumWidth(110)
        self.connect_button.clicked.connect(self.toggle_connection)
        grid.addWidget(self.connect_button, 0, 8)

        self.sim_label = CaptionLabel(t("sim.waiting"))
        self.fsd_label = CaptionLabel(t("net.disconnected"))
        self.voice_label = CaptionLabel(t("voicebar.disconnected"))
        for i, label in enumerate((self.sim_label, self.fsd_label, self.voice_label)):
            label.setStyleSheet(f"color: {theme.IDLE_COLOR};")
            grid.addWidget(label, 1, i * 3, 1, 3)
        return card.body(grid)

    def _build_messages(self):
        card = Card(t("messages.title"))
        layout = QVBoxLayout()

        self.messages = TextEdit()
        self.messages.setReadOnly(True)
        self.messages.setFont(QFont(theme.MONO_FONT, 10))
        layout.addWidget(self.messages)

        row = QHBoxLayout()
        self.recipient_input = LineEdit()
        self.recipient_input.setPlaceholderText(t("messages.recipient_hint"))
        self.recipient_input.setMaximumWidth(240)
        self.message_input = LineEdit()
        self.message_input.setPlaceholderText(t("messages.body_hint"))
        self.message_input.returnPressed.connect(self.send_message)
        send = PushButton(FluentIcon.SEND, t("messages.send"))
        send.clicked.connect(self.send_message)
        row.addWidget(self.recipient_input)
        row.addWidget(self.message_input)
        row.addWidget(send)
        layout.addLayout(row)
        return card.body(layout)

    def _build_controllers(self):
        card = Card(t("controllers.title"))
        layout = QVBoxLayout()
        self.controller_list = ListWidget()
        self.controller_list.setFont(QFont(theme.MONO_FONT, 10))
        self.controller_list.itemDoubleClicked.connect(self.controller_clicked)
        layout.addWidget(self.controller_list)
        hint = CaptionLabel(t("controllers.hint"))
        hint.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return card.body(layout)

    def _build_radio_bar(self):
        card = Card(t("radio.title"))
        layout = QHBoxLayout()

        self.tx_light = Indicator("TX", theme.MUTED_COLOR)
        self.rx_light = Indicator("RX", theme.ON_COLOR)
        layout.addWidget(self.tx_light)
        layout.addWidget(self.rx_light)

        self.com1_label = BodyLabel(t("radio.com1_none"))
        self.com1_label.setFont(QFont(theme.MONO_FONT, 15, QFont.Weight.Bold))
        layout.addWidget(self.com1_label)

        self.channel_label = CaptionLabel("")
        self.channel_label.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        layout.addWidget(self.channel_label)

        layout.addStretch()

        self.traffic_label = CaptionLabel(t("radio.traffic", count=0))
        self.traffic_label.setFont(QFont(theme.MONO_FONT, 10))
        self.traffic_label.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        layout.addWidget(self.traffic_label)

        self.position_label = CaptionLabel(t("radio.position_none"))
        self.position_label.setFont(QFont(theme.MONO_FONT, 10))
        self.position_label.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        layout.addWidget(self.position_label)

        self.ident_button = PushButton(t("radio.ident"))
        self.ident_button.setEnabled(False)
        self.ident_button.clicked.connect(self.send_ident)
        layout.addWidget(self.ident_button)

        self.ptt_button = PrimaryPushButton(FluentIcon.MICROPHONE, t("radio.ptt"))
        self.ptt_button.setMinimumWidth(130)
        self.ptt_button.setToolTip(t("radio.ptt_tip"))
        self.ptt_button.pressed.connect(lambda: self.set_ptt(True))
        self.ptt_button.released.connect(lambda: self.set_ptt(False))
        layout.addWidget(self.ptt_button)
        return card.body(layout)

    def _connect_signals(self):
        self.signals.sim_state.connect(self.on_sim_state)
        self.signals.fsd_status.connect(self.on_fsd_status)
        self.signals.voice_status.connect(self.on_voice_status)
        self.signals.text_message.connect(self.on_text_message)
        self.signals.controllers.connect(self.on_controllers)
        self.signals.ptt.connect(self.tx_light.set_lit)
        self.signals.rx.connect(self.rx_light.set_lit)
        self.signals.channel.connect(self.on_channel)

    # ---------- 消息区 ----------
    def add_message(self, text, colour=None):
        colour = colour or theme.TEXT_COLOR
        stamp = time.strftime("%H:%M:%S")
        self.messages.append(
            f'<span style="color:{theme.IDLE_COLOR}">{stamp}</span> '
            f'<span style="color:{colour}">{text}</span>')
        self.messages.moveCursor(QTextCursor.MoveOperation.End)

    # ---------- 连接 ----------
    def toggle_connection(self):
        if self.fsd or self.voice:
            self.disconnect_all()
        else:
            self.connect_all()

    def connect_all(self):
        callsign = self.callsign_input.text().strip().upper()
        cid = self.cid_input.text().strip()
        password = self.password_input.text()

        problem = fsdpilot.callsign_problem(callsign)
        if problem:
            QMessageBox.warning(self, t("dialog.callsign_bad"), problem)
            return
        if not cid or not password:
            QMessageBox.warning(self, t("dialog.missing"), t("dialog.missing_body"))
            return
        if not self.sim.connected:
            answer = QMessageBox.question(
                self, t("dialog.no_sim"), t("dialog.no_sim_body"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        # 存下来，下次开机不用重填
        self.settings.callsign = callsign
        self.settings.cid = cid
        self.settings.password = password
        self.settings.aircraft = self.aircraft_input.text().strip().upper()
        self.settings.save()

        self.connect_button.setEnabled(False)
        self.connect_button.setText(t("connect.connecting"))

        if self.settings.connect_fsd:
            self.fsd = fsdpilot.FSDPilot(
                host=self.settings.fsd_host, port=self.settings.fsd_port,
                callsign=callsign, cid=cid, password=password,
                real_name=self.settings.real_name or cid,
                rating=self.settings.rating,
                aircraft=self.settings.aircraft,
                on_status=self.signals.fsd_status.emit,
                on_text=self.signals.text_message.emit,
                on_controllers=self.signals.controllers.emit,
                traffic=self.traffic)
            self.fsd.start()

        if self.settings.connect_voice:
            self.voice = voice_module.Voice(
                host=self.settings.mumble_host, username=cid, password=password,
                settings=self.settings,
                on_status=self.signals.voice_status.emit,
                on_ptt=self.signals.ptt.emit,
                on_rx=self.signals.rx.emit,
                on_channel=self.signals.channel.emit)
            threading.Thread(target=self.voice.start, daemon=True).start()

        self.ptt_watcher.start()
        self.connect_button.setEnabled(True)
        self.connect_button.setText(t("connect.disconnect"))

    def disconnect_all(self):
        self.ptt_watcher.stop()
        if self.fsd:
            self.fsd.stop()
            self.fsd = None
        if self.voice:
            voice = self.voice
            self.voice = None
            threading.Thread(target=voice.stop, daemon=True).start()
        self.connect_button.setText(t("connect.connect"))
        self.ident_button.setEnabled(False)
        self.controller_list.clear()
        self.fsd_label.setText(t("net.disconnected"))
        self.voice_label.setText(t("voicebar.disconnected"))
        self.channel_label.setText("")
        self.tx_light.set_lit(False)
        self.rx_light.set_lit(False)
        self.add_message(t("msg.disconnected"), theme.ACTIVE_COLOR)

    # ---------- 定时 ----------
    def tick(self):
        """每 0.5 秒：把模拟器的数据分发到 FSD 和语音。"""
        snapshot = self.sim.snapshot()
        if not snapshot:
            return
        self.snapshot = snapshot

        com1 = snapshot.get("com1")
        self.com1_label.setText(t("radio.com1", frequency=f"{com1:.3f}") if com1
                                else t("radio.com1_none"))
        self.position_label.setText(
            f"{snapshot['latitude']:.4f} {snapshot['longitude']:.4f}  "
            f"{snapshot['altitude']} ft  {snapshot['groundspeed']} kt  "
            f"{snapshot['heading']:03.0f}°  A{snapshot['squawk']:04d}")

        if self.fsd:
            self.fsd.update_position(snapshot)
        if self.voice and com1 and snapshot.get("com1_power", True):
            self.voice.set_frequency(com1)

        self._push_traffic(snapshot)

    # ---------- 他机 ----------
    def _load_models(self):
        """把 CSL 模型包读进来。没配路径就只送 TCAS，不画模型。"""
        path = (self.settings.csl_path or "").strip()
        if not path:
            return
        if not os.path.isdir(path):
            log.warning("the CSL directory does not exist: %s", path)
            return
        try:
            self.models = cslmatch.ModelSet.load(path)
        except Exception as e:
            log.warning("failed to read the CSL models: %s", e)
            return
        self._model_cache.clear()
        log.info("loaded %d CSL models covering %d aircraft types",
                 len(self.models), len(self.models.types))

    def _push_traffic(self, snapshot):
        """把他机插值到当前时刻，推给插件去画。"""
        if not self.settings.render_traffic:
            return
        self.traffic.prune()

        origin = (snapshot["latitude"], snapshot["longitude"])
        entries = self.traffic.snapshot(
            origin=origin,
            limit=bridge.MAX_TRAFFIC,
            max_range_nm=self.settings.traffic_range_nm or None)

        for entry in entries:
            entry["object"] = self._model_for(entry)
        self.bridge.send_traffic(entries, own=origin)
        self.traffic_label.setText(t("radio.traffic", count=len(entries)))

    def _model_for(self, entry):
        """给一架飞机挑模型。匹配结果缓存住，别每帧都算。"""
        callsign = entry["callsign"]
        if not entry.get("model_dirty") and callsign in self._model_cache:
            return self._model_cache[callsign]

        model, why = self.models.match(
            equipment=entry.get("equipment", ""),
            airline=entry.get("airline", ""),
            csl=entry.get("csl", ""))
        path = model.path if model else ""
        self._model_cache[callsign] = path
        self.traffic.mark_model_clean(callsign)
        if model:
            log.info("%s (%s/%s) → %s: %s", callsign,
                     entry.get("equipment") or "?", entry.get("airline") or "?",
                     model.name, why)
        else:
            log.debug("no model available for %s: %s", callsign, why)
        return path

    # ---------- 槽 ----------
    def on_sim_state(self, connected, message):
        self.sim_label.setText(t("sim.connected") if connected else t("sim.waiting"))
        self.sim_label.setStyleSheet(
            f"color: {theme.ON_COLOR if connected else theme.IDLE_COLOR};")
        self.add_message(t("msg.sim", message=message),
                         theme.ON_COLOR if connected else theme.ACTIVE_COLOR)

    def on_fsd_status(self, state, message):
        colour = {'online': theme.ON_COLOR, 'error': theme.MUTED_COLOR,
                  'offline': theme.MUTED_COLOR}.get(state, theme.ACTIVE_COLOR)
        keys = {'online': "net.online", 'reconnecting': "net.reconnecting",
                'offline': "net.offline"}
        self.fsd_label.setText(t(keys[state]) if state in keys
                               else t("net.other", state=state))
        self.fsd_label.setStyleSheet(f"color: {colour};")
        self.add_message(t("msg.net", message=message), colour)
        self.ident_button.setEnabled(state == 'online')

        # 还在重连：这条连接没死，引用留着——置空的话 tick() 就不再喂位置，
        # 连回来之后飞机在别人屏幕上停在掉线的那一点。
        if state == 'reconnecting':
            return

        # 重连次数用尽：整个下线。FSD 自己已经收摊了，这里连语音一起收。
        if state == 'offline':
            self.fsd = None
            self.disconnect_all()
            return

        if state == 'error':
            # 首次登录失败这类不重试的错误。语音不受影响——两条链路互不依赖，
            # 只有"重连到底也没成功"才一起下线。
            self.fsd = None
            self.connect_button.setText(
                t("connect.disconnect") if self.voice else t("connect.connect"))

    def on_voice_status(self, state, message):
        colour = {'online': theme.ON_COLOR, 'error': theme.MUTED_COLOR,
                  'offline': theme.MUTED_COLOR}.get(state, theme.ACTIVE_COLOR)
        keys = {'online': "voicebar.online", 'reconnecting': "voicebar.reconnecting",
                'offline': "voicebar.offline"}
        self.voice_label.setText(t(keys[state]) if state in keys
                                 else t("voicebar.other", state=state))
        self.voice_label.setStyleSheet(f"color: {colour};")
        self.add_message(t("msg.voice", message=message), colour)

        # 掉线重连中：连接还活着，**绝对不能把 self.voice 置空**。置空的话
        # tick() 里的 `if self.voice` 不成立，连回来之后再也不跟着 COM1 走了——
        # 语音是通的，人却停在掉线前那个频道上。
        if state == 'reconnecting':
            return

        # 重连次数用尽：整个下线。语音自己已经收摊了（Voice._give_up 走的是
        # stop()），这里把引用清掉再统一走断开，FSD 也一起收。
        if state == 'offline':
            self.voice = None
            self.disconnect_all()
            return

        if state == 'error':
            voice = self.voice
            self.voice = None
            # 也要真的收掉。只把引用置空的话，PyAudio 还占着麦克风、pymumble
            # 还在后台重连，而服务端 login.py 对认证失败按 ASN ID 限流。
            if voice:
                threading.Thread(target=voice.stop, daemon=True).start()
            self.connect_button.setText(
                t("connect.disconnect") if self.fsd else t("connect.connect"))

    def on_text_message(self, sender, recipient, body):
        if recipient.startswith("@"):
            self.add_message(
                t("msg.text_private", sender=sender, recipient=recipient, body=body),
                theme.THEME_COLOR)
        else:
            self.add_message(t("msg.text", sender=sender, body=body),
                             theme.ACTIVE_COLOR)

    def on_controllers(self, controllers):
        self.controller_list.clear()
        for entry in sorted(controllers, key=lambda c: c["callsign"]):
            item = QListWidgetItem(f"{entry['callsign']:<12} {entry['frequency']}")
            item.setData(Qt.ItemDataRole.UserRole, entry["callsign"])
            self.controller_list.addItem(item)

    def on_channel(self, frequency, name):
        self.channel_label.setText(name)

    def controller_clicked(self, item):
        self.recipient_input.setText(item.data(Qt.ItemDataRole.UserRole))
        self.message_input.setFocus()

    # ---------- 操作 ----------
    def send_message(self):
        body = self.message_input.text().strip()
        if not body:
            return
        if not (self.fsd and self.fsd.connected):
            self.add_message(t("msg.not_connected"), theme.MUTED_COLOR)
            return
        recipient = self.recipient_input.text().strip().upper()
        if not recipient:
            com1 = (self.snapshot or {}).get("com1")
            if not com1:
                self.add_message(t("msg.no_recipient"), theme.MUTED_COLOR)
                return
            # 发到频率上：@ 后面是 5 位电台频率，去掉开头的 1 和小数点
            recipient = f"@{int(round(com1 * 1000)) % 100000:05d}"
        if self.fsd.send_text(recipient, body):
            self.add_message(t("msg.sent", recipient=recipient, body=body),
                             theme.IDLE_COLOR)
            self.message_input.clear()

    def send_ident(self):
        if self.fsd and self.fsd.connected:
            self.fsd.ident()
            self.add_message(t("msg.ident"), theme.ACTIVE_COLOR)

    def set_ptt(self, value):
        if self.voice:
            self.voice.set_transmitting(value)

    def on_ptt_change(self, down):
        """ptt.PttWatcher 的回调，在监听线程或摇杆轮询线程上跑。

        **故意不经 pyqtSignal**：这里一个控件都不碰（发话灯是 voice 的 on_ptt
        回调经 signals.ptt 点亮的），而绕一趟 Qt 事件循环会让 PTT 的响应跟着
        界面忙不忙走——重画他机的那一帧里按下 PTT，话头就被切掉了。
        """
        try:
            self.set_ptt(down)
        except Exception as e:
            log.warning("could not switch the transmitter: %s", e)

    # ---------- 对话框 ----------
    def open_settings(self):
        # 开设置期间把监听整个停掉。录绑定时 PttCapture 要独占 SDL 的事件队列
        # （两个线程一起 pump 不是线程安全的），而且用户为了录绑定按的那一下
        # 不该真的发出去一段语音。
        was_running = self.ptt_watcher.is_running()
        self.ptt_watcher.stop()
        dialog = SettingsDialog(self.settings, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            previous_csl = self.settings.csl_path
            dialog.apply()
            self.settings.save()
            if self.settings.csl_path != previous_csl:
                self._load_models()
            if self.voice:
                try:
                    self.voice.reopen_audio()
                except Exception as e:
                    QMessageBox.warning(self, t("dialog.audio"),
                                        t("dialog.audio_failed", error=e))
        self.ptt_watcher.set_bindings(self.settings.ptt_bindings)
        if was_running:
            self.ptt_watcher.start()

    def open_flight_plan(self):
        dialog = FlightPlanDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        plan = dialog.plan()
        self.settings.flight_plan = plan
        self.settings.save()
        if self.fsd and self.fsd.connected:
            if self.fsd.file_flight_plan(plan):
                self.add_message(t("msg.plan_filed"), theme.ON_COLOR)
        else:
            self.add_message(t("msg.plan_local"), theme.ACTIVE_COLOR)

    def show_about(self):
        QMessageBox.information(
            self, t("dialog.about"),
            t("dialog.about_body", name=APP_NAME, version=version.full(),
              log=applog.log_path() or t("dialog.no_log")))

    def closeEvent(self, event):
        self.timer.stop()
        self.ptt_watcher.stop()
        if self.fsd:
            self.fsd.stop()
        if self.voice:
            self.voice.stop()
        self.sim.stop()
        self.bridge.close()
        self.settings.save()
        event.accept()


class PttBindingList(QWidget):
    """PTT 绑定的编辑器：一行一条，底下一个"添加绑定"。

    添加走 ptt.PttCapture——按什么就是什么。原来这里是一个 0-63 的数字框，要用户
    自己数出扳机是几号按钮，实际上没人填得对。

    **调用方必须在打开对话框之前把 PttWatcher 停掉**：录制要独占 SDL 的事件队列，
    而且录制时按下的那一下不该真的发出去一段语音。
    """

    captured = pyqtSignal(object)      # 录到的绑定，从监听线程转回界面线程

    def __init__(self, bindings, parent=None):
        super().__init__(parent)
        self.bindings = list(bindings)
        self._capture = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.hint = CaptionLabel(t("settings.ptt_hint"))
        self.hint.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.rows = QVBoxLayout()
        self.rows.setSpacing(4)
        layout.addLayout(self.rows)

        self.add_button = PushButton(FluentIcon.ADD, t("settings.ptt_add"))
        self.add_button.clicked.connect(self.toggle_capture)
        layout.addWidget(self.add_button)

        self.captured.connect(self.on_captured)
        self.rebuild()

    def rebuild(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        if not self.bindings:
            empty = BodyLabel(t("settings.ptt_none"))
            empty.setStyleSheet(f"color: {theme.MUTED_COLOR};")
            self.rows.addWidget(empty)
            return
        for binding in list(self.bindings):
            self.rows.addLayout(self._row(binding))

    def _row(self, binding):
        row = QHBoxLayout()
        row.setSpacing(6)
        label = BodyLabel(i18n.binding_label(binding))
        remove = PushButton(FluentIcon.DELETE, "")
        remove.setToolTip(t("settings.ptt_remove"))
        remove.setFixedWidth(40)
        remove.clicked.connect(lambda _=False, b=binding: self.remove(b))
        row.addWidget(label)
        row.addStretch()
        row.addWidget(remove)
        return row

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def remove(self, binding):
        self.bindings = [b for b in self.bindings if b != binding]
        self.rebuild()

    def toggle_capture(self):
        if self._capture is not None:
            self.stop_capture()
            return
        self.add_button.setText(t("settings.ptt_capturing"))
        # 录制期间必须能取消：用户可能只想看看，或者手边根本没有摇杆。按钮
        # 自己就是取消键，所以不能禁用它。
        self._capture = ptt.PttCapture(self.captured.emit)
        self._capture.start()

    def stop_capture(self):
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.stop()
        self.add_button.setText(t("settings.ptt_add"))

    def on_captured(self, binding):
        """在界面线程上跑（captured 信号转过来的）。"""
        self.stop_capture()
        if binding in self.bindings:
            self.hint.setText(t("settings.ptt_duplicate"))
            self.hint.setStyleSheet(f"color: {theme.ACTIVE_COLOR};")
            return
        self.hint.setText(t("settings.ptt_hint"))
        self.hint.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        self.bindings.append(binding)
        self.rebuild()


class SettingsDialog(QDialog):
    """音频设备、音量、PTT、网络和他机。

    分页用 SegmentedWidget + QStackedWidget，不用 QTabWidget——后者不吃 Fluent
    主题，深色对话框里会顶着一排系统原生的浅色页签。
    """

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(t("settings.title"))
        self.setStyleSheet(theme.dialog_qss())
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        self.pivot = SegmentedWidget()
        self.pages = QStackedWidget()
        layout.addWidget(self.pivot)
        layout.addWidget(self.pages)

        self._add_page("audio", t("settings.tab_audio"), self._audio_tab())
        self._add_page("network", t("settings.tab_network"), self._network_tab())
        self._add_page("traffic", t("settings.tab_traffic"), self._traffic_tab())
        self.pivot.setCurrentItem("audio")
        self.pages.setCurrentIndex(0)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_page(self, name, title, widget):
        widget.setObjectName(name)
        self.pages.addWidget(widget)
        self.pivot.addItem(
            routeKey=name, text=title,
            onClick=lambda: self.pages.setCurrentWidget(widget))

    def _audio_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        form = QFormLayout()
        outer.addLayout(form)

        self.input_box = ComboBox()
        self.output_box = ComboBox()
        for box, is_input, current in (
                (self.input_box, True, self.settings.input_device_index),
                (self.output_box, False, self.settings.output_device_index)):
            box.addItem(t("settings.system_default"), userData=None)
            for index, name in self._devices(is_input):
                box.addItem(name, userData=index)
            position = self._find_data(box, current)
            box.setCurrentIndex(position if position >= 0 else 0)

        form.addRow(BodyLabel(t("settings.mic")), self.input_box)
        form.addRow(BodyLabel(t("settings.speaker")), self.output_box)

        self.mic_slider = Slider(Qt.Orientation.Horizontal)
        self.mic_slider.setRange(0, 200)
        self.mic_slider.setValue(int(self.settings.mic_volume))
        self.speaker_slider = Slider(Qt.Orientation.Horizontal)
        self.speaker_slider.setRange(0, 200)
        self.speaker_slider.setValue(int(self.settings.speaker_volume))
        form.addRow(BodyLabel(t("settings.mic_volume")), self.mic_slider)
        form.addRow(BodyLabel(t("settings.speaker_volume")), self.speaker_slider)

        self.language_box = ComboBox()
        for code, name in i18n.available().items():
            self.language_box.addItem(name, userData=code)
        position = self._find_data(self.language_box, i18n.current())
        self.language_box.setCurrentIndex(max(0, position))
        form.addRow(BodyLabel(t("settings.language")), self.language_box)

        outer.addWidget(StrongBodyLabel(t("settings.ptt_title")))
        self.ptt_list = PttBindingList(self.settings.ptt_bindings)
        outer.addWidget(self.ptt_list)

        log_row = QHBoxLayout()
        self.debug_check = CheckBox(t("settings.debug"))
        self.debug_check.setChecked(bool(self.settings.debug))
        self.debug_check.setToolTip(t("settings.debug_tip"))
        open_log = PushButton(FluentIcon.FOLDER, t("settings.open_log"))
        open_log.clicked.connect(lambda: applog.open_log_folder())
        log_row.addWidget(self.debug_check)
        log_row.addStretch()
        log_row.addWidget(open_log)
        outer.addLayout(log_row)

        outer.addStretch()
        return page

    def _network_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        self.mumble_input = LineEdit()
        self.mumble_input.setText(self.settings.mumble_host)
        self.fsd_input = LineEdit()
        self.fsd_input.setText(self.settings.fsd_host)
        self.fsd_port_input = SpinBox()
        self.fsd_port_input.setRange(1, 65535)
        self.fsd_port_input.setValue(int(self.settings.fsd_port))
        self.real_name_input = LineEdit()
        self.real_name_input.setText(self.settings.real_name)
        self.voice_check = CheckBox(t("settings.connect_voice"))
        self.voice_check.setChecked(bool(self.settings.connect_voice))
        self.fsd_check = CheckBox(t("settings.connect_fsd"))
        self.fsd_check.setChecked(bool(self.settings.connect_fsd))

        form.addRow(BodyLabel(t("settings.mumble_host")), self.mumble_input)
        form.addRow(BodyLabel(t("settings.fsd_host")), self.fsd_input)
        form.addRow(BodyLabel(t("settings.fsd_port")), self.fsd_port_input)
        form.addRow(BodyLabel(t("settings.real_name")), self.real_name_input)
        form.addRow(self.voice_check)
        form.addRow(self.fsd_check)
        return page

    def _traffic_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        layout.addLayout(form)

        self.render_check = CheckBox(t("settings.render_traffic"))
        self.render_check.setChecked(bool(self.settings.render_traffic))
        form.addRow(self.render_check)

        row = QHBoxLayout()
        self.csl_input = LineEdit()
        self.csl_input.setText(self.settings.csl_path)
        self.csl_input.setPlaceholderText(t("settings.csl_hint"))
        browse = PushButton(FluentIcon.FOLDER, t("settings.browse"))
        browse.clicked.connect(self._browse_csl)
        row.addWidget(self.csl_input)
        row.addWidget(browse)
        form.addRow(BodyLabel(t("settings.csl_path")), row)

        self.range_input = SpinBox()
        self.range_input.setRange(5, 200)
        self.range_input.setSuffix(t("settings.range_suffix"))
        self.range_input.setValue(int(self.settings.traffic_range_nm or 60))
        form.addRow(BodyLabel(t("settings.range")), self.range_input)

        note = CaptionLabel(t("settings.traffic_note"))
        note.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addWidget(StrongBodyLabel(t("plugin.title")))
        self.plugin_status = BodyLabel("")
        self.plugin_status.setWordWrap(True)
        layout.addWidget(self.plugin_status)
        self.plugin_hint = CaptionLabel("")
        self.plugin_hint.setWordWrap(True)
        layout.addWidget(self.plugin_hint)

        plugin_row = QHBoxLayout()
        self.plugin_button = PushButton(FluentIcon.DOWNLOAD, t("plugin.install"))
        self.plugin_button.clicked.connect(self._install_plugin)
        pick = PushButton(FluentIcon.FOLDER, t("plugin.pick_root"))
        pick.clicked.connect(self._pick_xplane_root)
        plugin_row.addWidget(self.plugin_button)
        plugin_row.addWidget(pick)
        plugin_row.addStretch()
        layout.addLayout(plugin_row)
        self._refresh_plugin_status()

        layout.addStretch()
        return page

    # ---------- 他机插件 ----------
    def _refresh_plugin_status(self):
        """探测一次，把三行文字和按钮的状态一起刷新。

        探测是纯读文件，很便宜，所以每次开对话框、每次选目录、每次装完都重来
        一遍——记着上一次的结果会在用户中途装了 XPPython3 之后继续说"没装"。
        """
        status = xpinstall.inspect(self.settings.xplane_path)
        self._plugin = status

        if status.state == xpinstall.NO_ROOT:
            self.plugin_status.setText(t("plugin.no_root"))
        elif status.state == xpinstall.NOT_XPLANE:
            self.plugin_status.setText(t("plugin.not_xplane", path=status.root))
        else:
            key = {xpinstall.MISSING: "plugin.missing",
                   xpinstall.OUTDATED: "plugin.outdated",
                   xpinstall.CURRENT: "plugin.current"}[status.state]
            self.plugin_status.setText(t(key, path=status.root))

        # 提示行按严重程度排：协议对不上是静默失效，最该先说；其次是没装
        # XPPython3——那种情况下装了插件也不会被加载。
        if status.protocol_mismatch:
            self.plugin_hint.setText(t("plugin.protocol_mismatch",
                                       installed=status.installed_protocol,
                                       bundled=status.bundled_protocol))
            self.plugin_hint.setStyleSheet(f"color: {theme.MUTED_COLOR};")
        elif status.can_install and not status.xppython3:
            self.plugin_hint.setText(t("plugin.no_xppython3"))
            self.plugin_hint.setStyleSheet(f"color: {theme.ACTIVE_COLOR};")
        else:
            self.plugin_hint.setText("")

        label = {xpinstall.MISSING: "plugin.install",
                 xpinstall.OUTDATED: "plugin.update",
                 xpinstall.CURRENT: "plugin.reinstall"}.get(status.state,
                                                            "plugin.install")
        self.plugin_button.setText(t(label))
        self.plugin_button.setEnabled(status.can_install)

    def _pick_xplane_root(self):
        from PyQt6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, t("plugin.pick_title"),
                                                self.settings.xplane_path or "")
        if not path:
            return
        # 选错了也存下来：状态行会说清楚"这里没有 Resources/plugins"，比默默
        # 退回自动探测好——后者看起来像点了没反应。
        self.settings.xplane_path = path
        self._refresh_plugin_status()

    def _install_plugin(self):
        status = getattr(self, "_plugin", None)
        if status is None or not status.can_install:
            return
        try:
            target = xpinstall.install(status.root)
        except OSError as e:
            QMessageBox.warning(self, t("plugin.failed"),
                                t("plugin.failed_body", error=e))
            return
        # 记住这次用的目录，下次开对话框不用再探测
        self.settings.xplane_path = status.root
        self._refresh_plugin_status()
        # 装完必须重启模拟器，插件是 X-Plane 启动时加载的
        QMessageBox.information(self, t("plugin.installed"),
                                t("plugin.installed_body", path=target))

    def _browse_csl(self):
        from PyQt6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, t("settings.csl_pick"),
                                                self.csl_input.text())
        if path:
            self.csl_input.setText(path)

    @staticmethod
    def _find_data(combo, value):
        """qfluentwidgets 的 ComboBox 没有 findData()。"""
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                return i
        return -1

    @staticmethod
    def _devices(is_input):
        """列出输入或输出设备。没有 PyAudio 也要能把对话框打开。"""
        try:
            import pyaudio
            audio = pyaudio.PyAudio()
        except Exception as e:
            log.warning("could not enumerate the audio devices: %s", e)
            return []
        devices = []
        try:
            key = "maxInputChannels" if is_input else "maxOutputChannels"
            for index in range(audio.get_device_count()):
                info = audio.get_device_info_by_index(index)
                if info.get(key, 0) > 0:
                    devices.append((index, info.get("name", str(index))))
        except Exception as e:
            log.warning("could not read the audio device info: %s", e)
        finally:
            try:
                audio.terminate()
            except Exception:
                pass
        return devices

    def cleanup(self):
        """把录制线程停掉。

        对话框关了而录制还开着的话，pynput 的监听器会一直挂着，用户之后随便按个
        键就会被录进一个已经不存在的对话框里。
        """
        self.ptt_list.stop_capture()

    def reject(self):
        self.cleanup()
        super().reject()

    def accept(self):
        self.cleanup()
        super().accept()

    def apply(self):
        self.settings.input_device_index = self.input_box.currentData()
        self.settings.output_device_index = self.output_box.currentData()
        self.settings.mic_volume = self.mic_slider.value()
        self.settings.speaker_volume = self.speaker_slider.value()
        self.settings.ptt_bindings = list(self.ptt_list.bindings)
        self.settings.debug = self.debug_check.isChecked()
        self.settings.language = self.language_box.currentData()
        i18n.set_language(self.settings.language)
        self.settings.mumble_host = self.mumble_input.text().strip()
        self.settings.fsd_host = self.fsd_input.text().strip()
        self.settings.fsd_port = self.fsd_port_input.value()
        self.settings.real_name = self.real_name_input.text().strip()
        self.settings.connect_voice = self.voice_check.isChecked()
        self.settings.connect_fsd = self.fsd_check.isChecked()
        self.settings.render_traffic = self.render_check.isChecked()
        self.settings.csl_path = self.csl_input.text().strip()
        self.settings.traffic_range_nm = self.range_input.value()


class FlightPlanDialog(QDialog):
    """飞行计划，字段对应 $FP 包。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("plan.title"))
        self.setStyleSheet(theme.dialog_qss())
        self.setMinimumWidth(560)
        saved = dict(getattr(settings, "flight_plan", {}) or {})

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.rules = ComboBox()
        self.rules.addItem(t("plan.ifr"), userData="I")
        self.rules.addItem(t("plan.vfr"), userData="V")
        position = SettingsDialog._find_data(self.rules, saved.get("rules", "I"))
        self.rules.setCurrentIndex(max(0, position))

        self.aircraft = self._line(saved.get("aircraft", settings.aircraft))
        self.cruise_speed = self._line(saved.get("cruise_speed", ""))
        self.departure = self._line(saved.get("departure", ""))
        self.arrival = self._line(saved.get("arrival", ""))
        self.alternate = self._line(saved.get("alternate", ""))
        self.cruise_altitude = self._line(saved.get("cruise_altitude", ""))
        self.departure_time = self._line(saved.get("departure_time", ""))
        self.departure_time.setPlaceholderText(t("plan.time_hint"))
        self.route = PlainTextEdit()
        self.route.setPlainText(saved.get("route", ""))
        self.route.setMaximumHeight(80)
        self.remarks = PlainTextEdit()
        self.remarks.setPlainText(saved.get("remarks", ""))
        self.remarks.setMaximumHeight(60)

        form.addRow(BodyLabel(t("plan.rules")), self.rules)
        form.addRow(BodyLabel(t("plan.aircraft")), self.aircraft)
        form.addRow(BodyLabel(t("plan.cruise_speed")), self.cruise_speed)
        form.addRow(BodyLabel(t("plan.departure")), self.departure)
        form.addRow(BodyLabel(t("plan.arrival")), self.arrival)
        form.addRow(BodyLabel(t("plan.alternate")), self.alternate)
        form.addRow(BodyLabel(t("plan.cruise_altitude")), self.cruise_altitude)
        form.addRow(BodyLabel(t("plan.departure_time")), self.departure_time)

        # 航路时间和续航时间是 $FP 的必填段，漏了服务端会整包拒收
        self.enroute_hours = self._line(saved.get("enroute_hours", "0"))
        self.enroute_minutes = self._line(saved.get("enroute_minutes", "0"))
        self.fuel_hours = self._line(saved.get("fuel_hours", "0"))
        self.fuel_minutes = self._line(saved.get("fuel_minutes", "0"))
        for widget in (self.enroute_hours, self.enroute_minutes,
                       self.fuel_hours, self.fuel_minutes):
            widget.setMaximumWidth(60)

        enroute_row = QHBoxLayout()
        enroute_row.addWidget(self.enroute_hours)
        enroute_row.addWidget(BodyLabel(t("plan.hours")))
        enroute_row.addWidget(self.enroute_minutes)
        enroute_row.addWidget(BodyLabel(t("plan.minutes")))
        enroute_row.addStretch()
        form.addRow(BodyLabel(t("plan.enroute")), enroute_row)

        fuel_row = QHBoxLayout()
        fuel_row.addWidget(self.fuel_hours)
        fuel_row.addWidget(BodyLabel(t("plan.hours")))
        fuel_row.addWidget(self.fuel_minutes)
        fuel_row.addWidget(BodyLabel(t("plan.minutes")))
        fuel_row.addStretch()
        form.addRow(BodyLabel(t("plan.fuel")), fuel_row)
        form.addRow(BodyLabel(t("plan.route")), self.route)
        form.addRow(BodyLabel(t("plan.remarks")), self.remarks)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _line(text):
        widget = LineEdit()
        widget.setText(text or "")
        return widget

    def plan(self):
        return {
            "rules": self.rules.currentData(),
            "aircraft": self.aircraft.text().strip().upper(),
            "cruise_speed": self.cruise_speed.text().strip(),
            "departure": self.departure.text().strip().upper(),
            "arrival": self.arrival.text().strip().upper(),
            "alternate": self.alternate.text().strip().upper(),
            "cruise_altitude": self.cruise_altitude.text().strip(),
            "departure_time": self.departure_time.text().strip(),
            "actual_time": self.departure_time.text().strip(),
            "enroute_hours": self.enroute_hours.text().strip() or "0",
            "enroute_minutes": self.enroute_minutes.text().strip() or "0",
            "fuel_hours": self.fuel_hours.text().strip() or "0",
            "fuel_minutes": self.fuel_minutes.text().strip() or "0",
            "route": self.route.toPlainText().strip().upper(),
            "remarks": self.remarks.toPlainText().strip(),
        }


def main():
    settings = Settings()
    applog.setup(debug="--debug" in sys.argv or bool(settings.debug))
    logging.getLogger("startup").info("%s %s", APP_NAME, version.full())
    app = QApplication(sys.argv)
    theme.apply_theme()             # 必须在建窗口之前
    window = XpcWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
