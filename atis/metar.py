"""METAR 解析：把电码拆成一个个气象要素。

按 vATIS 的做法，每个要素同时有两种形态：

    text   照抄电码，给文字通播看的      "09004MPS"
    voice  念出来的样子，给语音合成用的  "wind zero niner zero at four meters per second"

模板里 [WIND] 取 text、[WIND:VOX] 取 voice，两者分开是因为文字通播要紧凑、
语音要能听懂——这也是 vATIS 模板变量 :VOX 后缀的由来。

只解析 ATIS 用得上的组，认不出来的组直接跳过，不会因为一个奇怪的组就整份报文
解析失败。
"""

import re

# 数字念法。航空惯例：9 念 niner，3 念 tree（这里保留 three，中文语音更常见）
DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "niner",
}

CLOUD_COVER = {
    "FEW": "few",
    "SCT": "scattered",
    "BKN": "broken",
    "OVC": "overcast",
    "VV": "vertical visibility",
}

CLOUD_TYPE = {"CB": "cumulonimbus", "TCU": "towering cumulus"}

WEATHER_CODES = {
    "-": "light", "+": "heavy", "VC": "in the vicinity",
    "MI": "shallow", "BC": "patches of", "PR": "partial",
    "DR": "low drifting", "BL": "blowing", "SH": "showers of",
    "TS": "thunderstorm", "FZ": "freezing",
    "DZ": "drizzle", "RA": "rain", "SN": "snow", "SG": "snow grains",
    "PL": "ice pellets", "GR": "hail", "GS": "small hail",
    "BR": "mist", "FG": "fog", "FU": "smoke", "HZ": "haze",
    "DU": "dust", "SA": "sand", "SQ": "squalls", "VA": "volcanic ash",
    "PO": "dust whirls", "FC": "funnel cloud", "SS": "sandstorm", "DS": "duststorm",
}

_WIND = re.compile(r'^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS|KMH)$')
_WIND_VAR = re.compile(r'^(\d{3})V(\d{3})$')
_CLOUD = re.compile(r'^(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)?$')
_TEMP = re.compile(r'^(M?\d{2})/(M?\d{2})$')
_TIME = re.compile(r'^(\d{2})(\d{2})(\d{2})Z$')
_RVR = re.compile(r'^R(\d{2}[LCR]?)/([PM]?)(\d{4})(?:V([PM]?)(\d{4}))?(?:([UDN]))?(FT)?$')
_WEATHER = re.compile(r'^(-|\+|VC)?([A-Z]{2,8})$')
_TREND = ("NOSIG", "TEMPO", "BECMG", "RMK")


# 情报字母的通话表。和 server/ATIS/process.py 里那份一致——通播念的是
# "INFORMATION ALPHA"，直接给 TTS 一个孤零零的 "A"，SAPI 会念成"诶"。
NATO = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo",
    "F": "Foxtrot", "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliett",
    "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November", "O": "Oscar",
    "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray",
    "Y": "Yankee", "Z": "Zulu",
}


def spell_letter(letter):
    """情报字母 → 通话字母表的词。不认识的原样返回。"""
    return NATO.get(str(letter).strip().upper(), str(letter))


def spell(text):
    """把数字逐位念出来：090 → zero niner zero。非数字原样保留。"""
    return " ".join(DIGITS.get(ch, ch) for ch in str(text))


def spell_number(value):
    """带正负号的整数：-3 → minus three，25 → two five。"""
    value = int(value)
    if value < 0:
        return "minus " + spell(abs(value))
    return spell(value)


def spell_altitude(feet):
    """高度按航空习惯念：3000 → three thousand，10000 → one zero thousand。

    逐位念成 "three zero zero zero" 是听不出高度的。
    """
    feet = int(feet)
    thousands, remainder = divmod(feet, 1000)
    hundreds = remainder // 100
    parts = []
    if thousands:
        parts.append(f"{spell(thousands)} thousand")
    if hundreds:
        parts.append(f"{spell(hundreds)} hundred")
    return " ".join(parts) if parts else spell(feet)


class Element:
    """一个要素的文本形态和语音形态。"""

    def __init__(self, text="", voice=""):
        self.text = text
        self.voice = voice or text

    def __bool__(self):
        return bool(self.text or self.voice)

    def __repr__(self):
        return f"Element({self.text!r}, {self.voice!r})"


EMPTY = Element()


def _wind(token, variation):
    match = _WIND.match(token)
    if not match:
        return None
    direction, speed, gust, unit = match.groups()
    speed_value = int(speed)

    unit_voice = {"KT": "knots", "MPS": "meters per second",
                  "KMH": "kilometers per hour"}[unit]

    text = token
    if direction != "VRB" and speed_value == 0:
        voice = "wind calm"
    elif direction == "VRB":
        voice = f"wind variable {spell(speed_value)} {unit_voice}"
    else:
        # 念法照本网通播的稿子：WIND 140 DEGREES 5 METRES PER SECOND。
        # 度数和风速仍然逐位念（"one four zero"），不能交给 TTS 去读 140
        # ——那会念成 "one hundred forty"，不是航空用语。
        voice = f"wind {spell(direction)} degrees {spell(speed_value)} {unit_voice}"

    if gust:
        voice += f" gusting {spell(int(gust))} {unit_voice}"

    if variation:
        low, high = variation
        text += f" {low}V{high}"
        voice += f" variable between {spell(low)} and {spell(high)}"

    return Element(text, voice)


def _visibility(token):
    if token == "9999":
        return Element(token, "visibility one zero kilometers or more")
    if re.match(r'^\d{4}$', token):
        meters = int(token)
        if meters % 1000 == 0:
            return Element(token, f"visibility {spell(meters // 1000)} kilometers")
        return Element(token, f"visibility {spell(meters)} meters")
    if token.endswith("SM"):
        return Element(token, f"visibility {token[:-2]} statute miles")
    return None


def _rvr(token):
    match = _RVR.match(token)
    if not match:
        return None
    runway, prefix, value, _, _, trend, feet = match.groups()
    unit = "feet" if feet else "meters"
    voice = f"runway {spell(runway)} visual range"
    if prefix == "P":
        voice += " more than"
    elif prefix == "M":
        voice += " less than"
    voice += f" {spell(int(value))} {unit}"
    if trend == "U":
        voice += " increasing"
    elif trend == "D":
        voice += " decreasing"
    return Element(token, voice)


def _weather(token):
    match = _WEATHER.match(token)
    if not match:
        return None
    intensity, body = match.groups()
    if len(body) % 2 or not all(body[i:i + 2] in WEATHER_CODES
                                for i in range(0, len(body), 2)):
        return None
    words = []
    if intensity:
        words.append(WEATHER_CODES[intensity])
    words += [WEATHER_CODES[body[i:i + 2]] for i in range(0, len(body), 2)]
    return Element(token, " ".join(words))


def _cloud(token):
    if token in ("NSC", "NCD"):
        return Element(token, "no significant clouds")
    if token in ("SKC", "CLR"):
        return Element(token, "sky clear")
    match = _CLOUD.match(token)
    if not match:
        return None
    cover, height, cloud_type = match.groups()
    voice = CLOUD_COVER[cover]
    if height != "///":
        voice += f" {spell_altitude(int(height) * 100)}"
    if cloud_type:
        voice += f" {CLOUD_TYPE[cloud_type]}"
    return Element(token, voice)


class Metar:
    """解析后的 METAR。每个属性都是 Element，缺失时是空 Element。"""

    def __init__(self, raw):
        self.raw = (raw or "").replace("=", "").strip()
        self.station = ""
        self.observation_time = EMPTY
        self.wind = EMPTY
        self.visibility = EMPTY
        self.rvr = EMPTY
        self.present_weather = EMPTY
        self.clouds = EMPTY
        self.temperature = EMPTY
        self.dew_point = EMPTY
        self.pressure = EMPTY
        self.trend = EMPTY
        self.recent_weather = EMPTY
        self.cavok = False
        self.auto = False
        self._parse()

    def _parse(self):
        tokens = self.raw.split()
        if not tokens:
            return

        # 有的源会带 METAR / SPECI 前缀
        while tokens and tokens[0] in ("METAR", "SPECI"):
            tokens.pop(0)
        if not tokens:
            return

        self.station = tokens[0]
        rest = tokens[1:]

        variation = None
        for token in rest:
            if _WIND_VAR.match(token):
                variation = _WIND_VAR.match(token).groups()

        clouds, weather, rvrs = [], [], []
        recent = []
        trend_tokens = []
        in_trend = False

        for token in rest:
            if token in _TREND:
                in_trend = True
            if in_trend:
                trend_tokens.append(token)
                continue

            if token == "AUTO":
                self.auto = True
                continue
            if token == "CAVOK":
                self.cavok = True
                self.visibility = Element("CAVOK", "cavok")
                continue
            if _WIND_VAR.match(token):
                continue        # 已经并进风组了

            match = _TIME.match(token)
            if match and not self.observation_time:
                hhmm = f"{match.group(2)}{match.group(3)}"
                self.observation_time = Element(token, f"time {spell(hhmm)}")
                continue

            if not self.wind:
                element = _wind(token, variation)
                if element:
                    self.wind = element
                    continue

            element = _rvr(token)
            if element:
                rvrs.append(element)
                continue

            if not self.visibility and not self.cavok:
                element = _visibility(token)
                if element:
                    self.visibility = element
                    continue

            element = _cloud(token)
            if element:
                clouds.append(element)
                continue

            match = _TEMP.match(token)
            if match:
                temp, dew = match.groups()
                self.temperature = Element(
                    temp, f"temperature {spell_number(self._signed(temp))}")
                self.dew_point = Element(
                    dew, f"dewpoint {spell_number(self._signed(dew))}")
                continue

            if re.match(r'^Q\d{4}$', token):
                value = int(token[1:])
                self.pressure = Element(token, f"QNH {spell(value)} hectopascals")
                continue
            if re.match(r'^A\d{4}$', token):
                value = token[1:]
                self.pressure = Element(
                    token, f"altimeter {spell(value[:2])} point {spell(value[2:])}")
                continue

            if token.startswith("RE"):
                element = _weather(token[2:])
                if element:
                    recent.append(Element(token, "recent " + element.voice))
                    continue

            element = _weather(token)
            if element:
                weather.append(element)

        self.clouds = self._join(clouds)
        self.present_weather = self._join(weather)
        self.rvr = self._join(rvrs)
        self.recent_weather = self._join(recent)
        if trend_tokens:
            self.trend = Element(" ".join(trend_tokens),
                                 "no significant change"
                                 if trend_tokens[0] == "NOSIG" else "")

    @staticmethod
    def _signed(value):
        return -int(value[1:]) if value.startswith("M") else int(value)

    @staticmethod
    def _join(elements):
        if not elements:
            return EMPTY
        return Element(" ".join(e.text for e in elements),
                       ", ".join(e.voice for e in elements))

    def full_wx(self):
        """按标准顺序拼起来，对应 vATIS 的 [WX] / [FULL_WX_STRING]。"""
        # 温度和露点在电码里本来就是 25/18 一组，文字形态要拼回去
        if self.temperature and self.dew_point:
            temp_dew = Element(f"{self.temperature.text}/{self.dew_point.text}",
                               f"{self.temperature.voice}, {self.dew_point.voice}")
        else:
            temp_dew = self.temperature or self.dew_point

        order = [self.wind, self.visibility, self.rvr, self.present_weather,
                 self.clouds, temp_dew, self.pressure]
        present = [e for e in order if e]
        return Element(" ".join(e.text for e in present),
                       ", ".join(e.voice for e in present))

    def is_valid(self):
        return bool(self.station and (self.wind or self.pressure))
