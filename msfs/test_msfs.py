"""MSFS 版特有部分的单元测试。

    python -m unittest test_msfs -v

不连模拟器、不连服务器、不碰音频。FSD 协议、他机插值那些和 xpc 共用的部分由
xpc/test_xpc.py 覆盖，这里只测换掉的那一层：SimConnect 的单位换算、aircraft.cfg
解析、机型匹配，以及他机注入里那段自己接管的 objectID 关联。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

# pymumble 要本机的 opus 原生库，这些测试碰不到音频，缺库时放个替身。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())

import aimatch
import simlink


class SquawkTest(unittest.TestCase):
    """应答机码在 SimVar 里是 BCD。当十进制读会得到乱码。"""

    def test_common_codes(self):
        self.assertEqual(simlink.bcd_to_squawk(0x1200), 1200)
        self.assertEqual(simlink.bcd_to_squawk(0x2000), 2000)
        self.assertEqual(simlink.bcd_to_squawk(0x7700), 7700)

    def test_leading_zero_is_kept(self):
        self.assertEqual(simlink.bcd_to_squawk(0x0021), 21)

    def test_all_sevens(self):
        self.assertEqual(simlink.bcd_to_squawk(0x7777), 7777)

    def test_zero(self):
        self.assertEqual(simlink.bcd_to_squawk(0x0000), 0)

    def test_non_octal_nibble_is_not_treated_as_bcd(self):
        # 出现 8 或 9 说明这不是 BCD。0x1290 = 4752，本身是个合法八进制码，
        # 那就按十进制照用，别硬套 BCD 解出个乱码。
        self.assertEqual(simlink.bcd_to_squawk(0x1290), 4752)

    def test_garbage_falls_back(self):
        self.assertEqual(simlink.bcd_to_squawk(None), 2000)
        self.assertEqual(simlink.bcd_to_squawk("x"), 2000)

    def test_plain_decimal_in_range_is_accepted(self):
        # 1200 十进制 = 0x4B0，第三个半字节是 11，不是 BCD；但 1200 本身就是
        # 常见的合法应答机码，照用。
        self.assertEqual(simlink.bcd_to_squawk(1200), 1200)

    def test_out_of_range_falls_back(self):
        # 既不是 BCD，十进制也超出 0000-7777，只能给默认值
        self.assertEqual(simlink.bcd_to_squawk(88888), 2000)


class SnapshotTest(unittest.TestCase):
    """SimVar 的角度是弧度，字段名骗人（PLANE_PITCH_DEGREES 也是弧度）。

    snapshot() 的输出必须和 xpc/xplane.py 逐字段一致，否则 fsdpilot 和 voice
    没法原样复用。
    """

    def setUp(self):
        import math
        self.link = simlink.SimLink()
        self.link.values = {
            # Python-SimConnect 按 Degrees 请求经纬度，拿到的已经是度。
            # 这个测试原来喂弧度、断言出度，把错误假设一起钉住了，所以
            # math.degrees 那个 bug 一路绿灯到实飞才暴露。
            "latitude": 31.1434,
            "longitude": 121.805,
            "altitude": 35000.0,
            "agl": 34000.0,
            "groundspeed": 450.0,
            "pitch": math.radians(-2.0),      # SimVar 抬头为负
            "bank": math.radians(5.0),        # SimVar 右坡为负
            "heading": math.radians(271.0),
            "squawk": 0x2000,
            "com1": 121.5, "com2": 118.0,
            "on_ground": 0,
            "gear": 1, "flaps": 40.0, "spoilers": 0,
            "engine_on": 1,
            "light_strobe": 1, "light_nav": 1,
        }

    def test_latitude_passes_through_unconverted(self):
        self.assertAlmostEqual(self.link.snapshot()["latitude"], 31.1434, places=4)

    def test_longitude_passes_through_unconverted(self):
        self.assertAlmostEqual(self.link.snapshot()["longitude"], 121.805, places=4)

    def test_position_stays_inside_the_valid_range(self):
        """经纬度必须落在合法范围内。

        实飞时每个位置包都被回 "Invalid latitude/longitude"：经纬度已经是度，
        又 math.degrees 了一次，31.14 变成 1784.2。这条断言是那次的回归。
        """
        for latitude, longitude in ((31.1434, 121.805), (-33.94, 151.18),
                                    (0.0, 0.0), (89.9, -179.9)):
            self.link.values["latitude"] = latitude
            self.link.values["longitude"] = longitude
            snapshot = self.link.snapshot()
            self.assertTrue(-90 <= snapshot["latitude"] <= 90,
                            f"纬度 {snapshot['latitude']} 越界")
            self.assertTrue(-180 <= snapshot["longitude"] <= 180,
                            f"经度 {snapshot['longitude']} 越界")

    def test_attitude_is_still_converted_from_radians(self):
        # 名字里带 DEGREES 的那几个反而是弧度，这些转换是对的，别一起改掉
        import math
        self.link.values["pitch"] = math.radians(-2.0)
        self.link.values["heading"] = math.radians(271.0)
        snapshot = self.link.snapshot()
        self.assertAlmostEqual(snapshot["pitch"], 2.0, places=3)
        self.assertAlmostEqual(snapshot["heading"], 271.0, places=3)

    def test_pitch_sign_is_flipped(self):
        # SimVar 里抬头是负的，FSD 那边抬头是正的
        self.assertAlmostEqual(self.link.snapshot()["pitch"], 2.0, places=3)

    def test_bank_sign_is_flipped(self):
        self.assertAlmostEqual(self.link.snapshot()["bank"], -5.0, places=3)

    def test_heading_in_degrees(self):
        self.assertAlmostEqual(self.link.snapshot()["heading"], 271.0, places=3)

    def test_heading_wraps(self):
        import math
        self.link.values["heading"] = math.radians(370.0)
        self.assertAlmostEqual(self.link.snapshot()["heading"], 10.0, places=3)

    def test_altitude_already_in_feet(self):
        self.assertEqual(self.link.snapshot()["altitude"], 35000)

    def test_groundspeed_already_in_knots(self):
        self.assertEqual(self.link.snapshot()["groundspeed"], 450)

    def test_squawk_is_decoded(self):
        self.assertEqual(self.link.snapshot()["squawk"], 2000)

    def test_frequency_passes_through(self):
        self.assertEqual(self.link.snapshot()["com1"], 121.5)

    def test_out_of_band_frequency_is_none(self):
        self.link.values["com1"] = 0.0
        self.assertIsNone(self.link.snapshot()["com1"])
        self.link.values["com1"] = 999.0
        self.assertIsNone(self.link.snapshot()["com1"])

    def test_flaps_scaled_to_ratio(self):
        self.assertAlmostEqual(self.link.snapshot()["flaps"], 0.4)

    def test_lights_reported(self):
        lights = self.link.snapshot()["lights"]
        self.assertTrue(lights["strobe_on"])
        self.assertFalse(lights["beacon_on"])

    def test_no_values_means_no_snapshot(self):
        self.assertIsNone(simlink.SimLink().snapshot())

    def test_field_names_match_the_xplane_client(self):
        """和 xpc 共用 fsdpilot/voice，字段名对不上就会静默出错。"""
        required = {"latitude", "longitude", "altitude", "groundspeed",
                    "pitch", "bank", "heading", "squawk", "xpdr_mode",
                    "com1", "com2", "com1_power", "on_ground"}
        self.assertTrue(required.issubset(self.link.snapshot()))


class PollResultTest(unittest.TestCase):
    """在主菜单里读不到位置是常态，不该把 SimConnect 连接推倒重来。

    实飞日志里每隔五六秒一条 "SIM OPEN"，就是把"没进飞行"当成"连接断了"。
    """

    def setUp(self):
        self.link = simlink.SimLink()

    def test_three_distinct_results(self):
        self.assertEqual(len({simlink.OK, simlink.NO_DATA, simlink.FAILED}), 3)

    def test_no_data_when_position_is_missing(self):
        self.link._requests = type("R", (), {"get": lambda s, v: None})()
        self.assertIs(self.link._poll(), simlink.NO_DATA)

    def test_failed_when_simconnect_raises(self):
        def boom(self, simvar):
            raise OSError("连接没了")
        self.link._requests = type("R", (), {"get": boom})()
        self.assertIs(self.link._poll(), simlink.FAILED)

    def test_ok_when_position_is_present(self):
        self.link._requests = type("R", (), {"get": lambda s, v: 1.0})()
        self.assertIs(self.link._poll(), simlink.OK)

    def test_no_data_does_not_reopen_the_connection(self):
        # 只有 FAILED 才该走 _close()
        import inspect
        source = inspect.getsource(simlink.SimLink._run)
        no_data_block = source.split("if result is NO_DATA:")[1].split("continue")[0]
        self.assertNotIn("_close()", no_data_block)


class AircraftCfgTest(unittest.TestCase):
    """aircraft.cfg 是人手写的，格式相当随意。"""

    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write(self, text, name="aircraft.cfg"):
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_reads_title_and_type(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "A20N"\n\n'
            '[FLTSIM.0]\ntitle = "Airbus A320neo Asobo"\nicao_airline = ""\n'))
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].title, "Airbus A320neo Asobo")
        self.assertEqual(models[0].icao, "A20N")
        self.assertEqual(models[0].airline, "")

    def test_several_liveries_in_one_file(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "A20N"\n\n'
            '[FLTSIM.0]\ntitle = "A320neo Asobo"\n\n'
            '[FLTSIM.1]\ntitle = "A320neo Air China"\nicao_airline = "CCA"\n'))
        self.assertEqual(len(models), 2)
        self.assertEqual({m.airline for m in models}, {"", "CCA"})
        # 机型码来自 [GENERAL]，每个涂装都该拿到
        self.assertEqual({m.icao for m in models}, {"A20N"})

    def test_entries_without_a_title_are_skipped(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "B738"\n\n'
            '[FLTSIM.0]\nicao_airline = "CCA"\n\n'
            '[FLTSIM.1]\ntitle = "737 Max"\n'))
        self.assertEqual([m.title for m in models], ["737 Max"])

    def test_duplicate_keys_do_not_break_it(self):
        # configparser 默认会抛，必须 strict=False
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "B738"\n\n'
            '[FLTSIM.0]\ntitle = "A"\ntitle = "B"\n'))
        self.assertEqual(len(models), 1)

    def test_trailing_comments_and_quotes_stripped(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "B738" ; 注释\n\n'
            '[FLTSIM.0]\ntitle = "Boeing 738"\n'))
        self.assertEqual(models[0].icao, "B738")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(
            aimatch.parse_aircraft_cfg(os.path.join(self.directory, "nope.cfg")), [])

    def test_finds_cfgs_in_a_tree(self):
        inner = os.path.join(self.directory, "pkg", "SimObjects",
                             "Airplanes", "A320")
        os.makedirs(inner)
        with open(os.path.join(inner, "aircraft.cfg"), "w") as f:
            f.write('[FLTSIM.0]\ntitle = "x"\n')
        self.assertEqual(len(aimatch.find_aircraft_cfgs(self.directory)), 1)

    def test_texture_directories_are_skipped(self):
        # 贴图目录里没有飞机定义，跳过能省掉大量磁盘遍历
        inner = os.path.join(self.directory, "pkg", "texture.cca")
        os.makedirs(inner)
        with open(os.path.join(inner, "aircraft.cfg"), "w") as f:
            f.write('[FLTSIM.0]\ntitle = "x"\n')
        self.assertEqual(aimatch.find_aircraft_cfgs(self.directory), [])


class RealWorldLayoutTest(unittest.TestCase):
    """这几条都是拿开发机上真实的 MSFS 安装跑出来才发现的。

    合成的 aircraft.cfg 全过，真机上却只扫到 10 个涂装、3 种机型，而且所有飞机
    都被一个 Fenix 的部件配置顶替了。
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_usercfg_gives_the_real_package_path(self):
        # 包目录默认在 AppData 下，但装的时候可以改到任何地方。开发机上
        # UserCfg.opt 写的是 D:\MSFS2022（257 个飞机），AppData 下一个都没有。
        packages = os.path.join(self.directory, "MSFS2022")
        os.makedirs(packages)
        cfg = os.path.join(self.directory, "UserCfg.opt")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('SomeOther "x"\n')
            f.write(f'InstalledPackagesPath "{packages}"\n')
        self.assertEqual(aimatch._packages_from_usercfg(cfg), packages)

    def test_usercfg_pointing_nowhere_is_ignored(self):
        cfg = os.path.join(self.directory, "UserCfg.opt")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('InstalledPackagesPath "Z:\\\\does\\\\not\\\\exist"\n')
        self.assertIsNone(aimatch._packages_from_usercfg(cfg))

    def test_missing_usercfg_is_not_an_error(self):
        self.assertIsNone(aimatch._packages_from_usercfg(
            os.path.join(self.directory, "nope.opt")))

    def test_attachments_are_not_aircraft(self):
        # Fenix 在 attachments/ 下放了几十个部件配置，每个都有 [GENERAL] 和
        # title 但没有机型码。当成飞机会污染匹配表。
        inner = os.path.join(self.directory, "pkg", "SimObjects", "Airplanes",
                             "FNX_32X", "attachments", "fnx", "x", "config")
        os.makedirs(inner)
        with open(os.path.join(inner, "aircraft.cfg"), "w") as f:
            f.write('[GENERAL]\nicao_model = "A-319 CFM SL"\n\n'
                    '[FLTSIM.0]\ntitle = "FenixA319 CFM SL"\n')
        self.assertEqual(aimatch.find_aircraft_cfgs(self.directory), [])

    def test_type_designator_with_a_suffix(self):
        # 真机上见过 icao_type_designator = "A359 ULR"
        self.assertEqual(aimatch._clean_icao('"A359 ULR"'), "A359")

    def test_type_designator_normalised(self):
        self.assertEqual(aimatch._clean_icao("a20n"), "A20N")
        self.assertEqual(aimatch._clean_icao(" B738 "), "B738")

    def test_nonsense_type_designator_is_dropped(self):
        # 假机型码进了索引，真正是这个机型的飞机就永远匹配不到了
        self.assertEqual(aimatch._clean_icao("A-319 CFM SL"), "")
        self.assertEqual(aimatch._clean_icao("X"), "")
        self.assertEqual(aimatch._clean_icao(""), "")

    def test_fallback_prefers_a_model_with_a_type(self):
        # 没有机型码的多半是装得不规范的附加件，拿它当所有飞机的替身最难看
        models = aimatch.ModelSet([
            aimatch.Model("某个部件配置"),
            aimatch.Model("Cessna 172", icao="C172"),
        ])
        model, _ = models.match(equipment="ZZZZ")
        self.assertEqual(model.title, "Cessna 172")


class ModelMatchingTest(unittest.TestCase):
    """退化链。最重要的一条：永远要有结果。"""

    def setUp(self):
        self.models = aimatch.ModelSet([
            aimatch.Model("738 Air China", icao="B738", airline="CCA"),
            aimatch.Model("738 China Eastern", icao="B738", airline="CES"),
            aimatch.Model("739 Air China", icao="B739", airline="CCA"),
            aimatch.Model("A320neo Asobo", icao="A20N"),
            aimatch.Model("Cessna 172", icao="C172"),
        ])

    def test_exact_type_and_airline(self):
        model, why = self.models.match(equipment="B738", airline="CES")
        self.assertEqual(model.title, "738 China Eastern")
        self.assertIn("都匹配", why)

    def test_type_only_when_airline_unknown(self):
        self.assertEqual(self.models.match(equipment="B738")[0].icao, "B738")

    def test_unknown_airline_still_matches_type(self):
        model, why = self.models.match(equipment="B738", airline="UAL")
        self.assertEqual(model.icao, "B738")
        self.assertIn("涂装不对", why)

    def test_family_fallback_prefers_right_airline(self):
        model, why = self.models.match(equipment="B737", airline="CCA")
        self.assertEqual(model.airline, "CCA")
        self.assertIn("同族", why)

    def test_neo_variants_are_one_family(self):
        # A320 和 A20N 是同一架飞机的两种代码，必须互相顶替
        model, why = self.models.match(equipment="A320")
        self.assertEqual(model.icao, "A20N")
        self.assertIn("同族", why)

    def test_generic_fallback(self):
        model, why = self.models.match(equipment="A359")
        self.assertIn("通用", why)

    def test_widebody_is_not_replaced_by_a_narrowbody(self):
        """拿 A319 去顶 B777 视觉上差得离谱。

        实测发现的：本机装了 787 和 A350，但没装 777，原来会一路掉到兜底挑中
        一架 A319。同族之后加一级"同类机身"就能救回来。
        """
        models = aimatch.ModelSet([
            aimatch.Model("A319", icao="A319"),        # 排在前面，容易被兜底选中
            aimatch.Model("787-10", icao="B78X"),
        ])
        model, why = models.match(equipment="B77W")
        self.assertEqual(model.icao, "B78X", why)
        self.assertIn("宽体", why)

    def test_narrowbody_substitutes_for_narrowbody(self):
        models = aimatch.ModelSet([
            aimatch.Model("747", icao="B748"),
            aimatch.Model("A319", icao="A319"),
        ])
        model, why = models.match(equipment="B738")
        self.assertEqual(model.icao, "A319", why)
        self.assertIn("窄体", why)

    def test_light_aircraft_not_replaced_by_an_airliner(self):
        models = aimatch.ModelSet([
            aimatch.Model("A319", icao="A319"),
            aimatch.Model("172", icao="C172"),
        ])
        model, why = models.match(equipment="SR22")
        self.assertEqual(model.icao, "C172", why)

    def test_category_lookup(self):
        self.assertEqual(aimatch.category_of("B77W"), "宽体")
        self.assertEqual(aimatch.category_of("B738"), "窄体")
        self.assertEqual(aimatch.category_of("CRJ9"), "支线")
        self.assertEqual(aimatch.category_of("C172"), "通航")
        self.assertEqual(aimatch.category_of("ZZZZ"), "")

    def test_categories_do_not_overlap(self):
        # 一个机型落进两类，替身就成了看字典顺序的抽奖
        seen = {}
        for name, types in aimatch.CATEGORIES.items():
            for icao in types:
                self.assertNotIn(icao, seen,
                                 f"{icao} 同时在 {seen.get(icao)} 和 {name}")
                seen[icao] = name

    def test_unknown_type_still_returns_something(self):
        model, why = self.models.match(equipment="ZZZZ")
        self.assertIsNotNone(model, why)

    def test_no_information_still_returns_something(self):
        self.assertIsNotNone(self.models.match()[0])

    def test_empty_set_reports_why(self):
        model, why = aimatch.ModelSet().match(equipment="B738")
        self.assertIsNone(model)
        self.assertIn("没有找到", why)

    def test_explicit_title_wins_when_installed(self):
        model, why = self.models.match(equipment="B738", csl="Cessna 172")
        self.assertEqual(model.title, "Cessna 172")

    def test_unknown_csl_name_is_ignored(self):
        # 对方报的多半是 X-Plane 的 CSL 名，这里装不着，应当继续按机型匹配
        model, _ = self.models.match(equipment="B738", airline="CCA",
                                     csl="BB_A320_CCA")
        self.assertEqual(model.title, "738 Air China")

    def test_lowercase_input(self):
        self.assertEqual(
            self.models.match(equipment="b738", airline="ces")[0].title,
            "738 China Eastern")

    def test_models_without_a_type_are_not_indexed(self):
        # 没有 icao_type_designator 的飞机进不了索引，但一架带机型码的都没有时
        # 仍然要拿它兜底——看不见的飞机比涂装错的飞机危险得多
        models = aimatch.ModelSet([aimatch.Model("怪飞机")])
        model, why = models.match(equipment="B738")
        self.assertEqual(model.title, "怪飞机")
        self.assertIn("没有带机型码", why)


class FlightPlanTest(unittest.TestCase):
    """$FP 的字段布局。协议层和 xpc 共用同一份 fsdpilot.py。"""

    def setUp(self):
        import fsdpilot
        self.fsdpilot = fsdpilot
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1", "pw")
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def test_field_count(self):
        # can-fsd 的 minimumFields 要求 17 段
        self.pilot.file_flight_plan({})
        self.assertEqual(len(self.sent[0].split(":")), 17)

    def test_identifies_as_msfs_not_xplane(self):
        # 这份是从 xpc 复制来的，连它报 X-Plane 的编号一起带了过来
        self.assertEqual(self.fsdpilot.SIMULATOR,
                         self.fsdpilot.SIMULATOR_MSFS_2020)
        self.assertEqual(self.fsdpilot.CLIENT_NAME, "MSFS for CAN")


class InjectorTest(unittest.TestCase):
    """他机注入。真正跑要 SimConnect，这里只测不依赖模拟器的那部分。"""

    def setUp(self):
        import inject
        self.inject = inject

    def test_position_definition_field_count(self):
        # 写进去的结构体字段数必须和数据定义一致，错位飞机会跑到地球另一边
        self.assertEqual(len(self.inject._Definition.FIELDS), 8)

    def test_definition_fields_are_bytes(self):
        # ctypes 的 c_char_p 只吃 bytes，写成 str 会在运行时才炸
        for name, unit in self.inject._Definition.FIELDS:
            self.assertIsInstance(name, bytes)
            self.assertIsInstance(unit, bytes)

    def test_unavailable_without_simconnect(self):
        # 模拟器没开时构造不该抛，只是标记不可用
        injector = self.inject.TrafficInjector(sim=None)
        self.assertFalse(injector.available)

    def test_sync_is_a_noop_when_unavailable(self):
        injector = self.inject.TrafficInjector(sim=None)
        injector.sync([{"callsign": "CES2345", "latitude": 0, "longitude": 0,
                        "altitude": 0, "model": "x"}])
        self.assertEqual(injector.aircraft, {})

    def test_cap_leaves_headroom(self):
        # 每架都是完整的飞机模型，放太多会掉帧
        self.assertLessEqual(self.inject.MAX_AIRCRAFT, 64)
        self.assertGreater(self.inject.MAX_AIRCRAFT, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
