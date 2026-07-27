"""通播的测试：METAR 解析、模板渲染、席位模型。

    python -m unittest test_atis -v      （在 atis 目录下运行）

重点在两处容易出错的地方：
- 每个气象要素的 text / voice 两种形态（对应 vATIS 的 :VOX）
- 情报字母的推进和范围限制
"""

import json
import os
import tempfile
import threading
import time
import unittest

import metar as metar_module
import profile as profile_module
import template as template_module
import weather
from metar import Metar
from profile import Preset, Profile, Station

ZSPD = "ZSPD 251300Z 09004MPS 9999 FEW030 SCT100 25/18 Q1013 NOSIG"


class MetarTest(unittest.TestCase):

    def setUp(self):
        self.metar = Metar(ZSPD)

    def test_station_and_time(self):
        self.assertEqual(self.metar.station, "ZSPD")
        self.assertEqual(self.metar.observation_time.text, "251300Z")
        self.assertEqual(self.metar.observation_time.voice, "time one three zero zero")

    def test_wind_has_both_forms(self):
        # 文字通播照抄电码，语音要念得出来——这就是 :VOX 的意义
        self.assertEqual(self.metar.wind.text, "09004MPS")
        self.assertEqual(self.metar.wind.voice,
                         "wind zero niner zero at four meters per second")

    def test_visibility_and_clouds(self):
        self.assertEqual(self.metar.visibility.voice,
                         "visibility one zero kilometers or more")
        self.assertEqual(self.metar.clouds.text, "FEW030 SCT100")
        self.assertEqual(self.metar.clouds.voice, "few three thousand, scattered one zero thousand")

    def test_temperature_and_pressure(self):
        self.assertEqual(self.metar.temperature.voice, "temperature two five")
        self.assertEqual(self.metar.dew_point.voice, "dew point one eight")
        self.assertEqual(self.metar.pressure.voice, "QNH one zero one three hectopascals")

    def test_calm_and_variable_wind(self):
        self.assertEqual(Metar("ZBAA 251300Z 00000MPS 9999 25/18 Q1013").wind.voice,
                         "wind calm")
        self.assertEqual(Metar("ZBAA 251300Z VRB02MPS 9999 25/18 Q1013").wind.voice,
                         "wind variable at two meters per second")

    def test_gust_and_variation(self):
        wind = Metar("KLAX 251300Z 26015G25KT 220V300 10SM 25/18 A2992").wind
        self.assertIn("gusting two five knots", wind.voice)
        self.assertIn("variable between two two zero and three zero zero", wind.voice)

    def test_negative_temperature(self):
        parsed = Metar("ZYTX 251300Z 09004MPS 9999 M03/M07 A2992")
        self.assertEqual(parsed.temperature.voice, "temperature minus three")
        self.assertIn("altimeter two niner point niner two", parsed.pressure.voice)

    def test_weather_and_rvr(self):
        parsed = Metar("ZSPD 251300Z 09004MPS 3000 -SHRA R35L/1200 BKN010 25/18 Q1013")
        self.assertEqual(parsed.present_weather.voice, "light showers of rain")
        self.assertIn("runway three five", parsed.rvr.voice)
        self.assertEqual(parsed.visibility.voice, "visibility three kilometers")

    def test_cavok_and_cb(self):
        self.assertTrue(Metar("ZSPD 251300Z 09004MPS CAVOK 25/18 Q1013").cavok)
        parsed = Metar("ZSPD 251300Z 09004MPS 9999 BKN020CB 25/18 Q1013")
        self.assertEqual(parsed.clouds.voice, "broken two thousand cumulonimbus")

    def test_trend_is_separated(self):
        # NOSIG 之后的内容不该混进气象要素里
        self.assertEqual(self.metar.trend.text, "NOSIG")
        self.assertNotIn("NOSIG", self.metar.full_wx().text)

    def test_full_wx_order(self):
        full = self.metar.full_wx()
        self.assertEqual(full.text, "09004MPS 9999 FEW030 SCT100 25/18 Q1013")
        self.assertLess(full.voice.index("wind"), full.voice.index("visibility"))
        self.assertLess(full.voice.index("visibility"), full.voice.index("QNH"))

    def test_garbage_does_not_explode(self):
        parsed = Metar("ZSPD 251300Z 09004MPS 9999 XYZZY123 25/18 Q1013")
        self.assertTrue(parsed.is_valid())
        self.assertEqual(parsed.temperature.voice, "temperature two five")

    def test_metar_prefix_and_empty(self):
        self.assertEqual(Metar("METAR " + ZSPD).station, "ZSPD")
        self.assertFalse(Metar("").is_valid())


class TemplateTest(unittest.TestCase):

    def setUp(self):
        self.metar = Metar(ZSPD)
        self.context = template_module.build_context(
            self.metar, facility="ZSPD", letter="B",
            airport_conditions="跑道 35L 使用中", notams="无")

    def render(self, template):
        return template_module.render(template, self.context)

    def test_text_uses_raw_and_voice_uses_spoken(self):
        text, voice = self.render("[WIND]")
        self.assertEqual(text, "09004MPS")
        self.assertEqual(voice, "wind zero niner zero at four meters per second")

    def test_vox_suffix_forces_spoken_form_in_text(self):
        text, _ = self.render("[WIND:VOX]")
        self.assertEqual(text, "wind zero niner zero at four meters per second")

    def test_aliases_point_at_the_same_value(self):
        for name in ("[ATIS_LETTER]", "[ATIS_CODE]", "[LETTER]", "[ID]"):
            self.assertEqual(self.render(name)[0], "B", name)
        self.assertEqual(self.render("[VIS]")[0], self.render("[PREVAILING_VISIBILITY]")[0])

    def test_free_text_variables(self):
        self.assertEqual(self.render("[ARPT_COND]")[0], "跑道 35L 使用中")
        self.assertEqual(self.render("[NOTAMS]")[0], "无")

    def test_closing_can_reference_the_letter(self):
        _, voice = self.render("[CLOSING]")
        self.assertIn("information B", voice)

    def test_unknown_variable_is_left_alone(self):
        text, _ = self.render("[NOPE]")
        self.assertEqual(text, "[NOPE]", "认不出的变量要留着，方便发现拼错")
        self.assertEqual(template_module.unknown_variables("[NOPE] [WIND]"), ["NOPE"])

    def test_empty_variables_do_not_leave_debris(self):
        # 没有 RVR 时不该留下多余的空格和标点
        text, _ = self.render("[WIND]. [RVR]. [PRESSURE]")
        self.assertNotIn("  ", text)
        self.assertNotIn("..", text)

    def test_default_template_renders(self):
        text, voice = self.render(template_module.DEFAULT_TEMPLATE)
        self.assertTrue(text.startswith("ZSPD ATIS B"))
        self.assertIn("跑道 35L 使用中", text)
        self.assertIn("wind zero niner zero", voice)
        self.assertIn("information B", voice)


class StationTest(unittest.TestCase):

    def test_callsign_by_type(self):
        self.assertEqual(Station("zspd").callsign, "ZSPD_ATIS")
        self.assertEqual(Station("ZSPD", atis_type=profile_module.TYPE_DEPARTURE).callsign,
                         "ZSPD_D_ATIS")
        self.assertEqual(Station("ZSPD", atis_type=profile_module.TYPE_ARRIVAL).callsign,
                         "ZSPD_A_ATIS")

    def test_channel_matches_the_network_convention(self):
        station = Station("ZSPD", frequency="127.850")
        self.assertEqual(station.frequency_khz, 127850)
        self.assertEqual(station.channel, "FREQ_127850")

    def test_letter_advances_and_wraps(self):
        station = Station("ZSPD")
        self.assertEqual(station.letter, "A")
        self.assertEqual(station.advance_letter(), "B")
        station.set_letter("Z")
        self.assertEqual(station.advance_letter(), "A")

    def test_code_range_limits_the_letters(self):
        station = Station("ZSPD", code_range=("A", "C"))
        self.assertEqual(station.letters_in_range(), ["A", "B", "C"])
        station.letter = "C"
        self.assertEqual(station.advance_letter(), "A")
        self.assertFalse(station.set_letter("M"), "范围外的字母不该被接受")

    def test_code_range_can_wrap_past_z(self):
        station = Station("ZSPD", code_range=("Y", "B"))
        self.assertEqual(station.letters_in_range(), ["Y", "Z", "A", "B"])
        station.letter = "Z"
        self.assertEqual(station.advance_letter(), "A")


class ProfileTest(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.remove(self.path)
        self.profile = Profile(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_add_and_reject_duplicates(self):
        self.profile.add(Station("ZSPD"))
        with self.assertRaises(ValueError):
            self.profile.add(Station("ZSPD"))
        # 类型不同就是不同席位，可以共存
        self.profile.add(Station("ZSPD", atis_type=profile_module.TYPE_DEPARTURE))
        self.assertEqual(len(self.profile), 2)

    def test_round_trip(self):
        station = Station("ZSPD", "浦东", "127.850", code_range=("A", "M"))
        station.presets = [Preset("白天", "[FACILITY] [ATIS_LETTER]", "跑道 35L")]
        station.advance_letter()
        self.profile.add(station)
        self.profile.save()

        restored = Profile(self.path)
        self.assertEqual(len(restored), 1)
        loaded = restored.get("ZSPD_ATIS")
        self.assertEqual(loaded.name, "浦东")
        self.assertEqual(loaded.frequency, "127.850")
        self.assertEqual(loaded.letter, "B")
        self.assertEqual(loaded.code_range, ("A", "M"))
        self.assertEqual(loaded.presets[0].airport_conditions, "跑道 35L")

    def test_bad_entries_are_skipped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"stations": [{"identifier": "ZSPD"}, {"nope": 1}]}, f)
        restored = Profile(self.path)
        self.assertEqual(len(restored), 1)


VATIS_PROFILE = {
    "name": "測試配置",
    "id": "9d1f5c3a-0000-4000-8000-000000000001",
    "stations": [
        {
            "id": "s1",
            "identifier": "KLAX",
            "name": "Los Angeles Intl",
            "atisType": "Combined",
            "codeRange": {"low": "A", "high": "M"},
            "frequency": 133800000,
            "idsEndpoint": "https://ids.example/api",
            "atisFormat": {"surfaceWind": {"speakLeadingZero": True}},
            "presets": [
                {"id": "p2", "ordinal": 1, "name": "夜间",
                 "template": "[FACILITY] ATIS [ATIS_LETTER]. [WX]. @RWY_CLOSED",
                 "airportConditions": "", "notams": "TWY B CLOSED"},
                {"id": "p1", "ordinal": 0, "name": "白天",
                 "template": "[FACILITY] ATIS [ATIS_LETTER]. [WX]. [ARPT_COND]",
                 "airportConditions": "RWY 25L IN USE", "notams": ""},
            ],
            "contractions": [
                {"variableName": "RWY_CLOSED", "text": "RWY 07L CLSD",
                 "voice": "runway zero seven left closed"},
            ],
        },
        {
            "id": "s2",
            "identifier": "KLAX",
            "name": "Los Angeles Departure",
            "atisType": "Departure",
            "codeRange": {"low": "N", "high": "Z"},
            "frequency": 135650000,
            "presets": [{"name": "默认", "template": "[FACILITY] [ATIS_LETTER]"}],
        },
    ],
}


class VatisImportTest(unittest.TestCase):

    def setUp(self):
        import vatis_import
        self.module = vatis_import
        handle, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(VATIS_PROFILE, f)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_frequency_is_hertz(self):
        # vATIS 存的是赫兹，133800000 就是 133.800
        self.assertEqual(self.module.parse_frequency(133800000), "133.800")
        self.assertEqual(self.module.parse_frequency(118000), "118.000")   # 千赫
        self.assertEqual(self.module.parse_frequency(127.85), "127.850")   # 兆赫

    def test_frequency_out_of_range_is_rejected(self):
        for bad in (99000000, 250000000, "abc"):
            with self.assertRaises(self.module.ImportError_, msg=str(bad)):
                self.module.parse_frequency(bad)

    def test_imports_stations(self):
        name, stations, _ = self.module.load_profile(self.path)
        self.assertEqual(name, "測試配置")
        self.assertEqual([s.callsign for s in stations],
                         ["KLAX_ATIS", "KLAX_D_ATIS"])
        self.assertEqual(stations[0].frequency, "133.800")
        self.assertEqual(stations[0].code_range, ("A", "M"))
        self.assertEqual(stations[1].code_range, ("N", "Z"))

    def test_presets_keep_order_and_content(self):
        _, stations, _ = self.module.load_profile(self.path)
        presets = stations[0].presets
        self.assertEqual([p.name for p in presets], ["白天", "夜间"],
                         "应当按 ordinal 排序")
        self.assertEqual(presets[0].airport_conditions, "RWY 25L IN USE")
        self.assertEqual(presets[1].notams, "TWY B CLOSED")

    def test_contractions_are_imported_and_expand(self):
        _, stations, _ = self.module.load_profile(self.path)
        station = stations[0]
        self.assertEqual(station.contractions["RWY_CLOSED"][1],
                         "runway zero seven left closed")

        context = template_module.build_context(Metar(ZSPD), "KLAX", "A")
        text, voice = template_module.render(
            station.presets[1].template, context, station.contractions)
        self.assertIn("RWY 07L CLSD", text, "文字形态")
        self.assertIn("runway zero seven left closed", voice, "语音形态")

    def test_unsupported_settings_are_reported(self):
        _, _, notes = self.module.load_profile(self.path)
        joined = " ".join(notes)
        self.assertIn("IDS", joined, "没有对应功能的设置要说出来，别让人以为全导进来了")

    def test_old_profiles_use_composites(self):
        self.write({"name": "旧版", "composites": VATIS_PROFILE["stations"][:1]})
        _, stations, _ = self.module.load_profile(self.path)
        self.assertEqual(stations[0].callsign, "KLAX_ATIS")

    def test_numeric_atis_type(self):
        entry = dict(VATIS_PROFILE["stations"][0], atisType=1)
        self.write({"stations": [entry]})
        _, stations, _ = self.module.load_profile(self.path)
        self.assertEqual(stations[0].atis_type, profile_module.TYPE_DEPARTURE)

    def test_bad_station_is_skipped_not_fatal(self):
        entry = dict(VATIS_PROFILE["stations"][0])
        self.write({"stations": [{"identifier": ""}, entry]})
        _, stations, notes = self.module.load_profile(self.path)
        self.assertEqual(len(stations), 1)
        self.assertTrue(any("无法导入" in n for n in notes))

    def test_helpful_errors(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        with self.assertRaises(self.module.ImportError_):
            self.module.load_profile(self.path)

        self.write({"name": "空的"})
        with self.assertRaises(self.module.ImportError_):
            self.module.load_profile(self.path)


class FsdCallsignTest(unittest.TestCase):
    """呼号规则来自 can-fsd 的 IsValidCallsign / IsATISCallsign。"""

    def setUp(self):
        import fsdclient
        self.module = fsdclient

    def test_combined_callsign_is_accepted(self):
        self.assertIsNone(self.module.callsign_problem("ZSPD_ATIS"))

    def test_departure_callsign_is_too_long(self):
        # ZSPD_D_ATIS 有 11 个字符，服务端上限是 10 —— 会被直接拒登
        problem = self.module.callsign_problem("ZSPD_D_ATIS")
        self.assertIsNotNone(problem)
        self.assertIn("11", problem)

    def test_must_end_with_atis(self):
        self.assertIn("_ATIS", self.module.callsign_problem("ZSPD_TWR"))

    def test_bad_characters(self):
        self.assertIsNotNone(self.module.callsign_problem("ZS PD_ATIS"))


class FsdProtocolTest(unittest.TestCase):
    """对着按 can-fsd 包格式应答的假服务端跑一遍，验的是"能不能和真服务端对上"。"""

    def setUp(self):
        import fsdclient
        self.module = fsdclient

    def make_server(self, reject=False):
        import socket
        import threading

        class FakeServer(threading.Thread):
            def __init__(self):
                super().__init__(daemon=True)
                self.listener = socket.socket()
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind(("127.0.0.1", 0))
                self.listener.listen(1)
                self.port = self.listener.getsockname()[1]
                self.received = []
                self.position_seen = threading.Event()
                self.running = True
                self._conn = None

            def send(self, packet):
                if self._conn:
                    self._conn.sendall((packet + "\r\n").encode())

            def run(self):
                try:
                    conn, _ = self.listener.accept()
                except OSError:
                    return
                self._conn = conn
                conn.settimeout(0.5)
                self.send("$DISERVER:CLIENT:VATSIM FSD V3.41b:abc123")
                buffer = b""
                while self.running:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        packet = line.decode().strip()
                        if packet:
                            self.handle(packet)
                try:
                    conn.close()
                except OSError:
                    pass

            def handle(self, packet):
                self.received.append(packet)
                fields = packet.split(":")
                if packet.startswith("#AA") and reject:
                    self.send("$ERSERVER:unknown:006::Invalid CID/password.")
                    return
                if packet.startswith("$CQ") and len(fields) >= 3 and fields[2] == "CAPS":
                    self.send(f"$CRSERVER:{packet[3:].split(':')[0]}:CAPS:ATCINFO=1")
                    return
                if packet.startswith("$AX") and len(fields) >= 4 and fields[2] == "METAR":
                    callsign = packet[3:].split(":")[0]
                    self.send(f"$ARserver:{callsign}:METAR:{fields[3]} 251300Z "
                              f"09004MPS 9999 25/18 Q1013")
                    return
                if packet.startswith("%"):
                    self.position_seen.set()

            def stop(self):
                self.running = False
                try:
                    self.listener.close()
                except OSError:
                    pass

            def packets(self, prefix):
                return [p for p in self.received if p.startswith(prefix)]

        server = FakeServer()
        server.start()
        self.addCleanup(server.stop)
        return server

    def make_client(self, server, **kwargs):
        client = self.module.FSDClient(
            "127.0.0.1", "ZSPD_ATIS", "1005", "secret", "127.850",
            real_name="Test", port=server.port,
            latitude=31.1434, longitude=121.805,
            atis_lines=["ZSPD ATIS A", "WIND CALM"], **kwargs)
        self.addCleanup(client.stop)
        client.start()
        return client

    def wait(self, predicate, timeout=10):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_frequency_wire_encoding(self):
        # can-fsd 的 formatFrequency 反过来：包里 "27850" → 显示 127.850
        self.assertEqual(self.module.encode_frequency("127.850"), "27850")
        self.assertEqual(self.module.encode_frequency("118.000"), "18000")

    def test_login_packets(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(server.position_seen.wait(10), "没有等到位置包")

        ident = server.packets("$ID")[0].split(":")
        self.assertEqual(ident[0], "$IDZSPD_ATIS")
        self.assertEqual(ident[6], "1005")
        self.assertEqual(len(ident), 8, "第 9 个字段（质询）必须留空，否则服务端会发 $ZC")

        add = server.packets("#AA")[0].split(":")
        self.assertEqual(add[3], "1005")
        self.assertEqual(add[4], "secret")
        self.assertEqual(add[5], str(self.module.RATING_OBSERVER))

    def test_position_layout(self):
        server = self.make_server()
        self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))

        # %callsign:frequency:facility:visRange:rating:lat:lon:0
        position = server.packets("%")[0].split(":")
        self.assertEqual(position[1], "27850")
        self.assertEqual(position[2], str(self.module.FACILITY_ATIS))
        self.assertAlmostEqual(float(position[5]), 31.1434, places=4)
        self.assertEqual(position[7], "0")

    def test_answers_pilot_atis_query(self):
        server = self.make_server()
        self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))

        server.send("$CQCES2345:ZSPD_ATIS:ATIS")
        self.assertTrue(self.wait(lambda: server.packets("$CRZSPD_ATIS:CES2345")))
        replies = server.packets("$CRZSPD_ATIS:CES2345")
        self.assertEqual(replies[0], "$CRZSPD_ATIS:CES2345:ATIS:T:ZSPD ATIS A")
        self.assertEqual(replies[-1], "$CRZSPD_ATIS:CES2345:ATIS:E:2")

    def test_metar_round_trip(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(self.wait(lambda: client.connected))

        report = client.request_metar("ZSPD", timeout=10)
        self.assertIsNotNone(report, "没有拿到 $AR 回来的报文")
        self.assertTrue(report.startswith("ZSPD 251300Z"))

    def test_error_after_login_does_not_drop_connection(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))

        server.send("$ERserver:ZSPD_ATIS:012::No METAR available for ZZZZ")
        import time
        time.sleep(1)
        self.assertTrue(client.thread.is_alive(), "登录后的 $ER 不该把连接拆掉")

    def test_rejected_login_reports_reason(self):
        server = self.make_server(reject=True)
        states = []
        self.make_client(server, on_status=lambda s, m: states.append((s, m)))
        self.assertTrue(self.wait(lambda: any(s == 'error' for s, _ in states)))
        self.assertIn("Invalid CID/password", [m for s, m in states if s == 'error'][0])

    def test_logoff_packet(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))
        client.stop()
        self.assertTrue(self.wait(lambda: server.packets("#DA")))
        self.assertEqual(server.packets("#DA")[0], "#DAZSPD_ATIS:1005")


DATAFEED = {
    "controllers": [
        {"cid": "1000", "callsign": "RJTT_TWR", "rating": 5},
        {"cid": "1007", "callsign": "ZSPD_APP", "rating": 4},
    ],
    "atis": [
        {"cid": "1009", "callsign": "ZBAA_ATIS", "rating": 2},
    ],
}


class RatingLookupTest(unittest.TestCase):
    """通播的等级要跟本人此刻的管制席位一致，写死 OBS 会显示成观察员。"""

    def setUp(self):
        import datafeed
        self.module = datafeed

    def test_finds_the_rating_of_a_controller(self):
        self.assertEqual(self.module.rating_for("1000", data=DATAFEED), 5)

    def test_also_looks_in_atis_stations(self):
        self.assertEqual(self.module.rating_for("1009", data=DATAFEED), 2)

    def test_absent_cid_returns_none(self):
        # 人不在线时查不到，调用方退回配置里的值
        self.assertIsNone(self.module.rating_for("9999", data=DATAFEED))

    def test_blank_cid(self):
        self.assertIsNone(self.module.rating_for("", data=DATAFEED))

    def test_bad_rating_is_ignored(self):
        data = {"controllers": [{"cid": "1000", "rating": "abc"},
                                {"cid": "1000", "callsign": "X_TWR", "rating": 3}]}
        self.assertEqual(self.module.rating_for("1000", data=data), 3)

    def test_empty_datafeed(self):
        self.assertIsNone(self.module.rating_for("1000", data={}))
        self.assertIsNone(self.module.rating_for("1000", data=None, url="http://0.0.0.0:1"))


class ControllerGateTest(unittest.TestCase):
    """没在管制就不该挂通播，否则会留下无人值守的通播席位。"""

    def setUp(self):
        import datafeed
        self.module = datafeed

    def feed(self, **kwargs):
        data = {"controllers": [], "atis": []}
        data.update(kwargs)
        return data

    def test_active_controller_passes(self):
        data = self.feed(controllers=[
            {"cid": "1000", "callsign": "RJTT_TWR", "facility": 4, "rating": 5}])
        entry = self.module.controller_for("1000", data=data)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["callsign"], "RJTT_TWR")

    def test_observer_does_not_count(self):
        # 判定和 can-fsd 的 handleQueryATC 一致：facility 要高于观察员
        data = self.feed(controllers=[
            {"cid": "1000", "callsign": "RJTT_OBS", "facility": 0, "rating": 1}])
        self.assertIsNone(self.module.controller_for("1000", data=data))

    def test_an_existing_atis_does_not_count(self):
        data = self.feed(atis=[
            {"cid": "1000", "callsign": "RJAA_ATIS", "facility": 7, "rating": 5}])
        self.assertIsNone(self.module.controller_for("1000", data=data),
                          "已经开着的通播不能当作在管制")

    def test_someone_else_controlling_does_not_count(self):
        data = self.feed(controllers=[
            {"cid": "1007", "callsign": "RJTT_TWR", "facility": 4, "rating": 5}])
        self.assertIsNone(self.module.controller_for("1000", data=data))

    def test_missing_facility_is_treated_as_observer(self):
        data = self.feed(controllers=[{"cid": "1000", "callsign": "X"}])
        self.assertIsNone(self.module.controller_for("1000", data=data))

    def test_no_data_returns_none(self):
        self.assertIsNone(self.module.controller_for("1000", data={}))


class AutoRatingTest(unittest.TestCase):
    """FSDClient 的 rating=None 表示自动，登录前查一次。"""

    def setUp(self):
        import fsdclient
        self.module = fsdclient

    def test_explicit_rating_is_kept(self):
        client = self.module.FSDClient("h", "ZSPD_ATIS", "1", "p", "118.000", rating=5)
        self.assertEqual(client.rating, 5)

    def test_auto_defers_until_connect(self):
        client = self.module.FSDClient("h", "ZSPD_ATIS", "1", "p", "118.000",
                                       rating=0, rating_lookup=lambda: 5)
        self.assertIsNone(client.rating, "连接之前不该定下来")

    def test_lookup_failure_falls_back_to_observer(self):
        def explode():
            raise RuntimeError("数据源挂了")
        client = self.module.FSDClient("h", "ZSPD_ATIS", "1", "p", "118.000",
                                       rating=None, rating_lookup=explode)
        client.running = True
        client._connect()          # 连不上，但等级应当已经定好
        self.assertEqual(client.rating, self.module.RATING_OBSERVER,
                         "查不到就退回 OBS，不能让播出起不来")


class AirportPositionTest(unittest.TestCase):
    """席位不填坐标就会落在 0/0——几内亚湾外海。"""

    def setUp(self):
        import airports
        self.airports = airports

    def test_known_airports(self):
        self.assertEqual(self.airports.coordinates("RJAA"), (35.76694, 140.38778))
        self.assertEqual(self.airports.coordinates("zspd"), (31.14233, 121.79084))

    def test_unknown_airport(self):
        self.assertIsNone(self.airports.coordinates("XXXX"))
        self.assertIsNone(self.airports.coordinates(""))

    def test_station_fills_its_position_from_the_icao(self):
        station = Station("RJAA", frequency="128.250")
        self.assertAlmostEqual(station.latitude, 35.76694, places=4)
        self.assertAlmostEqual(station.longitude, 140.38778, places=4)

    def test_manual_position_wins(self):
        station = Station("RJAA", frequency="128.250",
                          latitude=35.5, longitude=140.1)
        self.assertEqual(station.latitude, 35.5)

    def test_unknown_airport_leaves_zero(self):
        station = Station("XXXX", frequency="128.250")
        self.assertEqual((station.latitude, station.longitude), (0.0, 0.0))

    def test_position_survives_a_round_trip(self):
        station = Station("RJAA", frequency="128.250")
        restored = Station.from_dict(station.to_dict())
        self.assertAlmostEqual(restored.latitude, 35.76694, places=4)


class SettingsTest(unittest.TestCase):
    """FSD 和语音是两台不同的服务器，配错了会一直连不上。"""

    def setUp(self):
        import settings as settings_module
        self.module = settings_module
        self.previous_cwd = os.getcwd()
        self.temp = tempfile.mkdtemp(prefix="atis_settings_")
        os.chdir(self.temp)
        self.addCleanup(lambda: os.chdir(self.previous_cwd))

    def write(self, data):
        with open("atis_settings.json", "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_default_fsd_host(self):
        self.assertEqual(self.module.Settings().fsd_host, "fsd.airwaysn.org")
        self.assertEqual(self.module.Settings().fsd_port, 6809)

    def test_migrates_the_voice_host_away(self):
        # 早期版本把语音服务器的地址填成了 FSD 地址，那台机器上没有 FSD
        self.write({"fsd_host": "hjdczy.top"})
        self.assertEqual(self.module.Settings().fsd_host, "fsd.airwaysn.org")

    def test_keeps_a_deliberate_override(self):
        self.write({"fsd_host": "127.0.0.1", "fsd_port": 16809})
        settings = self.module.Settings()
        self.assertEqual(settings.fsd_host, "127.0.0.1")
        self.assertEqual(settings.fsd_port, 16809)


class ResampleTest(unittest.TestCase):
    """语音合成出来的采样率不一定是 48kHz，送进 Mumble 前要转换。"""

    def setUp(self):
        # broadcast 会拉起 pymumble，缺 opus 原生库时用替身放行
        import sys
        from unittest import mock
        try:
            import opuslib  # noqa: F401
        except Exception:
            for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                         "opuslib.api.encoder", "opuslib.api.info",
                         "opuslib.exceptions"):
                sys.modules.setdefault(name, mock.MagicMock())
        global broadcast
        import broadcast

    def test_length_scales_with_rate(self):
        import numpy as np
        audio = np.zeros(22050, dtype=np.int16)
        out = broadcast.resample(audio, 22050, 48000)
        self.assertEqual(len(out), 48000, "1 秒的音频重采样后还该是 1 秒")

    def test_same_rate_is_untouched(self):
        import numpy as np
        audio = np.arange(10, dtype=np.int16)
        self.assertIs(broadcast.resample(audio, 48000, 48000), audio)

    def test_signal_shape_is_preserved(self):
        import numpy as np
        source = np.linspace(0, 1000, 1000)
        out = broadcast.resample(source, 8000, 16000)
        self.assertAlmostEqual(out[0], source[0], places=3)
        self.assertAlmostEqual(out[-1], source[-1], places=3)
        self.assertTrue(np.all(np.diff(out) >= 0), "单调上升的信号不该出现回折")

    def test_empty_input(self):
        import numpy as np
        self.assertEqual(len(broadcast.resample(np.zeros(0), 22050, 48000)), 0)


class VoiceFixTest(unittest.TestCase):
    """语音稿收尾。用例全部来自 RJTT 的真实播出输出。"""

    def setUp(self):
        global voicefix
        import voicefix

    # 真实播出时管制员填的自由文本，原样照抄自 vATIS 的 RJTT profile
    FREE = ("ILS ZULU RWY34L APCH AND ILS ZULU RWY34R APCH. LDG RWY 34 LEFT "
            "AND 34 RIGHT. DEP RWY 05 AND 34 RIGHT. DEP FREQ 126.0, "
            "SIMUL PARL ILS APCHS TO RWY34L_R ARE INPR")

    def test_abbreviations_are_expanded(self):
        out = voicefix.expand_free_text(self.FREE)
        for short in ("APCH", "LDG", "RWY", "DEP", "SIMUL", "PARL", "INPR"):
            self.assertNotIn(short, out.upper().split(),
                             f"{short} 没展开，TTS 会逐字母念")

    def test_runway_number_is_spelled(self):
        # RWY34L 念成"三十四"是错的，通播里逐位念
        out = voicefix.expand_free_text("RWY34L")
        self.assertIn("three four left", out)
        self.assertNotIn("34", out)

    def test_underscore_means_and(self):
        # 日本通播里 RWY34L_R 是 34 左和 34 右
        out = voicefix.expand_free_text("RWY34L_R")
        self.assertIn("left and right", out)
        self.assertNotIn("_", out)

    def test_frequency_decimal(self):
        out = voicefix.expand_free_text("DEP FREQ 126.0")
        self.assertIn("one two six decimal zero", out)

    def test_no_stray_symbols_left(self):
        out = voicefix.expand_free_text(self.FREE)
        for symbol in ("_", "/"):
            self.assertNotIn(symbol, out)

    # 真实播出里粘在一起的那段
    GLUED = ("broken niner thousandtemperature two five/dew point two two "
             "QNH one zero one four hectopascals2992")

    def test_glued_words_are_separated(self):
        # 全小写的粘连没有模式能识别，靠已知的段起始词切
        out = voicefix.polish(self.GLUED)
        self.assertNotIn("thousandtemperature", out)
        self.assertIn("thousand, temperature", out)

    def test_glued_number_is_separated_and_spelled(self):
        out = voicefix.polish(self.GLUED)
        self.assertNotIn("hectopascals2992", out)
        self.assertIn("two niner niner two", out)

    def test_slash_is_removed(self):
        self.assertNotIn("/", voicefix.polish(self.GLUED))

    def test_polish_leaves_good_text_alone(self):
        good = "wind zero eight zero at one zero knots, visibility one zero kilometers"
        self.assertEqual(voicefix.polish(good), good)

    def test_join_elements_separates(self):
        self.assertEqual(
            voicefix.join_elements(["broken niner thousand", "temperature two five"]),
            "broken niner thousand, temperature two five")

    def test_join_skips_empties(self):
        self.assertEqual(voicefix.join_elements(["a", "", None, "b"]), "a, b")

    def test_empty_input(self):
        self.assertEqual(voicefix.polish(""), "")
        self.assertEqual(voicefix.expand_free_text(""), "")

    def test_punctuation_spacing(self):
        self.assertEqual(voicefix.polish("a ,b .c"), "a, b. c")


class ChineseVoiceTest(unittest.TestCase):
    """中文通播稿。不是英文的逐词翻译，语序和读法都是民航自己的一套。"""

    def setUp(self):
        global chinese
        import chinese

    def test_radio_digits(self):
        # 幺两拐洞是无线电通话规范，不是方言；和 server/ATIS/process.py 一致
        self.assertEqual(chinese.spell("09004"), "洞 九 洞 洞 四")
        self.assertEqual(chinese.spell("7"), "拐")
        self.assertEqual(chinese.spell("21"), "两 幺")

    def test_counting_numbers(self):
        # 温度、米数念整数，不逐位
        self.assertEqual(chinese.spell_count(25), "二十五")
        self.assertEqual(chinese.spell_count(900), "九百")
        self.assertEqual(chinese.spell_count(3000), "三千")
        self.assertEqual(chinese.spell_count(15), "十五")
        self.assertEqual(chinese.spell_count(-3), "零下 三")
        self.assertEqual(chinese.spell_count(0), "零")

    def test_wind(self):
        self.assertEqual(chinese._wind("09004MPS"), "风 洞 九 洞 度 四 米每秒")

    def test_wind_variable(self):
        self.assertIn("风向不定", chinese._wind("VRB02MPS"))

    def test_wind_calm(self):
        self.assertEqual(chinese._wind("00000MPS"), "静风")

    def test_wind_gusts(self):
        self.assertIn("阵风", chinese._wind("27010G18MPS"))

    def test_visibility(self):
        self.assertEqual(chinese._visibility("9999"), "能见度 幺洞 公里 以上")
        self.assertEqual(chinese._visibility("3000"), "能见度 三 公里")
        self.assertEqual(chinese._visibility("0800"), "能见度 八百 米")

    def test_cavok(self):
        self.assertIn("云高", chinese._visibility("CAVOK"))

    def test_cloud_height_uses_the_domestic_convention(self):
        """按 100 英尺 = 30 米折算。

        用精确的 30.48 会念出"九百一十米"，真实通播念的是"九百米"。
        """
        self.assertEqual(chinese._clouds("FEW030"), "少云 九百 米")
        self.assertEqual(chinese._clouds("OVC050"), "阴天 一千五百 米")
        self.assertEqual(chinese._clouds("SCT100"), "疏云 三千 米")

    def test_cloud_with_type(self):
        self.assertIn("积雨云", chinese._clouds("BKN020CB"))

    def test_cloud_without_height(self):
        self.assertEqual(chinese._clouds("NSC"), "无重要云")

    def test_weather_intensity(self):
        self.assertEqual(chinese._weather("-RA"), "小雨")
        self.assertEqual(chinese._weather("+TSRA"), "大雷暴雨")
        self.assertIn("附近有", chinese._weather("VCSH"))

    def test_temperature(self):
        self.assertEqual(chinese._temperature("25"), "二十五")
        self.assertEqual(chinese._temperature("M03"), "零下 三")

    def test_pressure_hectopascals(self):
        self.assertEqual(chinese._pressure("Q1013"), "修正海压 幺 洞 幺 三 百帕")

    def test_pressure_inches(self):
        self.assertIn("英寸汞柱", chinese._pressure("A2992"))

    def test_full_script(self):
        parsed = metar_module.Metar(ZSPD)
        script = chinese.render(parsed, facility="上海浦东", letter="D",
                                runway="三六左")
        for fragment in ("上海浦东", "通播", "D", "风", "能见度", "温度",
                         "修正海压", "完毕"):
            self.assertIn(fragment, script)

    def test_script_has_no_latin_weather_codes(self):
        # 漏翻的电码会被 TTS 逐字母念出来，非常难听
        script = chinese.render(metar_module.Metar(
            "ZBAA 270830Z 27010G18MPS 3000 -RA BKN020CB OVC050 M03/M07 Q1025"))
        for code in ("MPS", "BKN", "OVC", "CB", "RA", "Q10"):
            self.assertNotIn(code, script)

    def test_missing_metar_does_not_raise(self):
        self.assertIn("通播", chinese.render(None, facility="上海浦东", letter="A"))

    def test_garbage_metar_does_not_raise(self):
        chinese.render(metar_module.Metar("完全不是报文"))


class JoinChannelTest(unittest.TestCase):
    """频率频道不存在时要新建，并且要等服务器把它回报回来。

    以前是建完 sleep(0.2) 就去找，远程服务器上经常还没回来，报出来是
    "Channel FREQ_127800 does not exists"——听着像频道建不了，其实只是没等到。
    """

    def setUp(self):
        import sys
        from unittest import mock
        try:
            import opuslib  # noqa: F401
        except Exception:
            for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                         "opuslib.api.encoder", "opuslib.api.info",
                         "opuslib.exceptions"):
                sys.modules.setdefault(name, mock.MagicMock())
        global broadcast
        import broadcast
        self.broadcast = broadcast
        # 有的用例要改这个模块级常量，跑完必须还原，否则会影响别的用例
        self._timeout = broadcast.CHANNEL_TIMEOUT

        # 不跑真的构造函数——它会拉起合成器和线程
        self.caster = broadcast.Broadcaster.__new__(broadcast.Broadcaster)
        self.caster.running = True
        self.caster.stop_event = threading.Event()
        self.caster._denial = None
        self.caster.states = []
        self.caster._state = lambda kind, message: self.caster.states.append(
            (kind, message))
        self.caster.station = type("S", (), {
            "channel": "FREQ_127800", "frequency": "127.800",
            "callsign": "ROAH_ATIS"})()

        self.created = []
        self.moved = []
        self.channels = {}

        caster = self.caster
        outer = self

        class Channels:
            def find_by_name(self, name):
                import pymumble_py3 as pymumble
                if name in outer.channels:
                    return outer.channels[name]
                raise pymumble.errors.UnknownChannelError(name)

            def new_channel(self, parent, name, temporary=False):
                outer.created.append((parent, name, temporary))

        class Myself:
            def move_in(self, channel_id):
                outer.moved.append(channel_id)

        caster.mumble = type("M", (), {
            "channels": Channels(),
            "users": type("U", (), {"myself": Myself()})(),
        })()

    def tearDown(self):
        self.broadcast.CHANNEL_TIMEOUT = self._timeout

    def test_existing_channel_is_used_directly(self):
        self.channels["FREQ_127800"] = {"channel_id": 7}
        self.assertTrue(self.caster._join_channel())
        self.assertEqual(self.created, [], "已存在就不该再建")
        self.assertEqual(self.moved, [7])

    def test_missing_channel_is_created_as_temporary(self):
        # 服务器"稍后"才回报：新建之后才让它出现
        original = self.broadcast.Broadcaster._wait_for_channel

        def appear(caster, name):
            self.channels[name] = {"channel_id": 9}
            return original(caster, name)

        self.broadcast.Broadcaster._wait_for_channel = appear
        try:
            self.assertTrue(self.caster._join_channel())
        finally:
            self.broadcast.Broadcaster._wait_for_channel = original
        self.assertEqual(self.created, [(0, "FREQ_127800", True)])
        self.assertEqual(self.moved, [9])

    def test_waits_rather_than_giving_up_immediately(self):
        # 频道在 0.3 秒后才出现——固定 sleep(0.2) 的老写法会在这里失败
        def appear():
            time.sleep(0.3)
            self.channels["FREQ_127800"] = {"channel_id": 11}

        threading.Thread(target=appear, daemon=True).start()
        self.assertTrue(self.caster._join_channel())
        self.assertEqual(self.moved, [11])

    def test_timeout_says_so_without_blaming_the_channel(self):
        self.broadcast.CHANNEL_TIMEOUT = 0.3
        self.assertFalse(self.caster._join_channel())
        kind, message = self.caster.states[-1]
        self.assertEqual(kind, "error")
        self.assertIn("没有出现", message)

    def test_permission_denial_is_reported_as_such(self):
        # 缺 MakeTempChannel 时再等也不会有，要说清楚是权限问题
        self.broadcast.CHANNEL_TIMEOUT = 5.0

        def deny():
            time.sleep(0.1)
            self.caster._denial = "没有权限（建立频率频道需要根频道的 MakeTempChannel 权限）"

        threading.Thread(target=deny, daemon=True).start()
        started = time.time()
        self.assertFalse(self.caster._join_channel())
        self.assertLess(time.time() - started, 2.0, "被拒绝之后不该继续等满超时")
        self.assertIn("MakeTempChannel", self.caster.states[-1][1])

    def test_denial_callback_translates_the_type(self):
        self.caster.mumble.denial_type = lambda t: "Permission"
        self.caster._on_permission_denied(type("E", (), {"type": 1, "reason": ""})())
        self.assertIn("MakeTempChannel", self.caster._denial)

    def test_unknown_denial_type_still_reports_something(self):
        self.caster.mumble.denial_type = lambda t: "SomethingNew"
        self.caster._on_permission_denied(type("E", (), {"type": 9, "reason": ""})())
        self.assertIn("SomethingNew", self.caster._denial)

    def test_stopping_aborts_the_wait(self):
        self.broadcast.CHANNEL_TIMEOUT = 30.0
        self.caster.stop_event.set()
        started = time.time()
        self.assertFalse(self.caster._join_channel())
        self.assertLess(time.time() - started, 2.0, "停止时不该继续等")


class WeatherFetchTest(unittest.TestCase):

    def test_normalize_strips_prefix_and_checks_station(self):
        self.assertEqual(weather.normalize("METAR " + ZSPD, "ZSPD"), ZSPD)
        self.assertIsNone(weather.normalize("ZBAA 251300Z", "ZSPD"))

    def test_bad_icao_is_rejected_before_any_request(self):
        with self.assertRaises(weather.WeatherError):
            weather.fetch_metar("ZS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
