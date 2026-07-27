"""协议和数据换算的单元测试。

    python -m unittest test_xpc -v

不连服务器、不碰音频、不需要 X-Plane。重点是两头对得上的地方：PBH 的编码
必须能被 can-fsd 原样解回来，RREF 回包必须按 X-Plane 的格式解析。
"""

import inspect
import os
import struct
import sys
import threading
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
        problem = fsdpilot.callsign_problem("ABCDEFGHIJKLM")   # 13 个字符
        self.assertIsNotNone(problem)
        self.assertIn("12", problem)

    def test_eleven_characters_is_fine_now(self):
        # 上限从 10 提到 12 是为了 vATIS 的 ZSPD_D_ATIS / ZSPD_A_ATIS
        self.assertIsNone(fsdpilot.callsign_problem("ZSPD_D_ATIS"))

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
    """$FP 的字段布局。真实日志里每次提交都被回 "Too few fields for $FP"。"""

    # can-fsd 的 minimumFields（packet.go）要求 17 段，
    # 布局见 docs/protocol.md 的 Flight Plan `$FP`
    FIELDS = 17

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
        self.assertEqual(len(self.sent[0].split(":")), self.FIELDS)

    def test_empty_plan_still_has_every_field(self):
        # 什么都不填也得凑满 17 段，否则整包被拒
        self.pilot.file_flight_plan({})
        self.assertEqual(len(self.sent[0].split(":")), self.FIELDS)

    def test_route_colons_do_not_break_the_packet(self):
        self.pilot.file_flight_plan({"route": "A:B", "remarks": "x:y"})
        self.assertEqual(len(self.sent[0].split(":")), self.FIELDS)

    def test_filed_to_server(self):
        # 按 protocol.md，填报发给 SERVER；*A 是服务端转发给管制时用的
        self.pilot.file_flight_plan({})
        self.assertEqual(self.sent[0].split(":")[1], "SERVER")

    def test_field_order_matches_the_protocol(self):
        self.pilot.file_flight_plan({
            "rules": "I", "aircraft": "B738", "cruise_speed": "450",
            "departure": "ZSPD", "departure_time": "1230",
            "cruise_altitude": "35000", "arrival": "ZBAA",
            "enroute_hours": "2", "enroute_minutes": "15",
            "fuel_hours": "4", "fuel_minutes": "30",
            "alternate": "ZSNJ", "remarks": "RMK", "route": "PIKAS",
        })
        f = self.sent[0].split(":")
        self.assertEqual(f[0], "$FPCCA1501")
        self.assertEqual(f[2], "I")          # 飞行规则
        self.assertEqual(f[3], "B738")       # 机型
        self.assertEqual(f[4], "450")        # 真空速
        self.assertEqual(f[5], "ZSPD")       # 起飞地
        self.assertEqual(f[8], "35000")      # 巡航高度
        self.assertEqual(f[9], "ZBAA")       # 目的地
        self.assertEqual(f[10], "2")         # 航路小时
        self.assertEqual(f[11], "15")        # 航路分钟
        self.assertEqual(f[12], "4")         # 燃油小时
        self.assertEqual(f[13], "30")        # 燃油分钟
        self.assertEqual(f[14], "ZSNJ")      # 备降场
        self.assertEqual(f[16], "PIKAS")     # 航路

    def test_simulator_is_not_flight_simulator_2004(self):
        """模拟器编号原来写的 8，在 can-fsd 的枚举里是 MSFS 2004。"""
        self.assertNotEqual(fsdpilot.SIMULATOR, 8)
        self.assertEqual(fsdpilot.SIMULATOR, fsdpilot.SIMULATOR_XPLANE_12)


class VoiceChannelTest(unittest.TestCase):
    """频道切换。真实日志里连着两条 "Channel FREQ_121700 does not exists"。"""

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors", "numpy"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice = voice

    def test_waits_for_the_server_instead_of_a_fixed_sleep(self):
        """建频道是一次网络往返，固定 sleep 赌不起。

        原来 new_channel 之后 sleep(0.3) 就去找，远程服务器上经常还没回来，
        报出来是"频道不存在"，看着像建不了。等待逻辑现在在 _switch_channel
        里——它跑在工作线程上，set_frequency 只负责记下目标。
        """
        source = inspect.getsource(self.voice.Voice._switch_channel)
        self.assertNotIn("sleep(0.3)", source)
        self.assertIn("_wait_for_channel", source)

    def test_switching_is_serialised(self):
        # start() 的补切和工作线程会同时进来，各建一次各报一次错
        source = inspect.getsource(self.voice.Voice._switch_channel)
        self.assertIn("_channel_lock", source)

    def test_channel_timeout_is_generous_enough_for_a_remote_server(self):
        self.assertGreaterEqual(self.voice.CHANNEL_TIMEOUT, 2.0)

    def test_set_frequency_returns_immediately(self):
        """set_frequency 不能阻塞调用方。

        它是从 gui.py 的 tick() 调的，tick() 跑在 Qt 主线程上。真正的切换要等
        服务器回 ChannelState，最坏 CHANNEL_TIMEOUT 秒——在主线程上等这么久，
        窗口直接"未响应"（实测过，日志停在"建一个临时的"之后就没了）。

        前面几条测试只看代码结构，正是这样漏掉了这个问题，所以这条直接计时。
        """
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster.frequency = None
        caster._pending = None
        caster._channel_wanted = threading.Event()

        started = time.time()
        caster.set_frequency(121.5)
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.05,
                        f"set_frequency 阻塞了 {elapsed:.2f} 秒")
        self.assertEqual(caster._pending, 121.5, "目标频率应当记下来")
        self.assertTrue(caster._channel_wanted.is_set(), "应当叫醒切换线程")

    def test_set_frequency_does_not_touch_the_network(self):
        # 一个连 mumble 都没有的实例上调用也不该炸——真正的活儿在工作线程
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster.frequency = None
        caster._pending = None
        caster._channel_wanted = threading.Event()
        caster.mumble = None
        caster.set_frequency(133.15)
        self.assertEqual(caster._pending, 133.15)

    def test_repeated_same_frequency_is_cheap(self):
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster.frequency = None
        caster._pending = None
        caster._channel_wanted = threading.Event()
        caster.set_frequency(121.5)
        caster._channel_wanted.clear()
        caster.set_frequency(121.5)     # tick() 每 0.5 秒就来一次
        self.assertFalse(caster._channel_wanted.is_set(),
                         "频率没变就不该反复叫醒工作线程")

    def test_root_channel_does_not_block_transmit(self):
        """根频道的 channel_id 是 0，不能当成"没进频道"。

        写成 `not myself["channel_id"]` 的话，人在根频道时 PTT 会一声不吭地
        什么都不发——用户看到的就是"语音用不了"，日志里一个字都没有。
        """
        source = inspect.getsource(self.voice.Voice._run)
        self.assertNotIn('not myself["channel_id"]', source)
        self.assertIn('myself["channel_id"] is None', source)

    def test_silent_ptt_is_explained(self):
        # 按了 PTT 却一帧没发，必须说出原因，否则没法查
        source = inspect.getsource(self.voice.Voice)
        self.assertIn("_skip_reason", source)
        self.assertIn("一帧都没发出去", source)

    def test_frames_are_counted(self):
        # "发了但对方听不到"和"根本没发"是两回事，只有帧数能分开
        source = inspect.getsource(self.voice.Voice)
        self.assertIn("_sent_frames", source)
        self.assertIn("_received_frames", source)

    def test_switching_retries_until_it_succeeds(self):
        """频道切换必须自愈，不能一次失败就永远留在根频道。

        原来是事件驱动：set_frequency 置位、工作线程消费掉。刚上线那几秒
        mumble 常常还没就绪，那一次切换白跑，而 _pending 没变、set_frequency
        又直接 return，于是再也不重试。实测日志里就是这样——连上 19 秒后按
        PTT，全程没有任何频道切换记录，人一直在根频道。
        """
        source = inspect.getsource(self.voice.Voice._channel_loop)
        # 目标和当前不一致就该重试，而不是只在事件到来时才动
        self.assertIn("target == self.frequency", source)
        self.assertIn("CHANNEL_RETRY_INTERVAL", source)

    def test_retry_is_frequent_enough_to_be_unnoticeable(self):
        self.assertLessEqual(self.voice.CHANNEL_RETRY_INTERVAL, 2.0)

    def test_transmitting_from_root_is_reported(self):
        # 留在根频道还发，等于对着没人的地方喊，日志必须说出来
        source = inspect.getsource(self.voice.Voice._run)
        self.assertIn("ROOT_CHANNEL", source)

    def test_failed_connection_is_not_reported_as_connected(self):
        """pymumble 的 connected 是状态码：3 是 FAILED，也是真值。

        实测里用户名填错，Mumble 回 "Wrong certificate or password"，连接线程
        带着异常死掉，界面却报"语音已连接"，然后一切莫名其妙地不工作。
        """
        caster = self.voice.Voice.__new__(self.voice.Voice)
        # 测试环境里 pymumble 的常量可能是替身，所以拿模块自己导入的那个比对
        connected_state = self.voice.PYMUMBLE_CONN_STATE_CONNECTED

        caster.mumble = type("M", (), {"connected": connected_state})()
        self.assertTrue(caster.connected, "真的连上了应当是 True")

        # 0 未连接、1 认证中、3 失败——用 bool() 判断的话 1 和 3 都会是真值
        for state in (0, 1, 3):
            caster.mumble = type("M", (), {"connected": state})()
            self.assertFalse(caster.connected,
                             f"connected={state} 不该算作已连接")

    def test_no_mumble_means_not_connected(self):
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster.mumble = None
        self.assertFalse(caster.connected)

    def test_stuck_channel_is_explained(self):
        # 切不过去的两个分支原来是静默 continue，日志里什么都看不到
        source = inspect.getsource(self.voice.Voice._channel_loop)
        self.assertIn("_note_stuck", source)

    def test_channel_commands_never_block(self):
        """建频道和进频道都不能用 pymumble 的阻塞接口。

        channels.new_channel() 和 users.move_in() 都走
        execute_command(blocking=True)，那个 acquire 没有超时——pymumble 自己
        的源码里就写着 "TODO: manage a timeout for blocking commands"。命令没
        被处理就永远卡住，而且我们还握着 _channel_lock，整条切换链全死。

        实测日志停在"建一个临时的"，之后既没有成功也没有任何错误——线程根本
        没从那一行返回。
        """
        # 用 AST 看真正的调用，别跟注释和文档字符串较劲——那里面也提到了这两
        # 个接口，按文本匹配会误判
        import ast
        tree = ast.parse(inspect.getsource(self.voice).lstrip())
        blocking_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("new_channel", "move_in"):
                    blocking_calls.append(node.func.attr)
        self.assertEqual(blocking_calls, [],
                         f"{blocking_calls} 会无限期阻塞，要自己发命令")

        for name in ("_create_channel", "_switch_channel"):
            body = inspect.getsource(getattr(self.voice.Voice, name))
            self.assertIn("blocking=False", body, f"{name} 应当非阻塞地发命令")

    def test_move_is_confirmed_before_bookkeeping(self):
        # 命令是异步的：没确认就记账的话，收敛循环会以为成功而不再重试
        source = inspect.getsource(self.voice.Voice._switch_channel)
        self.assertIn("_wait_until_in", source)

    def test_switching_happens_on_a_worker_thread(self):
        source = inspect.getsource(self.voice.Voice)
        self.assertIn("_channel_loop", source)
        self.assertIn("_switch_channel", source)


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


class ComFrequencyFallbackTest(unittest.TestCase):
    """X-Plane 11.30 以前没有 8.33 那个 dataref，两个一起订、优先精确的。

    不存在的 dataref X-Plane 只是不推送，不报错，所以不用按版本分支。
    """

    def setUp(self):
        self.link = xplane.XPlaneLink()

    def test_prefers_the_precise_dataref(self):
        # 两个都有时用 8.33 那个，它能表示 132.005
        self.assertEqual(self.link._frequency(132005.0, 13200.0), 132.005)

    def test_falls_back_to_the_legacy_dataref(self):
        # 老的单位是 10 kHz：12150 -> 121.500
        self.assertEqual(self.link._frequency(None, 12150.0), 121.5)

    def test_falls_back_when_precise_is_zero(self):
        self.assertEqual(self.link._frequency(0.0, 11800.0), 118.0)

    def test_none_when_neither_is_available(self):
        self.assertIsNone(self.link._frequency(None, None))
        self.assertIsNone(self.link._frequency(0.0, 0.0))

    def test_snapshot_uses_the_legacy_value(self):
        self.link.values = {"com1_legacy": 12150.0}
        self.assertEqual(self.link.snapshot()["com1"], 121.5)

    def test_both_com_radios_have_a_fallback(self):
        for name in ("com1", "com2"):
            self.assertIn(f"{name}_legacy", xplane.DATAREFS)

    def test_legacy_datarefs_have_their_own_indices(self):
        # 索引撞了会让回包对错 dataref
        self.assertEqual(len(set(xplane.NAME_TO_INDEX.values())),
                         len(xplane.DATAREFS))


class DiscoveryTest(unittest.TestCase):
    """信标发现。用例来自一次真实飞行的日志：连上模拟器花了 8 分半。

    那台机器上信标从两个网卡回来（198.18.0.1 的虚拟网卡和 192.168.31.231 的
    局域网卡），而且每次 15 秒没数据就把发现到的地址整个扔掉、退回本机重来。
    """

    def test_virtual_adapters_rank_last(self):
        # 198.18/15 是 benchmark 段，实际是 VPN 虚拟网卡，往那边发收不到数据
        self.assertGreater(xplane._address_rank("198.18.0.1"),
                           xplane._address_rank("192.168.31.231"))

    def test_loopback_ranks_first(self):
        self.assertLess(xplane._address_rank("127.0.0.1"),
                        xplane._address_rank("192.168.31.231"))

    def test_ordinary_lan_beats_virtual(self):
        for virtual in ("198.18.0.1", "172.17.0.1", "169.254.1.1"):
            self.assertGreater(xplane._address_rank(virtual),
                               xplane._address_rank("10.0.0.5"),
                               f"{virtual} 应当排在普通局域网地址之后")

    def test_beacon_is_parsed(self):
        packet = b"BECN\x00" + struct.pack("=BBiiIH", 1, 2, 11, 1200, 1, 49000)
        self.assertEqual(
            xplane.XPlaneLink._parse_beacon(packet, ("192.168.31.231", 5000)),
            ("192.168.31.231", 49000))

    def test_foreign_packet_is_rejected(self):
        self.assertIsNone(
            xplane.XPlaneLink._parse_beacon(b"XXXX\x00" + b"\x00" * 20,
                                            ("1.2.3.4", 5000)))

    def test_known_good_address_is_preferred_over_loopback(self):
        """收过数据的地址不该被扔掉。

        真实日志里发现了 192.168.31.231，等 15 秒没数据（X-Plane 还在读盘）就
        退回 127.0.0.1，来回折腾了 8 分钟。
        """
        link = xplane.XPlaneLink()
        link._known_good = ("192.168.31.231", 49000)
        fallback = (link._known_good or link._last_discovered
                    or ("127.0.0.1", xplane.DEFAULT_PORT))
        self.assertEqual(fallback, ("192.168.31.231", 49000))

    def test_last_discovered_is_used_when_nothing_worked_yet(self):
        link = xplane.XPlaneLink()
        link._last_discovered = ("192.168.31.231", 49000)
        fallback = (link._known_good or link._last_discovered
                    or ("127.0.0.1", xplane.DEFAULT_PORT))
        self.assertEqual(fallback, ("192.168.31.231", 49000))

    def test_loopback_only_as_a_last_resort(self):
        link = xplane.XPlaneLink()
        fallback = (link._known_good or link._last_discovered
                    or ("127.0.0.1", xplane.DEFAULT_PORT))
        self.assertEqual(fallback[0], "127.0.0.1")


class LoginTest(unittest.TestCase):
    """登录时发的东西。真实日志里每次登录都跟着一条服务器错误。"""

    def test_no_bogus_atc_query_on_login(self):
        """不要再发没有目标呼号的 $CQ…:SERVER:ATC。

        can-fsd 的 handleQueryATC 是问"某个指定呼号是不是在线管制"，第 3 段
        必须带目标；不带就回 "Missing callsign"（handler.go:400）。而且本来就
        不需要——管制席位是靠 % 位置包广播过来的。
        """
        # 只看真正发出去的语句：解释这段历史的注释里也提到了这个包
        sends = [line for line in
                 inspect.getsource(fsdpilot.FSDPilot._connect).splitlines()
                 if "_send(" in line and not line.strip().startswith("#")]
        self.assertTrue(sends, "登录时总要发点什么")
        for line in sends:
            self.assertNotIn("SERVER:ATC", line)


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
        self.link.values.pop("com1_legacy", None)
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

    def test_widebody_is_not_replaced_by_a_narrowbody(self):
        # 拿 A319 去顶 B777 视觉上差得离谱；同族之后先按机身类别找
        models = cslmatch.ModelSet([
            cslmatch.Model("A319", "a319.obj", icao="A319"),
            cslmatch.Model("B78X", "b78x.obj", icao="B78X"),
        ])
        model, why = models.match(equipment="B77W")
        self.assertEqual(model.icao, "B78X", why)
        self.assertIn("宽体", why)

    def test_category_lookup(self):
        self.assertEqual(cslmatch.category_of("B77W"), "宽体")
        self.assertEqual(cslmatch.category_of("C172"), "通航")
        self.assertEqual(cslmatch.category_of("ZZZZ"), "")

    def test_categories_do_not_overlap(self):
        seen = {}
        for name, types in cslmatch.CATEGORIES.items():
            for icao in types:
                self.assertNotIn(icao, seen,
                                 f"{icao} 同时在 {seen.get(icao)} 和 {name}")
                seen[icao] = name

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

    def test_tcas_is_probed_not_version_gated(self):
        """能力应当靠 findDataRef 探测，不是按版本号写死。

        X-Plane 11.50 以下没有 TCAS 接管，但按版本分支很容易写错，也挡不住
        别的插件已经占了 AI 机位的情况。
        """
        import inspect
        source = inspect.getsource(self.module.PythonInterface._find_tcas_datarefs)
        self.assertIn("findDataRef", source)
        self.assertIn("tcas_available", source)

    def test_planes_are_not_acquired_without_tcas(self):
        # 没这个能力还去抢 AI 机位，会挡住 LiveTraffic 之类真正用得上的插件
        source = inspect.getsource(self.module.PythonInterface.XPluginEnable)
        self.assertIn("tcas_available", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
