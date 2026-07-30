"""把他机插件装进 X-Plane。

只有 XPC 需要这个：MSFS 那边靠 SimConnect 直接建 AI 飞机，一个插件都不用装。

**X-Plane 的安装目录只能另找来源。** UDP 那条链路给不出来——BECN 信标里只有
地址和端口，客户端连模拟器装在哪个盘都不知道。所以这里两条路：先读 X-Plane
安装器自己写的那份记录（`x-plane_install_12.txt`），读不到就让用户自己选目录。
自动探测是省事用的，**选目录那条路必须一直留着**：那份记录的位置在各平台上
不一样，用绿色版或者搬过目录的人根本没有它。

**这里不装 XPPython3。** 那是编译出来的二进制，版本还跟模拟器绑（X-Plane 12
要 v4.x，11.52 要 v3.1.5——v4 是拿 SDK 420 编的，装到 XP11 上是静默不工作）。
替用户下载解压第三方二进制是另一个风险级别。这里只**检测**它在不在，不在就
让界面把话说清楚，链接给出去。

**新旧用文件内容比，不用版本号。** 插件是个平铺的源文件，跑在 X-Plane 自己的
Python 里，import 不到 version.py，写个版本常量就得手动维护、迟早忘。内容一样
就是最新，不一样就该更新——顺带把协议号变化也覆盖了。

协议号那件事值得单独说：`bridge.py` 和插件各存一份 `PROTOCOL_VERSION`，对不上
时插件**静默丢掉每一帧**（收到就 return，不记日志），症状是"他机一架都不出现"
而两边日志都干干净净。这是最难自查的一类故障，所以 `inspect()` 把装好的那份的
协议号也解析出来单独回报，而不是只说一句"版本旧"。
"""

import hashlib
import logging
import os
import re
import shutil
import sys

import bridge

log = logging.getLogger("xpinstall")

PLUGIN_NAME = "PI_XpcTraffic.py"

# X-Plane 根目录下的几个位置
PLUGINS_DIR = os.path.join("Resources", "plugins")
PYTHON_PLUGINS_DIR = os.path.join("Resources", "plugins", "PythonPlugins")
XPPYTHON3_DIR = os.path.join("Resources", "plugins", "XPPython3")

# inspect() 的结果
NO_ROOT = "no_root"             # 没找到 X-Plane 目录
NOT_XPLANE = "not_xplane"       # 指的那个目录不像 X-Plane
MISSING = "missing"             # 没装过
OUTDATED = "outdated"           # 装过，但和随包这份不一样
CURRENT = "current"             # 已是最新


def _install_records():
    """X-Plane 安装器写的那份记录可能在哪。

    每行一个安装目录。位置各平台不同，而且是**尽力而为**——这几条要在真机上
    核过才能当准，核不到也没关系，界面上永远有"自己选目录"。12 排在 11 前面：
    两个都装着的时候，插件更可能是给 12 用的。
    """
    names = ("x-plane_install_12.txt", "x-plane_install_11.txt")
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local")
        roots = [base]
    elif sys.platform == "darwin":
        roots = [os.path.join(home, "Library", "Preferences")]
    else:
        roots = [os.path.join(home, ".x-plane")]
    return [os.path.join(root, name) for root in roots for name in names]


def is_xplane_root(path):
    """这个目录看着像不像 X-Plane 装的地方。

    认 `Resources/plugins`：那是每个 X-Plane 安装都有的，而 `PythonPlugins`
    不一定（没装 XPPython3 就没有），拿后者判断会把好目录判成坏的。
    """
    if not path:
        return False
    return os.path.isdir(os.path.join(path, PLUGINS_DIR))


def find_installs():
    """从安装记录里读出 X-Plane 目录，按记录顺序去重，只留还在的。"""
    found = []
    for record in _install_records():
        try:
            if not os.path.isfile(record):
                continue
            with open(record, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            log.debug("could not read %s: %s", record, e)
            continue
        for line in lines:
            path = line.strip()
            if path and path not in found and is_xplane_root(path):
                found.append(path)
        log.info("read %d X-Plane install path(s) from %s", len(found), record)
    return found


def bundled_plugin():
    """随包带的那份插件源文件在哪。

    打包之后当前目录是用户双击时所在的目录，不是程序目录，用相对路径取不到
    ——PyInstaller 把 datas 解到 sys._MEIPASS，gui.spec 里它落在 plugin/ 下。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "plugin", PLUGIN_NAME)


def has_xppython3(root):
    return os.path.isdir(os.path.join(root, XPPYTHON3_DIR))


def plugin_path(root):
    return os.path.join(root, PYTHON_PLUGINS_DIR, PLUGIN_NAME)


def _digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError as e:
        log.debug("could not read %s: %s", path, e)
        return None


def protocol_version(path):
    """从一份插件源文件里把 PROTOCOL_VERSION 抠出来，抠不到返回 None。

    用正则而不是 import：那个文件在 X-Plane 之外 import 得进来（它守着自己的
    `import xp`），但为了读一个常量去执行它没有必要，何况用户手上那份可能是
    改过的、甚至是坏的。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    match = re.search(r"^PROTOCOL_VERSION\s*=\s*(\d+)", text, re.MULTILINE)
    return int(match.group(1)) if match else None


class Status:
    """一次探测的结果。界面直接照着它画。"""

    def __init__(self, root="", state=NO_ROOT, xppython3=False,
                 installed_protocol=None, path=""):
        self.root = root
        self.state = state
        self.xppython3 = xppython3
        self.installed_protocol = installed_protocol
        self.bundled_protocol = bridge.PROTOCOL_VERSION
        self.path = path

    @property
    def can_install(self):
        return self.state in (MISSING, OUTDATED, CURRENT)

    @property
    def protocol_mismatch(self):
        """装好的那份和客户端说的不是同一种话。

        这一条要单独报：协议对不上时插件是**静默**丢帧的，用户看到的是"他机
        一架都不出现"，日志里两边都干干净净。
        """
        return (self.installed_protocol is not None
                and self.installed_protocol != self.bundled_protocol)


def inspect(root=""):
    """看一眼现状。`root` 留空就自动探测。"""
    if not root:
        installs = find_installs()
        root = installs[0] if installs else ""
    if not root:
        return Status(state=NO_ROOT)
    if not is_xplane_root(root):
        return Status(root=root, state=NOT_XPLANE)

    target = plugin_path(root)
    status = Status(root=root, state=MISSING, xppython3=has_xppython3(root),
                    path=target)
    if not os.path.isfile(target):
        return status
    status.installed_protocol = protocol_version(target)
    source = bundled_plugin()
    mine, theirs = _digest(source), _digest(target)
    # 源文件读不到（打包漏了 datas）时不要报"最新"——那会让用户以为装好了
    status.state = CURRENT if (mine and mine == theirs) else OUTDATED
    return status


def install(root):
    """把插件复制过去，返回落地的路径。

    出错一律抛 OSError，让界面把 X-Plane 装在 Program Files 里这类权限问题
    原样说给用户听——自己吞掉的话，界面只能说一句"失败了"。
    """
    source = bundled_plugin()
    if not os.path.isfile(source):
        raise OSError("the bundled plugin is missing: %s" % source)
    target_dir = os.path.join(root, PYTHON_PLUGINS_DIR)
    # 装了 XPPython3 也不一定已经有这个目录，它是第一次用时才建的
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, PLUGIN_NAME)
    shutil.copyfile(source, target)
    log.info("installed the traffic plugin into %s", target)
    return target
