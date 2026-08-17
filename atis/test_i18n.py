"""多语言的测试。

    python -m unittest test_i18n -v      （在 atis 目录下运行）

最要紧的一条是**每个键两种语言都得有**：只翻一半的话，用户看到的是中英混排，
而这种缺失单看代码是发现不了的——界面上那句话照样显示，只是显示的是另一种
语言。
"""

import os
import re
import unittest

import i18n


class CoverageTest(unittest.TestCase):
    """翻译表本身的完整性。"""

    def test_every_key_has_every_language(self):
        missing = []
        for key, entry in i18n.TEXT.items():
            for code in i18n.LANGUAGES:
                if not entry.get(code):
                    missing.append(f"{key}[{code}]")
        self.assertEqual(missing, [], f"这些条目缺翻译: {missing}")

    def test_no_entry_carries_an_unknown_language(self):
        """打错语言代码（比如写成 'cn'）会静默失效，界面退回默认语言。"""
        unknown = []
        for key, entry in i18n.TEXT.items():
            for code in entry:
                if code not in i18n.LANGUAGES:
                    unknown.append(f"{key}[{code}]")
        self.assertEqual(unknown, [], f"不认识的语言代码: {unknown}")

    def test_placeholders_match_across_languages(self):
        """两种语言的占位符必须一模一样。

        少一个的话 format 出来会丢信息，多一个直接抛 KeyError——而两者都只在
        运行到那一句时才炸，多半是用户先撞上。
        """
        pattern = re.compile(r"\{(\w+)\}")
        bad = []
        for key, entry in i18n.TEXT.items():
            sets = {code: set(pattern.findall(text))
                    for code, text in entry.items()}
            reference = sets.get(i18n.DEFAULT, set())
            for code, names in sets.items():
                if names != reference:
                    bad.append(f"{key}: {code}={sorted(names)} vs "
                               f"{i18n.DEFAULT}={sorted(reference)}")
        self.assertEqual(bad, [], f"占位符对不上: {bad}")

    # 英文条目里**故意**留着中文的几条。
    #
    # 这两个提示举的例子是"要填进中文稿的那个词本身"——中文稿里念的机场名和跑道，
    # 最后是交给 TTS 念出来的。把 "上海浦东" 翻成 "Shanghai Pudong" 会让英文界面的
    # 操作者照着填进去，然后中文通播里冒出一句英文。例子必须是中文，界面语言无关。
    CHINESE_BY_DESIGN = {"station.chinese_name_hint", "station.chinese_runway_hint"}

    def test_translations_are_not_just_copies_of_chinese(self):
        """英文条目里不该还留着中文——那是漏翻却看着像翻过了。"""
        chinese = re.compile(r"[一-鿿]")
        untranslated = [key for key, entry in i18n.TEXT.items()
                        if key not in self.CHINESE_BY_DESIGN
                        and chinese.search(entry.get("en", ""))]
        self.assertEqual(untranslated, [], f"这些英文条目里还有中文: {untranslated}")

    def test_the_exemptions_are_still_needed(self):
        """豁免名单不能烂在这儿。

        哪条被翻掉了、或者键名改了，这里就该把它从名单里删掉——否则名单会慢慢
        变成一块谁也不敢动的免检区。
        """
        chinese = re.compile(r"[一-鿿]")
        stale = [key for key in self.CHINESE_BY_DESIGN
                 if key not in i18n.TEXT
                 or not chinese.search(i18n.TEXT[key].get("en", ""))]
        self.assertEqual(stale, [], f"这些豁免已经不需要了，从名单里删掉: {stale}")


class NoHardcodedUiStringTest(unittest.TestCase):
    """源码里不该再有写死的界面文字。

    这条是给"漏翻"兜底的：漏掉一句的话，中文用户完全看不出问题，英文用户会
    在满屏英文里撞见一句中文，而这种事只有真的切到英文跑一遍才会发现。用扫描
    源码代替人眼。

    只看**会显示给用户**的地方（setText / 占位符 / 提示 / 抛给界面的异常 /
    _state 消息）。日志和注释里的中文不管——日志是给排查问题的人看的，翻了
    反而不利于对照。
    """

    # 会把文字送到界面上的调用。Fluent 那套控件（PushButton / CheckBox / …）也要在
    # 里面——换控件之前这张表只认 Q 开头的，换完之后同样一句写死的中文就扫不出来了。
    UI_CALLS = re.compile(
        r"(setText|setPlaceholderText|setToolTip|setWindowTitle|setTitle|addItem|"
        r"addTab|addRow|setSpecialValueText|"
        r"_state|_status|raise ValueError|"
        r"QCheckBox|QLabel|QPushButton|QGroupBox|"
        r"BodyLabel|CaptionLabel|StrongBodyLabel|SubtitleLabel|"
        r"PushButton|CheckBox|SwitchButton)\s*\(")
    CHINESE = re.compile(r"[一-鿿]")

    def offenders(self, filename):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, filename), encoding="utf-8") as f:
            lines = f.readlines()
        bad = []
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 文档字符串也放过。有几处的说明里正好写着 `_status(state, message)`
            # 这种签名，会被下面的 UI_CALLS 认成一次界面调用，再把整行的中文说明
            # 报成漏翻——那是注释，不是界面文字。
            quoted = stripped[1:] if stripped[:1] == "r" else stripped
            if quoted[:3] in ('"' * 3, "'" * 3):
                continue
            if not self.UI_CALLS.search(line):
                continue
            # 取出这一行里的字符串字面量，含中文的就是漏翻的
            for match in re.finditer(r"""(['"])((?:(?!\1).)*)\1""", line):
                if self.CHINESE.search(match.group(2)):
                    bad.append(f"{filename}:{number}: {stripped[:70]}")
                    break
        return bad

    def test_gui_has_no_hardcoded_chinese(self):
        """主窗口、席位对话框和预设对话框都在 gui.py 里。"""
        self.assertEqual(self.offenders("gui.py"), [])

    def test_settings_dialog_has_no_hardcoded_chinese(self):
        self.assertEqual(self.offenders("settings.py"), [])

    def test_broadcast_status_messages_are_translated(self):
        """broadcast.py 的 _state 消息会直接进状态栏。"""
        self.assertEqual(self.offenders("broadcast.py"), [])

    def test_fsd_status_messages_are_translated(self):
        """fsdclient.py 同理，还包括本地先查一遍的呼号错误。"""
        self.assertEqual(self.offenders("fsdclient.py"), [])

    def test_profile_errors_are_translated(self):
        """profile.py 抛的 ValueError 会原样进 QMessageBox。"""
        self.assertEqual(self.offenders("profile.py"), [])


class LookupTest(unittest.TestCase):

    def setUp(self):
        self._saved = i18n.current()

    def tearDown(self):
        i18n.set_language(self._saved)

    def test_switching_changes_what_comes_out(self):
        i18n.set_language("zh")
        chinese = i18n.t("main.settings")
        i18n.set_language("en")
        english = i18n.t("main.settings")
        self.assertNotEqual(chinese, english)
        self.assertEqual(chinese, "设置")
        self.assertEqual(english, "Settings")

    def test_placeholders_are_filled(self):
        i18n.set_language("zh")
        text = i18n.t("weather.updated", callsign="ZSPD_ATIS", letter="J")
        self.assertIn("ZSPD_ATIS", text)
        self.assertIn("J", text)
        self.assertNotIn("{", text)

    def test_an_unknown_key_returns_the_key(self):
        """返回键本身，界面上很扎眼——正好当成"这里漏翻了"的信号。"""
        self.assertEqual(i18n.t("没有这个键"), "没有这个键")

    def test_a_missing_placeholder_does_not_crash(self):
        # 调用方漏传参数时，宁可显示带 {} 的原文，也不能让界面炸掉
        text = i18n.t("weather.updated", callsign="ZSPD_ATIS")
        self.assertIsInstance(text, str)

    def test_an_unknown_language_falls_back(self):
        i18n.set_language("kl")            # 克林贡语
        self.assertEqual(i18n.current(), i18n.DEFAULT)
        self.assertEqual(i18n.t("main.settings"), "设置")

    def test_empty_language_falls_back(self):
        i18n.set_language("")
        self.assertEqual(i18n.current(), i18n.DEFAULT)


class SystemLanguageTest(unittest.TestCase):

    def setUp(self):
        self._env = {k: os.environ.get(k)
                     for k in ("CAN_LANG", "LANGUAGE", "LC_ALL", "LANG")}

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_environment_wins(self):
        os.environ["CAN_LANG"] = "en"
        self.assertEqual(i18n.system_language(), "en")

    def test_locale_style_values_are_understood(self):
        os.environ["CAN_LANG"] = "zh_CN.UTF-8"
        self.assertEqual(i18n.system_language(), "zh")

    def test_an_unsupported_locale_is_never_returned_verbatim(self):
        """环境变量说法语（不支持）时，绝不能把 "fr" 原样返回。

        往下退到系统语言是对的——环境说法语、系统是英语时，英语比中文更接近
        用户想要的，所以这里不断言一定是 DEFAULT，只断言结果必须是支持的语言。
        """
        for key in ("CAN_LANG", "LANGUAGE", "LC_ALL", "LANG"):
            os.environ[key] = "fr_FR.UTF-8"
        result = i18n.system_language()
        self.assertNotEqual(result, "fr")
        self.assertIn(result, i18n.LANGUAGES)

    def test_it_always_returns_something_supported(self):
        self.assertIn(i18n.system_language(), i18n.LANGUAGES)


class VoiceLanguageTest(unittest.TestCase):
    """通播稿的语言和界面语言是两回事。

    界面语言是操作者看什么，voice_language 是播出去给飞行员听什么。一个英文界面的
    操作者照样可能在管一份中文通播——把两者搅在一起，切个界面语言就会把在播的
    通播稿也换掉。
    """

    def setUp(self):
        self._saved = i18n.current()

    def tearDown(self):
        i18n.set_language(self._saved)

    def test_the_interface_language_does_not_touch_the_script(self):
        import profile as profile_module
        station = profile_module.Station(
            "ZSPD", frequency="127.850",
            voice_language=profile_module.LANGUAGE_CHINESE)
        i18n.set_language("en")
        self.assertEqual(station.voice_language, profile_module.LANGUAGE_CHINESE,
                         "切界面语言把通播稿的语言也改了")

    def test_the_language_names_follow_the_interface_language(self):
        import profile as profile_module
        i18n.set_language("zh")
        chinese = profile_module.language_label(profile_module.LANGUAGE_BOTH)
        i18n.set_language("en")
        english = profile_module.language_label(profile_module.LANGUAGE_BOTH)
        # 名字本身要跟着界面走：中文界面里写"中英双语"，英文界面里写英文
        self.assertNotEqual(chinese, english)

    def test_station_types_are_translated_lazily(self):
        """类型名不能在导入时定死，否则切语言之后还是旧的那一套。"""
        import profile as profile_module
        i18n.set_language("zh")
        chinese = profile_module.type_label(profile_module.TYPE_DEPARTURE)
        i18n.set_language("en")
        english = profile_module.type_label(profile_module.TYPE_DEPARTURE)
        self.assertNotEqual(chinese, english)


class EveryUsedKeyExistsTest(unittest.TestCase):
    """源码里 t("…") 引用的键，翻译表里必须真有。

    这条是 CoverageTest 补不上的那一半：那边查的是"表里已有的条目翻全了没有"，
    查不出"代码要用的条目根本不在表里"。少一条的结果是界面上明晃晃地显示
    `plugin.install` 这种生键名——两种语言下都一样难看，而 t() 只在日志里留一行
    warning，跑测试的人不会注意到。

    **这不是假想的故障。** 一次给翻译表做整段替换的改动，正好把插进那两条中间
    的一整块键切掉了，四个组件的测试全绿、冒烟全过，直到有人打开那个设置页。

    只认字面量：`t(key)` 这种由变量拼出来的键（状态 → 键名的映射表）扫不到，
    那部分靠 smoke_gui.py 真的把窗口建出来兜底。
    """

    # \bt\( 的词边界不能省：没有它，FSDPilot("example.invalid") 里的那个 t
    # 会被当成一次 t() 调用，把机器名报成缺失的键
    CALL = re.compile(r'\bt\(\s*["\']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["\']')

    def test_no_source_file_uses_a_key_that_does_not_exist(self):
        here = os.path.dirname(os.path.abspath(__file__))
        missing = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py") or name == "i18n.py":
                continue
            with open(os.path.join(here, name), encoding="utf-8") as f:
                text = f.read()
            for match in self.CALL.finditer(text):
                key = match.group(1)
                if key not in i18n.TEXT:
                    missing.append(f"{name}: {key}")
        self.assertEqual(sorted(set(missing)), [],
                         f"这些键代码在用，翻译表里却没有: {sorted(set(missing))}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
