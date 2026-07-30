"""GUI 冒烟测试：离屏把窗口和对话框都建起来，不连服务器、不碰 X-Plane。

    python smoke_gui.py        （在 xpc 目录下运行）

模态对话框在离屏模式下照样会一直等人点，所以 QMessageBox 那几个静态方法必须
先换成记录器——漏一个整个测试就挂在那儿不动了。
"""

import os
import sys
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# pymumble 要本机的 opus 原生库。这里不碰音频，缺库时放个替身让导入过去。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())
    print("提示: 未找到 opus 原生库，已用替身放行（不影响本测试）\n")

from PyQt6.QtWidgets import QApplication, QMessageBox

_dialogs = []
for _name in ("warning", "critical", "information"):
    setattr(QMessageBox, _name,
            staticmethod(lambda *args, _n=_name: _dialogs.append(
                (_n, args[2] if len(args) > 2 else ""))))


def _question(*args, **kwargs):
    _dialogs.append(("question", args[2] if len(args) > 2 else ""))
    return QMessageBox.StandardButton.No       # 测试里一律选"否"


QMessageBox.question = staticmethod(_question)

# 查更新会真的发网络请求，而弹出来的那个 QMessageBox 用的是 exec()——离屏模式下
# 它照样会一直等人点，整个冒烟测试就挂死在那里。这里一律换成"没有新版"；
# 真正的对话框逻辑另有一条用例单独测。
import update
update.check = lambda *args, **kwargs: None

# 弹窗那条路单独测：把 exec() 换成"什么都不点"，clickedButton 换成 None。
_shown = []
QMessageBox.exec = lambda self: _shown.append(self.text()) or 0
QMessageBox.clickedButton = lambda self: None

import gui
import xplane

SNAPSHOT = {
    "latitude": 31.1434, "longitude": 121.805, "altitude": 35000, "agl": 34000,
    "groundspeed": 450, "pitch": 2.0, "bank": -5.0, "heading": 271.0,
    "squawk": 2000, "xpdr_mode": 2, "com1": 121.5, "com2": 118.0,
    "com1_power": True, "on_ground": False,
}


def main():
    app = QApplication(sys.argv)
    failures = []

    # 不要在冒烟测试里真的去找 X-Plane
    xplane.XPlaneLink.start = lambda self: None
    xplane.XPlaneLink.stop = lambda self: None
    xplane.XPlaneLink.snapshot = lambda self: SNAPSHOT

    def check(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            failures.append((name, e))
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    # 用临时配置，别动开发机上真实的 xpc_settings.json
    import settings as settings_module
    settings_module.Settings.save = lambda self: None

    print("主窗口：")
    window = gui.XpcWindow()
    check("建立主窗口", lambda: window)

    print("模拟器数据：")
    check("一次刷新", lambda: window.tick())

    def shows_com1():
        assert "121.500" in window.com1_label.text(), window.com1_label.text()
        assert "35000 ft" in window.position_label.text(), window.position_label.text()
    check("显示 COM1 和位置", shows_com1)

    print("连接校验：")

    def rejects_long_callsign():
        _dialogs.clear()
        window.callsign_input.setText("ABCDEFGHIJK")      # 11 个字符
        window.cid_input.setText("1234")
        window.password_input.setText("pw")
        window.connect_all()
        assert _dialogs, "超长呼号应当被拦下"
        assert window.fsd is None and window.voice is None, "不该建立任何连接"
    check("拦下超长呼号", rejects_long_callsign)

    def rejects_missing_credentials():
        _dialogs.clear()
        window.callsign_input.setText("CCA1501")
        window.cid_input.setText("")
        window.connect_all()
        assert _dialogs, "缺少账号应当被拦下"
        assert window.fsd is None, "不该建立任何连接"
    check("拦下空账号", rejects_missing_credentials)

    def asks_when_simulator_absent():
        _dialogs.clear()
        window.cid_input.setText("1234")
        window.connect_all()          # X-Plane 未连接，替身里 connected 为假
        assert _dialogs, "模拟器没连上时应当询问而不是直接连"
        assert window.fsd is None, "选了否就不该连"
    check("模拟器缺席时询问", asks_when_simulator_absent)

    print("消息区：")
    check("收到文字消息", lambda: window.on_text_message(
        "ZSPD_TWR", "CCA1501", "contact ground 121.8"))
    check("收到频率消息", lambda: window.on_text_message(
        "CES2345", "@28500", "request pushback"))

    def refuses_to_send_offline():
        window.message_input.setText("hello")
        window.send_message()
        assert "尚未连接" in window.messages.toPlainText()
    check("未连接时不发消息", refuses_to_send_offline)

    print("管制列表：")

    def lists_controllers():
        window.on_controllers([
            {"callsign": "ZSPD_TWR", "frequency": "118.850", "facility": 4},
            {"callsign": "ZSPD_APP", "frequency": "119.700", "facility": 5}])
        assert window.controller_list.count() == 2
    check("列出在线管制", lists_controllers)

    def double_click_fills_recipient():
        window.controller_clicked(window.controller_list.item(0))
        assert window.recipient_input.text() == "ZSPD_APP", window.recipient_input.text()
    check("双击填入收件人", double_click_fills_recipient)

    print("状态回调：")
    check("模拟器状态", lambda: window.on_sim_state(True, "已连接 X-Plane"))
    check("网络状态", lambda: window.on_fsd_status("online", "已上线"))
    check("语音状态", lambda: window.on_voice_status("online", "语音已连接"))
    check("频道切换", lambda: window.on_channel(121.5, "FREQ_121500"))

    def fsd_error_keeps_voice():
        # 两条链路互不依赖，网络断了不该把语音一起收掉
        window.voice = object()
        window.on_fsd_status("error", "连接被拒绝")
        assert window.voice is not None, "语音不该被清掉"
        assert window.fsd is None, "网络那条应当收掉"
        assert window.connect_button.text() == "断开", window.connect_button.text()
        window.voice = None
    check("网络失败时语音继续", fsd_error_keeps_voice)

    print("指示灯：")
    check("TX 点亮", lambda: window.tx_light.set_lit(True))
    check("RX 点亮", lambda: window.rx_light.set_lit(True))
    check("未连接时按 PTT 不炸", lambda: window.set_ptt(True))

    print("他机：")

    def traffic_from_fsd():
        import cslmatch
        import fsdpilot
        window.models = cslmatch.ModelSet([
            cslmatch.Model("B738_CCA", "b738_cca.obj", icao="B738", airline="CCA"),
            cslmatch.Model("A320_GEN", "a320.obj", icao="A320")])
        pbh = fsdpilot.pack_pbh(2.0, -5.0, 271.0)
        pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1", "pw",
                                  traffic=window.traffic)
        pilot._send = lambda packet: True
        pilot._handle_packet(f"@N:CES2345:2000:1:31.2:121.6:34000:450:{pbh}:0")
        pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738:AIRLINE=CCA")
        assert "CES2345" in window.traffic
    check("从 FSD 收到一架他机", traffic_from_fsd)

    def traffic_reaches_the_bridge():
        sent = []
        window.bridge.send_traffic = lambda entries, own=None: sent.append(entries)
        window.tick()
        assert sent and sent[0], "他机没有推给插件"
        entry = sent[0][0]
        assert entry["callsign"] == "CES2345", entry
        assert entry["object"].endswith("b738_cca.obj"), entry["object"]
        assert "range_nm" in entry, "缺少距离，插件没法按远近取舍"
    check("推给插件并匹配到模型", traffic_reaches_the_bridge)

    def label_updates():
        assert "1" in window.traffic_label.text(), window.traffic_label.text()
    check("界面显示他机数", label_updates)

    def render_can_be_turned_off():
        sent = []
        window.bridge.send_traffic = lambda entries, own=None: sent.append(entries)
        window.settings.render_traffic = False
        window.tick()
        window.settings.render_traffic = True
        assert not sent, "关掉之后不该再推"
    check("可以关掉他机渲染", render_can_be_turned_off)

    def survives_without_models():
        import cslmatch
        window.models = cslmatch.ModelSet()
        window._model_cache.clear()
        window.traffic.set_plane_info("CES2345", equipment="B738")
        window.tick()      # 没有模型也不该炸，TCAS 还是要送
    check("没装模型也能跑", survives_without_models)

    print("对话框：")
    settings_dialog = gui.SettingsDialog(window.settings, window)
    check("建立设置对话框", lambda: settings_dialog)
    check("设置可应用", lambda: settings_dialog.apply())

    plan_dialog = gui.FlightPlanDialog(window.settings, window)
    check("建立飞行计划对话框", lambda: plan_dialog)

    def plan_has_every_field():
        plan = plan_dialog.plan()
        for key in ("rules", "aircraft", "departure", "arrival", "route"):
            assert key in plan, key
    check("飞行计划字段齐全", plan_has_every_field)

    print("关闭：")
    print("更新提示：")

    def a_new_version_is_offered():
        """查到新版要弹一次，而且文案里得有版本号和体积——不然用户不知道要下多大。"""
        _shown.clear()
        window.on_update_found(update.Update(
            version="2.9.9", notes="https://example/notes",
            download="https://airwaysn.org/api/v1/clients/download/xpc-for-can",
            size=59057038))
        assert _shown, "有新版却没有弹提示"
        assert "2.9.9" in _shown[0], _shown[0]
        assert "56.3 MB" in _shown[0], _shown[0]
    check("有新版会提示", a_new_version_is_offered)

    def a_skipped_version_is_not_offered_again():
        """用户说过"跳过这一版"就别每次启动再问一遍——那和自动更新一样烦人。"""
        window.settings.skipped_version = "2.9.9"
        _shown.clear()
        window.on_update_found(update.Update(version="2.9.9"))
        assert not _shown, "跳过的版本又弹了一次"
        window.settings.skipped_version = ""
    check("跳过的版本不再提示", a_skipped_version_is_not_offered_again)

    def no_update_is_silent_at_startup():
        """启动时那次是静默的：没有新版就不该打扰任何人。"""
        _shown.clear()
        window.on_update_found(None)
        assert not _shown, "没有新版却弹了框"
    check("没有新版时不出声", no_update_is_silent_at_startup)

    check("关窗", lambda: window.close())

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
