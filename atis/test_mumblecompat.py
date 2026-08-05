"""ssl.wrap_socket 兼容补丁的测试。

    python -m unittest test_mumblecompat -v

这个补丁是语音能不能连上的前提：Python 3.12 删掉了 ssl.wrap_socket，而
pymumble 1.6.1 还在用它，没有补丁的话连接线程一起来就抛 AttributeError，
界面上表现为"服务器拒绝了连接"——排查方向完全被带偏。
"""

import socket
import ssl
import sys
import threading
import unittest
from unittest import mock

import mumblecompat


class InstallTest(unittest.TestCase):

    def setUp(self):
        self.had_wrap = hasattr(ssl, "wrap_socket")
        self.original = getattr(ssl, "wrap_socket", None)
        self.addCleanup(self.restore)

    def restore(self):
        if self.had_wrap:
            ssl.wrap_socket = self.original
        elif hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket

    def test_installs_when_missing(self):
        if hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket
        self.assertTrue(mumblecompat.install())
        self.assertTrue(hasattr(ssl, "wrap_socket"))

    def test_does_not_touch_an_existing_implementation(self):
        sentinel = object()
        ssl.wrap_socket = sentinel
        self.assertFalse(mumblecompat.install())
        self.assertIs(ssl.wrap_socket, sentinel)

    def test_wraps_a_real_tls_connection(self):
        """pymumble 的用法：先包再 connect。"""
        if hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket
        mumblecompat.install()

        # 起一个本地 TLS 服务器要证书，这里只验证包出来的对象形态正确，
        # 真正的握手在连服务器的那次诊断里验过了
        plain = socket.socket()
        wrapped = ssl.wrap_socket(plain)
        self.addCleanup(wrapped.close)
        self.assertIsInstance(wrapped, ssl.SSLSocket)
        self.assertEqual(wrapped.context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(wrapped.context.check_hostname,
                         "Mumble 用自签证书，开了主机名校验就连不上了")

    def test_accepts_the_arguments_pymumble_passes(self):
        if hasattr(ssl, "wrap_socket"):
            del ssl.wrap_socket
        mumblecompat.install()

        # pymumble 传的是 certfile/keyfile/ssl_version 三个关键字
        plain = socket.socket()
        wrapped = ssl.wrap_socket(plain, certfile=None, keyfile=None,
                                  ssl_version=ssl.PROTOCOL_TLS_CLIENT)
        self.addCleanup(wrapped.close)
        self.assertIsInstance(wrapped, ssl.SSLSocket)


class PymumbleIntegrationTest(unittest.TestCase):
    """确认补丁之后 pymumble 真的能建起连接对象。"""

    def test_pymumble_connect_no_longer_raises_attributeerror(self):
        import sys
        from unittest import mock
        for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                     "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
            sys.modules.setdefault(name, mock.MagicMock())

        mumblecompat.install()
        import pymumble_py3 as pymumble

        errors = []
        previous = threading.excepthook
        threading.excepthook = lambda args: errors.append(args.exc_type.__name__)
        self.addCleanup(lambda: setattr(threading, "excepthook", previous))

        # 连一个必然连不上的地址：重点是不能再出现 AttributeError
        client = pymumble.Mumble("127.0.0.1", "test", port=1, reconnect=False)
        client.start()
        client.join(timeout=15)

        self.assertNotIn("AttributeError", errors,
                         "补丁之后不该再因为 ssl.wrap_socket 缺失而崩")


class SendBufferTest(unittest.TestCase):
    """发送缓冲满了不等于连接断了。

    这是"一发话就掉线"的病根。pymumble 没有 UDP 通道，话音塞进 UDPTUNNEL 和
    控制消息走同一条 TCP，而那条 socket 是非阻塞的，发送循环却按 C 的写法兜错
    （`if sent < 0`——Python 里 send 是抛异常）。于是上行堵一下，
    `BlockingIOError` 一路穿到 `Mumble.run()` 的 `except socket.error`，
    连接被判死。真实日志里：掉线都在按下 PTT 两三秒后，而且每次第一下重连就成功
    ——因为网络上根本没断。
    """

    def make(self, blocks, error=None, timeout=1.0):
        """一个前 `blocks` 次写不动、之后正常的 socket。blocks=None 表示永远写不动。"""
        outer = self

        class Sock:
            def __init__(self):
                self.attempts = 0
                self.sent = []
                self.selected = 0

            def send(self, data):
                self.attempts += 1
                if blocks is None or self.attempts <= blocks:
                    raise (error or BlockingIOError(10035, "would block"))
                self.sent.append(bytes(data))
                return len(data)

            def fileno(self):
                return 1

            def recv(self, size):
                return b"payload"

        sock = Sock()
        guarded = mumblecompat._RetryingSocket(sock, timeout=timeout)
        # 不真的去 select 一个假 fd
        original_select = mumblecompat.select.select
        mumblecompat.select.select = lambda r, w, x, t=None: (
            sock.__setattr__("selected", sock.selected + 1), ([], [], []))[1]
        outer.addCleanup(setattr, mumblecompat.select, "select", original_select)
        return sock, guarded

    def test_a_full_buffer_is_retried_not_raised(self):
        sock, guarded = self.make(blocks=3)
        self.assertEqual(guarded.send(b"audio"), 5)
        self.assertEqual(sock.attempts, 4, "应当重试到写得进去为止")
        self.assertEqual(sock.sent, [b"audio"])
        self.assertGreaterEqual(sock.selected, 1, "重试之间要等 socket 可写")

    def test_tls_want_write_is_the_same_case(self):
        """TLS 上缓冲满抛的是 SSLWantWriteError，不是 BlockingIOError。"""
        sock, guarded = self.make(blocks=2, error=ssl.SSLWantWriteError())
        self.assertEqual(guarded.send(b"x"), 1)
        self.assertEqual(sock.attempts, 3)

    def test_a_socket_that_never_drains_still_reports_a_failure(self):
        """真的堵死了还是要当断线——不能无限期挂在那里。"""
        sock, guarded = self.make(blocks=None, timeout=0.05)
        with self.assertRaises(BlockingIOError):
            guarded.send(b"x")

    def test_everything_else_passes_through(self):
        sock, guarded = self.make(blocks=0)
        self.assertEqual(guarded.recv(7), b"payload")
        self.assertEqual(guarded.fileno(), 1, "select() 要靠 fileno()")

    def test_every_connection_gets_the_guard_including_reconnects(self):
        """必须挂在 connect() 上：pymumble 每次重连都会重建 socket。

        漏掉重连那一次，那条连接就退回旧行为——而这个症状只在网络不好的用户
        那里出现，自己这边根本复现不了。
        """
        import pymumble_py3.mumble as mumble_module

        original = mumble_module.Mumble.connect
        self.addCleanup(setattr, mumble_module.Mumble, "connect", original)

        def fake_connect(self):
            self.control_socket = object()      # 每次连接都是一个新 socket
            return 1

        mumble_module.Mumble.connect = fake_connect
        self.assertTrue(mumblecompat.patch_send(), "补丁没打上")

        client = mumble_module.Mumble.__new__(mumble_module.Mumble)
        for round_number in (1, 2):             # 首连，然后一次重连
            client.control_socket = None
            mumble_module.Mumble.connect(client)
            self.assertIsInstance(
                client.control_socket, mumblecompat._RetryingSocket,
                f"第 {round_number} 次连接之后 socket 没有被包住")

    def test_the_guard_is_not_stacked(self):
        client = type("M", (), {"control_socket": object()})()
        plain = client.control_socket
        self.assertTrue(mumblecompat.guard_control_socket(client))
        self.assertIs(client.control_socket._sock, plain)
        self.assertFalse(mumblecompat.guard_control_socket(client),
                         "已经包过就不该再套一层")


class OpusCtlTest(unittest.TestCase):
    """opuslib 的 ctl 调用在 Apple 芯片上要走可变参数约定。

    **这是"语音在 Mac 上连不上"的病根**，而且它不是 macOS 打包的问题——是
    ABI 的问题：`opus_encoder_ctl(st, request, ...)` 是可变参数函数，opuslib
    只设了 restype，没设 argtypes。Apple 的 arm64 ABI 把可变参数放栈上，
    ctypes 却按普通调用塞进寄存器，libopus 读到垃圾，一律回 OPUS_BAD_ARG。

    下面第一条是**真的建一个编码器、真的设一次码率**。这一点很要紧：任何用
    替身的写法都验不到这个 bug——问题出在真实的 C 调用上，Mock 一律会"成功"。
    """

    def encoder(self):
        try:
            import opuslib
        except Exception as e:
            self.skipTest(f"没有 opus 原生库: {e}")
        mumblecompat.patch_opus_ctl()
        try:
            return opuslib.Encoder(48000, 1, "audio")
        except Exception as e:
            self.skipTest(f"建不出编码器: {e}")

    def test_the_bitrate_can_actually_be_set(self):
        """pymumble 一连上就会做这件事（create_encoder -> _set_bandwidth）。

        失败的话语音线程当场抛未捕获异常，界面只说"连接意外结束"。
        """
        encoder = self.encoder()
        for bitrate in (52400, 32000):
            encoder.bitrate = bitrate
            self.assertEqual(encoder.bitrate, bitrate,
                             "设进去的码率读回来对不上")

    def test_it_only_touches_macos(self):
        """别的平台本来就是好的，不顺手改 Windows 那条路。"""
        with mock.patch.object(mumblecompat.sys, "platform", "win32"):
            self.assertFalse(mumblecompat.patch_opus_ctl())

    def test_a_missing_opus_does_not_raise(self):
        """补不上顶多是回到原样；import 阶段抛异常会让整个客户端起不来。"""
        with mock.patch.object(mumblecompat.sys, "platform", "darwin"):
            with mock.patch.dict(sys.modules, {"opuslib.api.encoder": None}):
                with mock.patch("builtins.__import__",
                                side_effect=ImportError("no opus")):
                    self.assertFalse(mumblecompat.patch_opus_ctl())

    def test_install_applies_it(self):
        """客户端只调 install()，所以它必须把这一条也带上。"""
        with mock.patch.object(mumblecompat, "patch_opus_ctl") as patched:
            mumblecompat.install()
        patched.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
