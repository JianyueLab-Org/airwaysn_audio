"""观察员模式：双人机组里坐在右座的那位。

一架飞机在网络上只能有一个位置，副驾要的是守频和通话，不是再放一架飞机
上去。所以这个模式下客户端**根本不连 FSD**——只连语音，进和机长同一个
`FREQ_*` 频道。三个后果都是有意的：

- 网络上不会多出一架飞机，也不会有重复呼号被服务端踢掉。
- 因此**收不到管制的文字消息**：那是发给机长呼号的，服务端只投递给那一条
  连接（can-fsd `handleTextMessage` 的 `sendDirect`）。副驾听得见、说得出，
  看不到文字。这是这个模式的代价，写在这里免得以后当成 bug 查。
- 频率可以手输。副驾不一定开着模拟器——很多时候他就是个话务员——没有 COM1
  可跟的时候，手输是唯一的入口。

**手输频率只在观察员模式下存在。** 正常连着 FSD 的飞行员要是能把语音频率和
座舱里的 COM1 分开设，迟早会出现"管制以为你在 121.8、你人在别的频道"这种
事，那比听不见更糟。`frequency_for()` 里那个 `observer` 参数就是这条规矩，
非观察员传什么手输频率都不认。

**为什么不用 ATC 观察员（`#AA` + facility 0）那条路**：can-fsd 认这种登录，
但它是 `IsATC`，会进 `/v1/data.json` 的 `controllers` 数组
（`internal/api/datafeed.go` 的 `BuildDatafeed`），于是 radar.ceruleanavi.net
和每个飞行员端的"附近管制"列表里都会多出一个其实是别人副驾的席位。多收几条
频率文字换全网管制列表被污染，不值。

**两个人必须用各自的账号。** `server/login.py` 在同名再次登录时会踢掉前一条
会话，共用一个账号的结果是两个人轮流掉线。

这个模块只有纯函数，没有界面文字，日志也不写——判断在这里，副作用在 gui.py。
"""

# 航空 VHF 话音频段。上界是 136.975，因为 25 kHz 间隔的最后一格就是它。
MIN_FREQUENCY = 118.0
MAX_FREQUENCY = 136.975


def parse_frequency(text):
    """把用户输入读成 MHz，读不出来返回 None。

    认三种写法：`121.8`、`121.800`，以及六位千赫 `121800`——最后那种是 Mumble
    频道名（`FREQ_121800`）里的形式，照着抄的人不会少。

    **先量化到千赫，再判范围。** 频道名本来就只到千赫（`FREQ_136975`），所以
    `136.9754` 就是 `136.975`，多打的那一位不该让整条输入作废；而 `137.0004`
    量化成 `137.000`，照样挡在外面。
    """
    if text is None:
        return None
    if isinstance(text, bool):          # True 是 1，不是频率
        return None
    if isinstance(text, (int, float)):
        value = float(text)
    else:
        cleaned = str(text).strip().replace(" ", "")
        if not cleaned:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
        # 没有小数点又是四位以上，按千赫理解
        if "." not in cleaned and abs(value) >= 1000:
            value = value / 1000.0
    value = round(value, 3)
    if not (MIN_FREQUENCY <= value <= MAX_FREQUENCY):
        return None
    return value


def frequency_for(com1=None, com1_power=True, manual=None, observer=False):
    """语音这一刻该待在哪个频率上，没有就返回 None。

    观察员手输的频率**压过**模拟器：他多半根本没开模拟器，就算开着，手输
    也一定是刚刚才敲的。清空输入框就退回跟着 COM1 走——"空 = 跟随"省掉了
    第二个开关，也省掉了"开关和输入框谁说了算"这个必然会被问到的问题。

    非观察员一律看模拟器，`manual` 连读都不读。
    """
    if observer:
        chosen = parse_frequency(manual)
        if chosen is not None:
            return chosen
    if com1 and com1_power:
        return round(float(com1), 3)
    return None


def format_frequency(frequency):
    """存进配置、填回输入框用的写法。None 就是空字符串。"""
    value = parse_frequency(frequency)
    return f"{value:.3f}" if value is not None else ""
