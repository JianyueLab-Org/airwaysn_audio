"""版本号的测试。

    python -m unittest test_version -v      （在 controller 目录下运行）

这个仓库靠复制共享代码而不是 import，`version.py` 因此有六份。版本号最怕的就是
"改了一份、忘了其余五份"：六个程序报出来的版本各不相同，而用户报问题时给的正是
这个号，对不上就查不下去。所以这里逐份比对。
"""

import os
import re
import unittest

import version

COMPONENTS = ("client", "xplane_client", "controller", "atis", "xpc", "msfs")


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VersionStringTest(unittest.TestCase):

    def test_version_looks_like_a_version(self):
        self.assertRegex(version.VERSION, r"^\d+\.\d+\.\d+$")

    def test_full_contains_both_numbers(self):
        # 比的是 version()（可能来自 buildinfo.json）而不是 VERSION 常量——
        # CI 每次推到 main 都会算一个新的补丁号写进去，常量不跟着动
        text = version.full()
        self.assertIn(version.version(), text)
        self.assertIn(version.build(), text)

    def test_the_frozen_version_wins_over_the_constant(self):
        """打包时固化的版本号优先。

        补丁号是 CI 数 v2.0.* 标签算出来的，只信 VERSION 常量的话，所有自动
        发出去的包都会显示同一个版本号。
        """
        import json
        import tempfile
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, version.BUILDINFO_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"version": "2.0.99", "build": "123.abcdef0"}, f)

        original_dir, original_cache = version._resource_dir, version._cached_version
        try:
            version._resource_dir = lambda: folder
            version._cached_version = None
            # 基线的解析和 DEV 开关无关，所以比的是 release_version()
            self.assertEqual(version.release_version(), "2.0.99")
            self.assertNotEqual(version.release_version(), version.VERSION)
        finally:
            version._resource_dir = original_dir
            version._cached_version = original_cache

    def test_the_constant_is_the_fallback(self):
        """没有 buildinfo.json（从源码跑）时用常量。"""
        import tempfile
        original_dir, original_cache = version._resource_dir, version._cached_version
        try:
            version._resource_dir = lambda: tempfile.mkdtemp()
            version._cached_version = None
            self.assertEqual(version.release_version(), version.VERSION)
        finally:
            version._resource_dir = original_dir
            version._cached_version = original_cache

    def test_build_is_never_empty(self):
        """拿不到 git、也没有 buildinfo.json 时要退回 dev，不能是空串。

        空串会让界面显示成 "v1.1.0 (build )"，看着像程序坏了。
        """
        self.assertTrue(version.build())

    def test_build_never_raises(self):
        # 显示不出 build 号是小事，为它起不来是大事
        version._cached = None
        try:
            self.assertIsInstance(version.build(), str)
        finally:
            version._cached = None

    def test_unreadable_buildinfo_falls_back_instead_of_raising(self):
        saved, version._cached = version._cached, None
        try:
            original = version._resource_dir
            version._resource_dir = lambda: os.path.join(repo_root(), "不存在的目录")
            try:
                self.assertIsInstance(version.build(), str)
            finally:
                version._resource_dir = original
        finally:
            version._cached = saved


class DisplayedVersionTest(unittest.TestCase):
    """界面上显示的版本号必须来自函数，不能是那个手写的常量。

    `VERSION` 是源码里的**回退值**；真正发出去的版本号是 CI 打包时固化进
    buildinfo.json 的，只有 `version.version()` 会去读。用错的后果在实测里
    出现过：v2.0.3 的包，日志首行写着 v2.0.3，标题栏却是 v2.0.0——用户拿标题栏
    对版本，会以为自己根本没更新成功。

    源码匹配当然弱，但这条差别没有别的测法：真要跑起来得有打好的包和 Qt。
    """

    def test_no_client_shows_the_fallback_constant(self):
        import re
        offenders = []
        for component in COMPONENTS:
            path = os.path.join(repo_root(), component, "gui.py")
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            # 注释里提它是可以的，赋值给界面用的名字不行
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"=\s*version\.VERSION\b", stripped):
                    offenders.append(f"{component}/gui.py: {stripped}")
        self.assertEqual(
            offenders, [],
            "这些地方把回退常量当成版本号显示了，打包之后会一直显示旧版本号："
            + "；".join(offenders))


class DevBuildTest(unittest.TestCase):
    """Dev 测试版：DEV 置 True 时报下一个补丁号加 (Dev Build N)。

    最新正式版是 v2.1.1 时，第一个 Dev 包是 v2.1.2 (Dev Build 1)；同一个
    版本再出一个测试包 DEV_BUILD 加一。取"下一个补丁号"是为了让测试包在
    排序上永远新于它基于的正式版。
    """

    def setUp(self):
        self._dev = version.DEV
        self._build = version.DEV_BUILD
        self._cache = version._cached_version

    def tearDown(self):
        version.DEV = self._dev
        version.DEV_BUILD = self._build
        version._cached_version = self._cache

    def test_the_dev_flag_is_a_plain_bool(self):
        # 开发分支上 DEV 可以是 True（当前就是）；这里只钉住类型和 build 号
        # 从 1 起——正式发布的分支把 DEV 关回 False、DEV_BUILD 归 1
        self.assertIsInstance(version.DEV, bool)
        self.assertGreaterEqual(version.DEV_BUILD, 1)

    def test_bump_patch(self):
        self.assertEqual(version._bump_patch("2.1.1"), "2.1.2")
        self.assertEqual(version._bump_patch("2.1.9"), "2.1.10")
        self.assertEqual(version._bump_patch("2.0.99"), "2.0.100")

    def test_dev_version_is_the_next_patch(self):
        version._cached_version = "2.1.1"
        version.DEV = True
        self.assertEqual(version.version(), "2.1.2")
        self.assertEqual(version.display(), "2.1.2 (Dev Build 1)")

    def test_second_dev_build_bumps_the_build_number_not_the_version(self):
        version._cached_version = "2.1.1"
        version.DEV = True
        version.DEV_BUILD = 2
        self.assertEqual(version.version(), "2.1.2")
        self.assertEqual(version.display(), "2.1.2 (Dev Build 2)")

    def test_full_carries_the_dev_label(self):
        version._cached_version = "2.1.1"
        version.DEV = True
        self.assertIn("Dev Build 1", version.full())
        self.assertIn("v2.1.2", version.full())

    def test_release_display_is_the_plain_version(self):
        version._cached_version = "2.1.1"
        version.DEV = False
        self.assertEqual(version.display(), "2.1.1")


class CopiesAgreeTest(unittest.TestCase):
    """六份 version.py 必须完全一致。"""

    def read(self, component):
        path = os.path.join(repo_root(), component, "version.py")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_every_component_has_one(self):
        missing = [c for c in COMPONENTS if self.read(c) is None]
        self.assertEqual(missing, [], f"这些组件没有 version.py: {missing}")

    def test_all_copies_are_identical(self):
        texts = {c: self.read(c) for c in COMPONENTS if self.read(c) is not None}
        mine = texts.get("controller")
        differing = [c for c, text in texts.items() if text != mine]
        self.assertEqual(
            differing, [],
            f"这几份 version.py 和 controller 的不一样了: {differing}"
            "——改了一份就要把其余几份同步过去")

    def test_all_components_report_the_same_version(self):
        """就算文件整体允许不同，版本号本身也绝不能不同。"""
        pattern = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.M)
        found = {}
        for component in COMPONENTS:
            text = self.read(component)
            if text is None:
                continue
            match = pattern.search(text)
            self.assertIsNotNone(match, f"{component}/version.py 里找不到 VERSION")
            found[component] = match.group(1)
        self.assertEqual(
            len(set(found.values())), 1,
            f"各组件的版本号对不上: {found}")


class SpecsFreezeTheBuildTest(unittest.TestCase):
    """每个 spec 都要固化 build 号并把 buildinfo.json 打进包。

    漏掉任何一步，那个包里的 build 号就会永远显示 dev——而它恰恰是打包出去、
    真正会有人拿去用的那一份，从源码跑反而是好的，最容易漏掉。
    """

    def spec(self, component):
        path = os.path.join(repo_root(), component, "gui.spec")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_specs_call_freeze(self):
        bad = [c for c in COMPONENTS
               if (self.spec(c) or "") and "version.freeze(" not in self.spec(c)]
        self.assertEqual(bad, [], f"这些 spec 没有固化 build 号: {bad}")

    def test_specs_bundle_buildinfo(self):
        bad = [c for c in COMPONENTS
               if (self.spec(c) or "") and "buildinfo.json" not in self.spec(c)]
        self.assertEqual(bad, [], f"这些 spec 没把 buildinfo.json 打进去: {bad}")

    def test_buildinfo_is_gitignored(self):
        """buildinfo.json 是打包时生成的，不该进仓库。"""
        bad = []
        for component in COMPONENTS:
            path = os.path.join(repo_root(), component, ".gitignore")
            if not os.path.exists(path):
                bad.append(component)
                continue
            with open(path, "r", encoding="utf-8-sig") as f:
                if "buildinfo.json" not in f.read().split():
                    bad.append(component)
        self.assertEqual(bad, [], f"这些组件没有忽略 buildinfo.json: {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
