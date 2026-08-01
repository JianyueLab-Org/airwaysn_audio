"""日志。

打包出来的程序是 console=False 的，print 出去的东西没有任何地方能看到——用户
报"连不上"的时候手里一点线索都没有。所以统一写到文件里：

    atis-for-can.log      当前目录，滚动保留 4 份

从源码跑的时候同时打到控制台。加 --debug 会把级别降到 DEBUG，那一档会记录
协议层面的细节（进出的频道、发出去的语音目标、收到的每个包），排查现场问题
基本够用。

还接管了未捕获异常：GUI 程序里一个没接住的异常本来会静默吃掉，界面就那么僵在
那儿，日志里什么都没有。
"""

import logging
import logging.handlers
import platform
import os
import sys

LOG_NAME = "atis-for-can.log"
# 一行大约 80 字节，1 MB 就是一万三千行。把每段收听从两行压成一行、
# 把他机的中间态降到 DEBUG 之后，同样一个文件能装下的时长翻了一倍还多，
# 所以上限从 2 MB 收到 1 MB——用户要发日志时，四个文件加起来 4 MB 比
# 8 MB 好发得多，而覆盖的时间反而更长。
MAX_BYTES = 1024 * 1024
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

    _log_path = os.path.abspath(LOG_NAME)
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

    startup = logging.getLogger("startup")
    startup.info("log level %s, file %s", logging.getLevelName(level),
                 _log_path or "(none)")
    # 一行环境。用户发上来的日志十有八九缺的就是这些：装在哪、是不是解压到临时
    # 目录跑的（见过在 360zip 的临时目录里跑的，设置每次都丢）、Python 和系统
    # 版本、以及这是打包的还是从源码跑的。问一遍要一个来回，写进去零成本。
    startup.info("%s %s | %s | cwd %s | %s",
                 platform.system(), platform.release(),
                 "packaged" if getattr(sys, "frozen", False) else "from source",
                 os.getcwd(), sys.version.split()[0])
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
    """在文件管理器里打开日志所在目录。"""
    if not _log_path:
        return False
    try:
        os.startfile(os.path.dirname(_log_path))
        return True
    except Exception as e:
        logging.getLogger("applog").warning("could not open the log folder: %s", e)
        return False
