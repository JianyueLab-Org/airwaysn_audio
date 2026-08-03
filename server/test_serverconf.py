"""口令加载的测试。

    python -m unittest test_serverconf -v      （在 server 目录下运行）

盯住一件事：**源码里不能有能用的默认口令**。有默认值就等于永远不会被改，而且
会跟着仓库到处走——git 历史里也永远留着。
"""

import json
import os
import tempfile
import unittest

import serverconf


class EnvGuard(unittest.TestCase):
    """每个用例都在干净的环境里跑，别被开发机上真实的环境变量影响。"""

    VARS = ("MUMBLE_ICE_SECRET", "ATIS_PASSWORD", "ATIS_CID")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.VARS}
        self._secrets_file = serverconf.SECRETS_FILE
        # 指到一个不存在的路径，默认就是"没有配"
        serverconf.SECRETS_FILE = os.path.join(
            tempfile.gettempdir(), "no-such-server-secrets.json")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        serverconf.SECRETS_FILE = self._secrets_file

    def write_secrets(self, data):
        handle, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.addCleanup(os.remove, path)
        serverconf.SECRETS_FILE = path
        return path

    def write_ini(self, text):
        handle, path = tempfile.mkstemp(suffix=".ini")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path


class NoHardcodedSecretTest(EnvGuard):

    def test_nothing_configured_means_a_loud_failure(self):
        """什么都没配就必须抛，不能悄悄用一个源码里的默认值。"""
        with self.assertRaises(serverconf.MissingSecret):
            serverconf.ice_secret(ini_path="/绝对不存在的路径.ini")

    def test_the_error_says_where_to_put_it(self):
        with self.assertRaises(serverconf.MissingSecret) as caught:
            serverconf.ice_secret(ini_path="/绝对不存在的路径.ini")
        message = str(caught.exception)
        # 三条出路都要说出来，否则运维只能翻源码
        self.assertIn("MUMBLE_ICE_SECRET", message)
        self.assertIn(serverconf.SECRETS_FILE, message)
        self.assertIn("icesecretwrite", message)

    def test_the_source_tree_carries_no_working_secret(self):
        """整个仓库里不该再有能直接用的口令。

        这些字面量在这个文件里也是拼出来的——写全了的话，这条测试自己就会
        变成它要抓的东西。

        扫的不只是 .py：部署文件才是口令最容易回来的地方，写进 Dockerfile 的
        口令还会永远留在镜像层和 docker history 里。
        """
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        markers = ("yoyo" + "14185721", "p@" + "ssw0rd")
        skip = {".git", ".venv-test", "__pycache__", "build", "dist", "release"}
        suffixes = (".py", ".sh", ".yml", ".yaml", ".ini", ".env", ".ps1")
        names = {"Dockerfile", ".dockerignore"}
        offenders = []
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs
                       if d not in skip and not d.startswith(".venv")]
            for name in files:
                if not name.endswith(suffixes) and name not in names:
                    continue
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                for marker in markers:
                    if marker in text:
                        offenders.append(os.path.relpath(path, repo))
        self.assertEqual(offenders, [], f"源码里还留着口令: {offenders}")

    def test_atis_password_is_absent_rather_than_guessed(self):
        self.assertIsNone(serverconf.atis_password())

    def test_atis_password_can_be_required(self):
        with self.assertRaises(serverconf.MissingSecret):
            serverconf.atis_password(required=True)


class SourceOrderTest(EnvGuard):

    def test_environment_wins(self):
        self.write_secrets({"ice_secret": "从文件"})
        os.environ["MUMBLE_ICE_SECRET"] = "从环境"
        self.assertEqual(serverconf.ice_secret(), "从环境")

    def test_file_beats_the_ini(self):
        self.write_secrets({"ice_secret": "从文件"})
        ini = self.write_ini("icesecretwrite=从ini\n")
        self.assertEqual(serverconf.ice_secret(ini_path=ini), "从文件")

    def test_the_ini_is_enough_on_its_own(self):
        """服务器上 icesecretwrite 本来就有一份，等于零配置。"""
        ini = self.write_ini("; 注释\ndatabase=/var/lib/x.sqlite\n"
                             "icesecretwrite=来自ini的口令\n")
        self.assertEqual(serverconf.ice_secret(ini_path=ini), "来自ini的口令")

    def test_icesecret_is_accepted_too(self):
        ini = self.write_ini("icesecret=只有这一个\n")
        self.assertEqual(serverconf.ice_secret(ini_path=ini), "只有这一个")

    def test_write_key_wins_over_the_generic_one(self):
        ini = self.write_ini("icesecret=通用\nicesecretwrite=写口令\n")
        self.assertEqual(serverconf.ice_secret(ini_path=ini), "写口令")

    def test_commented_out_lines_are_ignored(self):
        ini = self.write_ini("#icesecretwrite=被注释掉的\n;icesecret=也被注释\n")
        with self.assertRaises(serverconf.MissingSecret):
            serverconf.ice_secret(ini_path=ini)

    def test_an_empty_value_counts_as_absent(self):
        ini = self.write_ini("icesecretwrite=\n")
        with self.assertRaises(serverconf.MissingSecret):
            serverconf.ice_secret(ini_path=ini)

    def test_blank_environment_variable_counts_as_absent(self):
        os.environ["MUMBLE_ICE_SECRET"] = "   "
        ini = self.write_ini("icesecretwrite=来自ini\n")
        self.assertEqual(serverconf.ice_secret(ini_path=ini), "来自ini")

    def test_a_broken_secrets_file_does_not_crash(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        self.addCleanup(os.remove, path)
        serverconf.SECRETS_FILE = path
        ini = self.write_ini("icesecretwrite=来自ini\n")
        # 坏文件当作没配，继续往下找，而不是把整个进程带崩
        self.assertEqual(serverconf.ice_secret(ini_path=ini), "来自ini")

    def test_atis_account_defaults_to_900(self):
        self.assertEqual(serverconf.atis_account(), "900")

    def test_atis_account_can_be_overridden(self):
        os.environ["ATIS_CID"] = "901"
        self.assertEqual(serverconf.atis_account(), "901")


if __name__ == "__main__":
    unittest.main(verbosity=2)
