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
    match = re.match(r"^(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(MPS|KT)$", token.strip())
    if not match:
        return ""
    direction, speed, gust, unit = match.groups()

    if direction == "VRB":
        head = "风向不定"
    elif direction == "000" and speed == "00":
        return "静风"
    else:
        head = f"风 {spell(direction)} 度"

    measure = "米每秒" if unit == "MPS" else "海里每小时"
    parts = [f"{head} {spell_count(int(speed))} {measure}"]
    if gust:
        parts.append(f"阵风 {spell_count(int(gust))} {measure}")
    return " ".join(parts)


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


def _temperature(text):
    """25 / M03"""
    text = (text or "").strip().upper()
    if not text:
        return ""
    negative = text.startswith("M") or text.startswith("-")
    digits = text.lstrip("M-")
    if not digits.isdigit():
        return ""
    value = int(digits)
    return ("零下 " if negative else "") + spell_count(value)


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
    parts = []
    if facility:
        parts.append(f"{facility} 通播")
    if letter:
        parts.append(f"{letter} 号")
    if metar is None:
        return " ".join(parts)

    time_text = _observation_time(metar)
    if time_text:
        parts.append(time_text)
    if runway:
        parts.append(f"使用跑道 {runway}")

    for value in (_wind(_text_of(metar, "wind")),
                  _visibility(_text_of(metar, "visibility")),
                  _weather(_text_of(metar, "present_weather")),
                  _clouds(_text_of(metar, "clouds"))):
        if value:
            parts.append(value)

    temperature = _temperature(_text_of(metar, "temperature"))
    dew_point = _temperature(_text_of(metar, "dew_point"))
    if temperature:
        parts.append(f"温度 {temperature}")
    if dew_point:
        parts.append(f"露点 {dew_point}")

    pressure = _pressure(_text_of(metar, "pressure"))
    if pressure:
        parts.append(pressure)

    if extra:
        parts.append(extra)
    if letter:
        parts.append(f"通播 {letter} 号 完毕")
    return " ".join(p for p in parts if p)


def _text_of(metar, name):
    element = getattr(metar, name, None)
    return getattr(element, "text", "") if element is not None else ""


def _observation_time(metar):
    """251300Z → 幺三洞洞 时"""
    text = _text_of(metar, "observation_time")
    match = re.match(r"^\d{2}(\d{4})Z$", (text or "").strip().upper())
    if not match:
        return ""
    return f"{spell(match.group(1))} 时"
