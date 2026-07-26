"""ssl.wrap_socket 兼容补丁的测试。

    python -m unittest test_mumblecompat -v

这个补丁是语音能不能连上的前提：Python 3.12 删掉了 ssl.wrap_socket，而
pymumble 1.6.1 还在用它，没有补丁的话连接线程一起来就抛 AttributeError，
界面上表现为"服务器拒绝了连接"——排查方向完全被带偏。
"""

import socket
import ssl
import threading
import unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
