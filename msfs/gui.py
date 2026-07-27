"""MSFS for CAN — Microsoft Flight Simulator 飞行员客户端。

和 xpc/（X-Plane 版）同一个布局和同一套模型，换掉的只有模拟器那一层：上面一条
连接栏，中间是消息区，右边是附近的管制席位，底下是无线电状态和 PTT。

三条链路各自独立，一条断了不影响另外两条：

    simlink.SimLink      从 MSFS 经 SimConnect 取位置、姿态、应答机和 COM1
    fsdpilot.FSDPilot    以飞行员身份连 FSD，把位置报上去，收发文字
    voice.Voice          Mumble 语音，频道跟着 COM1 走

他机比 X-Plane 版简单：SimConnect 允许外部进程直接建 AI 飞机，不需要插件，也
不用单独喂 TCAS——AI 飞机是真的 SimObject，座舱交通显示本来就看得见。

pymumble 和 FSD 的回调都在各自的后台线程上，碰 Qt 控件必须经 pyqtSignal 转到
GUI 线程——直接改控件在 Qt 里是未定义行为，表现出来是随机崩溃。
"""

import os

# 这两行必须在 import pygame 之前。pygame 只用来读摇杆，SDL 的视频和音频后端
# 一起来会和 PyQt6、PyAudio 抢设备。
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
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSlider, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

import aimatch
import fsdpilot
import inject
import simlink
import traffic as traffic_module
import voice as voice_module
from settings import Settings

log = logging.getLogger("界面")

APP_NAME = "MSFS for CAN"
VERSION = "1.0.0"

GREEN = "#2ecc71"
RED = "#e74c3c"
AMBER = "#f39c12"
GREY = "#555b63"


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
        self.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.set_lit(False)

    def set_lit(self, lit):
        colour = self._colour if lit else GREY
        text = "#ffffff" if lit else "#9aa0a6"
        self.setStyleSheet(
            f"background-color: {colour}; color: {text};"
            "border-radius: 4px; padding: 2px;")


class MsfsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.signals = Signals()

        self.sim = simlink.SimLink(on_state=self.signals.sim_state.emit)
        self.fsd = None
        self.voice = None
        self.snapshot = None

        # 他机：FSD 线程往表里写，tick() 读出来插值好放进模拟器
        self.traffic = traffic_module.TrafficTable()
        self.injector = None            # 要等 SimConnect 连上才能建
        self._injecting = threading.Event()   # 上一轮注入还没做完
        self.models = aimatch.ModelSet()
        self._model_cache = {}          # 呼号 -> 匹配到的 title
        self._load_models()
        self._joystick = None
        self._ptt_thread = None
        self._ptt_running = False

        self.setWindowTitle(f"{APP_NAME} v{VERSION}")
        self.resize(980, 660)
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
        self.statusBar().showMessage("就绪")

    def _build_menu(self):
        menu = self.menuBar()

        file_menu = menu.addMenu("文件(&F)")
        plan_action = QAction("飞行计划(&P)…", self)
        plan_action.triggered.connect(self.open_flight_plan)
        file_menu.addAction(plan_action)
        settings_action = QAction("设置(&S)…", self)
        settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction("退出(&Q)", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = menu.addMenu("帮助(&H)")
        log_action = QAction("打开日志目录(&L)", self)
        log_action.triggered.connect(lambda: applog.open_log_folder())
        help_menu.addAction(log_action)
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _build_connect_bar(self):
        box = QGroupBox("连接")
        grid = QGridLayout(box)

        self.callsign_input = QLineEdit(self.settings.callsign)
        self.callsign_input.setPlaceholderText("如 CCA1501")
        self.callsign_input.setMaxLength(fsdpilot.MAX_CALLSIGN_LENGTH)
        self.cid_input = QLineEdit(self.settings.cid)
        self.cid_input.setPlaceholderText("ASN ID")
        self.password_input = QLineEdit(self.settings.password)
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.aircraft_input = QLineEdit(self.settings.aircraft)
        self.aircraft_input.setPlaceholderText("如 B738")

        grid.addWidget(QLabel("呼号"), 0, 0)
        grid.addWidget(self.callsign_input, 0, 1)
        grid.addWidget(QLabel("ASN ID"), 0, 2)
        grid.addWidget(self.cid_input, 0, 3)
        grid.addWidget(QLabel("密码"), 0, 4)
        grid.addWidget(self.password_input, 0, 5)
        grid.addWidget(QLabel("机型"), 0, 6)
        grid.addWidget(self.aircraft_input, 0, 7)

        self.connect_button = QPushButton("连接")
        self.connect_button.setMinimumWidth(110)
        self.connect_button.clicked.connect(self.toggle_connection)
        grid.addWidget(self.connect_button, 0, 8)

        self.sim_label = QLabel("MSFS：等待中")
        self.fsd_label = QLabel("网络：未连接")
        self.voice_label = QLabel("语音：未连接")
        for i, label in enumerate((self.sim_label, self.fsd_label, self.voice_label)):
            label.setStyleSheet("color: #9aa0a6;")
            grid.addWidget(label, 1, i * 3, 1, 3)
        return box

    def _build_messages(self):
        box = QGroupBox("消息")
        layout = QVBoxLayout(box)

        self.messages = QTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setFont(QFont("Consolas", 10))
        layout.addWidget(self.messages)

        row = QHBoxLayout()
        self.recipient_input = QLineEdit()
        self.recipient_input.setPlaceholderText("收件人（呼号，留空发到当前频率）")
        self.recipient_input.setMaximumWidth(240)
        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("输入消息后回车发送")
        self.message_input.returnPressed.connect(self.send_message)
        send = QPushButton("发送")
        send.clicked.connect(self.send_message)
        row.addWidget(self.recipient_input)
        row.addWidget(self.message_input)
        row.addWidget(send)
        layout.addLayout(row)
        return box

    def _build_controllers(self):
        box = QGroupBox("附近管制")
        layout = QVBoxLayout(box)
        self.controller_list = QListWidget()
        self.controller_list.setFont(QFont("Consolas", 10))
        self.controller_list.itemDoubleClicked.connect(self.controller_clicked)
        layout.addWidget(self.controller_list)
        hint = QLabel("双击把呼号填进收件人")
        hint.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        layout.addWidget(hint)
        return box

    def _build_radio_bar(self):
        box = QGroupBox("无线电")
        layout = QHBoxLayout(box)

        self.tx_light = Indicator("TX", RED)
        self.rx_light = Indicator("RX", GREEN)
        layout.addWidget(self.tx_light)
        layout.addWidget(self.rx_light)

        self.com1_label = QLabel("COM1  ---.---")
        self.com1_label.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        layout.addWidget(self.com1_label)

        self.channel_label = QLabel("")
        self.channel_label.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.channel_label)

        layout.addStretch()

        self.traffic_label = QLabel("他机 0")
        self.traffic_label.setFont(QFont("Consolas", 10))
        self.traffic_label.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.traffic_label)

        self.position_label = QLabel("位置 --")
        self.position_label.setFont(QFont("Consolas", 10))
        self.position_label.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.position_label)

        self.ident_button = QPushButton("IDENT")
        self.ident_button.setEnabled(False)
        self.ident_button.clicked.connect(self.send_ident)
        layout.addWidget(self.ident_button)

        self.ptt_button = QPushButton("按住通话")
        self.ptt_button.setMinimumWidth(130)
        self.ptt_button.pressed.connect(lambda: self.set_ptt(True))
        self.ptt_button.released.connect(lambda: self.set_ptt(False))
        layout.addWidget(self.ptt_button)
        return box

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
    def add_message(self, text, colour="#dcdcdc"):
        stamp = time.strftime("%H:%M:%S")
        self.messages.append(
            f'<span style="color:#7f8c8d">{stamp}</span> '
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
            QMessageBox.warning(self, "呼号不可用", problem)
            return
        if not cid or not password:
            QMessageBox.warning(self, "缺少信息", "请填写 ASN ID 和密码。")
            return
        if not self.sim.connected:
            answer = QMessageBox.question(
                self, "MSFS 未连接",
                "还没有从 MSFS 收到数据。没有位置就无法把飞机报到网络上。\n\n"
                "仍然继续连接吗？",
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
        self.connect_button.setText("连接中…")

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

        self._start_ptt_watch()
        self.connect_button.setEnabled(True)
        self.connect_button.setText("断开")

    def disconnect_all(self):
        self._stop_ptt_watch()
        if self.fsd:
            self.fsd.stop()
            self.fsd = None
        if self.voice:
            voice = self.voice
            self.voice = None
            threading.Thread(target=voice.stop, daemon=True).start()
        # 断开网络就没有他机来源了，把已经放进去的清掉，否则会冻在天上
        if self.injector is not None:
            self.injector.clear()
        self._model_cache.clear()
        self.connect_button.setText("连接")
        self.ident_button.setEnabled(False)
        self.controller_list.clear()
        self.traffic_label.setText("他机 0")
        self.fsd_label.setText("网络：未连接")
        self.voice_label.setText("语音：未连接")
        self.channel_label.setText("")
        self.tx_light.set_lit(False)
        self.rx_light.set_lit(False)
        self.add_message("已断开", AMBER)

    # ---------- 定时 ----------
    def tick(self):
        """每 0.5 秒：把模拟器的数据分发到 FSD 和语音。"""
        snapshot = self.sim.snapshot()
        if not snapshot:
            return
        self.snapshot = snapshot

        com1 = snapshot.get("com1")
        self.com1_label.setText(f"COM1  {com1:.3f}" if com1 else "COM1  ---.---")
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
    def package_roots(self):
        """要扫哪些目录找飞机。没配就自动探测。"""
        roots = [r for r in (self.settings.package_roots or []) if r]
        return roots or aimatch.default_roots()

    def _load_models(self):
        """扫一遍装好的飞机。

        扫盘可能要几秒（社区包多的话上万个文件），放后台，界面不要卡住。
        """
        roots = self.package_roots()
        if not roots:
            log.warning("没有找到 MSFS 的包目录，他机将没有模型可用")
            return

        def scan():
            try:
                models = aimatch.ModelSet.load(*roots)
            except Exception as e:
                log.warning("扫描飞机失败: %s", e)
                return
            self.models = models
            self._model_cache.clear()
            log.info("载入 %d 个涂装，覆盖 %d 种机型",
                     len(models), len(models.types))

        threading.Thread(target=scan, daemon=True).start()

    def _push_traffic(self, snapshot):
        """把他机插值到当前时刻，放进模拟器。"""
        if not self.settings.render_traffic:
            return
        self.traffic.prune()

        # 注入器要等 SimConnect 真的连上才能建
        if self.injector is None and self.sim.sim is not None:
            self.injector = inject.TrafficInjector(self.sim.sim)
            if not self.injector.available:
                self.add_message("他机注入不可用，详见日志", AMBER)

        origin = (snapshot["latitude"], snapshot["longitude"])
        entries = self.traffic.snapshot(
            origin=origin,
            limit=inject.MAX_AIRCRAFT,
            max_range_nm=self.settings.traffic_range_nm or None)

        for entry in entries:
            entry["model"] = self._model_for(entry)

        # 注入走后台线程。sync() 里每架飞机都是一次 SimConnect 同步 IPC，最多
        # 40 架、每 0.5 秒一轮——在 Qt 主线程上做这件事窗口会"未响应"。
        # 上一轮还没做完就跳过这一轮，别让请求越堆越多。
        if self.injector is not None and not self._injecting.is_set():
            self._injecting.set()
            threading.Thread(target=self._inject, args=(entries,),
                             daemon=True).start()
        self.traffic_label.setText(f"他机 {len(entries)}")

    def _inject(self, entries):
        try:
            self.injector.sync(entries)
        except Exception as e:
            log.warning("放他机进模拟器出错: %s", e)
        finally:
            self._injecting.clear()

    def _model_for(self, entry):
        """给一架飞机挑模型。匹配结果缓存住，别每帧都算。

        模拟器拒绝生成过的模型要排除掉。缓存也要跟着失效——否则会一直返回那个
        建不出来的模型，注入端又因为它在黑名单里而跳过，飞机永远出不来。
        """
        callsign = entry["callsign"]
        rejected = self.injector.bad_titles if self.injector else frozenset()
        cached = self._model_cache.get(callsign)
        if (not entry.get("model_dirty") and cached is not None
                and cached not in rejected):
            return cached

        model, why = self.models.match(
            equipment=entry.get("equipment", ""),
            airline=entry.get("airline", ""),
            csl=entry.get("csl", ""),
            exclude=rejected)
        title = model.title if model else ""
        self._model_cache[callsign] = title
        self.traffic.mark_model_clean(callsign)
        if model:
            log.info("%s (%s/%s) → %s：%s", callsign,
                     entry.get("equipment") or "?", entry.get("airline") or "?",
                     model.title, why)
        else:
            log.debug("%s 没有模型可用：%s", callsign, why)
        return title

    # ---------- 槽 ----------
    def on_sim_state(self, connected, message):
        self.sim_label.setText(f"MSFS：{'已连接' if connected else '等待中'}")
        self.sim_label.setStyleSheet(f"color: {GREEN if connected else '#9aa0a6'};")
        self.add_message(f"[MSFS] {message}", GREEN if connected else AMBER)

    def on_fsd_status(self, state, message):
        colour = {'online': GREEN, 'error': RED}.get(state, AMBER)
        self.fsd_label.setText(f"网络：{'已上线' if state == 'online' else state}")
        self.fsd_label.setStyleSheet(f"color: {colour};")
        self.add_message(f"[网络] {message}", colour)
        self.ident_button.setEnabled(state == 'online')
        if state == 'error':
            # 网络断了不该把语音一起收掉，两条链路互不依赖
            self.fsd = None
            self.connect_button.setText("断开" if self.voice else "连接")

    def on_voice_status(self, state, message):
        colour = {'online': GREEN, 'error': RED}.get(state, AMBER)
        self.voice_label.setText(f"语音：{'已连接' if state == 'online' else state}")
        self.voice_label.setStyleSheet(f"color: {colour};")
        self.add_message(f"[语音] {message}", colour)
        if state == 'error':
            self.voice = None
            self.connect_button.setText("断开" if self.fsd else "连接")

    def on_text_message(self, sender, recipient, body):
        if recipient.startswith("@"):
            self.add_message(f"{sender} → {recipient}: {body}", "#5dade2")
        else:
            self.add_message(f"{sender}: {body}", "#f7dc6f")

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
            self.add_message("尚未连接到网络，消息没有发出去", RED)
            return
        recipient = self.recipient_input.text().strip().upper()
        if not recipient:
            com1 = (self.snapshot or {}).get("com1")
            if not com1:
                self.add_message("没有收件人，也读不到 COM1 频率", RED)
                return
            # 发到频率上：@ 后面是 5 位电台频率，去掉开头的 1 和小数点
            recipient = f"@{int(round(com1 * 1000)) % 100000:05d}"
        if self.fsd.send_text(recipient, body):
            self.add_message(f"我 → {recipient}: {body}", "#aab7b8")
            self.message_input.clear()

    def send_ident(self):
        if self.fsd and self.fsd.connected:
            self.fsd.ident()
            self.add_message("IDENT", AMBER)

    def set_ptt(self, value):
        if self.voice:
            self.voice.set_transmitting(value)

    # ---------- 按键 / 摇杆 PTT ----------
    def _start_ptt_watch(self):
        if self._ptt_running:
            return
        self._ptt_running = True
        self._ptt_thread = threading.Thread(target=self._ptt_loop, daemon=True)
        self._ptt_thread.start()

    def _stop_ptt_watch(self):
        self._ptt_running = False
        thread = self._ptt_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._ptt_thread = None

    def _ptt_loop(self):
        """轮询快捷键和摇杆按钮。"""
        try:
            import keyboard
        except Exception as e:
            log.warning("按键 PTT 不可用: %s", e)
            keyboard = None

        joystick = self._open_joystick()
        pressed = False
        while self._ptt_running:
            state = False
            try:
                if keyboard and self.settings.ptt_key:
                    state = keyboard.is_pressed(self.settings.ptt_key)
                if not state and joystick is not None and self.settings.joystick_ptt is not None:
                    import pygame
                    pygame.event.pump()
                    if self.settings.joystick_ptt < joystick.get_numbuttons():
                        state = bool(joystick.get_button(self.settings.joystick_ptt))
            except Exception as e:
                log.debug("读 PTT 出错: %s", e)

            if state != pressed:
                pressed = state
                self.set_ptt(state)
            time.sleep(0.02)

        if pressed:
            self.set_ptt(False)

    def _open_joystick(self):
        if self.settings.joystick_ptt is None:
            return None
        try:
            import pygame
            if not pygame.get_init():
                pygame.init()
            if not pygame.joystick.get_init():
                pygame.joystick.init()
            if pygame.joystick.get_count() == 0:
                return None
            stick = pygame.joystick.Joystick(0)
            stick.init()
            log.info("摇杆: %s", stick.get_name())
            return stick
        except Exception as e:
            log.warning("摇杆初始化失败: %s", e)
            return None

    # ---------- 对话框 ----------
    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            previous_roots = list(self.settings.package_roots or [])
            dialog.apply()
            self.settings.save()
            if list(self.settings.package_roots or []) != previous_roots:
                self._load_models()
            if self.voice:
                try:
                    self.voice.reopen_audio()
                except Exception as e:
                    QMessageBox.warning(self, "音频设备", f"重开音频设备失败：{e}")

    def open_flight_plan(self):
        dialog = FlightPlanDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        plan = dialog.plan()
        self.settings.flight_plan = plan
        self.settings.save()
        if self.fsd and self.fsd.connected:
            if self.fsd.file_flight_plan(plan):
                self.add_message("飞行计划已提交", GREEN)
        else:
            self.add_message("尚未连接到网络，飞行计划只保存在本地", AMBER)

    def show_about(self):
        QMessageBox.information(
            self, "关于",
            f"{APP_NAME} v{VERSION}\n\n"
            "Cerulean Aviation Network 的 MSFS 飞行员客户端。\n"
            "语音走 Mumble，网络走 FSD，飞行数据经 SimConnect 取。\n\n"
            f"日志：{applog.log_path() or '（未写入文件）'}")

    def closeEvent(self, event):
        self.timer.stop()
        self._stop_ptt_watch()
        if self.fsd:
            self.fsd.stop()
        if self.voice:
            self.voice.stop()
        self.sim.stop()
        # 放进模拟器的 AI 飞机不会自己消失，不清掉会在天上冻住
        if self.injector is not None:
            self.injector.clear()
        self.settings.save()
        event.accept()


class SettingsDialog(QDialog):
    """音频设备、音量和 PTT。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        tabs.addTab(self._audio_tab(), "音频")
        tabs.addTab(self._network_tab(), "网络")
        tabs.addTab(self._traffic_tab(), "他机")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _audio_tab(self):
        page = QWidget()
        form = QFormLayout(page)

        self.input_box = QComboBox()
        self.output_box = QComboBox()
        for box, is_input, current in (
                (self.input_box, True, self.settings.input_device_index),
                (self.output_box, False, self.settings.output_device_index)):
            box.addItem("系统默认", None)
            for index, name in self._devices(is_input):
                box.addItem(name, index)
            position = box.findData(current)
            box.setCurrentIndex(position if position >= 0 else 0)

        form.addRow("麦克风", self.input_box)
        form.addRow("扬声器", self.output_box)

        self.mic_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_slider.setRange(0, 200)
        self.mic_slider.setValue(int(self.settings.mic_volume))
        self.speaker_slider = QSlider(Qt.Orientation.Horizontal)
        self.speaker_slider.setRange(0, 200)
        self.speaker_slider.setValue(int(self.settings.speaker_volume))
        form.addRow("麦克风音量", self.mic_slider)
        form.addRow("扬声器音量", self.speaker_slider)

        self.ptt_key_input = QLineEdit(self.settings.ptt_key or "")
        form.addRow("PTT 按键", self.ptt_key_input)

        self.joystick_box = QSpinBox()
        self.joystick_box.setRange(-1, 63)
        self.joystick_box.setSpecialValueText("不使用")
        self.joystick_box.setValue(
            -1 if self.settings.joystick_ptt is None else int(self.settings.joystick_ptt))
        form.addRow("摇杆 PTT 按钮", self.joystick_box)
        return page

    def _network_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        self.mumble_input = QLineEdit(self.settings.mumble_host)
        self.fsd_input = QLineEdit(self.settings.fsd_host)
        self.fsd_port_input = QSpinBox()
        self.fsd_port_input.setRange(1, 65535)
        self.fsd_port_input.setValue(int(self.settings.fsd_port))
        self.real_name_input = QLineEdit(self.settings.real_name)
        self.voice_check = QCheckBox("连接语音服务器")
        self.voice_check.setChecked(bool(self.settings.connect_voice))
        self.fsd_check = QCheckBox("连接 FSD 网络")
        self.fsd_check.setChecked(bool(self.settings.connect_fsd))

        form.addRow("语音服务器", self.mumble_input)
        form.addRow("FSD 服务器", self.fsd_input)
        form.addRow("FSD 端口", self.fsd_port_input)
        form.addRow("真实姓名", self.real_name_input)
        form.addRow(self.voice_check)
        form.addRow(self.fsd_check)
        return page

    def _traffic_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        layout.addLayout(form)

        self.render_check = QCheckBox("把其他飞机放进 MSFS")
        self.render_check.setChecked(bool(self.settings.render_traffic))
        form.addRow(self.render_check)

        row = QHBoxLayout()
        self.roots_input = QLineEdit("；".join(self.settings.package_roots or []))
        self.roots_input.setPlaceholderText("留空则自动探测")
        browse = QPushButton("添加…")
        browse.clicked.connect(self._browse_root)
        row.addWidget(self.roots_input)
        row.addWidget(browse)
        form.addRow("MSFS 包目录", row)

        self.range_input = QSpinBox()
        self.range_input.setRange(5, 200)
        self.range_input.setSuffix(" 海里")
        self.range_input.setValue(int(self.settings.traffic_range_nm or 60))
        form.addRow("显示范围", self.range_input)

        detected = aimatch.default_roots()
        note = QLabel(
            "不需要装任何插件——他机是通过 SimConnect 直接放进模拟器的，\n"
            "而且因为它们是真的 AI 飞机，座舱的交通显示本来就看得见。\n\n"
            "机型是从装好的飞机里挑的，读的是各个 aircraft.cfg 里的 title。\n"
            "装的飞机越多，别人的涂装越准；一架都匹配不到时会用兜底模型。\n\n"
            + (f"自动探测到 {len(detected)} 个包目录。" if detected else
               "没有自动探测到包目录，请手工指定。")
            + "多个目录用中文分号分隔。")
        note.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _browse_root(self):
        from PyQt6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "选择 MSFS 包目录")
        if not path:
            return
        current = [r for r in self.roots_input.text().split("；") if r.strip()]
        if path not in current:
            current.append(path)
        self.roots_input.setText("；".join(current))

    @staticmethod
    def _devices(is_input):
        """列出输入或输出设备。没有 PyAudio 也要能把对话框打开。"""
        try:
            import pyaudio
            audio = pyaudio.PyAudio()
        except Exception as e:
            log.warning("列举音频设备失败: %s", e)
            return []
        devices = []
        try:
            key = "maxInputChannels" if is_input else "maxOutputChannels"
            for index in range(audio.get_device_count()):
                info = audio.get_device_info_by_index(index)
                if info.get(key, 0) > 0:
                    devices.append((index, info.get("name", f"设备 {index}")))
        except Exception as e:
            log.warning("读取音频设备信息失败: %s", e)
        finally:
            try:
                audio.terminate()
            except Exception:
                pass
        return devices

    def apply(self):
        self.settings.input_device_index = self.input_box.currentData()
        self.settings.output_device_index = self.output_box.currentData()
        self.settings.mic_volume = self.mic_slider.value()
        self.settings.speaker_volume = self.speaker_slider.value()
        self.settings.ptt_key = self.ptt_key_input.text().strip()
        value = self.joystick_box.value()
        self.settings.joystick_ptt = None if value < 0 else value
        self.settings.mumble_host = self.mumble_input.text().strip()
        self.settings.fsd_host = self.fsd_input.text().strip()
        self.settings.fsd_port = self.fsd_port_input.value()
        self.settings.real_name = self.real_name_input.text().strip()
        self.settings.connect_voice = self.voice_check.isChecked()
        self.settings.connect_fsd = self.fsd_check.isChecked()
        self.settings.render_traffic = self.render_check.isChecked()
        self.settings.package_roots = [
            r.strip() for r in self.roots_input.text().split("；") if r.strip()]
        self.settings.traffic_range_nm = self.range_input.value()


class FlightPlanDialog(QDialog):
    """飞行计划，字段对应 $FP 包。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("飞行计划")
        self.setMinimumWidth(560)
        saved = dict(getattr(settings, "flight_plan", {}) or {})

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.rules = QComboBox()
        self.rules.addItem("IFR 仪表", "I")
        self.rules.addItem("VFR 目视", "V")
        position = self.rules.findData(saved.get("rules", "I"))
        self.rules.setCurrentIndex(max(0, position))

        self.aircraft = QLineEdit(saved.get("aircraft", settings.aircraft))
        self.cruise_speed = QLineEdit(saved.get("cruise_speed", ""))
        self.departure = QLineEdit(saved.get("departure", ""))
        self.arrival = QLineEdit(saved.get("arrival", ""))
        self.alternate = QLineEdit(saved.get("alternate", ""))
        self.cruise_altitude = QLineEdit(saved.get("cruise_altitude", ""))
        self.departure_time = QLineEdit(saved.get("departure_time", ""))
        self.departure_time.setPlaceholderText("UTC，如 1230")
        self.route = QPlainTextEdit(saved.get("route", ""))
        self.route.setMaximumHeight(80)
        self.remarks = QPlainTextEdit(saved.get("remarks", ""))
        self.remarks.setMaximumHeight(60)

        form.addRow("飞行规则", self.rules)
        form.addRow("机型", self.aircraft)
        form.addRow("巡航速度", self.cruise_speed)
        form.addRow("起飞地", self.departure)
        form.addRow("目的地", self.arrival)
        form.addRow("备降场", self.alternate)
        form.addRow("巡航高度", self.cruise_altitude)
        form.addRow("预计起飞", self.departure_time)

        # 航路时间和续航时间是 $FP 的必填段，漏了服务端会整包拒收
        self.enroute_hours = QLineEdit(saved.get("enroute_hours", "0"))
        self.enroute_minutes = QLineEdit(saved.get("enroute_minutes", "0"))
        self.fuel_hours = QLineEdit(saved.get("fuel_hours", "0"))
        self.fuel_minutes = QLineEdit(saved.get("fuel_minutes", "0"))
        for widget in (self.enroute_hours, self.enroute_minutes,
                       self.fuel_hours, self.fuel_minutes):
            widget.setMaximumWidth(60)

        enroute_row = QHBoxLayout()
        enroute_row.addWidget(self.enroute_hours)
        enroute_row.addWidget(QLabel("小时"))
        enroute_row.addWidget(self.enroute_minutes)
        enroute_row.addWidget(QLabel("分钟"))
        enroute_row.addStretch()
        form.addRow("航路时间", enroute_row)

        fuel_row = QHBoxLayout()
        fuel_row.addWidget(self.fuel_hours)
        fuel_row.addWidget(QLabel("小时"))
        fuel_row.addWidget(self.fuel_minutes)
        fuel_row.addWidget(QLabel("分钟"))
        fuel_row.addStretch()
        form.addRow("续航时间", fuel_row)
        form.addRow("航路", self.route)
        form.addRow("备注", self.remarks)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
    applog.setup(debug="--debug" in sys.argv)
    logging.getLogger("启动").info("%s v%s", APP_NAME, VERSION)
    app = QApplication(sys.argv)
    window = MsfsWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
