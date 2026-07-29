"""语音客户端里几处并发行为的测试。

    python -m unittest test_voice -v

钉的是"话音发到错误频率"这一类问题：PTT 和交叉耦合共用一条 Mumble 连接，
而 sound_output.target 是全局的一个字段，两条线程一起改就会串频。这种 bug
在真机上只表现为"偶尔有人在别的频率听到我说话"，几乎无法复现，所以必须在
这一层挡住。

不连服务器：Mumble 侧用替身。
"""

import sys
import threading
import time
import unittest
from unittest import mock

for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
              "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
    sys.modules.setdefault(_name, mock.MagicMock())

import radiostack
import voice
from voice import VoiceClient


class FakeSoundOutput:
    """记下每一块音频是用哪个 target 发出去的。"""

    def __init__(self):
        self.target = 0
        self.sent = []          # (target, 数据长度)
        self.lock = threading.Lock()

    def add_sound(self, pcm):
        with self.lock:
            self.sent.append((self.target, len(pcm)))

    def get_buffer_size(self):
        return 0


class FakeMumble:
    def __init__(self):
        self.sound_output = FakeSoundOutput()
        self.messages = []
        self.users = {}

    def send_message(self, type, message):
        self.messages.append((type, message))

    def is_alive(self):
        return True


def make_client():
    client = VoiceClient("host", "1000", "pw")
    client.mumble = FakeMumble()
    client.connected = True
    client.running = True
    return client


class VoiceTargetTest(unittest.TestCase):
    """PTT 和交叉耦合必须用不同的 VoiceTarget 编号。"""

    def test_ptt_and_cross_couple_use_different_targets(self):
        client = make_client()
        client._xc_channels = [11, 22, 33]
        client._program_cross_couple_targets()

        self.assertNotIn(voice.PTT_TARGET_ID, client._xc_targets.values(),
                         "交叉耦合不能占用 PTT 的编号，否则转发时会把 PTT 的"
                         "目标一起改掉")
        self.assertEqual(len(set(client._xc_targets.values())), 3,
                         "每个来源频率要有自己的编号")

    def test_cross_couple_excludes_its_own_source(self):
        client = make_client()
        client._xc_channels = [11, 22]
        client._program_cross_couple_targets()

        # 检查发出去的 VoiceTarget 消息：11 的目标里不该有 11
        programmed = {}
        for _type, message in client.mumble.messages:
            programmed[message.id] = [t.channel_id for t in message.targets]
        for source, target_id in client._xc_targets.items():
            self.assertNotIn(source, programmed[target_id],
                             "转发不能发回来源频率，否则会回环")

    def test_no_targets_when_fewer_than_two_xc(self):
        client = make_client()
        client._xc_channels = [11]
        client._program_cross_couple_targets()
        self.assertEqual(client._xc_targets, {})


class CrossCoupleDuringPttTest(unittest.TestCase):
    """管制员讲话时不转发——一条连接只有一个发送队列。"""

    def setUp(self):
        self.client = make_client()
        self.client._channel_ids = {118000: 11, 121700: 22}
        self.client._xc_channels = [11, 22]
        self.client._program_cross_couple_targets()
        self.client.mumble.messages.clear()

    def chunk(self):
        return mock.Mock(pcm=b"\x00" * 1920)

    def test_forwards_when_idle(self):
        self.client._forward_cross_couple(118000, self.chunk())
        self.assertEqual(len(self.client.mumble.sound_output.sent), 1)
        target, _ = self.client.mumble.sound_output.sent[0]
        self.assertEqual(target, self.client._xc_targets[11])

    def test_does_not_forward_while_transmitting(self):
        self.client.transmitting = True
        self.client._forward_cross_couple(118000, self.chunk())
        self.assertEqual(self.client.mumble.sound_output.sent, [],
                         "PTT 期间不该插入转发的音频")

    def test_target_is_reset_after_forwarding(self):
        self.client._forward_cross_couple(118000, self.chunk())
        self.assertEqual(self.client.mumble.sound_output.target, 0,
                         "转发完要把 target 归零，否则下一次发话会串频")

    def test_unknown_source_is_ignored(self):
        self.client._forward_cross_couple(999000, self.chunk())
        self.assertEqual(self.client.mumble.sound_output.sent, [])

    def test_concurrent_ptt_and_forwarding_never_mixes_targets(self):
        """并发跑一遍：每块音频的 target 要么是 PTT 的，要么是转发的。"""
        client = self.client
        client._tx_channels = [11]
        client.input_stream = mock.Mock()
        client.input_stream.read.return_value = b"\x01" * 1920

        stop = threading.Event()

        def forwarder():
            while not stop.is_set():
                client._forward_cross_couple(118000, self.chunk())
                time.sleep(0.001)

        thread = threading.Thread(target=forwarder, daemon=True)
        thread.start()
        client.start_transmit()
        time.sleep(0.3)
        client.stop_transmit()
        stop.set()
        thread.join(timeout=2)
        time.sleep(0.2)

        targets = {t for t, _ in client.mumble.sound_output.sent}
        allowed = {voice.PTT_TARGET_ID} | set(client._xc_targets.values())
        self.assertTrue(targets, "应当发出了一些音频")
        self.assertTrue(targets <= allowed,
                        f"出现了意料之外的 target: {targets - allowed}")


class TransmitThreadTest(unittest.TestCase):

    def test_rapid_ptt_does_not_start_two_threads(self):
        client = make_client()
        client._tx_channels = [11]
        client.input_stream = mock.Mock()
        client.input_stream.read.return_value = b"\x01" * 1920

        client.start_transmit()
        first = client._tx_thread
        client.stop_transmit()
        client.start_transmit()          # 立刻再按
        second = client._tx_thread

        self.assertIsNot(first, second)
        self.assertFalse(first.is_alive(),
                         "上一条发话线程要先收完尾，否则它退出时会把 target 清零")
        client.stop_transmit()

    def test_transmit_needs_a_tx_frequency(self):
        client = make_client()
        client._tx_channels = []
        client.start_transmit()
        self.assertFalse(client.transmitting, "没有 TX 频率时不该开始发话")


class RxIndicatorTest(unittest.TestCase):

    def test_continuous_audio_never_reports_rx_end(self):
        """有人一直在讲话时，监控线程不该报 RX 结束——那会让指示灯闪。"""
        client = make_client()
        events = []
        client.on_rx = lambda khz, active, name: events.append((khz, active))

        stop = threading.Event()

        def keep_talking():
            # 模拟回调线程持续收到话音，间隔远小于 RX_TIMEOUT
            while not stop.is_set():
                client._last_rx[118000] = time.time()
                time.sleep(0.02)

        talker = threading.Thread(target=keep_talking, daemon=True)
        monitor = threading.Thread(target=client._rx_monitor_loop, daemon=True)
        talker.start()
        monitor.start()
        time.sleep(0.6)
        stop.set()
        client.running = False
        talker.join(timeout=2)
        monitor.join(timeout=2)

        self.assertNotIn((118000, False), events,
                         "一直在收话音，不该报 RX 结束")

    def test_rx_ends_after_the_timeout(self):
        client = make_client()
        events = []
        client.on_rx = lambda khz, active, name: events.append((khz, active))

        client._last_rx[118000] = time.time() - 10      # 早就超时了
        monitor = threading.Thread(target=client._rx_monitor_loop, daemon=True)
        monitor.start()
        time.sleep(0.3)
        client.running = False
        monitor.join(timeout=2)

        self.assertIn((118000, False), events, "超时之后应当报 RX 结束")


class FakeChannels:
    def __init__(self, server):
        self.server = server

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
        raise KeyError(key)

    def get(self, key, default=None):
        if key == "channel_id":
            return self.server.my_channel
        return default

    def move_in(self, channel_id, token=None):
        self.server.hang("users.myself.move_in")


class FakeUsers:
    def __init__(self, server, session):
        self.myself = FakeMyself(server)
        self.myself_session = session


class FakeServer:
    """够用的 Mumble 替身，重点是把 pymumble 的两种接口区别开。

    - ``execute_command(cmd, blocking=False)``：命令排队，假服务器在
      ``latency`` 之后才让它生效——真实的 pymumble 就是这样，命令是异步的。
    - ``channels.new_channel()`` / ``users.myself.move_in()``：**永远不返回**。
      这两个入口走 ``execute_command(blocking=True)``，那个 ``lock.acquire()``
      没有任何超时（pymumble 源码里就写着 "TODO: manage a timeout for blocking
      commands"），服务器不处理命令时调用线程就死在那一行，而 sync() 是整条
      电台栈同步链的入口，一起死掉。

    照搬这个行为，任何还在走阻塞接口的代码都会在测试里挂住，被
    ``join(timeout=…)`` 抓出来。
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
        self.messages = []
        self.channels = FakeChannels(self)
        self.users = FakeUsers(self, session)
        self.sound_output = FakeSoundOutput()

    def hang(self, what):
        self.blocking_calls.append(what)
        threading.Event().wait()        # 永远不返回，和真的 pymumble 一样

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

    def send_message(self, type, message):
        self.messages.append((type, message))

    def is_alive(self):
        return True

    def created_names(self):
        return [(c.parameters["parent"], c.parameters["name"],
                 c.parameters["temporary"])
                for c in self.commands if "name" in c.parameters]

    def moves(self):
        return [c.parameters["channel_id"] for c in self.commands
                if "session" in c.parameters]


class ChannelSwitchTest(unittest.TestCase):
    """建频道和进频道都不能走 pymumble 的阻塞接口。

    那个 ``lock.acquire()`` 没有超时，服务器不处理命令就永久卡住，调用线程整个
    死掉——日志停在"建一个临时的"，之后既没有成功也没有任何错误。这里用一个
    会永久挂住的替身把它逼出来。
    """

    def setUp(self):
        self._timeout = voice.CHANNEL_TIMEOUT
        self.client = VoiceClient("host", "1000", "pw")
        self.server = FakeServer(latency=0.2)
        self.client.mumble = self.server
        self.client.connected = True
        self.client.running = True

    def tearDown(self):
        voice.CHANNEL_TIMEOUT = self._timeout

    def call(self, func, budget=None):
        """在独立线程里跑，卡住就当场失败而不是拖死整个测试。"""
        if budget is None:
            budget = voice.CHANNEL_TIMEOUT * 2 + 3
        box = {}

        def work():
            box["value"] = func()

        thread = threading.Thread(target=work, daemon=True)
        started = time.time()
        thread.start()
        thread.join(budget)
        elapsed = time.time() - started
        self.assertFalse(
            thread.is_alive(),
            f"{func} 在 {budget:.1f} 秒内没有返回；"
            f"走过的阻塞接口={self.server.blocking_calls}")
        return box.get("value"), elapsed

    # ---------- 建频道 ----------
    def test_missing_channel_is_created_and_waited_for(self):
        result, elapsed = self.call(lambda: self.client._resolve_channel(118000))
        self.assertEqual(self.server.created_names(), [(0, "FREQ_118000", True)])
        self.assertEqual(result, 1)
        self.assertGreaterEqual(elapsed, 0.2, "要等到服务器回 ChannelState")
        self.assertEqual(self.client._channel_ids[118000], 1)
        self.assertEqual(self.client._channel_to_khz[1], 118000)

    def test_existing_channel_is_not_created_again(self):
        self.server.by_name["FREQ_118000"] = {"channel_id": 7}
        result, _ = self.call(lambda: self.client._resolve_channel(118000))
        self.assertEqual(result, 7)
        self.assertEqual(self.server.commands, [])

    def test_unanswered_creation_gives_up_within_the_timeout(self):
        voice.CHANNEL_TIMEOUT = 0.5
        self.server.answers = False
        result, elapsed = self.call(lambda: self.client._resolve_channel(118000))
        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 0.5, "该等的还是要等满")
        self.assertLess(elapsed, 3.0, "但必须有上界——以前这里是永久卡死")
        self.assertEqual(self.server.blocking_calls, [],
                         "不能再走 pymumble 那两个没有超时的阻塞接口")

    def test_a_failed_resolve_is_not_cached(self):
        """失败不能记进表里，否则频道后来建出来了也永远用不上。"""
        voice.CHANNEL_TIMEOUT = 0.3
        self.server.answers = False
        self.call(lambda: self.client._resolve_channel(118000))
        self.assertNotIn(118000, self.client._channel_ids)

    # ---------- 进频道 ----------
    def test_join_waits_until_the_move_took_effect(self):
        result, elapsed = self.call(lambda: self.client._join(7))
        self.assertTrue(result)
        self.assertEqual(self.server.moves(), [7])
        self.assertEqual(self.server.my_channel, 7)
        self.assertGreaterEqual(elapsed, 0.2)

    def test_join_reports_failure_when_the_move_never_takes_effect(self):
        voice.CHANNEL_TIMEOUT = 0.5
        self.server.answers = False
        result, elapsed = self.call(lambda: self.client._join(7))
        self.assertFalse(result)
        self.assertEqual(self.server.moves(), [7], "命令还是要发出去的")
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 3.0)

    def test_join_does_nothing_when_already_there(self):
        self.server.my_channel = 7
        result, _ = self.call(lambda: self.client._join(7))
        self.assertTrue(result)
        self.assertEqual(self.server.commands, [])

    # ---------- 整条同步链 ----------
    def test_sync_finishes_even_if_the_server_never_answers(self):
        """sync() 是整条链的入口，卡在这里等于电台栈永远同步不上去。"""
        voice.CHANNEL_TIMEOUT = 0.3
        self.server.answers = False
        stack = radiostack.RadioStack()
        for freq in ("118.000", "121.700"):
            stack.add(freq)
            stack.get(radiostack.parse_frequency(freq)).rx = True
        stack.selected_khz = 118000
        _, elapsed = self.call(lambda: self.client.sync(stack), budget=6.0)
        self.assertLess(elapsed, 5.0, "两个频率各等一个超时，也该结束了")
        self.assertEqual(self.server.blocking_calls, [])

    def test_sync_joins_the_selected_frequency(self):
        stack = radiostack.RadioStack()
        for freq in ("118.000", "121.700"):
            stack.add(freq)
            stack.get(radiostack.parse_frequency(freq)).rx = True
        stack.selected_khz = 121700
        self.call(lambda: self.client.sync(stack), budget=8.0)
        joined = self.client._channel_ids[121700]
        self.assertEqual(self.server.my_channel, joined,
                         "主频率的频道要真的进去，不然服务端不支持监听时一个"
                         "频率都听不到")


class ReconnectTest(unittest.TestCase):
    """重连之后必须把整个电台栈重新推一遍。

    客户端是 reconnect=True 建的，掉线后 pymumble 自己会连回来，但服务器把重连
    上来的用户放回**根频道**，频道监听和 VoiceTarget 这些跟会话走的注册也一并
    没了。只把界面的灯点回绿色的话，就成了最糟的那种情况：显示一切正常，人却
    待在根频道，一个频率都收不到，PTT 也发不出去。
    """

    def setUp(self):
        self.client = VoiceClient("host", "1000", "pw")
        self.server = FakeServer(latency=0.05)
        self.client.mumble = self.server
        self.client.connected = True
        self.client.running = True

        self.stack = radiostack.RadioStack()
        self.stack.add(118000)
        self.stack.add(121700)
        for radio in self.stack:
            radio.rx = True
            radio.tx = True
        self.stack.select(118000)

    def sync_and_wait(self):
        thread = threading.Thread(target=self.client.sync, args=(self.stack,),
                                  daemon=True)
        thread.start()
        thread.join(voice.CHANNEL_TIMEOUT * 4 + 5)
        self.assertFalse(thread.is_alive(), "sync 没有在预算内返回")

    def test_reconnect_rejoins_and_reprograms_everything(self):
        self.sync_and_wait()
        primary = self.client._channel_ids[118000]
        self.assertEqual(self.server.my_channel, primary, "前提：先进了主频道")

        # 服务器把重连上来的用户放回根频道，会话级的注册全部作废
        self.server.my_channel = 0
        moves_before = len(self.server.moves())
        messages_before = len(self.server.messages)

        self.client._resync_after_reconnect()
        # 重推是在后台线程里做的，等它把该发的都发完
        deadline = time.time() + voice.CHANNEL_TIMEOUT * 4 + 5
        while time.time() < deadline and not (
                self.server.my_channel != 0
                and len(self.server.messages) > messages_before):
            time.sleep(0.05)

        self.assertNotEqual(self.server.my_channel, 0,
                            "重连之后没有回到频率频道，人还在根频道")
        self.assertGreater(len(self.server.moves()), moves_before,
                           "重连之后没有重新发进频道的命令")
        self.assertGreater(len(self.server.messages), messages_before,
                           "重连之后没有重新发频道监听 / 发话目标")

    def test_stale_caches_do_not_suppress_the_resend(self):
        """光调 sync 不够，本地缓存必须一起作废。

        `_listening` 还记着断线前监听了哪些频道，diff 出来是空的，一条监听消息
        都不会再发；`_sent_target` 会让发话目标的去重逻辑以为已经设好了。两个
        加起来的效果就是：重连后看着一切正常，实际上一个频率都收不到。
        """
        self.sync_and_wait()
        self.assertTrue(self.client._listening or self.client._sent_target,
                        "前提：第一次同步之后本地是有缓存的")

        # 把重推挡掉，单看"作废缓存"这一步——否则后台那次 sync 会立刻把缓存
        # 填回来，断言到底看到的是清掉之前还是之后全凭手速
        resynced = []
        self.client.sync = lambda stack: resynced.append(stack)
        self.client._resync_after_reconnect()

        self.assertEqual(self.client._listening, set())
        self.assertIsNone(self.client._sent_target)
        self.assertEqual(self.client._channel_ids, {},
                         "临时频道空了会被销毁，再建是新号，旧的频道号不能留")
        deadline = time.time() + 5
        while time.time() < deadline and not resynced:
            time.sleep(0.02)
        self.assertEqual(resynced, [self.stack], "作废了缓存却没有重新推一遍")


if __name__ == "__main__":
    unittest.main(verbosity=2)
