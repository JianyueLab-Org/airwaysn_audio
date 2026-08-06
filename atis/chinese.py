"""中文通播稿。

英文那套走 vATIS 的模板变量（metar.py 里每个元素都带 text 和 voice 两种形态），
中文这边**不是逐词翻译**——民航中文通播有自己的语序和读法，所以单独渲染一遍。

数字读法和 server/ATIS/process.py 一致，全网统一：

    0 洞   1 幺   2 两   3 三   4 四   5 五   6 六   7 拐   8 八   9 九

拐和洞这些不是方言而是无线电通话规范，为的是在嘈杂信道里不会把"七"听成"一"、
"零"听成"六"。改这张表会让管制员听到的和他们受训时的不一样。

高度在中文通播里念**米**，而 METAR 的云高是百英尺，所以要换算——念英尺的中文
通播在国内是不存在的。
"""

import re

# 无线电数字读法。和 server/ATIS/process.py 保持一致。
DIGITS = {
    "0": "洞", "1": "幺", "2": "两", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "拐", "8": "八", "9": "九",
}

# 通话字母的中文读法。中文通播念的是"情报通播 朱丽叶"，不是拉丁字母 J——
# 调用方传进来的是席位上的 letter（"J"），直接拼进中文句子的话，TTS 会在一串
# 汉字中间蹦一个英文字母。
LETTERS = {
    "A": "阿尔法", "B": "布拉沃", "C": "查理", "D": "德尔塔", "E": "埃科",
    "F": "福克斯特罗", "G": "高尔夫", "H": "霍特尔", "I": "印地亚",
    "J": "朱丽叶", "K": "基洛", "L": "利马", "M": "迈克", "N": "诺文贝",
    "O": "奥斯卡", "P": "帕帕", "Q": "魁北克", "R": "罗米欧", "S": "塞拉",
    "T": "探戈", "U": "尤尼佛", "V": "维克多", "W": "威士忌", "X": "爱克斯瑞",
    "Y": "洋基", "Z": "祖鲁",
}

CLOUD_AMOUNTS = {
    "FEW": "少云", "SCT": "疏云", "BKN": "多云", "OVC": "阴天",
    "NSC": "无重要云", "NCD": "未探测到云", "SKC": "碧空", "CLR": "碧空",
}

CLOUD_TYPES = {"CB": "积雨云", "TCU": "浓积云"}

WEATHER = {
    "RA": "雨", "SN": "雪", "DZ": "毛毛雨", "SG": "米雪", "GR": "冰雹",
    "GS": "霰", "FG": "雾", "BR": "轻雾", "HZ": "霾", "FU": "烟",
    "SA": "沙", "DU": "浮尘", "PO": "尘卷风", "SQ": "飑", "FC": "漏斗云",
    "TS": "雷暴", "SH": "阵性", "FZ": "冻", "BL": "吹", "DR": "低吹",
    "MI": "浅", "BC": "散片", "PR": "部分",
}

# 云高折算。国内通播惯例是 100 英尺算 30 米（精确值 30.48），这样 FEW030 念
# "九百米"而不是"九百一十米"。
METRES_PER_HUNDRED_FEET = 30


def spell(text):
    """把数字逐位念出来。非数字原样保留。"""
    return " ".join(DIGITS.get(c, c) for c in str(text))


def spell_letter(letter):
    """情报字母念成通话字母：J → 朱丽叶。认不出的原样返回。"""
    letter = (letter or "").strip().upper()
    return LETTERS.get(letter, letter)


def spell_count(value):
    """念成整数而不是逐位。温度、米数这些用它。

    只处理 0-9999，通播里不会出现更大的数。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return spell(value)

    if number < 0:
        return "零下 " + spell_count(-number)
    if number == 0:
        return "零"

    units = ["", "十", "百", "千"]
    digits = "零一二三四五六七八九"
    text = ""
    string = str(number)
    length = len(string)
    for index, char in enumerate(string):
        digit = int(char)
        position = length - index - 1
        if digit:
            # 一十五说成十五，但一百一十五的"一百"要留
            if digit == 1 and position == 1 and index == 0:
                text += units[position]
            else:
                text += digits[digit] + units[position]
        elif not text.endswith("零") and index != length - 1:
            text += "零"
    return text.rstrip("零") or "零"


def _wind(token):
    """09004MPS / VRB02MPS / 27010G18MPS / 09004MPS 350V050"""
    if not token:
        return ""
    # metar._wind() 会把风向变化组拼在后面（"09004MPS 350V050"）。这里按空格
    # 拆开分别念——原来整串去匹配一个单段的正则，匹配不上就整组静默丢掉，
    # 带变化组的 METAR 播出来的中文通播**完全没有风**。
    pieces = token.strip().split()
    token = pieces[0]
    variation = ""
    for extra in pieces[1:]:
        varied = re.match(r"^(\d{3})V(\d{3})$", extra)
        if varied:
            variation = (f" 风向在 {spell(varied.group(1))} 度 和 "
                         f"{spell(varied.group(2))} 度 之间变化")
    match = re.match(r"^(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(MPS|KT)$", token)
    if not match:
        return ""
    direction, speed, gust, unit = match.groups()

    if direction == "VRB":
        head = "风向不定"
    elif direction == "000" and speed == "00":
        return "静风"
    else:
        # 真实通播念的是"风向 三洞洞 度，风速 拐 米每秒"，风向和风速各带自己的
        # 名头；只说"风 三洞洞 度 拐 米每秒"是听不出哪个数是什么的
        head = f"风向 {spell(direction)} 度"

    measure = "米每秒" if unit == "MPS" else "海里每小时"
    parts = [f"{head} 风速 {spell_count(int(speed))} {measure}"]
    if gust:
        parts.append(f"阵风 {spell_count(int(gust))} {measure}")
    return " ".join(parts) + variation


def _visibility(token):
    """9999 / 5000 / CAVOK"""
    if not token:
        return ""
    token = token.strip().upper()
    if token == "CAVOK":
        return "能见度 幺洞 公里 以上 云高 幺五洞洞 米 以上"
    if token == "9999":
        return "能见度 幺洞 公里 以上"
    if token.isdigit():
        metres = int(token)
        if metres >= 1000 and metres % 1000 == 0:
            return f"能见度 {spell_count(metres // 1000)} 公里"
        return f"能见度 {spell_count(metres)} 米"
    return ""


def _clouds(text):
    """FEW030 SCT100 BKN020CB —— 云高换算成米。"""
    if not text:
        return ""
    parts = []
    for token in text.split():
        token = token.strip().upper()
        if token in CLOUD_AMOUNTS:
            parts.append(CLOUD_AMOUNTS[token])
            continue
        match = re.match(r"^(FEW|SCT|BKN|OVC)(\d{3}|///)(CB|TCU)?$", token)
        if not match:
            continue
        amount, height, kind = match.groups()
        piece = CLOUD_AMOUNTS[amount]
        if height != "///":
            # METAR 是百英尺，中文通播念米。国内惯例按 100 英尺 = 30 米折算，
            # 不是精确的 30.48——真实通播念的是"九百米"这种整数，按精确值算出
            # 来的"九百一十米"听着就不像话。
            piece += f" {spell_count(int(height) * METRES_PER_HUNDRED_FEET)} 米"
        if kind:
            piece += " " + CLOUD_TYPES.get(kind, kind)
        parts.append(piece)
    return " ".join(parts)


def _weather(text):
    """-RA / +TSRA / VCSH —— 拆成强度和现象。"""
    if not text:
        return ""
    parts = []
    for token in text.split():
        token = token.strip().upper()
        prefix = ""
        if token.startswith("-"):
            prefix, token = "小", token[1:]
        elif token.startswith("+"):
            prefix, token = "大", token[1:]
        elif token.startswith("VC"):
            prefix, token = "附近有", token[2:]

        words = ""
        while token:
            for code in (token[:2],):
                if code in WEATHER:
                    words += WEATHER[code]
                    token = token[2:]
                    break
            else:
                break
        if words:
            parts.append(prefix + words)
    return " ".join(parts)


def _temperature(label, text):
    """25 → 气温 二十五 摄氏度；M03 → 露点负 三 摄氏度

    负号紧跟在名头后面（"露点负 八"），单位要念出来——真实通播就是这么播的。
    写成"露点 零下 八"也听得懂，但和实际播出的不是一个说法。
    """
    text = (text or "").strip().upper()
    if not text:
        return ""
    negative = text.startswith("M") or text.startswith("-")
    digits = text.lstrip("M-")
    if not digits.isdigit():
        return ""
    return f"{label}{'负' if negative else ''} {spell_count(int(digits))} 摄氏度"


def _pressure(text):
    """Q1013 / A2992"""
    text = (text or "").strip().upper()
    match = re.match(r"^Q(\d{3,4})$", text)
    if match:
        return f"修正海压 {spell(match.group(1))} 百帕"
    match = re.match(r"^A(\d{4})$", text)
    if match:
        value = match.group(1)
        return f"修正海压 {spell(value[:2])} 点 {spell(value[2:])} 英寸汞柱"
    return ""


def render(metar, facility="", letter="", runway="", extra=""):
    """生成一段中文通播稿。

    facility  席位名，比如"上海浦东"
    letter    情报字母，会念成对应的汉字（A 阿尔法 …）
    runway    使用跑道，比如"三六左"，调用方自己写好
    extra     额外说明，接在最后
    """
    spoken_letter = spell_letter(letter)
    parts = []
    if facility:
        parts.append(f"{facility}情报通播")
    if spoken_letter:
        parts.append(spoken_letter)
    if metar is None:
        return " ".join(parts)

    time_text = _observation_time(metar)
    if time_text:
        parts.append(time_text)
    if runway:
        # 这一格可以只填跑道号（"三六左"），也可以填一整段构型说明
        # （"跑道独立平行离场，跑道 三六左 起始高度 六百米……"）——真实通播里
        # 跑道构型就是这么一整段，而且位置在气象**之前**。后者自带"跑道"二字，
        # 再套一层"使用跑道"就成了"使用跑道 跑道独立平行离场"。
        parts.append(runway if runway.lstrip().startswith("跑道")
                     else f"使用跑道 {runway}")

    for value in (_wind(_text_of(metar, "wind")),
                  _visibility(_text_of(metar, "visibility")),
                  _weather(_text_of(metar, "present_weather")),
                  _clouds(_text_of(metar, "clouds"))):
        if value:
            parts.append(value)

    for phrase in (_temperature("气温", _text_of(metar, "temperature")),
                   _temperature("露点", _text_of(metar, "dew_point"))):
        if phrase:
            parts.append(phrase)

    pressure = _pressure(_text_of(metar, "pressure"))
    if pressure:
        parts.append(pressure)

    if extra:
        parts.append(extra)
    if spoken_letter:
        # 真实通播的收尾是让机组回报收到了哪一份，不是"完毕"
        parts.append(f"首次与管制员联络时报告你已收到通播 {spoken_letter}")
    return " ".join(p for p in parts if p)


def _text_of(metar, name):
    element = getattr(metar, name, None)
    return getattr(element, "text", "") if element is not None else ""


def _observation_time(metar):
    """251300Z → 幺三洞洞 世界协调时

    真实通播报的是"世界协调时"，只说"时"听起来像本地时间。
    """
    text = _text_of(metar, "observation_time")
    match = re.match(r"^\d{2}(\d{4})Z$", (text or "").strip().upper())
    if not match:
        return ""
    return f"{spell(match.group(1))} 世界协调时"
