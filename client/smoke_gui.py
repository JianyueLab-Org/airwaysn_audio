"""GUI 冒烟测试：离屏把窗口都建起来，不连服务器、不连模拟器、不开音频设备。

    python smoke_gui.py        （在 client 目录下运行）

只验证"控件能不能建出来、信号能不能连上、槽函数会不会炸"。真正的频道切换和
PTT 判据在 test_radio.py 里，那些不需要 Qt。

单元测试完全碰不到 gui.py，所以这里是唯一会去动 RadioGUI / MainWindow 的地方
——包括 _switch_channel_async，它在 Qt 主线程上被调用，切换本身必须甩到后台
线程去做，否则窗口会"未响应"。
"""

import os
import sys
import threading
import time
import types
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# SimConnect 一导入就会去找模拟器；这里只要 gui.py / radio.py 能导进来
if "SimConnect" not in sys.modules:
    _sc = types.ModuleType("SimConnect")
    _sc.SimConnect = object
    _sc.AircraftRequests = object
    sys.modules["SimConnect"] = _sc

# pymumble 需要本机的 opus 原生库来编解码音频。这里不碰音频，缺库时放个替身
# 让导入过去；pymumble 本身仍然是真的。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())
    print("提示: 未找到 opus 原生库，已用替身放行（不影响本测试）\n")

from PyQt6.QtWidgets import QApplication, QMessageBox

import gui

# 弹出的提示框是模态的，离屏也一样会一直等人点。测试里把它们换成记录调用。
_dialogs = []
for _name in ("warning", "critical", "information"):
    setattr(QMessageBox, _name,
            staticmethod(lambda *args, _n=_name: _dialogs.append(
                (_n, args[2] if len(args) > 2 else ""))))


class FakeRadioClient:
    """MainWindow 只用到这几样东西，不值得为它拉起真的客户端。"""

    def __init__(self):
        self.settings = types.SimpleNamespace(
            mic_volume=100, speaker_volume=100, ptt_key="space",
            joystick_ptt=None, input_device_index=None,
            output_device_index=None, username="", password="")
        self.aq = types.SimpleNamespace(get=lambda name: 118.0)
        self._initial_freq = 118.0
        self.on_ptt_change = None
        self.on_rx_change = None
        self.on_connection_change = None
        self.switched = []              # (频率, caller, 所在线程)
        self.switch_started = threading.Event()
        self.release = threading.Event()

    def switch_channel(self, frequency, caller="unknown"):
        # 真实实现里这一步是网络往返，最坏要等满 CHANNEL_TIMEOUT
        self.switched.append((frequency, caller, threading.current_thread()))
        self.switch_started.set()
        self.release.wait(timeout=5)
        return True


def main():
    app = QApplication(sys.argv)
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            failures.append((name, e))
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    print("登录窗口：")
    window = gui.RadioGUI()
    check("建立主窗口", lambda: window)
    check("登录页字段", lambda: (window.login_window.username_input,
                                 window.login_window.password_input))
    check("显示错误", lambda: window.login_window.show_error("测试用的错误"))
    check("清掉错误", lambda: window.login_window.clear_error())

    print("主界面：")
    client = FakeRadioClient()
    main_window = gui.MainWindow(client)
    check("建立主界面", lambda: main_window)
    check("刷新频率显示", lambda: main_window.update_frequency())
    check("频率已显示", lambda: (_ for _ in ()).throw(AssertionError(
        main_window.freq_label.text()))
        if "118.000" not in main_window.freq_label.text() else None)
    check("连接指示灯亮", lambda: main_window.update_connection_status(True))
    check("连接指示灯灭", lambda: main_window.update_connection_status(False))
    check("断开时文字变红", lambda: (_ for _ in ()).throw(AssertionError(
        main_window.connection_label.text()))
        if "断开" not in main_window.connection_label.text() else None)
    check("PTT 指示灯", lambda: (main_window.update_ptt_status(True),
                                 main_window.update_ptt_status(False)))
    check("RX 指示灯", lambda: (main_window.update_rx_status(True),
                                main_window.update_rx_status(False)))

    def frequency_read_failure():
        # 模拟器没开时 aq.get 会抛，界面得退回占位符而不是崩掉
        client.aq.get = lambda name: (_ for _ in ()).throw(OSError("模拟器没开"))
        main_window.update_frequency()
        assert "-.---" in main_window.freq_label.text(), main_window.freq_label.text()
        client.aq.get = lambda name: 118.0
    check("读不到频率时退回占位符", frequency_read_failure)

    print("频道切换不能卡住 Qt 线程：")

    def switch_is_off_the_gui_thread():
        window.radio_client = client
        qt_thread = threading.current_thread()
        started = time.time()
        window._switch_channel_async(121.7, "冒烟测试")
        elapsed = time.time() - started
        # 真正的切换要等 5 秒才放行，主线程必须早就返回了
        assert elapsed < 1.0, f"_switch_channel_async 在 Qt 线程上等了 {elapsed:.1f} 秒"
        assert client.switch_started.wait(timeout=3), "后台线程没有真的去切"
        _, caller, thread = client.switched[-1]
        assert thread is not qt_thread, "切换跑在了 Qt 主线程上，窗口会未响应"
        assert caller == "冒烟测试"
        client.release.set()
    check("切换甩到后台线程", switch_is_off_the_gui_thread)

    def switch_failure_does_not_escape():
        client.switch_channel = lambda frequency, caller="": (
            _ for _ in ()).throw(RuntimeError("切换炸了"))
        window._switch_channel_async(121.7, "冒烟测试-异常")
        time.sleep(0.3)          # 后台线程里抛的异常不该弄死任何东西
    check("后台切换出错不外溢", switch_failure_does_not_escape)

    print("设置对话框：")
    from settings import SettingsDialog
    dialog = SettingsDialog(client.settings, main_window)
    check("建立设置对话框", lambda: dialog)
    check("PTT 按键字段", lambda: dialog.ptt_input.text())
    # 取消是用户真正走的收尾路径：要停掉摇杆定时器、摘掉键盘钩子
    check("取消并收尾", lambda: dialog.reject())

    main_window.timer.stop()

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
