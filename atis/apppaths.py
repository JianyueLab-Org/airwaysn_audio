"""用户数据（设置、日志）放哪儿。

和 applog.py、i18n.py 一样，每个组件各存一份——内容只差 APP_DIR 一行，但那一行
就是"这个程序的数据目录叫什么"，共享不了。

为什么需要这个模块
------------------
原来所有配置和日志都是裸文件名，相对**当前目录**解析。在 Windows 上这一直是对的：
用户解压到哪儿就在哪儿双击，当前目录就是程序目录，`atis_settings.json` 落在 exe
边上，绿色、可搬、看得见。

**在 macOS 上双击一个 .app，当前目录是 `/`。** 于是配置写不出来（根目录不可写）、
日志写不出来、每次启动都是全新的一份默认设置——而界面上一点异常都没有，用户只会
觉得"这软件记不住东西"。所以 macOS 上必须换个地方，按平台惯例是

    ~/Library/Application Support/atis-for-can/

三条规则，顺序就是优先级
------------------------
1. **`AIRWAYSN_DATA_DIR` 说了算。** 测试和冒烟脚本用它把数据目录钉在临时目录里
   ——否则在 macOS 上跑一遍 smoke_gui.py 就会读写使用者真实的 Application
   Support，跑完还给人清空。绿色版/便携安装也可以拿它把数据挪回程序边上。
2. **当前目录已经有这个文件，就用那一份。** 从源码跑（`cd atis; python
   gui.py`）、Windows 上的老安装、手工搬过来的配置，都靠这条继续有效。少了它，
   升级这一版就等于把所有人的设置清零。
3. 剩下的情况：macOS 进 Application Support，其余平台还是当前目录。

第 3 条在 Windows 和 Linux 上等价于 `os.path.abspath(name)`，也就是这个模块出现
之前的行为，一个字节都没变。**这是有意的**——这次要动的是 macOS，不该顺手把
Windows 用户的配置搬家，那种改动坏起来是静默的。
"""

import logging
import os
import sys

log = logging.getLogger("apppaths")

# 数据目录的名字。三个组件各不相同，和打出来的包同名，用户在 Finder 里能对上。
APP_DIR = "atis-for-can"

# 覆盖数据目录的环境变量。测试拿它隔离，便携安装拿它把数据搬回程序边上。
ENV_OVERRIDE = "AIRWAYSN_DATA_DIR"

_warned = False


def _default_dir():
    """按平台惯例的数据目录。"""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", APP_DIR)
    if sys.platform.startswith("win"):
        # Windows 上不换地方，见模块开头
        return os.getcwd()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, APP_DIR)


def data_dir():
    """数据目录，顺手建出来。

    建不出来就退回当前目录，并且**不抛异常**：一个存不下设置的客户端仍然能让
    管制员上席位说话，为了一个目录起不来则什么都做不了。
    """
    global _warned
    override = (os.environ.get(ENV_OVERRIDE) or "").strip()
    target = override or _default_dir()
    try:
        os.makedirs(target, exist_ok=True)
        return target
    except OSError as e:
        if not _warned:
            _warned = True
            log.warning("could not create the data directory %s, "
                        "falling back to the working directory: %s", target, e)
        return os.getcwd()


def data_file(name):
    """一个数据文件该用哪条路径。规则见模块开头。"""
    if (os.environ.get(ENV_OVERRIDE) or "").strip():
        return os.path.join(data_dir(), name)
    local = os.path.abspath(name)
    if os.path.exists(local):
        return local
    return os.path.join(data_dir(), name)
