"""界面文字的多语言。

用字典而不是 Qt 的 .ts/.qm：那一套要引入 pylupdate6 / lrelease 两个构建步骤，
再把二进制的 .qm 塞进每个 spec，而这个仓库从头到尾就是"纯 Python + PyInstaller"，
没有别的构建工具。字典跟着 .py 一起打包，不会漏。

用法：

    from i18n import t
    button.setText(t("main.settings"))
    label.setText(t("radio.last_rx", who="CES2345", stamp="12:00:00"))

约定：

- **键名统一在这里定义，源码里不再出现界面字面量**。这样漏翻的地方一眼能看出来
  （界面上会直接显示成 key），而不是混在中文里看不见。
- 每个键两种语言都必须有，test_i18n.py 会逐条核对——只翻一半的话，用户看到的是
  中英混排。
- 带占位符的用 str.format 的写法（`{who}`），不要用 % ——两种语言的语序不同，
  位置参数会错位，命名参数不会。
"""

import locale
import logging
import os

log = logging.getLogger("多语言")

DEFAULT = "zh"
LANGUAGES = {"zh": "中文", "en": "English"}

TEXT = {
    # ---------- 通用 ----------
    "app.title":            {"zh": "管制语音 - Airwaysn", "en": "Controller Voice - Airwaysn"},
    "app.name":             {"zh": "管制语音", "en": "Controller Voice"},

    # ---------- 登录 ----------
    "login.username":       {"zh": "用户名（ASN 号）", "en": "Username (ASN ID)"},
    "login.password":       {"zh": "密码", "en": "Password"},
    "login.connect":        {"zh": "连接", "en": "Connect"},
    "login.connecting":     {"zh": "正在连接…", "en": "Connecting…"},
    "login.disconnected":   {"zh": "未连接", "en": "Not connected"},
    "login.need_credentials": {"zh": "请填写用户名和密码",
                               "en": "Enter your username and password"},
    "login.failed":         {"zh": "连接失败", "en": "Connection failed"},

    # ---------- 主界面顶栏 ----------
    "main.connected":       {"zh": "已连接", "en": "Connected"},
    "main.disconnected":    {"zh": "已断开", "en": "Disconnected"},
    "main.pin":             {"zh": "置顶", "en": "Pin"},
    "main.pin_tip":         {"zh": "让窗口一直显示在其它窗口上面",
                             "en": "Keep this window above all others"},
    "main.settings":        {"zh": "设置", "en": "Settings"},
    "main.disconnect":      {"zh": "断开", "en": "Disconnect"},

    # ---------- 添加频率 ----------
    "main.freq_hint":       {"zh": "频率，例如 118.000", "en": "Frequency, e.g. 118.000"},
    "main.callsign_hint":   {"zh": "呼号，例如 ZSPD_TWR（可留空）",
                             "en": "Callsign, e.g. ZSPD_TWR (optional)"},
    "main.add":             {"zh": "添加频率", "en": "Add"},
    "main.add_failed":      {"zh": "添加失败", "en": "Could not add"},
    "main.empty":           {"zh": "电台栈是空的，先在上面加一个频率",
                             "en": "No frequencies yet — add one above"},

    # ---------- 电台 ----------
    "radio.rx_tip":         {"zh": "接收这个频率", "en": "Receive this frequency"},
    "radio.tx_tip":         {"zh": "PTT 按下时对这个频率发话",
                             "en": "Transmit here while PTT is held"},
    "radio.xc_tip":         {"zh": "和其它开了 XC 的频率交叉耦合",
                             "en": "Cross-couple with the other XC frequencies"},
    "radio.mute":           {"zh": "静音", "en": "Mute"},
    "radio.mute_tip":       {"zh": "静音这个频率", "en": "Mute this frequency"},
    "radio.remove_tip":     {"zh": "从电台栈移除", "en": "Remove from the stack"},
    "radio.remove_locked":  {"zh": "这是你正在管的席位频率，不能移除",
                             "en": "This is the position you are staffing — cannot be removed"},
    "radio.tx_needs_duty":  {"zh": "数据源上没有你的管制席位，只能收听",
                             "en": "You are not staffing a position — receive only"},
    "radio.no_callsign":    {"zh": "—", "en": "—"},
    "radio.last_rx_none":   {"zh": "最后通话: --", "en": "Last: --"},
    "radio.last_rx":        {"zh": "最后通话: {who}  {stamp}", "en": "Last: {who}  {stamp}"},

    # ---------- 底栏 / 状态 ----------
    "status.ready":         {"zh": "就绪", "en": "Ready"},
    "status.adopted":       {"zh": "已按你在网上的席位 {callsign} 自动加入 {frequency}",
                             "en": "Added {frequency} from your online position {callsign}"},
    "status.audio_device":  {"zh": "音频设备", "en": "Audio device"},

    # ---------- 值守状态（在不在管制席位上） ----------
    "duty.on":              {"zh": "值守 {callsign}", "en": "Staffing {callsign}"},
    "duty.observer":        {"zh": "未上席位 · 只收不发",
                             "en": "Not staffing · receive only"},
    "duty.tx_dropped":      {"zh": "已离开管制席位，所有 TX / XC 已关闭",
                             "en": "Left your position — all TX / XC turned off"},

    # ---------- 在线席位一览 ----------
    "online.title":         {"zh": "在线频率", "en": "Online frequencies"},
    "online.empty":         {"zh": "数据源上暂时没有在线席位",
                             "en": "No positions online"},
    "online.add_tip":       {"zh": "点一下把 {callsign} 的 {frequency} 加进电台栈",
                             "en": "Click to add {frequency} ({callsign}) to the stack"},
    "online.already":       {"zh": "{frequency} 已经在电台栈里了",
                             "en": "{frequency} is already in the stack"},
    "status.audio_failed":  {"zh": "切换设备失败: {error}",
                             "en": "Could not switch device: {error}"},

    # ---------- 电台栈的校验错误 ----------
    # radiostack.py 抛出来的 ValueError 会原样进 InfoBar，所以也要翻
    "stack.freq_empty":     {"zh": "频率不能为空", "en": "Enter a frequency"},
    "stack.freq_bad":       {"zh": "频率格式无效: {text}",
                             "en": "Not a valid frequency: {text}"},
    "stack.freq_range":     {"zh": "频率超出甚高频范围: {text}",
                             "en": "Outside the VHF band: {text}"},
    "stack.duplicate":      {"zh": "{frequency} 已经在电台栈里了",
                             "en": "{frequency} is already in the stack"},

    # ---------- 语音连接状态 ----------
    # voice.py 经 on_state 送到状态栏和登录页的文字
    "voice.connecting":     {"zh": "正在以 {cid} 连接 {server} …",
                             "en": "Connecting to {server} as {cid}…"},
    "voice.connect_failed": {"zh": "连接失败: {error}", "en": "Connection failed: {error}"},
    "voice.timeout":        {"zh": "连接 {server} 超时", "en": "Timed out connecting to {server}"},
    "voice.timeout_reason": {"zh": "连接 {server} 超时：{reason}",
                             "en": "Timed out connecting to {server}: {reason}"},
    "voice.no_response":    {"zh": "连接 {server} 超时，服务器没有响应",
                             "en": "Timed out connecting to {server} — no response"},
    "voice.audio_failed":   {"zh": "音频设备打开失败: {error}",
                             "en": "Could not open the audio device: {error}"},
    "voice.online":         {"zh": "已连接，账号 {cid}", "en": "Connected as {cid}"},
    "voice.dropped":        {"zh": "与语音服务器的连接已断开",
                             "en": "Lost the connection to the voice server"},
    "voice.stopped":        {"zh": "已断开", "en": "Disconnected"},
    "voice.rejected":       {"zh": "语音服务器拒绝了 {cid}：{reason}",
                             "en": "The voice server rejected {cid}: {reason}"},
    "voice.rejected_plain": {"zh": "到 {server} 的连接意外中断，服务器没有说明原因（详见日志）",
                             "en": "The connection to {server} ended unexpectedly with no "
                                   "reason given (see the log)"},

    # 服务器 Reject 的类型。全都笼统说成"用户名或密码"会把人引到错误的方向——
    # 认证器挂了的时候，用户会一直去改密码。
    "reject.WrongUserPW":     {"zh": "密码错误", "en": "Wrong password"},
    "reject.WrongServerPW":   {"zh": "服务器密码错误", "en": "Wrong server password"},
    "reject.InvalidUsername": {"zh": "用户名不符合服务器的规则",
                               "en": "The server does not accept this username"},
    "reject.UsernameInUse":   {"zh": "这个用户名已经在线了",
                               "en": "That username is already online"},
    "reject.ServerFull":      {"zh": "服务器已满", "en": "The server is full"},
    "reject.NoCertificate":   {"zh": "服务器要求证书",
                               "en": "The server requires a certificate"},
    "reject.AuthenticatorFail": {"zh": "服务端认证器故障（服务器上的 login.py 可能没在运行）",
                                 "en": "The server's authenticator failed (login.py may not "
                                       "be running on the server)"},
    "reject.WrongVersion":    {"zh": "客户端版本不被服务器接受",
                               "en": "The server does not accept this client version"},
    "reject.with_note":       {"zh": "{reason}（服务器附言：{note}）",
                               "en": "{reason} (server said: {note})"},

    # 服务器拒绝了某个动作
    "denied.UserListenerLimit":    {"zh": "服务器限制了每个用户能监听的频道数，部分频率收不到",
                                    "en": "The server caps how many channels one user may "
                                          "listen to — some frequencies will be silent"},
    "denied.ChannelListenerLimit": {"zh": "服务器限制了单个频道的监听人数，这个频率收不到",
                                    "en": "The server caps how many people may listen to one "
                                          "channel — this frequency will be silent"},
    "denied.Permission":           {"zh": "没有权限（频道监听需要 Listen 权限）",
                                    "en": "Not permitted (channel listening needs the Listen "
                                          "permission)"},
    "denied.other":                {"zh": "服务器拒绝了操作: {kind}",
                                    "en": "The server refused the action: {kind}"},
    "denied.with_note":            {"zh": "{reason}（{note}）", "en": "{reason} ({note})"},

    # ---------- 设置对话框 ----------
    "settings.title":       {"zh": "设置", "en": "Settings"},
    "settings.volume":      {"zh": "音量", "en": "Volume"},
    "settings.mic":         {"zh": "麦克风:", "en": "Microphone:"},
    "settings.speaker":     {"zh": "主音量:", "en": "Master:"},
    "settings.ptt_key":     {"zh": "PTT 按键:", "en": "PTT key:"},
    "settings.ptt_reset":   {"zh": "重设", "en": "Change"},
    "settings.ptt_press":   {"zh": "请按下按键...", "en": "Press a key..."},
    "settings.input":       {"zh": "输入设备:", "en": "Input device:"},
    "settings.output":      {"zh": "输出设备:", "en": "Output device:"},
    "settings.system_default": {"zh": "系统默认", "en": "System default"},
    "settings.language":    {"zh": "语言:", "en": "Language:"},
    "settings.debug":       {"zh": "记录调试信息（重启后生效）",
                             "en": "Write debug logs (takes effect after restart)"},
    "settings.debug_tip":   {"zh": "打开后会把协议细节也写进日志，排查连不上、听不到时用",
                             "en": "Logs protocol detail — use it when you cannot connect or hear anything"},
    "settings.open_log":    {"zh": "打开日志", "en": "Open log"},
    "settings.save":        {"zh": "保存", "en": "Save"},
    "settings.cancel":      {"zh": "取消", "en": "Cancel"},
}

_current = DEFAULT


def available():
    """能选的语言：代码 → 显示名。"""
    return dict(LANGUAGES)


def current():
    return _current


def set_language(code):
    """切换语言。不认识的代码退回默认，不抛异常——语言坏了不该让程序起不来。"""
    global _current
    code = (code or "").strip().lower()
    if code not in LANGUAGES:
        if code:
            log.warning("不认识的语言 %r，用 %s", code, DEFAULT)
        code = DEFAULT
    _current = code
    return _current


def system_language():
    """猜系统语言，第一次启动时用。猜不出来就用默认。"""
    try:
        # 环境变量优先，方便测试和命令行覆盖
        for name in ("AIRWAYSN_LANG", "LANGUAGE", "LC_ALL", "LANG"):
            value = os.environ.get(name)
            if value:
                code = value.split(".")[0].split("_")[0].lower()
                if code in LANGUAGES:
                    return code
        # Windows 上一般不设那些环境变量，得问系统。优先用 Qt——
        # locale.getdefaultlocale() 在 3.15 会被移除，而且在 Windows 上给出的是
        # "English_United States" 这种名字，不好解析。
        code = ""
        try:
            from PyQt6.QtCore import QLocale
            code = QLocale.system().name().split("_")[0].lower()
        except Exception:
            code = (locale.getdefaultlocale()[0] or "").split("_")[0].lower()
        if code in LANGUAGES:
            return code
    except Exception as e:
        log.debug("判断系统语言失败: %s", e)
    return DEFAULT


def t(key, **kwargs):
    """取一条界面文字。

    找不到键就返回键本身——在界面上很扎眼，正好当成"这里漏翻了"的提示，比默默
    显示一个空字符串强得多。某个语言缺这一条时退回默认语言，用户至少看得懂。
    """
    entry = TEXT.get(key)
    if entry is None:
        log.warning("没有这条文字: %s", key)
        return key
    text = entry.get(_current) or entry.get(DEFAULT) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError) as e:
            log.warning("文字 %s 的占位符对不上: %s", key, e)
            return text
    return text
