"""GUI 冒烟测试：离屏把窗口和电台栈都建起来，不连任何服务器。

    python smoke_gui.py        （在 controller 目录下运行）

只验证"控件能不能建出来、信号能不能连上、槽函数会不会炸"，
连接、音频、发话都不在这里测。
"""

import os
import sys
import time
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
            staticmethod(lambda *args, _n=_name: _dialogs.append((_n, args[2] if len(args) > 2 else ""))))


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

    print("主窗口：")
    window = gui.ControllerWindow()
    check("建立主窗口", lambda: window)
    check("切到电台栈页", lambda: window.pages.setCurrentIndex(1))

    print("电台栈：")

    def add_two():
        window.freq_input.setText("118.000")
        window.callsign_input.setText("zspd_twr")
        window.add_radio()
        window.freq_input.setText("121.700")
        window.add_radio()
        assert len(window.stack) == 2, "应该有两部电台"
        assert len(window.rows) == 2, "应该画出两行"
    check("添加两个频率", add_two)

    check("呼号大写并显示", lambda: (_ for _ in ()).throw(AssertionError(
        window.rows[118000].title.text()))
        if "ZSPD_TWR 118.000" not in window.rows[118000].title.text() else None)

    def reject_duplicate():
        _dialogs.clear()
        window.freq_input.setText("118.000")
        window.add_radio()
        assert len(window.stack) == 2, "重复频率不该被加进来"
        assert _dialogs, "应该给出提示"
    check("重复频率被拒并提示", reject_duplicate)

    def reject_bad_frequency():
        _dialogs.clear()
        window.freq_input.setText("八八八")
        window.add_radio()
        assert len(window.stack) == 2
        assert _dialogs, "应该给出提示"
    check("非法频率被拒并提示", reject_bad_frequency)

    check("点 RX", lambda: window.toggle_rx(118000))
    check("点 TX", lambda: window.toggle_tx(118000))
    check("点 XC", lambda: window.toggle_xc(118000))
    check("TX 连带打开 RX", lambda: (_ for _ in ()).throw(AssertionError("rx 没打开"))
          if not window.stack.get(118000).rx else None)

    check("选主频率", lambda: window.select_radio(121700))
    check("音量", lambda: window.set_volume(118000, 40))
    check("静音", lambda: window.set_muted(118000, True))

    print("运行时状态：")
    check("收到话音", lambda: window.on_voice_rx(118000, True, "CES2345"))
    check("话音结束", lambda: window.on_voice_rx(118000, False, ""))
    check("最后通话已记录", lambda: (_ for _ in ()).throw(AssertionError(
        window.rows[118000].last_rx.text()))
        if "CES2345" not in window.rows[118000].last_rx.text() else None)
    check("PTT 亮起", lambda: window.on_voice_tx(True))
    check("PTT 熄灭", lambda: window.on_voice_tx(False))
    check("连接状态回报", lambda: window.on_voice_state("online", "测试"))

    print("持久化：")
    check("电台栈已写进设置", lambda: (_ for _ in ()).throw(AssertionError(
        window.settings.radios)) if len(window.settings.radios) != 2 else None)

    def reload_stack():
        from radiostack import RadioStack
        restored = RadioStack()
        restored.load(window.settings.radios)
        assert len(restored) == 2
        assert restored.get(118000).callsign == "ZSPD_TWR"
    check("能从设置恢复", reload_stack)

    print("移除与设置：")
    check("移除一个频率", lambda: window.remove_radio(118000))
    check("行也跟着消失", lambda: (_ for _ in ()).throw(AssertionError(len(window.rows)))
          if len(window.rows) != 1 else None)

    from settings import SettingsDialog
    dialog = SettingsDialog(window.settings, window)
    check("建立设置对话框", lambda: dialog)
    check("PTT 按键字段", lambda: dialog.ptt_input.text())

    window.remove_radio(121700)
    window.ptt_listener.stop()

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
