"""日志。

打包出来的程序是 console=False 的，print 出去的东西没有任何地方能看到——用户
报"连不上"的时候手里一点线索都没有。所以统一写到文件里：

    atis-for-can.log       滚动保留 4 份

放在哪儿由 apppaths 决定：Windows 上还是当前目录（和以前一样，就在 exe 边上），
macOS 上是 ~/Library/Application Support/atis-for-can/——双击 .app 时当前目录是
`/`，写不进去。

从源码跑的时候同时打到控制台。加 --debug 会把级别降到 DEBUG，那一档会记录
协议层面的细节（进出的频道、发出去的语音目标、收到的每个包），排查现场问题
基本够用。

还接管了未捕获异常：GUI 程序里一个没接住的异常本来会静默吃掉，界面就那么僵在
那儿，日志里什么都没有。
"""

import logging
import logging.handlers
import os
import subprocess
import sys

import apppaths

LOG_NAME = "atis-for-can.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3

_log_path = None


def log_path():
    """日志文件位置，界面上"打开日志"用。"""
    return _log_path


def setup(debug=False):
    """配置日志，返回日志文件路径。"""
    global _log_path

    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S")

    # 不是裸的当前目录：macOS 上双击 .app 时当前目录是 `/`，日志根本写不出来，
    # 而写不出来正是最需要日志的时候。见 apppaths 的模块注释。
    _log_path = apppaths.data_file(LOG_NAME)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            _log_path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        # 目录不可写（比如装在 Program Files 下）也不能让程序起不来
        _log_path = None
        print(f"cannot write the log file: {e}", file=sys.stderr)

    # 从源码跑的时候顺便打到控制台；打包后 sys.stderr 可能是 None
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    logging.captureWarnings(True)
    _install_excepthook()

    logging.getLogger("startup").info(
        "log level %s, file %s", logging.getLevelName(level),
        _log_path or "(none)")
    return _log_path


def _install_excepthook():
    """把未捕获的异常写进日志，而不是让它悄悄消失。"""
    previous = sys.excepthook

    def handler(exc_type, exc_value, traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc_value, traceback)
            return
        logging.getLogger("uncaught").critical(
            "an exception went uncaught", exc_info=(exc_type, exc_value, traceback))
        previous(exc_type, exc_value, traceback)

    sys.excepthook = handler

    # 线程里的异常默认只打到 stderr，同样要落盘
    def thread_handler(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        logging.getLogger("uncaught").critical(
            "an exception went uncaught in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    try:
        import threading
        threading.excepthook = thread_handler
    except Exception:
        pass


def open_log_folder():
    """在文件管理器里打开日志所在目录。

    `os.startfile` **只有 Windows 有**——在 macOS 上它连属性都不存在，于是这里
    每次都走进 except、记一句警告、返回 False，而界面上那个"打开日志"按钮点下去
    毫无反应。macOS 用 `open`，Linux 用 `xdg-open`。

    日志目录在 macOS 上已经不是程序边上了（见 apppaths），所以这个按钮在那里
    比在 Windows 上更要紧：用户自己是找不到 Application Support 的。
    """
    if not _log_path:
        return False
    folder = os.path.dirname(_log_path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return True
    except Exception as e:
        logging.getLogger("applog").warning("could not open the log folder: %s", e)
        return False
