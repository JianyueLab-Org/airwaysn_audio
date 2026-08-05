"""日志的测试。

    python -m unittest test_applog -v

日志本身坏了是最难发现的——正因为坏了才没有日志。所以专门钉几条：文件真的写出来
了、级别对、未捕获的异常（含线程里的）确实落盘。

会在临时目录里跑，不碰使用者真实的日志。

**换当前目录是不够的。** 日志路径现在走 apppaths：macOS 上它落在
~/Library/Application Support/ 里，跟当前目录没关系——只 chdir 的话，跑一遍这个
文件就会往使用者真实的日志里写东西（而且断言照样通过，看不出来）。
AIRWAYSN_DATA_DIR 才是那个说了算的开关。
"""

import logging
import os
import tempfile
import threading
import unittest

import applog


class LogSetupTest(unittest.TestCase):

    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.temp = tempfile.mkdtemp(prefix="applog_test_")
        os.chdir(self.temp)
        # 见模块开头：光 chdir 挡不住 macOS 上的 Application Support
        self.previous_data_dir = os.environ.get(applog.apppaths.ENV_OVERRIDE)
        os.environ[applog.apppaths.ENV_OVERRIDE] = self.temp
        self.addCleanup(self.restore)

    def restore(self):
        logging.shutdown()
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
        os.chdir(self.previous_cwd)
        os.environ.pop(applog.apppaths.ENV_OVERRIDE, None)
        if self.previous_data_dir is not None:
            os.environ[applog.apppaths.ENV_OVERRIDE] = self.previous_data_dir

    def read_log(self, path):
        for handler in logging.getLogger().handlers:
            handler.flush()
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_creates_the_log_file(self):
        path = applog.setup(debug=False)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.basename(path), applog.LOG_NAME)

    def test_info_is_written_debug_is_not(self):
        path = applog.setup(debug=False)
        log = logging.getLogger("测试")
        log.info("这条要写进去")
        log.debug("这条不该写进去")

        content = self.read_log(path)
        self.assertIn("这条要写进去", content)
        self.assertNotIn("这条不该写进去", content)

    def test_debug_mode_records_everything(self):
        path = applog.setup(debug=True)
        logging.getLogger("测试").debug("协议细节")
        self.assertIn("协议细节", self.read_log(path))

    def test_logger_name_and_level_appear(self):
        path = applog.setup(debug=False)
        logging.getLogger("语音").warning("出事了")
        content = self.read_log(path)
        self.assertIn("语音", content)
        self.assertIn("WARNING", content)

    def test_uncaught_exception_is_logged(self):
        # GUI 程序里没接住的异常本来会静默消失，界面僵在那儿而日志空空
        path = applog.setup(debug=False)
        try:
            raise ValueError("故意抛的")
        except ValueError:
            import sys
            sys.excepthook(*sys.exc_info())

        content = self.read_log(path)
        # 日志文本是英文（界面文字仍是中文），见 CLAUDE.md 的日志约定
        self.assertIn("went uncaught", content)
        self.assertIn("故意抛的", content)
        self.assertIn("ValueError", content)

    def test_thread_exception_is_logged(self):
        path = applog.setup(debug=False)

        def explode():
            raise RuntimeError("线程里炸了")

        thread = threading.Thread(target=explode, name="测试线程")
        thread.start()
        thread.join()

        content = self.read_log(path)
        self.assertIn("线程里炸了", content)
        self.assertIn("测试线程", content)

    def test_survives_an_unwritable_directory(self):
        """目录写不了也不能让程序起不来——装在 Program Files 下就是这种情况。"""
        original = applog.logging.handlers.RotatingFileHandler

        def refuse(*args, **kwargs):
            raise OSError("拒绝访问")

        applog.logging.handlers.RotatingFileHandler = refuse
        try:
            path = applog.setup(debug=False)
            self.assertIsNone(path)
            logging.getLogger("测试").info("不该抛异常")
        finally:
            applog.logging.handlers.RotatingFileHandler = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
