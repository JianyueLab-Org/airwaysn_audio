"""语音稿的收尾处理。

通播稿里有两类东西是 TTS 念不好的，而它们都来自**自由文本**（机场条件、
NOTAM）——那部分是管制员照着真实通播抄进来的，全是缩写和数字：

    ILS ZULU RWY34L APCH AND ILS ZULU RWY34R APCH. LDG RWY 34 LEFT AND
    34 RIGHT. DEP RWY 05 AND 34 RIGHT. DEP FREQ 126.0, SIMUL PARL ILS
    APCHS TO RWY34L_R ARE INPR

SAPI 会把 `APCH` 念成"埃屁西埃奇"、`RWY34L_R` 念成"阿威三十四艾尔下划线啊"、
`126.0` 念成"一百二十六点零"。真实通播里这些都是要展开的：approach、runway
three four left and right、one two six decimal zero。

另一类是**元素之间粘住**，比如 `broken niner thousandtemperature two five`
和 `hectopascals2992`。这类是拼接时少了分隔符，念出来是一个怪词。

这一层只动语音稿，文字通播照旧保留电码原样——文字那份是给人看的，缩写正是
它该有的样子。
"""

import re

# 航空缩写。键必须是完整单词（按词边界匹配），否则 "DEP" 会把 "DEPARTURE"
# 里的前三个字母也换掉。长的写在前面，让 APCHS 先于 APCH 命中。
ABBREVIATIONS = [
    ("APCHS", "approaches"),
    ("APCH", "approach"),
    ("ARR", "arrival"),
    ("DEP", "departure"),
    ("DEPS", "departures"),
    ("LDG", "landing"),
    ("TKOF", "takeoff"),
    ("RWY", "runway"),
    ("RWYS", "runways"),
    ("TWY", "taxiway"),
    ("FREQ", "frequency"),
    ("SIMUL", "simultaneous"),
    ("PARL", "parallel"),
    ("INPR", "in progress"),
    ("INOP", "inoperative"),
    ("UNAVBL", "unavailable"),
    ("AVBL", "available"),
    ("CLSD", "closed"),
    ("TFC", "traffic"),
    ("ACFT", "aircraft"),
    ("CTC", "contact"),
    ("EXP", "expect"),
    ("MAINT", "maintenance"),
    ("CONST", "construction"),
    ("WIP", "work in progress"),
    ("BTN", "between"),
    ("TWR", "tower"),
    ("GND", "ground"),
    ("APP", "approach"),
    ("DME", "D M E"),
    ("VOR", "V O R"),
    ("NDB", "N D B"),
    ("ILS", "I L S"),
    ("RVR", "R V R"),
    ("CAT", "category"),
    ("MET", "met"),
    ("SFC", "surface"),
    ("BLW", "below"),
    ("ABV", "above"),
    ("TRL", "transition level"),
    ("TA", "transition altitude"),
    ("QNH", "Q N H"),
    ("STP", "standard time procedure"),
    # 这两个不展开的话 TTS 会当成单词念（"atk"、"arnav"）。放在最后，前面的
    # 规则都跑完了才轮到它们，不会被 CAT / TA 这些短缩写切到。
    ("ATC", "A T C"),
    ("RNAV", "R NAV"),
]

DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "niner",
}

# 跑道后缀。L/R/C 在跑道号后面读成方位词，别的地方不动。
RUNWAY_SIDES = {"L": "left", "R": "right", "C": "center"}


def spell_digits(text):
    """逐位念。通播里的数字基本都这么念。"""
    return " ".join(DIGITS.get(c, c) for c in str(text))


def _runway(match):
    """RWY34L / 34L_R / 05 —— 跑道号逐位念，L/R/C 念成方位。

    下划线是日本通播里"和"的写法：RWY34L_R 是 34 左和 34 右。
    """
    number = match.group("number")
    sides = match.group("sides") or ""
    spoken = spell_digits(number)
    words = [RUNWAY_SIDES[c] for c in sides.upper() if c in RUNWAY_SIDES]
    if words:
        spoken += " " + " and ".join(words)
    return spoken


def _frequency(match):
    """126.0 → one two six decimal zero"""
    whole, fraction = match.group(1), match.group(2)
    return f"{spell_digits(whole)} decimal {spell_digits(fraction)}"


def expand_free_text(text):
    """把自由文本里的缩写和数字展开成能念的形式。

    顺序有讲究：先处理跑道（它含数字和字母），再处理剩下的小数和整数，最后
    才展开纯字母缩写——反过来的话 RWY 已经变成 runway，跑道号就认不出来了。
    """
    if not text:
        return ""

    result = text

    # 跑道：RWY34L_R / RWY 34 LEFT 里的号码 / 单独的 34L_R
    result = re.sub(
        r"\bRWY\s*(?P<number>\d{2})(?P<sides>(?:[LRC](?:_[LRC])*)?)\b",
        lambda m: "RWY " + _runway(m), result, flags=re.IGNORECASE)
    result = re.sub(
        r"(?<=\bRWY )(?P<number>\d{2})(?P<sides>(?:[LRC](?:_[LRC])*)?)\b",
        _runway, result, flags=re.IGNORECASE)
    # 列表里的跑道：ARR RWY 16L, 17R, DEP RWY 16R, 17L —— 第二个之后不再紧跟
    # RWY，上面那条带后顾断言的规则够不着，实测 17R 原样留下，TTS 念成"十七阿"。
    # 真实通播的进离场跑道全是这种列表写法。带方位字母的两位数在通播自由文本里
    # 只可能是跑道号，所以单独成一条；不带字母的两位数不管（那可能是别的数）。
    result = re.sub(
        r"\b(?P<number>\d{2})(?P<sides>[LRC](?:_[LRC])*)\b",
        _runway, result, flags=re.IGNORECASE)

    # 频率一类的小数
    result = re.sub(r"\b(\d{2,3})\.(\d{1,3})\b", _frequency, result)

    # 展开缩写。词边界匹配，避免切到别的单词里
    for short, long in ABBREVIATIONS:
        result = re.sub(rf"\b{short}\b", long, result, flags=re.IGNORECASE)

    # 剩下的整数逐位念。四位以上多半是年份或编号，也一样逐位。
    result = re.sub(r"\b\d+\b", lambda m: spell_digits(m.group()), result)

    # 下划线和斜杠念不出来，换成词
    result = result.replace("_", " and ").replace("/", " ")
    return _tidy(result)


def _tidy(text):
    """收拾空白和标点，让 TTS 断句正常。"""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.])", r"\1", text)      # 标点前不要空格
    text = re.sub(r"([,.])(?=\S)", r"\1 ", text)  # 标点后要有空格
    text = re.sub(r"\.\s*\.+", ".", text)         # 连续句点
    text = re.sub(r",\s*,+", ",", text)
    return text.strip()


def join_elements(parts):
    """把各段拼起来，保证段与段之间断得开。

    `broken niner thousandtemperature two five` 就是这一步少了分隔符——两段
    直接首尾相接，TTS 当成一个词念。
    """
    cleaned = [p.strip().strip(",") for p in parts if p and p.strip()]
    return _tidy(", ".join(cleaned))


# 通播里每一段的起始词。上游少了分隔符时，这些词会和前一段的末尾粘成一个
# 怪词（真实案例：`broken niner thousandtemperature two five`）。全小写的粘连
# 没有任何模式能识别，只能靠"我知道通播里有哪些段"来切。
PHRASE_STARTS = [
    "temperature", "dew point", "dewpoint", "wind", "visibility",
    "altimeter", "QNH", "clouds", "few", "scattered", "broken", "overcast",
    "vertical visibility", "runway", "transition level", "remarks",
    "advise you have information",
]


def polish(voice):
    """语音稿的最后一道：补上缺失的分隔，去掉念不出来的符号。

    这一层是防御性的——就算上游拼错了，也不该让用户听到一个怪词。
    """
    if not voice:
        return ""

    text = voice
    # 已知的段起始词粘在前一个词尾巴上时，切开
    for word in PHRASE_STARTS:
        text = re.sub(rf"(?<=[a-z]){re.escape(word)}\b", f", {word}",
                      text, flags=re.IGNORECASE)

    # 字母紧跟数字（hectopascals2992）：切开，并且这种数字都是逐位念的
    text = re.sub(r"(?<=[a-z])(\d+)",
                  lambda m: ", " + spell_digits(m.group(1)), text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
    # 小写字母后面直接跟大写字母（thousandTemperature）也是粘住了
    text = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", ", ", text)
    text = text.replace("_", " ").replace("/", " ")
    return _tidy(text)
