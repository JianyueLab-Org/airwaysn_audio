"""版本号和 build 号。

`VERSION` 是手工维护的语义版本；`build()` 不能手工维护——手写的 build 号第一天
就会忘了改，然后所有用户报上来的都是同一个号，等于没有。

难点在于**打包之后程序里没有 git**：PyInstaller 不会把 `.git` 带上，运行时再去
问 git 只会得到"不是仓库"。所以分两条路：

- 打包时：`gui.spec` 调 `freeze()`，把当时的 git 状态写成 `buildinfo.json` 一起
  打进包里；运行时优先读它。
- 从源码跑：现问 git，顺带标出工作区脏没脏（`+dirty`）——开发机上跑的多半是改了
  一半的代码，报上来的号必须能看出这一点，否则会拿它去比对某个提交，白查半天。

两条都不行就返回 "dev"，不抛异常：显示不出 build 号是小事，为它起不来是大事。

build 号取 `提交数.短哈希`（如 `71.667d9b4`）。提交数是单调递增的，用户说"我这
个比他那个新"时能直接比；短哈希则能精确定位到是哪一次提交。
"""

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger("version")

# 手工维护的**回退**值。真正发出去的版本号由 CI 决定：推到 main 之后，
# 工作流数一遍已有的 v2.2.* 标签，取最大的那个 +1，写进 buildinfo.json。
# 从源码跑时没有 buildinfo.json，就显示这个。
#
# 主次版本（2.1）是人的决定，改这里的同时**必须**把 release.yml 的 SERIES
# 一起改成同一个系列：两边不一致的话，CI 会照旧在老系列里递增，而从源码跑
# 的人看到的是新系列——同一份代码报两个版本号，还没有任何地方会报错。
VERSION = "2.2.0"

# ---- Dev 测试版 ----
# 测试版把 DEV 置 True：版本号显示成**下一个补丁号**加 (Dev Build N)。
# 最新正式版是 v2.1.1 时，第一个 Dev 包就是 v2.1.2 (Dev Build 1)；同一个
# 版本再出一个测试包，把 DEV_BUILD 加一（v2.1.2 (Dev Build 2)）。正式发布
# 那个补丁号时，把 DEV 关回 False、DEV_BUILD 归 1。
#
# 取"下一个补丁号"而不是照抄最新版，是为了让测试包在排序上永远新于它基于
# 的正式版——用户拿版本号对话时（"我这个比他新"）不会把测试包错认成旧版。
DEV = True
DEV_BUILD = 1

# CI 用它把算出来的版本号传进打包过程（gui.spec 会调 freeze()）。
VERSION_ENV = "CAN_VERSION"

BUILDINFO_NAME = "buildinfo.json"

_cached = None
_cached_version = None


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def _resource_dir():
    """打包之后资源在 sys._MEIPASS，从源码跑就是本目录。"""
    return getattr(sys, "_MEIPASS", _here())


def _git(*args, cwd=None):
    # Windows 上必须 shell=False + 显式超时：git 不在 PATH 时 subprocess 会直接
    # 抛 FileNotFoundError，卡住比抛异常更糟
    result = subprocess.run(("git",) + args, cwd=cwd or _here(),
                            capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git 返回非零")
    return result.stdout.strip()


def from_git(cwd=None):
    """问 git 要 build 号。不是仓库、或者没装 git 就返回 None。"""
    try:
        count = _git("rev-list", "--count", "HEAD", cwd=cwd)
        short = _git("rev-parse", "--short", "HEAD", cwd=cwd)
    except Exception as e:
        log.debug("could not read the git info: %s", e)
        return None
    build = f"{count}.{short}"
    try:
        if _git("status", "--porcelain", cwd=cwd):
            build += "+dirty"
    except Exception:
        pass
    return build


def _buildinfo():
    path = os.path.join(_resource_dir(), BUILDINFO_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug("could not read %s: %s", path, e)
        return {}


def _from_file():
    return (_buildinfo().get("build") or "").strip() or None


def release_version():
    """最新正式版的版本号。打包的读固化值，从源码跑就用上面那个回退值。

    固化值优先是关键：CI 每次推到 main 都会算一个新的补丁号，而 VERSION 这个
    常量不会跟着动——只信常量的话，所有自动发出去的包都会显示同一个版本号。
    """
    global _cached_version
    if _cached_version is None:
        _cached_version = (_buildinfo().get("version") or "").strip() or VERSION
    return _cached_version


def _bump_patch(number):
    """2.1.1 → 2.1.2。从后往前找到第一段纯数字加一。"""
    pieces = str(number).split(".")
    for index in range(len(pieces) - 1, -1, -1):
        if pieces[index].isdigit():
            pieces[index] = str(int(pieces[index]) + 1)
            break
    return ".".join(pieces)


def version():
    """对外报告的版本号。Dev 包报下一个补丁号，正式包就是正式号。"""
    base = release_version()
    return _bump_patch(base) if DEV else base


def display():
    """标题栏用的版本串（不带 v 前缀）。

    正式版就是 "2.1.1"；Dev 版是 "2.1.2 (Dev Build 1)"——用户一眼能看出
    自己跑的是测试包，报问题时也能说清是第几个测试包。
    """
    if DEV:
        return f"{version()} (Dev Build {DEV_BUILD})"
    return version()


def build():
    """build 号。打包的读固化值，源码的现问 git，都没有就是 dev。"""
    global _cached
    if _cached is None:
        _cached = _from_file() or from_git() or "dev"
    return _cached


def full():
    """给界面和日志用的完整版本串。Dev 包把两种号都带上：Dev Build 号给
    用户对话用，git build 号给排查定位用。"""
    if DEV:
        return f"v{version()} (Dev Build {DEV_BUILD}; build {build()})"
    return f"v{version()} (build {build()})"


def freeze(target_dir, source_dir=None):
    """打包时把版本号和当前 git 状态写进 target_dir/buildinfo.json。

    给 gui.spec 调用。版本号优先取环境变量 CAN_VERSION——CI 推到 main
    之后会算出下一个补丁号（v2.1.xxx 里的 xxx）并从那里传进来；本地打包没设
    这个变量，就用 VERSION。

    写不出来也不让打包失败——没有 build 号的包仍然是能用的，为了一行版本号让
    CI 挂掉不值得，只是会退回显示 "dev"。
    """
    value = from_git(cwd=source_dir or target_dir) or "dev"
    number = (os.environ.get(VERSION_ENV) or "").strip() or VERSION
    path = os.path.join(target_dir, BUILDINFO_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": number, "build": value}, f,
                      ensure_ascii=False)
    except Exception as e:
        print(f"警告: 写不了 {path}（build 号会显示成 dev）: {e}")
        return None
    print(f"版本 {number}，build 号 {value}")
    return path


if __name__ == "__main__":
    print(full())
