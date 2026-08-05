"""用户数据目录的测试。

    python -m unittest test_apppaths -v      （在 atis 目录下运行）

这里盯着的是**一条不能坏的向后兼容**：Windows 上的老安装，配置就在 exe 边上，
路径解析一改就等于把所有人的设置清零——而且是静默的，程序照常起来，只是所有
东西都回到了默认值。所以"当前目录已经有这个文件就用那一份"必须永远成立。

另一半是 macOS：那里双击 .app 的当前目录是 `/`，写不进去，必须落到
Application Support。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import apppaths


class DataDirTest(unittest.TestCase):

    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.folder = tempfile.mkdtemp(prefix="apppaths-test-")
        os.chdir(self.folder)
        self.saved_env = os.environ.pop(apppaths.ENV_OVERRIDE, None)
        # 家目录也换掉。下面那条 macOS 用例会真的去建
        # ~/Library/Application Support/<app>/——不换的话，跑一遍测试就在使用者
        # 的家目录里留下一个目录。
        self.saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.folder

    def tearDown(self):
        os.chdir(self.previous_cwd)
        os.environ.pop(apppaths.ENV_OVERRIDE, None)
        if self.saved_env is not None:
            os.environ[apppaths.ENV_OVERRIDE] = self.saved_env
        if self.saved_home is not None:
            os.environ["HOME"] = self.saved_home
        else:
            os.environ.pop("HOME", None)

    # ---------- 环境变量 ----------
    def test_the_override_wins_everywhere(self):
        """AIRWAYSN_DATA_DIR 优先级最高，而且在每个平台上都一样。

        冒烟测试靠它把数据钉在临时目录里。不生效的话，在 macOS 上跑一遍
        smoke_gui.py 就会读写使用者真实的设置，跑完还给人清空。
        """
        target = os.path.join(self.folder, "elsewhere")
        os.environ[apppaths.ENV_OVERRIDE] = target
        self.assertEqual(apppaths.data_dir(), target)
        self.assertEqual(apppaths.data_file("atis_profile.json"),
                         os.path.join(target, "atis_profile.json"))
        self.assertTrue(os.path.isdir(target), "数据目录应该被建出来")

    def test_the_override_beats_a_file_in_the_working_directory(self):
        """钉住之后，当前目录里的同名文件也不能把它抢回去。"""
        with open("atis_profile.json", "w") as f:
            f.write("{}")
        target = os.path.join(self.folder, "elsewhere")
        os.environ[apppaths.ENV_OVERRIDE] = target
        self.assertEqual(apppaths.data_file("atis_profile.json"),
                         os.path.join(target, "atis_profile.json"))

    # ---------- 向后兼容 ----------
    def test_an_existing_file_in_the_working_directory_wins(self):
        """当前目录里已经有的那一份必须继续被用。

        这条是给 Windows 的老安装和"从源码跑"兜底的。坏了的话，升级这一版就
        等于把所有人的配置清零——程序照常起来，只是所有设置都回到默认值。
        """
        with open("atis_profile.json", "w") as f:
            f.write("{}")
        expected = os.path.join(os.getcwd(), "atis_profile.json")
        for platform in ("win32", "darwin", "linux"):
            with mock.patch.object(sys, "platform", platform):
                self.assertEqual(
                    os.path.realpath(apppaths.data_file("atis_profile.json")),
                    os.path.realpath(expected),
                    "%s 上没有认当前目录里已有的配置" % platform)

    def test_windows_still_uses_the_working_directory(self):
        """Windows 的行为一个字节都不该变：还是当前目录下的裸文件名。"""
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(
                os.path.realpath(apppaths.data_file("atis_profile.json")),
                os.path.realpath(os.path.join(os.getcwd(),
                                              "atis_profile.json")))

    # ---------- macOS ----------
    def test_macos_goes_to_application_support(self):
        """macOS 上没有现成文件时落到 Application Support。

        双击 .app 的当前目录是 `/`，裸文件名在那儿既读不到也写不进去。
        """
        with mock.patch.object(sys, "platform", "darwin"):
            path = apppaths.data_file("atis_profile.json")
        self.assertIn(os.path.join("Library", "Application Support",
                                   apppaths.APP_DIR), path)
        self.assertTrue(path.endswith("atis_profile.json"))

    def test_the_app_dir_is_named_after_the_package(self):
        """目录名要和发出去的包同名，用户在 Finder 里才对得上。"""
        self.assertTrue(apppaths.APP_DIR)
        self.assertNotIn(os.sep, apppaths.APP_DIR)

    # ---------- 不能因为它起不来 ----------
    def test_an_uncreatable_directory_falls_back_instead_of_raising(self):
        """建不出目录就退回当前目录，绝不抛异常。

        一个存不下设置的客户端仍然能让管制员上席位说话；为了一个目录起不来则
        什么都做不了。
        """
        os.environ[apppaths.ENV_OVERRIDE] = os.path.join(self.folder, "nope")
        with mock.patch("os.makedirs", side_effect=OSError("denied")):
            self.assertEqual(os.path.realpath(apppaths.data_dir()),
                             os.path.realpath(os.getcwd()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
