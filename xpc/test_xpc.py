"""协议和数据换算的单元测试。

    python -m unittest test_xpc -v

不连服务器、不碰音频、不需要 X-Plane。重点是两头对得上的地方：PBH 的编码
必须能被 can-fsd 原样解回来，RREF 回包必须按 X-Plane 的格式解析。
"""

import os
import struct
import sys
import time
import unittest
from unittest import mock

# pymumble 要本机的 opus 原生库，这些测试碰不到音频，缺库时放个替身。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())

import cslmatch
import fsdpilot
import traffic as traffic_module
import xplane


def unpack_pbh(packed):
    """can-fsd 的解码，抄自 internal/fsd/packet.go 的 PitchBankHeading。

    测试拿它当参照物：我们编出来的东西必须能被服务端原样解回去。
    """
    ratio = 360.0 / 1024.0
    mask = 0x3FF

    def normalise(value):
        return value - 360.0 if value > 180.0 else value

    pitch = normalise((packed >> 22 & mask) * ratio)
    bank = normalise((packed >> 12 & mask) * ratio)
    heading = (packed >> 2 & mask) * ratio
    return pitch, bank, heading


class PbhTest(unittest.TestCase):
    """姿态编码。错了别人会看到飞机以奇怪的角度飞。"""

    # 量化步长是 360/1024 ≈ 0.35°，来回一趟的误差不该超过半格
    TOLERANCE = 360.0 / 1024.0 / 2 + 1e-6

    def assert_round_trip(self, pitch, bank, heading):
        got_pitch, got_bank, got_heading = unpack_pbh(
            fsdpilot.pack_pbh(pitch, bank, heading))
        self.assertAlmostEqual(pitch, got_pitch, delta=self.TOLERANCE)
        self.assertAlmostEqual(bank, got_bank, delta=self.TOLERANCE)
        self.assertAlmostEqual(heading % 360.0, got_heading, delta=self.TOLERANCE)

    def test_level_flight(self):
        self.assert_round_trip(0.0, 0.0, 0.0)

    def test_typical_attitude(self):
        self.assert_round_trip(2.5, -15.0, 271.0)

    def test_negative_pitch_and_bank(self):
        # 下降加左坡度——负角度按 0..360 折回去，不能溢出成别的值
        self.assert_round_trip(-3.5, -30.0, 89.0)

    def test_extremes(self):
        for pitch, bank, heading in ((90.0, 0.0, 0.0), (-90.0, 0.0, 180.0),
                                     (0.0, 179.0, 359.0), (0.0, -179.0, 1.0)):
            with self.subTest(pitch=pitch, bank=bank, heading=heading):
                self.assert_round_trip(pitch, bank, heading)

    def test_heading_wraps(self):
        # 360 和 0 是同一个方向，编出来必须一样
        self.assertEqual(fsdpilot.pack_pbh(0, 0, 360.0),
                         fsdpilot.pack_pbh(0, 0, 0.0))

    def test_fits_in_32_bits(self):
        for heading in range(0, 360, 7):
            packed = fsdpilot.pack_pbh(-45.0, 45.0, heading)
            self.assertGreaterEqual(packed, 0)
            self.assertLess(packed, 2 ** 32)

    def test_on_ground_flag(self):
        air = fsdpilot.pack_pbh(0, 0, 90.0, on_ground=False)
        ground = fsdpilot.pack_pbh(0, 0, 90.0, on_ground=True)
        self.assertEqual(ground & 0x2, 0x2)
        self.assertEqual(air & 0x2, 0)
        # 地面标志不该动到姿态
        self.assertEqual(unpack_pbh(air), unpack_pbh(ground))


class CallsignTest(unittest.TestCase):
    """呼号规则来自 can-fsd 的 IsValidCallsign，客户端先拦一道。"""

    def test_normal_callsign_passes(self):
        self.assertIsNone(fsdpilot.callsign_problem("CCA1501"))

    def test_underscore_allowed(self):
        self.assertIsNone(fsdpilot.callsign_problem("ZSPD_TWR"))

    def test_too_long_rejected(self):
        problem = fsdpilot.callsign_problem("ABCDEFGHIJK")     # 11 个字符
        self.assertIsNotNone(problem)
        self.assertIn("10", problem)

    def test_too_short_rejected(self):
        self.assertIsNotNone(fsdpilot.callsign_problem("A"))

    def test_illegal_character_rejected(self):
        self.assertIsNotNone(fsdpilot.callsign_problem("CCA150#"))

    def test_lowercase_is_normalised(self):
        self.assertIsNone(fsdpilot.callsign_problem("cca1501"))


class SanitizeTest(unittest.TestCase):
    """包是冒号分帧的，正文里的冒号会把包切坏。"""

    def test_colon_replaced(self):
        self.assertEqual(fsdpilot.sanitize("a:b"), "a b")

    def test_newlines_replaced(self):
        self.assertEqual(fsdpilot.sanitize("a\r\nb"), "a  b")

    def test_none_is_empty(self):
        self.assertEqual(fsdpilot.sanitize(None), "")


class PositionPacketTest(unittest.TestCase):
    """位置包的字段顺序必须和 can-fsd 的 handlePilotPosition 对上。"""

    def setUp(self):
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        self.pilot._send = self.sent.append
        self.pilot.update_position({
            "latitude": 31.14340, "longitude": 121.80500,
            "altitude": 35000, "groundspeed": 450,
            "pitch": 2.0, "bank": -5.0, "heading": 271.0,
            "squawk": 2000, "xpdr_mode": 2, "on_ground": False,
        })

    def test_field_layout(self):
        self.pilot._send_position()
        fields = self.sent[0].split(":")
        self.assertEqual(fields[0], "@N")               # 应答机正常
        self.assertEqual(fields[1], "CCA1501")
        self.assertEqual(fields[2], "2000")             # squawk
        self.assertEqual(float(fields[4]), 31.14340)    # 纬度
        self.assertEqual(float(fields[5]), 121.80500)   # 经度
        self.assertEqual(fields[6], "35000")            # 高度
        self.assertEqual(fields[7], "450")              # 地速
        self.assertEqual(len(fields), 10)

    def test_attitude_survives_the_packet(self):
        self.pilot._send_position()
        pitch, bank, heading = unpack_pbh(int(self.sent[0].split(":")[8]))
        self.assertAlmostEqual(pitch, 2.0, delta=0.4)
        self.assertAlmostEqual(bank, -5.0, delta=0.4)
        self.assertAlmostEqual(heading, 271.0, delta=0.4)

    def test_squawk_is_four_digits(self):
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 0, "groundspeed": 0,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 21, "xpdr_mode": 2,
        })
        self.pilot._send_position()
        self.assertEqual(self.sent[0].split(":")[2], "0021")

    def test_standby_transponder(self):
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 0, "groundspeed": 0,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 2000, "xpdr_mode": 1,
        })
        self.pilot._send_position()
        self.assertTrue(self.sent[0].startswith("@S:"))

    def test_ident_changes_the_mode(self):
        self.pilot.ident()
        self.pilot._send_position()
        self.assertTrue(self.sent[0].startswith("@Y:"))

    def test_slows_down_when_parked(self):
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 0, "groundspeed": 0,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 2000, "xpdr_mode": 2,
            "on_ground": True,
        })
        self.assertEqual(self.pilot._send_position(), fsdpilot.SLOW_POSITION_INTERVAL)

    def test_full_rate_in_the_air(self):
        self.assertEqual(self.pilot._send_position(), fsdpilot.POSITION_INTERVAL)

    def test_no_packet_without_position(self):
        pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        pilot._send = self.sent.append
        pilot._send_position()
        self.assertEqual(self.sent, [])


class PasswordLoggingTest(unittest.TestCase):
    """日志会被用户贴出来，密码不能在里面。"""

    def test_login_packet_is_redacted(self):
        packet = "#APCCA1501:SERVER:1234:hunter2:1:100:8:Test Pilot"
        self.assertNotIn("hunter2", fsdpilot.FSDPilot._redact(packet))

    def test_other_packets_untouched(self):
        packet = "@N:CCA1501:2000:1:31.1:121.8:35000:450:0:0"
        self.assertEqual(fsdpilot.FSDPilot._redact(packet), packet)


class PacketHandlingTest(unittest.TestCase):
    def setUp(self):
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        self.pilot._send = lambda packet: True

    def test_error_before_login_stops_the_connection(self):
        self.pilot._logged_in = False
        result = self.pilot._handle_packet("$ERSERVER:CCA1501:6::Invalid CID/password")
        self.assertIs(result, False)

    def test_error_after_login_is_survivable(self):
        self.pilot._logged_in = True
        result = self.pilot._handle_packet("$ERSERVER:CCA1501:6::something")
        self.assertIsNot(result, False)

    def test_text_message_reaches_the_callback(self):
        received = []
        self.pilot.on_text = lambda *args: received.append(args)
        self.pilot._handle_packet("#TMZSPD_TWR:CCA1501:contact ground 121.8")
        self.assertEqual(received, [("ZSPD_TWR", "CCA1501", "contact ground 121.8")])

    def test_message_body_may_contain_colons(self):
        received = []
        self.pilot.on_text = lambda *args: received.append(args)
        self.pilot._handle_packet("#TMZSPD_TWR:CCA1501:climb FL350:expedite")
        self.assertEqual(received[0][2], "climb FL350:expedite")

    def test_controller_position_is_recorded(self):
        self.pilot._handle_packet("%ZSPD_TWR:28500:5:100:1:31.14:121.80:0")
        self.assertIn("ZSPD_TWR", self.pilot.controllers)
        self.assertEqual(self.pilot.controllers["ZSPD_TWR"]["frequency"], "128.500")

    def test_controller_removed_on_disconnect(self):
        self.pilot._handle_packet("%ZSPD_TWR:28500:5:100:1:31.14:121.80:0")
        self.pilot._handle_packet("#DAZSPD_TWR:1234")
        self.assertNotIn("ZSPD_TWR", self.pilot.controllers)

    def test_caps_reply_marks_login_done(self):
        self.pilot._handle_packet("$CRSERVER:CCA1501:CAPS:ATCINFO=1")
        self.assertTrue(self.pilot._logged_in)

    def test_ping_is_answered(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot._handle_packet("$PISERVER:CCA1501:12345")
        self.assertTrue(sent[0].startswith("$POCCA1501:SERVER:"))

    def test_capability_query_is_answered(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot._handle_packet("$CQZSPD_TWR:CCA1501:CAPS")
        self.assertTrue(sent[0].startswith("$CRCCA1501:ZSPD_TWR:CAPS"))

    def test_aircraft_query_is_answered(self):
        sent = []
        self.pilot.aircraft = "B738"
        self.pilot._send = sent.append
        self.pilot._handle_packet("$CQZSPD_TWR:CCA1501:ACC")
        self.assertIn("B738", sent[0])

    def test_query_for_someone_else_is_ignored(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot._handle_packet("$CQZSPD_TWR:CES2345:CAPS")
        self.assertEqual(sent, [])

    def test_unknown_packet_does_not_break_the_loop(self):
        self.assertIsNot(self.pilot._handle_packet("$XXgarbage"), False)


class FlightPlanTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def test_field_count(self):
        self.pilot.file_flight_plan({
            "rules": "I", "aircraft": "B738", "cruise_speed": "450",
            "departure": "ZSPD", "arrival": "ZBAA", "cruise_altitude": "35000",
            "route": "PIKAS A461 SASAN", "remarks": "/v/",
        })
        # $FP呼号 + *A + 13 个字段
        self.assertEqual(len(self.sent[0].split(":")), 15)

    def test_route_colons_do_not_break_the_packet(self):
        self.pilot.file_flight_plan({"route": "A:B", "remarks": "x:y"})
        self.assertEqual(len(self.sent[0].split(":")), 15)


class ChannelNameTest(unittest.TestCase):
    """频率到频道名是全网约定，改了三个客户端一起坏。"""

    def setUp(self):
        # voice 模块要 pyaudio 和 pymumble，这里只测纯函数，装个替身
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors", "numpy"):
            sys.modules.setdefault(name, mock.MagicMock())

    def test_known_frequencies(self):
        import voice
        self.assertEqual(voice.channel_name(125.400), "FREQ_125400")
        self.assertEqual(voice.channel_name(118.000), "FREQ_118000")
        self.assertEqual(voice.channel_name(99.900), "FREQ_099900")

    def test_matches_the_other_clients(self):
        import voice
        for frequency in (118.0, 121.5, 127.85, 132.025):
            expected = f"FREQ_{str(int(round(frequency * 1000))).zfill(6)}"
            self.assertEqual(voice.channel_name(frequency), expected)

    def test_833_spacing(self):
        import voice
        self.assertEqual(voice.channel_name(132.005), "FREQ_132005")


class XPlaneParsingTest(unittest.TestCase):
    """RREF 回包的解析和单位换算。"""

    def setUp(self):
        self.link = xplane.XPlaneLink()

    def _rref(self, pairs):
        packet = b"RREF\x00"
        for index, value in pairs:
            packet += struct.pack("=if", index, value)
        return packet

    def test_parses_a_reply(self):
        index = xplane.NAME_TO_INDEX["latitude"]
        self.assertTrue(self.link._handle(self._rref([(index, 31.1434)])))
        self.assertAlmostEqual(self.link.values["latitude"], 31.1434, places=4)

    def test_parses_several_values_at_once(self):
        pairs = [(xplane.NAME_TO_INDEX["latitude"], 31.0),
                 (xplane.NAME_TO_INDEX["longitude"], 121.0),
                 (xplane.NAME_TO_INDEX["groundspeed"], 100.0)]
        self.assertTrue(self.link._handle(self._rref(pairs)))
        self.assertEqual(len(self.link.values), 3)

    def test_rejects_a_short_packet(self):
        self.assertFalse(self.link._handle(b"RREF\x00short"))

    def test_rejects_a_foreign_packet(self):
        self.assertFalse(self.link._handle(b"DATA\x00" + b"\x00" * 32))

    def test_unknown_index_is_ignored(self):
        self.assertFalse(self.link._handle(self._rref([(999, 1.0)])))

    def test_indices_are_unique(self):
        self.assertEqual(len(xplane.NAME_TO_INDEX), len(xplane.DATAREFS))
        self.assertEqual(len(set(xplane.INDEX_TO_NAME)), len(xplane.DATAREFS))


class WaitingTest(unittest.TestCase):
    """X-Plane 没起来的时候不该一秒重订一次。

    Windows 上往没人监听的端口发 UDP 会回 ICMP 不可达，下一次 recvfrom 抛
    ConnectionResetError。第一版按 OSError 处理直接重来，日志里就是每秒一条
    "已订阅 14 个 dataref"。
    """

    def setUp(self):
        self.link = xplane.XPlaneLink()
        self.link.address = ("127.0.0.1", 49000)

    def test_keeps_waiting_at_first(self):
        self.assertTrue(self.link._still_waiting(time.time()))

    def test_reports_disconnected_once_stale(self):
        states = []
        self.link.on_state = lambda connected, message: states.append(connected)
        self.link._connected = True
        self.link._still_waiting(time.time() - xplane.STALE_AFTER - 1)
        self.assertEqual(states, [False])

    def test_still_waiting_while_stale_but_not_hopeless(self):
        self.assertTrue(self.link._still_waiting(time.time() - xplane.STALE_AFTER - 1))
        self.assertIsNotNone(self.link.address, "还不到重新发现的时候")

    def test_rediscovers_after_a_long_silence(self):
        self.assertFalse(
            self.link._still_waiting(time.time() - xplane.REDISCOVER_AFTER - 1))
        self.assertIsNone(self.link.address, "应当清掉地址重新发现")

    def test_rediscover_is_slower_than_stale(self):
        self.assertGreater(xplane.REDISCOVER_AFTER, xplane.STALE_AFTER)


class SnapshotTest(unittest.TestCase):
    """换算：X-Plane 用公制，FSD 要英尺和节。"""

    def setUp(self):
        self.link = xplane.XPlaneLink()
        self.link.values = {
            "latitude": 31.1434, "longitude": 121.805,
            "elevation": 10668.0,          # 米 = 35000 英尺
            "agl": 3048.0,                 # 米 = 10000 英尺
            "groundspeed": 231.5,          # 米每秒 ≈ 450 节
            "pitch": 2.0, "bank": -5.0, "heading_true": 271.0,
            "squawk": 2000.0, "xpdr_mode": 2.0,
            "com1": 121500.0, "com2": 118000.0,
            "com1_power": 1.0, "on_ground": 0.0,
        }

    def test_metres_to_feet(self):
        self.assertEqual(self.link.snapshot()["altitude"], 35000)

    def test_agl_in_feet(self):
        self.assertEqual(self.link.snapshot()["agl"], 10000)

    def test_metres_per_second_to_knots(self):
        self.assertEqual(self.link.snapshot()["groundspeed"], 450)

    def test_frequency_in_megahertz(self):
        self.assertEqual(self.link.snapshot()["com1"], 121.5)

    def test_833_frequency(self):
        self.link.values["com1"] = 132005.0
        self.assertEqual(self.link.snapshot()["com1"], 132.005)

    def test_zero_frequency_is_none(self):
        self.link.values["com1"] = 0.0
        self.assertIsNone(self.link.snapshot()["com1"])

    def test_heading_is_wrapped(self):
        self.link.values["heading_true"] = 370.0
        self.assertAlmostEqual(self.link.snapshot()["heading"], 10.0, places=3)

    def test_squawk_is_an_integer(self):
        self.link.values["squawk"] = 2000.9
        self.assertIsInstance(self.link.snapshot()["squawk"], int)

    def test_no_values_means_no_snapshot(self):
        self.assertIsNone(xplane.XPlaneLink().snapshot())

    def test_stale_data_is_not_connected(self):
        self.link._connected = True
        self.link.last_update = 0        # 很久以前
        self.assertFalse(self.link.connected)


class UnpackPbhTest(unittest.TestCase):
    """还原别人的姿态。判定标准仍然是 can-fsd 那份转写，不是我们自己的编码。"""

    def test_matches_the_reference_decoder(self):
        for packed in (0, 1, 0xFFFFFFFF, 0x12345678, 0xABCDEF01):
            with self.subTest(packed=packed):
                expected = unpack_pbh(packed)
                got = fsdpilot.unpack_pbh(packed)
                self.assertAlmostEqual(got["pitch"], expected[0], places=6)
                self.assertAlmostEqual(got["bank"], expected[1], places=6)
                self.assertAlmostEqual(got["heading"], expected[2], places=6)

    def test_round_trips_our_own_encoding(self):
        packed = fsdpilot.pack_pbh(-3.0, 12.0, 271.0, on_ground=True)
        got = fsdpilot.unpack_pbh(packed)
        self.assertAlmostEqual(got["pitch"], -3.0, delta=0.4)
        self.assertAlmostEqual(got["bank"], 12.0, delta=0.4)
        self.assertAlmostEqual(got["heading"], 271.0, delta=0.4)
        self.assertTrue(got["on_ground"])


class TrafficReceptionTest(unittest.TestCase):
    """从 FSD 收他机。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw",
                                       aircraft="B738", traffic=self.table)
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def _position(self, callsign="CES2345", lat=31.2, lon=121.5):
        pbh = fsdpilot.pack_pbh(2.0, -5.0, 271.0)
        return f"@N:{callsign}:2000:1:{lat}:{lon}:35000:450:{pbh}:0"

    def test_other_aircraft_is_recorded(self):
        self.pilot._handle_packet(self._position())
        self.assertIn("CES2345", self.table)

    def test_attitude_is_decoded(self):
        self.pilot._handle_packet(self._position())
        position = self.table.get("CES2345").position_at(time.time())
        self.assertAlmostEqual(position["heading"], 271.0, delta=0.4)
        self.assertAlmostEqual(position["bank"], -5.0, delta=0.4)

    def test_our_own_echo_is_ignored(self):
        self.pilot._handle_packet(self._position(callsign="CCA1501"))
        self.assertEqual(len(self.table), 0)

    def test_plane_info_is_requested_on_first_sight(self):
        self.pilot._handle_packet(self._position())
        self.assertIn("#SBCCA1501:CES2345:PIR", self.sent)

    def test_plane_info_is_not_requested_every_packet(self):
        for _ in range(5):
            self.pilot._handle_packet(self._position())
        self.assertEqual(sum(1 for p in self.sent if p.endswith(":PIR")), 1)

    def test_disconnect_removes_the_aircraft(self):
        self.pilot._handle_packet(self._position())
        self.pilot._handle_packet("#DPCES2345:1234")
        self.assertNotIn("CES2345", self.table)

    def test_malformed_position_does_not_raise(self):
        self.pilot._handle_packet("@N:CES2345:2000:1:notanumber:121.5:35000:450:0:0")
        self.assertEqual(len(self.table), 0)

    def test_works_without_a_traffic_table(self):
        pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        pilot._send = lambda packet: True
        self.assertIsNot(pilot._handle_packet(self._position()), False)


class PlaneInfoExchangeTest(unittest.TestCase):
    """#SB 机型交换。can-fsd 的 handleSquawkbox 原样转发，服务端不用改。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw",
                                       aircraft="A320", traffic=self.table)
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def test_we_answer_a_request(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PIR")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("EQUIPMENT=A320", self.sent[0])

    def test_our_answer_carries_the_airline(self):
        # 航司码取呼号前三位字母，别人才能挑到正确涂装
        self.pilot._handle_packet("#SBCES2345:CCA1501:PIR")
        self.assertIn("AIRLINE=CCA", self.sent[0])

    def test_numeric_callsign_has_no_airline(self):
        pilot = fsdpilot.FSDPilot("example.invalid", "N172SP", "1", "pw")
        self.assertEqual(pilot.airline, "")

    def test_we_record_what_they_answer(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738:AIRLINE=CES")
        aircraft = self.table.get("CES2345")
        self.assertEqual(aircraft.equipment, "B738")
        self.assertEqual(aircraft.airline, "CES")

    def test_key_order_does_not_matter(self):
        # protocol.md 明说顺序不保证
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:AIRLINE=CES:EQUIPMENT=B738")
        self.assertEqual(self.table.get("CES2345").equipment, "B738")

    def test_missing_keys_are_tolerated(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738")
        self.assertEqual(self.table.get("CES2345").airline, "")

    def test_unknown_keys_are_ignored(self):
        self.pilot._handle_packet(
            "#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738:SOMETHING=X")
        self.assertEqual(self.table.get("CES2345").equipment, "B738")

    def test_legacy_csl_form(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:X:0:1:CSL=A320_DAL")
        self.assertEqual(self.table.get("CES2345").csl, "A320_DAL")

    def test_legacy_tilde_form(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:X:0:0:~PA24")
        self.assertEqual(self.table.get("CES2345").csl, "PA24")

    def test_info_before_position_is_kept(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738")
        self.assertIn("CES2345", self.table)


class InterpolationTest(unittest.TestCase):
    """FSD 一秒才 5 个包，不插值飞机会一跳一跳。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def _add(self, at, lat, lon, altitude=10000, heading=90.0):
        self.table.update_position("CES2345", latitude=lat, longitude=lon,
                                   altitude=altitude, pitch=0.0, bank=0.0,
                                   heading=heading, groundspeed=250, now=at)

    def test_midpoint(self):
        self._add(100.0, 30.0, 120.0)
        self._add(101.0, 30.1, 120.2)
        position = self.table.get("CES2345").position_at(100.5)
        self.assertAlmostEqual(position["latitude"], 30.05, places=6)
        self.assertAlmostEqual(position["longitude"], 120.1, places=6)

    def test_altitude_interpolates(self):
        self._add(100.0, 30.0, 120.0, altitude=10000)
        self._add(101.0, 30.0, 120.0, altitude=11000)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.5)["altitude"], 10500, places=3)

    def test_heading_takes_the_short_way(self):
        # 359° 到 1° 应当往前走 2°，不是倒着走 358°
        self._add(100.0, 30.0, 120.0, heading=359.0)
        self._add(101.0, 30.0, 120.0, heading=1.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.5)["heading"], 0.0, places=6)

    def test_heading_short_way_downwards(self):
        self._add(100.0, 30.0, 120.0, heading=10.0)
        self._add(101.0, 30.0, 120.0, heading=350.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.5)["heading"], 0.0, places=6)

    def test_single_sample_is_held(self):
        self._add(100.0, 30.0, 120.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(105.0)["latitude"], 30.0)

    def test_extrapolation_is_bounded(self):
        # 对方掉线时飞机该停在原地，不是一直飞出天际
        self._add(100.0, 30.0, 120.0)
        self._add(101.0, 30.1, 120.0)
        far = self.table.get("CES2345").position_at(200.0)["latitude"]
        self.assertLess(far, 30.5, "外推没有封顶")

    def test_no_backward_extrapolation(self):
        self._add(100.0, 30.0, 120.0)
        self._add(101.0, 30.1, 120.0)
        early = self.table.get("CES2345").position_at(50.0)["latitude"]
        self.assertAlmostEqual(early, 30.0, places=6)

    def test_duplicate_timestamp_is_dropped(self):
        # 同一时刻的重复包会让插值除零
        self._add(100.0, 30.0, 120.0)
        self._add(100.0, 40.0, 130.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.0)["latitude"], 30.0)

    def test_vertical_speed(self):
        self._add(100.0, 30.0, 120.0, altitude=10000)
        self._add(101.0, 30.0, 120.0, altitude=10010)
        self.assertAlmostEqual(self.table.get("CES2345").vertical_speed, 600.0, places=3)


class TrafficTableTest(unittest.TestCase):
    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def _add(self, callsign, lat=30.0, lon=120.0, at=1000.0):
        self.table.update_position(callsign, latitude=lat, longitude=lon,
                                   altitude=10000, pitch=0.0, bank=0.0,
                                   heading=90.0, groundspeed=250, now=at)

    def test_prune_removes_stale(self):
        self._add("CES2345", at=1000.0)
        self.assertEqual(self.table.prune(now=1000.0 + traffic_module.STALE_AFTER + 1),
                         ["CES2345"])
        self.assertEqual(len(self.table), 0)

    def test_prune_keeps_fresh(self):
        self._add("CES2345", at=1000.0)
        self.assertEqual(self.table.prune(now=1001.0), [])

    def test_snapshot_sorted_by_range(self):
        self._add("FAR", lat=32.0)
        self._add("NEAR", lat=30.1)
        entries = self.table.snapshot(now=1000.0, origin=(30.0, 120.0))
        self.assertEqual([e["callsign"] for e in entries], ["NEAR", "FAR"])

    def test_snapshot_limit_keeps_the_closest(self):
        # TCAS 只有 64 个位置，超了必须先扔远的
        for i in range(5):
            self._add(f"AC{i}", lat=30.0 + i * 0.5)
        entries = self.table.snapshot(now=1000.0, origin=(30.0, 120.0), limit=2)
        self.assertEqual([e["callsign"] for e in entries], ["AC0", "AC1"])

    def test_snapshot_range_filter(self):
        self._add("NEAR", lat=30.05)
        self._add("FAR", lat=35.0)
        entries = self.table.snapshot(now=1000.0, origin=(30.0, 120.0),
                                      max_range_nm=50)
        self.assertEqual([e["callsign"] for e in entries], ["NEAR"])

    def test_snapshot_without_origin_has_no_range(self):
        self._add("CES2345")
        self.assertNotIn("range_nm", self.table.snapshot(now=1000.0)[0])

    def test_model_dirty_starts_true(self):
        self._add("CES2345")
        self.assertTrue(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_mark_model_clean(self):
        self._add("CES2345")
        self.table.mark_model_clean("CES2345")
        self.assertFalse(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_new_plane_info_makes_it_dirty_again(self):
        self._add("CES2345")
        self.table.mark_model_clean("CES2345")
        self.table.set_plane_info("CES2345", equipment="B738")
        self.assertTrue(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_same_plane_info_does_not_redirty(self):
        self._add("CES2345")
        self.table.set_plane_info("CES2345", equipment="B738")
        self.table.mark_model_clean("CES2345")
        self.table.set_plane_info("CES2345", equipment="B738")
        self.assertFalse(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_config_drives_animation(self):
        self._add("CES2345")
        self.table.set_config("CES2345", {
            "gear_down": True, "flaps_pct": 40, "spoilers_out": False,
            "lights": {"strobe_on": True},
            "engines": {"1": {"on": True}, "2": {"on": False}}})
        entry = self.table.snapshot(now=1000.0)[0]
        self.assertTrue(entry["gear_down"])
        self.assertAlmostEqual(entry["flaps"], 0.4)
        self.assertTrue(entry["lights"]["strobe_on"])
        self.assertTrue(entry["engines_on"])

    def test_config_for_unknown_aircraft_is_ignored(self):
        self.assertIsNone(self.table.set_config("NOBODY", {"gear_down": True}))

    def test_request_callback_fires_once(self):
        asked = []
        table = traffic_module.TrafficTable(on_request_info=asked.append)
        for _ in range(3):
            table.update_position("CES2345", latitude=30.0, longitude=120.0,
                                  altitude=10000, pitch=0, bank=0, heading=0,
                                  now=1000.0)
        self.assertEqual(asked, ["CES2345"])

    def test_request_callback_not_fired_once_known(self):
        asked = []
        table = traffic_module.TrafficTable(on_request_info=asked.append)
        table.set_plane_info("CES2345", equipment="B738")
        table.update_position("CES2345", latitude=30.0, longitude=120.0,
                              altitude=10000, pitch=0, bank=0, heading=0, now=1000.0)
        self.assertEqual(asked, [])

    def test_distance(self):
        # 1 度纬度 = 60 海里
        self.assertAlmostEqual(traffic_module.distance_nm(30.0, 120.0, 31.0, 120.0),
                               60.0, places=3)


class CslParsingTest(unittest.TestCase):
    """xsb_aircraft.txt 各家写得并不一致，读的时候要宽松。"""

    def setUp(self):
        import tempfile
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write(self, text):
        path = os.path.join(self.directory, "xsb_aircraft.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return self.directory

    def test_reads_a_simple_package(self):
        models = cslmatch.parse_package(self._write(
            "EXPORT_NAME BB_Airbus\n"
            "OBJ8_AIRCRAFT A320_CCA\n"
            "OBJ8 SOLID YES A320/A320_CCA.obj\n"
            "ICAO A320\n"
            "AIRLINE A320 CCA\n"))
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].icao, "A320")
        self.assertEqual(models[0].airline, "CCA")
        self.assertEqual(models[0].package, "BB_Airbus")

    def test_backslash_paths(self):
        models = cslmatch.parse_package(self._write(
            "OBJ8_AIRCRAFT X\nOBJ8 SOLID YES A320\\A320.obj\nICAO A320\n"))
        self.assertTrue(models[0].path.endswith("A320.obj"))

    def test_comments_are_skipped(self):
        models = cslmatch.parse_package(self._write(
            "# 注释\nOBJ8_AIRCRAFT X   # 行尾注释\n"
            "OBJ8 SOLID YES a.obj\nICAO B738\n"))
        self.assertEqual(models[0].icao, "B738")

    def test_entries_without_a_path_are_dropped(self):
        models = cslmatch.parse_package(self._write(
            "OBJ8_AIRCRAFT Broken\nICAO B738\n"
            "OBJ8_AIRCRAFT Good\nOBJ8 SOLID YES a.obj\nICAO A320\n"))
        self.assertEqual([m.icao for m in models], ["A320"])

    def test_missing_manifest_is_not_an_error(self):
        import tempfile
        self.assertEqual(cslmatch.parse_package(tempfile.mkdtemp()), [])

    def test_find_packages(self):
        import tempfile
        root = tempfile.mkdtemp()
        inner = os.path.join(root, "BB_Airbus")
        os.makedirs(inner)
        with open(os.path.join(inner, "xsb_aircraft.txt"), "w") as f:
            f.write("OBJ8_AIRCRAFT X\n")
        self.assertEqual(cslmatch.find_packages(root), [inner])


class ModelMatchingTest(unittest.TestCase):
    """匹配的退化链。最重要的一条：永远要有结果。"""

    def setUp(self):
        self.models = cslmatch.ModelSet([
            cslmatch.Model("B738_CCA", "b738_cca.obj", icao="B738", airline="CCA"),
            cslmatch.Model("B738_CES", "b738_ces.obj", icao="B738", airline="CES"),
            cslmatch.Model("B739_CCA", "b739_cca.obj", icao="B739", airline="CCA"),
            cslmatch.Model("A320_GEN", "a320.obj", icao="A320"),
            cslmatch.Model("C172_GEN", "c172.obj", icao="C172"),
        ])

    def test_exact_type_and_airline(self):
        model, why = self.models.match(equipment="B738", airline="CES")
        self.assertEqual(model.name, "B738_CES")
        self.assertIn("都匹配", why)

    def test_type_only_when_airline_unknown(self):
        model, _ = self.models.match(equipment="B738")
        self.assertEqual(model.icao, "B738")

    def test_type_matches_even_with_unknown_airline(self):
        model, why = self.models.match(equipment="B738", airline="UAL")
        self.assertEqual(model.icao, "B738")
        self.assertIn("涂装不对", why)

    def test_family_fallback_prefers_right_airline(self):
        # 没有 B737 的模型，同族里有 B738_CCA 和 B739_CCA
        model, why = self.models.match(equipment="B737", airline="CCA")
        self.assertEqual(model.airline, "CCA")
        self.assertIn("同族", why)

    def test_family_fallback_without_airline(self):
        model, why = self.models.match(equipment="B734")
        self.assertIn(model.icao, ("B738", "B739"))
        self.assertIn("同族", why)

    def test_generic_fallback_by_prefix(self):
        # A350 不在包里也不在同族表里，B7/A3 前缀退到通用
        model, why = self.models.match(equipment="A359")
        self.assertEqual(model.icao, "A320")
        self.assertIn("通用", why)

    def test_light_aircraft_generic(self):
        model, _ = self.models.match(equipment="P28A")
        self.assertEqual(model.icao, "C172")

    def test_unknown_type_still_returns_something(self):
        # 看不见的飞机比涂装错的飞机危险得多
        model, why = self.models.match(equipment="ZZZZ")
        self.assertIsNotNone(model, why)

    def test_no_information_at_all_still_returns_something(self):
        model, _ = self.models.match()
        self.assertIsNotNone(model)

    def test_explicit_csl_name_wins(self):
        model, why = self.models.match(equipment="B738", airline="CCA", csl="A320_GEN")
        self.assertEqual(model.name, "A320_GEN")
        self.assertIn("CSL 名字", why)

    def test_empty_model_set_reports_why(self):
        model, why = cslmatch.ModelSet().match(equipment="B738")
        self.assertIsNone(model)
        self.assertIn("没有装", why)

    def test_lowercase_input_is_handled(self):
        model, _ = self.models.match(equipment="b738", airline="ces")
        self.assertEqual(model.name, "B738_CES")

    def test_family_lookup(self):
        self.assertIn("B739", cslmatch.family_of("B738"))
        self.assertEqual(cslmatch.family_of("ZZZZ"), ())


class BridgeTest(unittest.TestCase):
    """客户端和插件之间的分片协议。两边各有一份重组器，必须对称。"""

    def setUp(self):
        import bridge
        self.bridge = bridge
        self.reassembler = bridge.Reassembler()

    def _round_trip(self, message, max_payload=None, sequence=1):
        packets = (self.bridge.encode(message, sequence, max_payload)
                   if max_payload else self.bridge.encode(message, sequence))
        result = None
        for packet in packets:
            result = self.reassembler.feed(packet) or result
        return result, packets

    def test_small_message_is_one_packet(self):
        result, packets = self._round_trip({"type": "traffic", "aircraft": []})
        self.assertEqual(len(packets), 1)
        self.assertEqual(result["type"], "traffic")

    def test_large_message_is_split_and_rejoined(self):
        message = {"type": "traffic",
                   "aircraft": [{"callsign": f"AC{i:04d}", "latitude": 30.0 + i}
                                for i in range(200)]}
        result, packets = self._round_trip(message, max_payload=500)
        self.assertGreater(len(packets), 1, "应当分片")
        self.assertEqual(result, message)

    def test_partial_message_yields_nothing(self):
        message = {"a": "x" * 2000}
        packets = self.bridge.encode(message, 1, max_payload=100)
        self.assertIsNone(self.reassembler.feed(packets[0]))

    def test_new_frame_discards_the_old_incomplete_one(self):
        # 位置流里迟到的帧没价值，留着会让飞机往回跳
        old = self.bridge.encode({"a": "x" * 2000}, 1, max_payload=100)
        self.reassembler.feed(old[0])
        result, _ = self._round_trip({"type": "traffic"}, sequence=2)
        self.assertEqual(result["type"], "traffic")

    def test_garbage_is_ignored(self):
        self.assertIsNone(self.reassembler.feed(b"not json"))

    def test_wrong_version_is_ignored(self):
        self.assertIsNone(self.reassembler.feed(b'{"v":999,"seq":1,"part":0}'))

    def test_chinese_survives(self):
        result, _ = self._round_trip({"note": "国航一五零一"})
        self.assertEqual(result["note"], "国航一五零一")

    def test_plugin_reassembler_matches_the_client_one(self):
        """插件里那份重组器是独立的一份代码，必须和这边行为一致。"""
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "plugin", "PI_XpcTraffic.py")
        spec = importlib.util.spec_from_file_location("pi_xpc", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        message = {"type": "traffic",
                   "aircraft": [{"callsign": f"AC{i}"} for i in range(150)]}
        plugin_side = module.Reassembler()
        result = None
        for packet in self.bridge.encode(message, 7, max_payload=400):
            result = plugin_side.feed(packet) or result
        self.assertEqual(result, message)

    def test_sender_does_not_raise_without_a_plugin(self):
        # 插件没开是常态，不该报错
        sender = self.bridge.BridgeSender()
        try:
            sender.send_traffic([])
        finally:
            sender.close()


class AnimationValuesTest(unittest.TestCase):
    """插件里 data 列表的顺序必须和 dataref 声明顺序一致，错了动画会串。"""

    def setUp(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "plugin", "PI_XpcTraffic.py")
        spec = importlib.util.spec_from_file_location("pi_xpc2", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.values = self.module.PythonInterface._animation_values

    def test_length_matches_the_dataref_list(self):
        self.assertEqual(len(self.values({})),
                         len(self.module.ANIMATION_DATAREFS))

    def test_gear_down_on_the_ground(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/gear_ratio")
        self.assertEqual(self.values({"on_ground": True})[index], 1.0)

    def test_gear_up_when_fast_and_airborne(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/gear_ratio")
        self.assertEqual(
            self.values({"on_ground": False, "groundspeed": 300})[index], 0.0)

    def test_reported_gear_overrides_the_guess(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/gear_ratio")
        entry = {"on_ground": False, "groundspeed": 300, "gear_down": True}
        self.assertEqual(self.values(entry)[index], 1.0)

    def test_flaps_pass_through(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/flap_ratio")
        self.assertAlmostEqual(self.values({"flaps": 0.4})[index], 0.4)

    def test_strobe_light(self):
        index = self.module.ANIMATION_DATAREFS.index(
            "libxplanemp/controls/strobe_lites_on")
        self.assertEqual(self.values({"lights": {"strobe_on": True}})[index], 1.0)

    def test_engines_off_means_no_thrust(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/thrust_ratio")
        self.assertEqual(self.values({"engines_on": False})[index], 0.0)

    def test_fixed_string_is_padded_and_terminated(self):
        raw = self.module.PythonInterface._fixed_string("CCA1501", 8)
        self.assertEqual(len(raw), 8)
        self.assertTrue(raw.endswith(b"\x00"))

    def test_fixed_string_truncates(self):
        raw = self.module.PythonInterface._fixed_string("VERYLONGCALLSIGN", 8)
        self.assertEqual(len(raw), 8)
        self.assertTrue(raw.endswith(b"\x00"))

    def test_tcas_cap_leaves_room_for_own_aircraft(self):
        # 数组是 64 个位置，0 号给本机
        self.assertEqual(self.module.MAX_TCAS_TARGETS, 63)


if __name__ == "__main__":
    unittest.main(verbosity=2)
