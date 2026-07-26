"""ATIS 模板渲染。

变量名和写法照搬 vATIS（vatis.app/docs/client/atis-configuration/presets）：
模板里写 [WIND]、[CLOUDS] 这类占位符，生成时替换成实际数据。变量名后面加
:VOX 表示取语音形态而不是文本形态——文字通播要紧凑（照抄电码），语音要能听懂。

    模板   "[FACILITY] ATIS [ATIS_LETTER] [OBS_TIME]. [WX]. [ARPT_COND]"
    文字   "ZSPD ATIS A 251300Z. 09004MPS 9999 FEW030 25/18 Q1013. 跑道 35L"
    语音   同一份模板渲染两遍，气象要素取 voice 形态

同一个模板渲染两次：一次给文字通播（text），一次给语音（voice）。没有显式写
:VOX 的变量，在语音那一遍里也会自动取 voice 形态——否则每个模板都得写满
:VOX，太啰嗦。
"""

import re

VARIABLE = re.compile(r'\[([A-Z_]+)(:VOX)?\]')
# 缩略语在模板里写成 @变量名，和 vATIS 一致
CONTRACTION = re.compile(r'@([A-Za-z_][A-Za-z0-9_]*)')

# 变量名 → 取值函数。一个含义有多个别名的，vATIS 里也是这样
ALIASES = {
    "FACILITY": "facility",
    "ARPT": "facility",
    "ATIS_CODE": "letter",
    "ATIS_LETTER": "letter",
    "LETTER": "letter",
    "ID": "letter",
    "WX": "full_wx",
    "FULL_WX_STRING": "full_wx",
    "OBS_TIME": "observation_time",
    "TIME": "observation_time",
    "WIND": "wind",
    "SURFACE_WIND": "wind",
    "RVR": "rvr",
    "VIS": "visibility",
    "PREVAILING_VISIBILITY": "visibility",
    "PRESENT_WX": "present_weather",
    "PRESENT_WEATHER": "present_weather",
    "CLOUDS": "clouds",
    "TEMP": "temperature",
    "DEW": "dew_point",
    "PRESSURE": "pressure",
    "TREND": "trend",
    "RECENT_WX": "recent_weather",
    "ARPT_COND": "airport_conditions",
    "NOTAMS": "notams",
    "TL": "transition_level",
    "CLOSING": "closing",
}

DEFAULT_TEMPLATE = (
    "[FACILITY] ATIS [ATIS_LETTER] [OBS_TIME]. "
    "[WX]. "
    "[ARPT_COND] [NOTAMS] "
    "[CLOSING]"
)

DEFAULT_CLOSING = "advise on initial contact you have information [ATIS_LETTER]"


def build_context(metar, facility="", letter="A", airport_conditions="",
                  notams="", transition_level="", closing=None):
    """把 METAR 和席位上的自由文本组装成变量表。

    值统一是 (text, voice) 二元组；自由文本两种形态相同。
    """
    from metar import Element

    def free(value):
        value = (value or "").strip()
        return Element(value, value)

    closing_text = DEFAULT_CLOSING if closing is None else closing
    context = {
        "facility": free(facility or metar.station),
        "letter": Element(letter, letter),
        "full_wx": metar.full_wx(),
        "observation_time": metar.observation_time,
        "wind": metar.wind,
        "rvr": metar.rvr,
        "visibility": metar.visibility,
        "present_weather": metar.present_weather,
        "clouds": metar.clouds,
        "temperature": metar.temperature,
        "dew_point": metar.dew_point,
        "pressure": metar.pressure,
        "trend": metar.trend,
        "recent_weather": metar.recent_weather,
        "airport_conditions": free(airport_conditions),
        "notams": free(notams),
        "transition_level": free(transition_level),
    }
    # 收尾语本身还能引用 [ATIS_LETTER]，所以要先渲染一遍
    closing_rendered = _substitute(closing_text, context, voice=False)
    context["closing"] = free(closing_rendered)
    return context


def _substitute(template, context, voice):
    def replace(match):
        name, vox = match.groups()
        key = ALIASES.get(name)
        if key is None:
            return match.group(0)        # 认不出的变量原样留着，方便发现拼错
        element = context.get(key)
        if element is None:
            return ""
        # 语音那一遍默认就取 voice 形态；文字那一遍只有显式写了 :VOX 才取
        return element.voice if (voice or vox) else element.text

    return VARIABLE.sub(replace, template or "")


def tidy(text):
    """收拾掉变量为空留下的多余空格和标点。"""
    text = re.sub(r'\s+', ' ', text or '').strip()
    text = re.sub(r'\s+([.,。，])', r'\1', text)
    text = re.sub(r'([.,。，])\1+', r'\1', text)
    text = re.sub(r'^[.,。，]\s*', '', text)
    return text.strip()


def _expand_contractions(text, contractions, voice):
    """把 @变量名 换成缩略语的文字或语音形态。

    认不出的 @xxx 原样留着——多半是拼错了，留着才看得见。
    """
    if not contractions:
        return text

    def replace(match):
        entry = contractions.get(match.group(1))
        if entry is None:
            return match.group(0)
        written, spoken = entry
        return (spoken or written) if voice else (written or spoken)

    return CONTRACTION.sub(replace, text)


def render(template, context, contractions=None):
    """渲染模板，返回（文字通播, 语音稿）。"""
    text = _expand_contractions(
        _substitute(template, context, voice=False), contractions, voice=False)
    voice = _expand_contractions(
        _substitute(template, context, voice=True), contractions, voice=True)
    return tidy(text), tidy(voice)


def unknown_variables(template):
    """模板里认不出来的变量，配置界面用来提示拼写错误。"""
    return sorted({name for name, _ in VARIABLE.findall(template or "")
                   if name not in ALIASES})
