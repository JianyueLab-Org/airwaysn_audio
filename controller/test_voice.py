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


if __name__ == "__main__":
    unittest.main(verbosity=2)
