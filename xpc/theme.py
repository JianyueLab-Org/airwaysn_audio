"""四个客户端共用的外观：配色、字体、Fluent 主题。

controller / atis / xpc / msfs 各存一份**逐字节相同**的副本，和 voice.py、ptt.py
一样——这个仓库靠复制共享代码，不靠 import。

为什么要单独成文件
------------------
颜色本来写在各自的 gui.py 顶上，于是同一个"绿"在仓库里有三种写法：controller 和
atis 用 TrackAudio 那套（#28a745），xpc / msfs 用另一套（#2ecc71）。四个客户端多半
同时开着，三种绿摆在一起看得出来是拼的。更实际的问题是 settings.py 拿不到这些
常量——它不能 import gui.py（gui.py 反过来 import 它，会绕成环），所以设置对话框
里的颜色只能再手写一遍，改主题时必然漏。

**颜色只在这里定义**。哪个 gui.py 里再出现一串 #rrggbb，就是又开始跑偏了。

三态用同一套语义色
------------------
取自 TrackAudio（src/renderer/src/style/variables.scss）。关键不是具体色值，而是
RX / TX / XC 三个开关共用一套三态色，而不是每个开关一个颜色：

    关 = 蓝灰      开 = 绿      正在响 = 琥珀

"正在响"是**色相**变化，不是把绿色调亮——管制员盯着雷达时，余光对色相远比对亮度
敏感。
"""

from qfluentwidgets import Theme, setTheme, setThemeColor

# ---------- 语义色 ----------
OFF_COLOR = "#436384"       # $primary：开关关着
ON_COLOR = "#28a745"        # $success：开着 / 已连接 / 正在播出
ACTIVE_COLOR = "#c7861d"    # $warning：此刻正在收发 / 正在重连
MUTED_COLOR = "#dc3545"     # $danger：静音 / 出错 / 已断开
THEME_COLOR = "#5eb1bf"     # $alias：强调色（选中的主频率、主按钮）
IDLE_COLOR = "#8b90a4"      # 次要文字
TEXT_COLOR = "#e6e8f0"      # 主要文字

# 连接状态的两个别名。语义上是"在线/离线"而不是"开/错"，分开命名是为了让
# gui.py 里那些 setStyleSheet 读起来还是人话。
ONLINE_COLOR = ON_COLOR
OFFLINE_COLOR = MUTED_COLOR
WARN_COLOR = ACTIVE_COLOR

# ---------- 底色 ----------
WINDOW_BG = "#2c2f45"       # $bg-color：偏蓝紫的深底，不是中性灰
SURFACE_BG = "#252839"      # 卡片底

# ---------- 字体 ----------
# 频率和呼号用等宽：一屏十几个频率时数字能对齐，扫视快得多。整个界面都用等宽
# 会让中文很难看，所以只给这几处用。Consolas 是 Windows 自带的，装机即有。
MONO_FONT = "Consolas"


def apply_theme():
    """深色 Fluent。**必须在建任何窗口之前调用**——qfluentwidgets 的控件是在
    构造时读主题的，建完窗口再切，已经建好的那些控件不会跟着变。
    """
    setTheme(Theme.DARK)
    setThemeColor(THEME_COLOR)


def window_qss():
    """QMainWindow 上那几个 qfluentwidgets 管不到的部件。

    它换的是**它自己那套**控件；Qt 原生的 QMenuBar / QStatusBar 不在其中，保持
    默认就是深色窗口顶上压一条亮白的菜单栏、底下再来一条——远看像是主题只刷了
    一半。这几行把它们拉回同一套配色。
    """
    return (
        f"QMainWindow {{ background-color: {WINDOW_BG}; }}"
        f"QMenuBar {{ background-color: {WINDOW_BG}; color: {TEXT_COLOR};"
        f"           border: none; }}"
        f"QMenuBar::item:selected {{ background-color: {SURFACE_BG}; }}"
        f"QMenu {{ background-color: {SURFACE_BG}; color: {TEXT_COLOR};"
        f"        border: 1px solid {OFF_COLOR}; }}"
        f"QMenu::item:selected {{ background-color: {OFF_COLOR}; }}"
        f"QStatusBar {{ background-color: {WINDOW_BG}; color: {IDLE_COLOR}; }}"
    )


def dialog_qss():
    """给 QDialog 用的底色。

    qfluentwidgets 没有自己的对话框基类可用在这里（它那套 MessageBoxBase 要一个
    遮罩父窗口），所以设置对话框还是 QDialog。QDialog 不吃 Fluent 主题，不铺一层
    底色的话，深色界面上会弹出一个纯白的框。
    """
    return ("QDialog { background-color: %s; }"
            "QLabel { color: %s; }" % (WINDOW_BG, TEXT_COLOR))
