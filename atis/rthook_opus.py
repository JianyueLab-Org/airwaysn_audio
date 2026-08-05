"""PyInstaller 运行时钩子：让 macOS 上的 opuslib 找得到随包带的 libopus。

**没有这个钩子，打出来的 .app 在任何一台没装 Homebrew 的 Mac 上一启动就死**，
报的还是那句熟悉的

    Exception: Could not find Opus library. Make sure it is installed.

而 libopus.dylib 明明就在包里。原因是两层的：

- `opuslib/api/__init__.py` 是 `find_library('opus')`，返回 None 就直接 raise，
  连 `ctypes.CDLL` 那一步都到不了——所以 PyInstaller 那个改写 CDLL 的 ctypes
  钩子在这里救不了场。
- macOS 上 `ctypes.util.find_library` **不看程序自己的目录**。它只找
  DYLD_LIBRARY_PATH、`@executable_path/../lib`，再就是
  `~/lib` `/usr/local/lib` `/lib` `/usr/lib` 这一串老路径。Windows 上把
  opus.dll 放进 `_internal/` 就能被找到，macOS 上放进 Contents/Frameworks 是
  找不到的——同样的做法，两个平台不是一回事。

这里之所以能在打包机上蒙混过关，是因为 **Homebrew 给自己那份 Python 打过补丁**，
往 `ctypes.macholib.dyld.DEFAULT_LIBRARY_FALLBACK` 里加了 `/opt/homebrew/lib`。
那份被改过的 dyld.py 会跟着一起打进包里，于是包在开发机上一切正常，到了用户
那台没有 /opt/homebrew 的 Mac 上就崩——最容易漏掉的那种故障。

做法是把 `sys._MEIPASS` 塞进 `DYLD_LIBRARY_PATH`。`find_library` 是**调用时**
现读 `os.environ` 的（ctypes.macholib.dyld 的 dyld_override_search），所以进程
起来之后再设也有效，不需要真的影响 dyld 本身。

**必须是运行时钩子，不能写在 mumblecompat 里。** `import pymumble_py3` 会一路
连带 import opuslib，而 controller/voice.py 里 pymumble 的 import 排在
`import mumblecompat` 前面——等 mumblecompat 跑起来，opus 早就已经抛过了。
运行时钩子跑在所有应用代码之前，是唯一保证来得及的地方。

从源码跑不经过这里，也不需要：那种情况下 opus 本来就得自己装
（brew install opus），装了就在搜索路径上。
"""

import os
import sys

if sys.platform == "darwin":
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        existing = os.environ.get("DYLD_LIBRARY_PATH", "")
        # 放在最前面：包里带的那一份才是和这个包一起测过的。后面接上原来的值，
        # 不覆盖——用户可能有意用 DYLD_LIBRARY_PATH 指了别的东西。
        parts = [bundle_dir] + [p for p in existing.split(os.pathsep) if p]
        os.environ["DYLD_LIBRARY_PATH"] = os.pathsep.join(parts)
