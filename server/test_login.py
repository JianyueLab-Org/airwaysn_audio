"""Ice 认证器的测试。

    python -m unittest test_login -v      （在 server 目录下运行）

不连 Murmur、不连 can-web：Ice、MumbleServer 和 requests 都在导入前换成替身，
所以开发机上不装 zeroc-ice 也能跑。

钉的是三件会让"全网都登不上语音"的事：
- 问 can-web 必须带超时。authenticate 跑在 Ice 的服务端分发线程上，上游变成
  黑洞就会把线程池占满，而进程看起来完全正常。
- 密码不能进日志。FSD 密码就是会员的网站密码，这个进程又是前台跑的。
- 验证不了和密码错了必须分开，否则用户会一直去改密码。
"""

import contextlib
import io
import sys
import threading
import time
import types
import unittest


def _install_stubs():
    """把 login.py 导入时要的三个外部模块换成替身。"""
    if "Ice" not in sys.modules:
        ice = types.ModuleType("Ice")
        ice.Exception = type("Exception", (Exception,), {})
        ice.ConnectionTimeoutException = type(
            "ConnectionTimeoutException", (ice.Exception,), {})
        ice.InitializationData = lambda: types.SimpleNamespace(properties=None)
        ice.createProperties = lambda: types.SimpleNamespace(
            setProperty=lambda *a: None)
        ice.initialize = lambda *a, **k: None
        sys.modules["Ice"] = ice

    if "MumbleServer" not in sys.modules:
        mumble = types.ModuleType("MumbleServer")
        # 这两个是要被继承的，必须是真的类
        mumble.ServerAuthenticator = type("ServerAuthenticator", (), {})
        mumble.ServerCallback = type("ServerCallback", (), {})
        for name in ("ServerPrx", "MetaPrx", "ServerAuthenticatorPrx",
                     "ServerCallbackPrx"):
            setattr(mumble, name, types.SimpleNamespace(
                checkedCast=lambda p: p, uncheckedCast=lambda p: p))
        sys.modules["MumbleServer"] = mumble

    if "requests" not in sys.modules:
        req = types.ModuleType("requests")
        exceptions = types.ModuleType("requests.exceptions")
        exceptions.RequestException = type("RequestException", (Exception,), {})
        exceptions.Timeout = type("Timeout", (exceptions.RequestException,), {})
        req.exceptions = exceptions
        req.post = lambda *a, **k: None
        sys.modules["requests"] = req
        sys.modules["requests.exceptions"] = exceptions


_install_stubs()

import login as login_module


class Response:
    def __init__(self, status_code, text="{}"):
        self.status_code = status_code
        self.text = text


class FakeUpstream:
    """假的 can-web。记下每一次调用，按剧本回应。"""

    def __init__(self, *outcomes):
        self.outcomes = outcomes
        self.calls = []          # 每次的 (url, kwargs)

    def post(self, url, headers=None, data=None, **kwargs):
        self.calls.append((url, dict(kwargs, headers=headers, data=data)))
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class UpstreamTestCase(unittest.TestCase):

    def setUp(self):
        self._delay = login_module.HTTP_RETRY_DELAY
        login_module.HTTP_RETRY_DELAY = 0.0      # 测试里不真的等
        self._post = login_module.requests.post

    def tearDown(self):
        login_module.HTTP_RETRY_DELAY = self._delay
        login_module.requests.post = self._post

    def upstream(self, *outcomes):
        fake = FakeUpstream(*outcomes)
        login_module.requests.post = fake.post
        return fake

    def network_error(self, message="连不上"):
        return login_module.requests.exceptions.RequestException(message)


class TimeoutTest(UpstreamTestCase):
    """问上游必须带超时。

    requests 没有默认超时。authenticate 跑在 Ice 的分发线程上，线程池
    SizeMax 是 8——上游一旦不回包，八个登录就能把认证器整个堵死，之后谁都
    连不上语音，而进程还活着、端口还听着，从外面完全看不出问题。
    """

    def test_every_request_carries_a_finite_timeout(self):
        fake = self.upstream(Response(200))
        login_module.verify("1000", "pw")
        self.assertTrue(fake.calls, "应当真的发出去了")
        for _url, kwargs in fake.calls:
            self.assertIn("timeout", kwargs,
                          "没有 timeout，requests 会一直等下去")
            self.assertIsInstance(kwargs["timeout"], (int, float))
            self.assertGreater(kwargs["timeout"], 0)

    def test_a_timing_out_upstream_does_not_hang_the_caller(self):
        """上游超时时 authenticate 必须照常返回，不能挂在分发线程上。"""
        self.upstream(login_module.requests.exceptions.Timeout("超时"))
        box = {}

        def work():
            box["value"] = login_module.verify("1000", "pw")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "verify 没有返回")
        self.assertEqual(box["value"], login_module.VERIFY_UNREACHABLE)


class RetryTest(UpstreamTestCase):

    def test_network_error_is_retried_and_can_still_succeed(self):
        fake = self.upstream(self.network_error(), Response(200))
        self.assertEqual(login_module.verify("1000", "pw"),
                         login_module.VERIFY_OK)
        self.assertEqual(len(fake.calls), 2, "网络错误应当再试一次")

    def test_a_rejection_is_never_retried(self):
        """接口明确说不行就别再打了。

        can-web 按 ASN 对认证失败限流，把明确的拒绝重发一遍等于自己把这个
        账号往锁死里推——密码本来就错，重试也不会变对。
        """
        fake = self.upstream(Response(400, '{"error":"bad"}'))
        self.assertEqual(login_module.verify("1000", "pw"),
                         login_module.VERIFY_REJECTED)
        self.assertEqual(len(fake.calls), 1)

    def test_it_gives_up_after_the_retries(self):
        fake = self.upstream(self.network_error())
        self.assertEqual(login_module.verify("1000", "pw"),
                         login_module.VERIFY_UNREACHABLE)
        self.assertEqual(len(fake.calls), login_module.HTTP_RETRIES + 1,
                         "不能无限重试")


class PasswordLoggingTest(UpstreamTestCase):
    """密码不能出现在任何一条日志里。

    FSD 密码就是会员的网站密码，而 start.sh 是前台跑 login.py 的，输出直接
    进终端和 journal。
    """

    SECRET = "hunter2-非常机密"

    def capture(self, call):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            call()
        return buffer.getvalue()

    def test_success_does_not_log_the_password(self):
        self.upstream(Response(200))
        output = self.capture(lambda: login_module.verify("1000", self.SECRET))
        self.assertNotIn(self.SECRET, output)
        self.assertIn("1000", output, "cid 还是要留着，否则没法查")

    def test_rejection_does_not_log_the_password(self):
        self.upstream(Response(400, '{"error":"bad"}'))
        output = self.capture(lambda: login_module.verify("1000", self.SECRET))
        self.assertNotIn(self.SECRET, output)

    def test_network_error_does_not_log_the_password(self):
        self.upstream(self.network_error())
        output = self.capture(lambda: login_module.verify("1000", self.SECRET))
        self.assertNotIn(self.SECRET, output)

    def test_atis_login_does_not_log_the_password(self):
        self.upstream(Response(200))
        output = self.capture(
            lambda: login_module.login_ATIS("1005_atis118000", self.SECRET))
        self.assertNotIn(self.SECRET, output)

    def test_no_atis_password_reaches_the_log_whatever_the_cid(self):
        """哪个 cid 都一样，口令不进日志。

        以前 900 这个保留账号走的是另一条分支，所以单独测过一遍；旁路去掉之后
        只剩一条路，但这条断言仍然值得留着——它盯的是"别把口令 print 出来"，
        和走哪条分支无关。
        """
        secret = "另一个账号的口令"
        self.upstream(Response(200))
        output = self.capture(
            lambda: login_module.login_ATIS("900_atis118000", secret))
        self.assertNotIn(secret, output)


class AuthenticateTest(UpstreamTestCase):
    """authenticate 的返回值决定用户看到什么。"""

    def setUp(self):
        super().setUp()
        self.kicked = []
        self.auth = login_module.AuthenticatorI.__new__(
            login_module.AuthenticatorI)
        self.auth.online_users = {}
        self.auth.context = {}
        self.auth.kick_previous_session = self.kicked.append

    def authenticate(self, name, password):
        return self.auth.authenticate(name, password, [], "", False)

    def test_a_good_login_returns_the_cid_as_the_user_id(self):
        self.upstream(Response(200))
        self.assertEqual(self.authenticate("1000", "pw"), (1000, "1000", []))

    def test_a_bad_password_is_a_plain_failure(self):
        self.upstream(Response(400))
        user_id, _, _ = self.authenticate("1000", "pw")
        self.assertEqual(user_id, login_module.AUTH_FAILED)

    def test_an_unreachable_upstream_is_not_reported_as_a_bad_password(self):
        """上游连不上时报 -1，用户看到的是"密码错误"，会一直去改密码。

        -3 是"暂时验证不了"，服务端不会把它当成密码问题。
        """
        self.upstream(self.network_error())
        user_id, _, _ = self.authenticate("1000", "pw")
        self.assertEqual(user_id, login_module.AUTH_TEMPORARY_FAILURE)
        self.assertNotEqual(user_id, login_module.AUTH_FAILED)

    def test_atis_user_id_is_the_frequency(self):
        # 同一个账号开多个不同频率的通播不能互相踢掉
        self.upstream(Response(200))
        user_id, name, _ = self.authenticate("1005_atis118000", "pw")
        self.assertEqual(user_id, 118000)
        self.assertEqual(name, "1005_atis118000")

    def test_there_is_no_shortcut_for_any_account(self):
        """保留账号的旁路已经去掉了，谁都得去问上游。

        默认部署只起 login.py，server/ATIS/ 那队服务端通播机不跑，那条免验证的
        旁路就是死代码。去掉之后连 900 也得老老实实过上游接口——这条断言就是钉
        住"没有任何账号能绕过认证"，免得哪天又被顺手加回来。
        """
        fake = self.upstream(Response(400))
        user_id, _, _ = self.authenticate("900_atis127800", "随便什么口令")
        self.assertEqual(user_id, login_module.AUTH_FAILED)
        self.assertEqual(len(fake.calls), 1, "应当老老实实去问上游")

    def test_a_normal_atis_client_still_authenticates(self):
        """管制员手里的桌面通播客户端不能被误伤。

        airwaysn-atis 登录用的是 `{自己的cid}_atis{频率}`，走的正是 login_ATIS。
        清理保留账号那条旁路时，很容易顺手把整条 _atis 路径一起删掉——那样管制员
        的通播客户端会直接登不上，而这是个还在用的功能。
        """
        self.upstream(Response(200))
        user_id, name, _ = self.authenticate("1005_atis127800", "pw")
        self.assertEqual(user_id, 127800, "用户 id 要是那 6 位频率")
        self.assertEqual(name, "1005_atis127800")

    def test_a_successful_login_kicks_the_previous_session(self):
        self.upstream(Response(200))
        self.authenticate("1000", "pw")
        self.assertEqual(self.kicked, ["1000"])

    def test_a_failed_login_does_not_kick_anyone(self):
        self.upstream(Response(400))
        self.authenticate("1000", "pw")
        self.assertEqual(self.kicked, [], "登录失败不该把在线的自己踢下去")

    def test_a_non_numeric_name_does_not_blow_up(self):
        self.upstream(Response(200))
        user_id, _, _ = self.authenticate("不是数字", "pw")
        self.assertEqual(user_id, login_module.AUTH_FAILED)

    def test_a_malformed_atis_name_is_refused_rather_than_given_a_bogus_id(self):
        """`_atis` 后面不是正好 6 位的，不能当通播账号放行。

        旧正则不卡结尾，1000_atis1180001 也算匹配，取 id 时 split 拿到 7 位的
        1180001——凭空造出一个谁也不认识的用户 id，而且它和任何真实频率都对不上。
        现在这种名字落回普通用户那条路，int() 抛错，认证失败。
        """
        self.upstream(Response(200))
        user_id, _, _ = self.authenticate("1000_atis1180001", "pw")
        self.assertEqual(user_id, login_module.AUTH_FAILED)


class UserIdAgreementTest(unittest.TestCase):
    """nameToId 必须和 authenticate 对同一个名字给出同一个 id。

    两个方法原来各写各的：authenticate 认得 `1000_atis118000` 并给 118000，
    nameToId 在 int() 上抛异常、回落到 -2。后果是按名字把通播账号写进 ACL
    不生效——setACL 收下了，权限却落在一个不存在的用户上，看着完全成功。
    """

    def setUp(self):
        self.auth = login_module.AuthenticatorI.__new__(
            login_module.AuthenticatorI)

    def test_a_plain_account_is_its_asn_id(self):
        self.assertEqual(self.auth.nameToId("1000"), 1000)
        self.assertEqual(login_module.user_id_for("1000"), 1000)

    def test_an_atis_account_is_its_frequency(self):
        self.assertEqual(self.auth.nameToId("1000_atis118000"), 118000)

    def test_it_agrees_with_authenticate(self):
        for name in ("1000", "1005_atis127800", "900_atis118000"):
            with self.subTest(name=name):
                self.assertEqual(self.auth.nameToId(name),
                                 login_module.user_id_for(name))

    def test_an_unknown_name_falls_through_to_the_server(self):
        # -2 = 不认识这个用户，交回服务端自己的账号库
        self.assertEqual(self.auth.nameToId("SuperUser"),
                         login_module.AUTH_FALLTHROUGH)


class WatchConnectionTest(unittest.TestCase):
    """守着和 Murmur 的链路。断了要退出，不能假装还活着。"""

    def tearDown(self):
        login_module._shutting_down = False

    def test_a_broken_link_returns_false_quickly(self):
        class Dead:
            def isRunning(self, context):
                raise RuntimeError("连接没了")

        started = time.time()
        alive = login_module.watch_connection(Dead(), {}, interval=0.05)
        self.assertFalse(alive)
        self.assertLess(time.time() - started, 2.0)

    def test_a_healthy_link_keeps_watching(self):
        class Alive:
            def __init__(self):
                self.checks = 0

            def isRunning(self, context):
                self.checks += 1
                if self.checks >= 3:
                    login_module._shutting_down = True
                return True

        server = Alive()
        self.assertTrue(login_module.watch_connection(server, {}, interval=0.01))
        self.assertGreaterEqual(server.checks, 3, "应当反复确认，而不是只看一次")

    def test_the_secret_is_passed_on_every_check(self):
        # 不带 secret 的 Ice 调用会抛 InvalidSecretException
        seen = []

        class Server:
            def isRunning(self, context):
                seen.append(context)
                login_module._shutting_down = True
                return True

        login_module.watch_connection(Server(), {"secret": "s"}, interval=0.01)
        self.assertEqual(seen, [{"secret": "s"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
