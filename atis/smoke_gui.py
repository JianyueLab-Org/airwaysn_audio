"""GUI 冒烟测试：离屏把窗口和对话框都建起来，不连服务器、不取天气。

    python smoke_gui.py        （在 atis 目录下运行）

天气获取被换成固定报文，避免测试依赖外网。
"""

import os
import sys
import tempfile
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 挪到临时目录再跑。Profile / Settings 的路径都是相对当前目录的裸文件名，直接在
# atis 目录下跑的话，这个测试会**读到并写坏使用者真实的 atis_profile.json**——
# "添加两个席位" 在你已经配过席位时必然失败（呼号重复），跑完还会把它覆盖掉。
# airports.py 和图标走的都是 __file__ / sys._MEIPASS，所以换目录是安全的。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="airwaysn-atis-smoke-"))

# pymumble 需要本机的 opus 原生库。这里不碰音频，缺库时放个替身让导入过去。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())
    print("提示: 未找到 opus 原生库，已用替身放行（不影响本测试）\n")

from PyQt6.QtWidgets import QApplication, QMessageBox

# 模态对话框在离屏模式下同样会一直等人点，测试里换成记录调用。
# question 也必须换掉——它会等一个按钮，漏了就整个测试挂死。
_dialogs = []
for _name in ("warning", "critical", "information"):
    setattr(QMessageBox, _name,
            staticmethod(lambda *args, _n=_name: _dialogs.append(
                (_n, args[2] if len(args) > 2 else ""))))


def _question(*args, **kwargs):
    _dialogs.append(("question", args[2] if len(args) > 2 else ""))
    return QMessageBox.StandardButton.No      # 测试里一律选"否"


QMessageBox.question = staticmethod(_question)

import gui
import weather
from profile import Station

ZSPD = "ZSPD 251300Z 09004MPS 9999 FEW030 SCT100 25/18 Q1013 NOSIG"


def main():
    app = QApplication(sys.argv)
    failures = []

    # 不要在冒烟测试里真的去取天气
    weather.fetch_metar = lambda icao, url=None, timeout=15: ZSPD

    def check(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            failures.append((name, e))
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    # 用临时配置，别动开发机上真实的 atis_profile.json
    import profile as profile_module
    profile_module.Profile.save = lambda self: None

    print("主窗口：")
    window = gui.AtisWindow()
    window.profile.path = os.devnull
    check("建立主窗口", lambda: window)

    print("席位：")

    def add_stations():
        window.profile.add(Station("ZSPD", "浦东", "127.850"))
        window.profile.add(Station("ZSPD", "浦东离场", "127.900",
                                   atis_type=profile_module.TYPE_DEPARTURE))
        window.refresh_stations()
        assert window.station_list.count() == 2
    check("添加两个席位", add_stations)

    check("呼号按类型区分", lambda: (_ for _ in ()).throw(AssertionError(
        [s.callsign for s in window.profile]))
        if {s.callsign for s in window.profile} != {"ZSPD_ATIS", "ZSPD_D_ATIS"} else None)

    check("选中第一个", lambda: window.station_list.setCurrentRow(0))

    def feed_metar():
        import metar as metar_module
        station = window.current_station()
        window.on_metar(station.callsign, metar_module.Metar(ZSPD), "")
    check("喂一份天气进去", feed_metar)

    def preview_generated():
        text = window.text_preview.toPlainText()
        voice = window.voice_preview.toPlainText()
        assert "ZSPD ATIS" in text, text
        assert "09004MPS" in text, "文字通播应当照抄电码"
        assert "wind zero niner zero" in voice, "语音稿应当念出来"
    check("生成文字通播和语音稿", preview_generated)

    def letter_advances_on_new_metar():
        import metar as metar_module
        station = window.current_station()
        before = station.letter
        window.on_metar(station.callsign,
                        metar_module.Metar(ZSPD.replace("25/18", "26/19")), "")
        assert station.letter != before, "报文变了就该换情报字母"
    check("天气变化推进情报字母", letter_advances_on_new_metar)

    check("手动推进字母", lambda: window.advance_letter())

    print("取天气失败：")

    def failure_shows_up():
        import metar as metar_module
        station = window.current_station()
        window.on_metar(station.callsign, None,
                        "取 ZSPD 的 METAR 失败: 证书校验没过")
        assert "失败" in window.status_label.text(), window.status_label.text()
    check("失败会显示出来", failure_shows_up)

    def recovery_clears_the_stale_error():
        """一次抖动留下的报错不能一直挂着。

        取天气成功是静默的（只有报文变了才写状态栏），所以没有谁会去改写它，
        天气早就正常了界面还在喊失败——用户只能截图状态栏来问。
        """
        import metar as metar_module
        station = window.current_station()
        # 喂一份和上次一样的报文：这一路是完全静默的，最容易漏
        window.on_metar(station.callsign,
                        metar_module.Metar(window.raw_metars[station.callsign]), "")
        assert "失败" not in window.status_label.text(), window.status_label.text()
    check("恢复之后撤掉旧报错", recovery_clears_the_stale_error)

    def other_stations_errors_survive():
        """两个席位各自失败，好了一个不该把另一个的错误也抹掉。"""
        import metar as metar_module
        first, second = list(window.profile)[0], list(window.profile)[1]
        window.on_metar(first.callsign, None, f"取 {first.callsign} 失败")
        window.on_metar(second.callsign, None, f"取 {second.callsign} 失败")
        window.on_metar(first.callsign, metar_module.Metar(ZSPD), "")
        assert second.callsign in window.status_label.text(), window.status_label.text()
    check("只撤掉自己的那条", other_stations_errors_survive)

    print("中文语音：")

    def chinese_script_is_generated():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_CHINESE
        station.chinese_name = "上海浦东"
        station.chinese_runway = "三六左"
        rendered = window.render_for(station)
        assert rendered, "没有渲染出通播"
        text, voice = rendered
        assert "09004MPS" in text, "文字通播仍应照抄电码"
        for fragment in ("上海浦东", "风向", "风速", "能见度", "气温", "露点",
                         "修正海压"):
            assert fragment in voice, f"中文稿缺少 {fragment}：{voice}"
        assert "wind" not in voice, "选了中文就不该混进英文稿"
    check("中文语音稿", chinese_script_is_generated)

    def bilingual_has_both():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_BOTH
        _, voice = window.render_for(station)
        assert "wind" in voice and "能见度" in voice, "双语应当两种都有"
    check("中英双语", bilingual_has_both)

    def english_is_unchanged():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_ENGLISH
        _, voice = window.render_for(station)
        assert "wind" in voice and "能见度" not in voice
    check("英文不受影响", english_is_unchanged)

    def language_survives_a_save_load():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_CHINESE
        station.chinese_name = "上海浦东"
        again = profile_module.Station.from_dict(station.to_dict())
        assert again.voice_language == profile_module.LANGUAGE_CHINESE
        assert again.chinese_name == "上海浦东"
    check("语言设置能存下来", language_survives_a_save_load)

    def old_profiles_default_to_english():
        # 升级不该改变已有席位的行为
        station = profile_module.Station.from_dict(
            {"identifier": "ZBAA", "frequency": "127.500"})
        assert station.voice_language == profile_module.LANGUAGE_ENGLISH
    check("老配置默认英文", old_profiles_default_to_english)

    print("自动更新：")

    def interval_is_configurable():
        import settings as settings_module
        window.settings.metar_refresh = 900
        assert window.apply_refresh_interval() == 900
        assert window.timer.interval() == 900 * 1000
    check("刷新间隔可配置", interval_is_configurable)

    def silly_intervals_are_clamped():
        import settings as settings_module
        window.settings.metar_refresh = 0
        assert window.apply_refresh_interval() == settings_module.MIN_METAR_REFRESH
        window.settings.metar_refresh = 999999
        assert window.apply_refresh_interval() == settings_module.MAX_METAR_REFRESH
        window.settings.metar_refresh = 300
        window.apply_refresh_interval()
    check("离谱的间隔被夹住", silly_intervals_are_clamped)

    print("对话框：")
    station_dialog = gui.StationDialog(window.current_station(), parent=window)
    check("建立席位对话框", lambda: station_dialog)

    def reject_bad_input():
        _dialogs.clear()
        station_dialog.identifier.setText("ZS")
        station_dialog.validate_and_accept()
        assert _dialogs, "非法机场代码应当被拦下"
    check("席位输入校验", reject_bad_input)

    preset_dialog = gui.PresetDialog(window.current_station().presets[0], parent=window)
    check("建立预设对话框", lambda: preset_dialog)
    check("预设可保存", lambda: preset_dialog.apply())

    settings_dialog = gui.SettingsDialog(window.settings, window)
    check("建立设置对话框", lambda: settings_dialog)

    print("导入 vATIS 配置：")

    def import_vatis_profile():
        import json
        import tempfile
        from PyQt6.QtWidgets import QFileDialog

        profile = {
            "name": "vATIS 测试",
            "stations": [{
                "identifier": "ZGGG", "name": "白云", "atisType": "Arrival",
                "codeRange": {"low": "A", "high": "F"}, "frequency": 127250000,
                "idsEndpoint": "https://ids.example",
                "presets": [{"name": "白天", "template": "[FACILITY] [ATIS_LETTER] @RWY"}],
                "contractions": [{"variableName": "RWY", "text": "RWY 01",
                                  "voice": "runway zero one"}],
            }],
        }
        handle, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(profile, f)

        # 文件选择框是模态的，测试里直接给定路径
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (path, ""))
        _dialogs.clear()
        try:
            window.import_vatis()
        finally:
            os.remove(path)

        station = window.profile.get("ZGGG_A_ATIS")
        assert station is not None, "应当导入 ZGGG_A_ATIS"
        assert station.frequency == "127.250", station.frequency
        assert station.code_range == ("A", "F"), station.code_range
        assert station.contractions["RWY"][1] == "runway zero one"
        assert _dialogs, "应当给出导入结果说明"
    check("导入并落到席位列表", import_vatis_profile)

    check("导入后能选中新席位", lambda: window.station_list.setCurrentRow(
        window.station_list.count() - 1))

    print("开播前的管制席位核对：")

    def refuse_without_controller():
        _dialogs.clear()
        window.on_precheck("ZSPD_ATIS", "1000", "pw", True, {}, 0)
        assert _dialogs, "没在管制就该拦下来"
        assert "ZSPD_ATIS" not in window.broadcasters, "不该建立任何连接"
    check("没在管制则拒绝开播", refuse_without_controller)

    def ask_when_datafeed_unreachable():
        _dialogs.clear()
        window.on_precheck("ZSPD_ATIS", "1000", "pw", False, {}, 0)
        assert _dialogs, "数据源连不上时应当询问而不是直接放行"
    check("数据源不可达时询问", ask_when_datafeed_unreachable)

    check("核对后按钮恢复可用", lambda: (_ for _ in ()).throw(
        AssertionError("按钮仍然禁用"))
        if not window.broadcast_button.isEnabled() else None)

    print("错误隔离：")

    def fsd_error_keeps_voice():
        # FSD 只是附加能力，登录失败不该把频率上的语音通播一起停掉
        window.broadcasters["ZSPD_ATIS"] = object()
        window.fsd_clients["ZSPD_ATIS"] = type("F", (), {"stop": lambda s: None})()
        _dialogs.clear()
        window.on_broadcast_state("ZSPD_ATIS", "fsd-error", "[FSD] 拒绝登录")
        assert "ZSPD_ATIS" in window.broadcasters, "语音不该被停掉"
        assert "ZSPD_ATIS" not in window.fsd_clients, "FSD 那条应当收掉"
        assert not _dialogs, "不该弹错误框打断播出"
        window.broadcasters.pop("ZSPD_ATIS")
    check("FSD 失败时语音继续", fsd_error_keeps_voice)

    print("播出状态回调：")
    check("状态回报", lambda: window.on_broadcast_state("ZSPD_ATIS", "online", "测试"))
    check("未播出时删除席位", lambda: window.remove_station())

    window.timer.stop()

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
