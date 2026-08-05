"""界面尺寸换算和等宽字体的测试。

    python -m unittest test_theme -v      （在 xpc 目录下运行）

这两条都是 macOS 上真实出现过的问题，而且**冒烟测试结构上就抓不到**：离屏平台
给的默认字号恰好是 9pt，和 Windows 一样，所以离屏画出来的排版永远是"对"的，
真机上 cocoa 给的是 13pt 才会翻车。所以这里显式把字号钉住来验。
"""

import unittest

from PyQt6.QtGui import QFont, QFontInfo
from PyQt6.QtWidgets import QApplication

import theme

app = QApplication.instance() or QApplication([])


class UiScaleTest(unittest.TestCase):
    """写死的像素值要跟着界面字号走。"""

    def setUp(self):
        self.previous = app.font()

    def tearDown(self):
        app.setFont(self.previous)

    def use(self, point_size):
        app.setFont(QFont("Helvetica", point_size))

    def test_the_design_size_is_unscaled(self):
        """9pt 是设计字号，也是 Windows 上 Qt 的默认——那条路一个像素都不能变。

        这一条要是坏了，Windows 用户的界面会无声地整体缩放，而这次改动的本意
        只是修 macOS。
        """
        self.use(theme.DESIGN_POINT_SIZE)
        self.assertEqual(theme.ui_scale(), 1.0)
        for value in (232, 116, 52, 26, 22, 620, 480):
            self.assertEqual(theme.px(value), value,
                             "设计字号下 px() 必须是恒等的")

    def test_a_bigger_font_scales_the_pixels(self):
        """macOS 给 13pt。不换算的话 52×26 的按钮会被 RX 两个字撑满。"""
        self.use(13)
        self.assertGreater(theme.ui_scale(), 1.4)
        self.assertGreater(theme.px(52), 52)
        # 卡片和按钮要按同一个倍数放大，否则里外对不上
        self.assertAlmostEqual(theme.px(232) / 232.0, theme.px(52) / 52.0,
                               delta=0.05)

    def test_a_smaller_font_does_not_shrink_anything(self):
        """只放大不缩小：留白比把控件挤扁好看，也不会挤掉文字。"""
        self.use(7)
        self.assertEqual(theme.ui_scale(), 1.0)
        self.assertEqual(theme.px(52), 52)

    def test_px_survives_a_pixel_sized_font(self):
        """字号用 pixelSize 设的时候 pointSize() 返回 -1，不能拿它去做除法。"""
        font = QFont("Helvetica")
        font.setPixelSize(20)
        app.setFont(font)
        self.assertEqual(theme.ui_scale(), 1.0)
        self.assertEqual(theme.px(52), 52)


class MonoFontTest(unittest.TestCase):
    """频率要用等宽的，不然一屏十几个频率的数字对不齐——那是用等宽的唯一理由。"""

    def test_it_really_resolves_to_a_fixed_pitch_font(self):
        """**这是 macOS 上真正坏掉的那一条。**

        `QFont("Consolas")` 在没有 Consolas 的机器上不会报错，只是悄悄退回默认的
        比例字体。所以这里不能只看请求了什么，要看 Qt **实际解析成**了什么。
        """
        info = QFontInfo(theme.mono_font(14))
        self.assertTrue(info.fixedPitch(),
                        f"解析成了 {info.family()}，不是等宽字体")

    def test_the_fallbacks_start_with_the_windows_one(self):
        """Consolas 排第一：Windows 上装机即有，那边的观感一个像素都不该变。"""
        self.assertEqual(theme.MONO_FALLBACKS[0], "Consolas")
        self.assertGreater(len(theme.MONO_FALLBACKS), 1,
                           "只有一个名字就等于没有后备")

    def test_size_and_weight_are_honoured(self):
        font = theme.mono_font(17, QFont.Weight.DemiBold)
        self.assertEqual(font.pointSize(), 17)
        self.assertEqual(font.weight(), QFont.Weight.DemiBold)


if __name__ == "__main__":
    unittest.main(verbosity=2)
