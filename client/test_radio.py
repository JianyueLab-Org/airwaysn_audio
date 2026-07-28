"""MSFS 飞行员客户端的频道切换与 PTT 判据测试。

    python -m unittest test_radio -v      （在 client 目录下运行）

不连服务器、不碰模拟器、不开音频设备：Mumble 侧用替身，构造函数整个绕开。

钉两个都会让"语音完全不工作"的坑：

1. **pymumble 的阻塞接口没有超时。** channels.new_channel() 和
   users.myself.move_in() 都走 execute_command(blocking=True)，那个
   lock.acquire() 没有任何超时（pymumble 源码里就写着 "TODO: manage a timeout
   for blocking commands"）。命令一旦没被服务器处理就永久卡住，调用线程整个
   死掉，日志停在"尝试创建临时频道"，之后既没有成功也没有任何错误。

2. **根频道的 channel_id 就是 0。** 判断"有没有进频道"写成 not 会把在根频道
   当成没进频道，PTT 于是一声不吭地什么都不做。必须用 is None。
"""

import sys
import threading
import time
import types
import unittest


def _stub(name, **attrs):
    """给测试环境里不该真的跑起来的东西做个占位。"""
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# SimConnect 一导入就会去找模拟器，这里只要 radio.py 能导进来
_stub("SimConnect", SimConnect=object, AircraftRequests=object)

import radio


class FakeChannels:
    def __init__(self, server):
        self.server = server

    def __bool__(self):
        return True             # handle_voice 里有 "频道列表为空" 的判断

    def find_by_name(self, name):
        import pymumble_py3 as pymumble
        with self.server.lock:
            if name in self.server.by_name:
                return self.server.by_name[name]
        raise pymumble.errors.UnknownChannelError(name)

    def new_channel(self, parent_id, name, temporary=False):
        self.server.hang("channels.new_channel")


class FakeMyself:
    def __init__(self, server):
        self.server = server

    def __getitem__(self, key):
        if key == "channel_id":
            return self.server.my_channel
        if key == "name":
            return "1000"
        raise KeyError(key)

    def move_in(self, channel_id, token=None):
        self.server.hang("users.myself.move_in")


class FakeUsers:
    def __init__(self, server, session):
        self.myself = FakeMyself(server)
        self.myself_session = session


class FakeSoundOutput:
    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def add_sound(self, pcm):
        with self.lock:
            self.sent.append(pcm)


class FakeMumble:
    """够用的 Mumble 替身，重点是把 pymumble 的两种接口区别开。

    - ``execute_command(cmd, blocking=False)``：命令排队，假服务器在
      ``latency`` 之后才让它生效——真实的 pymumble 就是这样，命令是异步的，
      发出去不等于已经生效。
    - ``channels.new_channel()`` / ``users.myself.move_in()``：**永远不返回**，
      和真的 pymumble 一样。任何还在走阻塞接口的代码都会在测试里挂住，被
      ``join(timeout=…)`` 抓出来——这比断言"有没有调用某个函数"结实得多。
    """

    def __init__(self, latency=0.0, answers=True, my_channel=0, session=42):
        self.lock = threading.Lock()
        self.latency = latency
        self.answers = answers          # False = 服务器收下命令但什么都不做
        self.by_name = {}
        self.my_channel = my_channel
        self.next_id = 1
        self.commands = []
        self.blocking_calls = []
        self.connected = 2              # PYMUMBLE_CONN_STATE_CONNECTED
        self.channels = FakeChannels(self)
        self.users = FakeUsers(self, session)
        self.sound_output = FakeSoundOutput()

    def hang(self, what):
        self.blocking_calls.append(what)
        threading.Event().wait()

    def is_alive(self):
        return True

    def execute_command(self, cmd, blocking=True):
        if blocking:
            self.hang("execute_command(blocking=True)")
        self.commands.append(cmd)
        if self.answers:
            timer = threading.Timer(self.latency, self._apply, args=(cmd,))
            timer.daemon = True
            timer.start()

    def _apply(self, cmd):
        params = cmd.parameters
        with self.lock:
            if "name" in params:                    # CreateChannel
                self.by_name[params["name"]] = {
                    "channel_id": self.next_id,
                    "name": params["name"],
                    "parent": params["parent"],
                    "temporary": params["temporary"],
                }
                self.next_id += 1
            elif "session" in params:               # MoveCmd
                self.my_channel = params["channel_id"]

    def created_names(self):
        return [(c.parameters["parent"], c.parameters["name"],
                 c.parameters["temporary"])
                for c in self.commands if "name" in c.parameters]

    def moves(self):
        return [c.parameters["channel_id"] for c in self.commands
                if "session" in c.parameters]


def make_client(server):
    """绕开构造函数——它会连模拟器、开音频设备、初始化摇杆。"""
    client = radio.MumbleRadioClient.__new__(radio.MumbleRadioClient)
    client.mumble = server
    client.running = True
    client.current_channel = None
    client._channel_lock = threading.Lock()
    client._connection_established = threading.Event()
    client._connection_established.set()
    return client


class SwitchChannelTest(unittest.TestCase):

    def setUp(self):
        self._timeout = radio.CHANNEL_TIMEOUT
        self.server = FakeMumble(latency=0.2)
        self.client = make_client(self.server)

    def tearDown(self):
        radio.CHANNEL_TIMEOUT = self._timeout

    def switch(self, frequency=118.0, budget=None):
        """在独立线程里切频道，卡住就当场失败而不是拖死整个测试。"""
        if budget is None:
            budget = radio.CHANNEL_TIMEOUT * 2 + 3
        box = {}

        def work():
            box["value"] = self.client.switch_channel(frequency, caller="测试")

        thread = threading.Thread(target=work, daemon=True)
        started = time.time()
        thread.start()
        thread.join(budget)
        elapsed = time.time() - started
        self.assertFalse(
            thread.is_alive(),
            f"switch_channel 在 {budget:.1f} 秒内没有返回；"
            f"走过的阻塞接口={self.server.blocking_calls}")
        return box.get("value"), elapsed

    def test_missing_channel_is_created_and_entered(self):
        result, elapsed = self.switch(118.0)
        self.assertTrue(result)
        self.assertEqual(self.server.created_names(), [(0, "FREQ_118000", True)])
        self.assertEqual(self.server.my_channel, self.server.moves()[0])
        self.assertEqual(self.client.current_channel, self.server.my_channel)
        self.assertGreaterEqual(elapsed, 0.2, "要等到服务器回 ChannelState")

    def test_existing_channel_is_not_created_again(self):
        self.server.by_name["FREQ_118000"] = {"channel_id": 7}
        result, _ = self.switch(118.0)
        self.assertTrue(result)
        self.assertEqual(self.server.created_names(), [])
        self.assertEqual(self.server.my_channel, 7)

    def test_already_in_the_channel_needs_no_command_at_all(self):
        self.server.by_name["FREQ_118000"] = {"channel_id": 7}
        self.server.my_channel = 7
        result, _ = self.switch(118.0)
        self.assertTrue(result)
        self.assertEqual(self.server.commands, [])
        self.assertEqual(self.client.current_channel, 7)

    def test_a_server_that_never_answers_does_not_hang_the_thread(self):
        """这才是那个 bug：阻塞接口下服务器不回话，调用线程永远回不来。"""
        radio.CHANNEL_TIMEOUT = 0.5
        self.server.answers = False
        result, elapsed = self.switch(118.0)
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.5, "该等的还是要等满")
        self.assertLess(elapsed, 3.0, "但必须有上界")
        self.assertEqual(self.server.blocking_calls, [],
                         "不能再走 pymumble 那两个没有超时的阻塞接口")

    def test_move_that_never_takes_effect_is_not_booked_as_success(self):
        """记账早了的话，_ensure_in_correct_channel 会以为已经到位而不再重试。"""
        radio.CHANNEL_TIMEOUT = 0.5
        self.server.by_name["FREQ_118000"] = {"channel_id": 7}
        self.server.answers = False
        result, elapsed = self.switch(118.0)
        self.assertFalse(result)
        self.assertEqual(self.server.moves(), [7], "命令还是要发出去的")
        self.assertIsNone(self.client.current_channel)
        self.assertGreaterEqual(elapsed, 0.5)

    def test_waits_rather_than_giving_up_immediately(self):
        # 服务器 0.4 秒后才回报频道——老写法在这里会报"创建后仍找不到频道"
        self.server.latency = 0.4
        result, elapsed = self.switch(118.0)
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 0.4)

    def test_stopping_aborts_the_wait(self):
        radio.CHANNEL_TIMEOUT = 30.0
        self.server.answers = False

        def stop():
            time.sleep(0.2)
            self.client.running = False

        threading.Thread(target=stop, daemon=True).start()
        result, elapsed = self.switch(118.0, budget=5.0)
        self.assertFalse(result)
        self.assertLess(elapsed, 2.0, "停止之后不该继续等")

    def test_concurrent_switches_only_create_the_channel_once(self):
        """GUI 线程和监控线程会同时切，两边各建一次频道是真实日志里见过的。"""
        self.server.latency = 0.2
        threads = [threading.Thread(
            target=lambda: self.client.switch_channel(118.0, caller="并发"),
            daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(radio.CHANNEL_TIMEOUT * 2 + 3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.server.created_names()), 1)


class PttChannelGuardTest(unittest.TestCase):
    """PTT 的"进没进频道"判据。根频道的 channel_id 就是 0。"""

    def setUp(self):
        self.server = FakeMumble()
        self.client = make_client(self.server)
        self.client.CHUNK = 960
        self.client.stream = object()
        self.client.is_talking = True
        self.client.is_receiving = False
        self.client._last_rx_time = time.time()
        self.client.on_ptt_change = None
        self.client.on_rx_change = None
        self.client.joystick = None
        self.client.pygame_lock = threading.Lock()
        self.client.pygame_initialized = True
        self.client.settings = types.SimpleNamespace(
            ptt_key="space", joystick_ptt=None, mic_volume=100)
        self.client.ensure_pygame_initialized = lambda: None
        self.client._safe_stream_read = lambda chunk: b"\x01\x02" * 480

        self._is_pressed = radio.keyboard.is_pressed
        radio.keyboard.is_pressed = lambda key: True

    def tearDown(self):
        self.client.running = False
        radio.keyboard.is_pressed = self._is_pressed

    def capture_ptt(self, seconds=0.3):
        """跑一轮 PTT 并收下 handle_voice 打出来的诊断。"""
        import contextlib, io as _io
        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.run_ptt(seconds)
        return buffer.getvalue()

    def run_ptt(self, seconds=0.3):
        # 每轮都重新开张，一个用例里可以跑好几次
        self.client.running = True
        self.server.sound_output.sent.clear()
        thread = threading.Thread(target=self.client.handle_voice, daemon=True)
        thread.start()
        time.sleep(seconds)
        self.client.running = False
        thread.join(2)
        self.assertFalse(thread.is_alive(), "语音线程没有收尾")
        return self.server.sound_output.sent

    def test_transmits_from_a_normal_channel(self):
        self.server.my_channel = 5
        self.client.current_channel = 5
        self.assertTrue(self.run_ptt(), "按着 PTT 就该发出去")

    def test_root_and_no_channel_are_told_apart(self):
        """根频道的 id 就是 0，不能和"没进任何频道"混为一谈。

        两种情况都不该发声，但原因完全不同：一个是服务器还没回报我们的用户
        信息，一个是频道切换没成功。写成  的话 0 会走进前一
        个分支，用户拿到的提示指向错误的方向——而这两句话是他手里唯一的线索。
        """
        self.server.my_channel = radio.ROOT_CHANNEL
        self.client.current_channel = None
        output = self.capture_ptt()
        self.assertIn("还留在根频道", output)
        self.assertNotIn("未加入任何频道", output)

        self.server.my_channel = None
        output = self.capture_ptt()
        self.assertIn("未加入任何频道", output)
        self.assertNotIn("还留在根频道", output)

    def test_no_channel_at_all_sends_nothing(self):
        self.server.my_channel = None
        self.client.current_channel = None
        self.assertEqual(self.run_ptt(), [])

    def test_still_stuck_in_root_sends_nothing(self):
        """一次都没切成功就还在根频道，发出去没人听得到，只会打扰根频道。"""
        self.server.my_channel = radio.ROOT_CHANNEL
        self.client.current_channel = None
        self.assertEqual(self.run_ptt(), [])

    def test_root_after_a_reconnect_still_sends_nothing(self):
        """掉线重连会把人放回根频道，而 current_channel 还停在旧值。

        判据里附带 current_channel 的话，这一格就漏过去了——话音真的被发进根
        频道：自己频率上没人听得到，根频道里的人全听见了。
        """
        self.server.my_channel = radio.ROOT_CHANNEL
        self.client.current_channel = 7        # 之前切成功过，重连后已经失效
        self.assertEqual(self.run_ptt(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
