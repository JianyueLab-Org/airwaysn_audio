"""被服务端踢下线之后不许连回去。

    python -m unittest test_kick -v      （在 controller 目录下运行）

自带替身：numpy / pyaudio / pymumble / opuslib 全部在导入 voice 之前换掉，所以
开发机上不装原生库（尤其是 opus）也能跑。test_voice.py 用的是真的 pymumble，
在没有 opus 的机器上根本导入不了，那正是这套逻辑需要一个跑得起来的家的原因。

钉的是一件事：**次数上限拦不住被踢**。上限数的是失败的重连，而被踢之前的那次
登录是成功的，计数已经清零了。同一个账号在两处登录时，两端互相顶掉、各自重
连、各自又把对方顶掉，每一轮都成功，预算永远用不完 —— 构造上的死循环，最后由
Murmur 的 autoban 把那个 IP 整个封掉收场。
"""

import importlib
import sys
import unittest
from unittest import mock

# 替身**用完就撤**。这个文件和别的测试模块跑在同一个进程里（CI 用的是
# `unittest discover`），装了 numpy 的机器上 setdefault 是空操作、什么都不会发
# 生；但在没装的机器上，留着的替身会漏给 test_voice.py 之类真的需要 numpy 的模
# 块，把"缺依赖"这种一眼能看懂的错误变成一堆莫名其妙的失败。所以导入完就把
# sys.modules 恢复原样 —— 我们自己已经拿到 voice 这个模块对象了，不需要它继续
# 留在表里。
_STUBS = ("numpy", "pyaudio", "pymumble_py3", "pymumble_py3.constants",
          "pymumble_py3.errors", "pymumble_py3.messages",
          "pymumble_py3.mumble", "opuslib", "opuslib.api",
          "opuslib.api.decoder", "opuslib.api.encoder",
          "opuslib.api.info", "opuslib.exceptions", "mumblecompat")


def _import_under_stubs(name):
    added = [n for n in _STUBS if n not in sys.modules]
    for n in added:
        sys.modules[n] = mock.MagicMock()
    had = sys.modules.pop(name, None)
    try:
        module = importlib.import_module(name)
    finally:
        for n in added:
            sys.modules.pop(n, None)
        # 我们导入的这一份是绑着替身的，不能留给别人。
        sys.modules.pop(name, None)
        if had is not None:
            sys.modules[name] = had
    return module


voice = _import_under_stubs("voice")


class BoundedReconnectKickTest(unittest.TestCase):
    """混入本身：被踢之后 connect() 不再往下走。"""

    def make(self):
        calls = []

        class FakeBase:
            def __init__(self):
                self.reconnect = True
                self.connected = 0

            def connect(self):
                calls.append(1)
                return "dialled"

        bounded = type("Bounded", (voice.BoundedReconnect, FakeBase), {})
        return bounded(), calls

    def test_a_kicked_connection_never_dials_again(self):
        instance, calls = self.make()
        instance._session_established()          # 被踢之前是连上过的
        instance.mark_kicked("您的账号在其他位置登录")

        result = instance.connect()

        self.assertEqual(calls, [], "被踢之后还去连，就是那场循环本身")
        self.assertTrue(instance.gave_up)
        self.assertFalse(instance.reconnect)
        self.assertEqual(result, voice.PYMUMBLE_CONN_STATE_FAILED)

    def test_a_successful_session_does_not_clear_the_kick(self):
        """计数清零管的是重连预算，不是"要不要重连"这个决定。"""
        instance, calls = self.make()
        instance.mark_kicked("挤下线了")
        instance._session_established()
        instance.connect()
        self.assertEqual(calls, [])

    def test_the_limit_alone_cannot_stop_a_kick_loop(self):
        """这一条钉的是"为什么需要 mark_kicked"：每轮成功的会话都会把计数清零，
        所以只靠三次上限，十轮下来十次都真的去连了。"""
        instance, calls = self.make()
        for _ in range(10):
            instance._session_established()
            instance.connect()
        self.assertEqual(len(calls), 10, "上限拦不住这种循环")
        self.assertFalse(instance.gave_up)


class UserRemovedTest(unittest.TestCase):
    """回调这一半：哪些 UserRemove 算被踢。"""

    def make(self):
        client = voice.VoiceClient.__new__(voice.VoiceClient)
        client.mumble = mock.MagicMock()
        client.mumble.users.myself_session = 42
        client.mumble.mark_kicked = mock.MagicMock()
        client.on_state = None
        self.states = []
        client._state = lambda state, message: self.states.append((state, message))
        return client

    def test_being_kicked_marks_the_connection(self):
        client = self.make()
        client._on_user_removed({"session": 42},
                                {"session": 42, "actor": 1,
                                 "reason": "您的账号在其他位置登录", "ban": False})

        client.mumble.mark_kicked.assert_called_once()
        self.assertEqual(self.states[-1][0], 'offline')
        self.assertIn("您的账号在其他位置登录", self.states[-1][1],
                      "服务端给的理由要原样告诉用户")

    def test_leaving_voluntarily_is_not_a_kick(self):
        """只有 session 的 UserRemove 是用户自己走的。当成被踢的话，一次正常
        退出就会把重连永久关掉。"""
        client = self.make()
        client._on_user_removed({"session": 42}, {"session": 42})
        client.mumble.mark_kicked.assert_not_called()
        self.assertEqual(self.states, [])

    def test_somebody_else_being_kicked_is_ignored(self):
        client = self.make()
        client._on_user_removed({"session": 7},
                                {"session": 7, "actor": 1, "reason": "x"})
        client.mumble.mark_kicked.assert_not_called()

    def test_a_kick_with_no_reason_still_counts(self):
        """Murmur 踢 ghost 时 reason 可以是空的 —— actor 在就够了。"""
        client = self.make()
        client._on_user_removed({"session": 42},
                                {"session": 42, "actor": 1, "reason": ""})
        client.mumble.mark_kicked.assert_called_once()
        self.assertEqual(self.states[-1][0], 'offline')


if __name__ == "__main__":
    unittest.main()
