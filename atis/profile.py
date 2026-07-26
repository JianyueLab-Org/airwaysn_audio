"""配置模型：Profile → Station → Preset，层级照搬 vATIS。

    Profile   一套配置，含若干席位
    Station   一个机场的通播席位：ICAO、频率、类型、情报字母范围、若干预设
    Preset    一份模板（含机场条件、NOTAM 自由文本），随天气/跑道构型切换

情报字母（ATIS letter）在 METAR 变化时前进一格，可以限定取值范围——离场和进场
分别用不同字母段是 vATIS 的 Code Range，避免飞行员把两份通播搞混。

只有数据和规则，没有 I/O 之外的东西，所以这一层可以直接测。
"""

import logging
import json
import os

log = logging.getLogger("配置")

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 通播类型决定网络上的呼号后缀，和 vATIS 一致
TYPE_COMBINED = "combined"
TYPE_DEPARTURE = "departure"
TYPE_ARRIVAL = "arrival"

TYPE_SUFFIX = {
    TYPE_COMBINED: "_ATIS",
    TYPE_DEPARTURE: "_D_ATIS",
    TYPE_ARRIVAL: "_A_ATIS",
}

TYPE_LABELS = {
    TYPE_COMBINED: "综合",
    TYPE_DEPARTURE: "离场",
    TYPE_ARRIVAL: "进场",
}


class Preset:
    def __init__(self, name="默认", template=None, airport_conditions="",
                 notams="", transition_level=""):
        from template import DEFAULT_TEMPLATE
        self.name = name
        self.template = DEFAULT_TEMPLATE if template is None else template
        self.airport_conditions = airport_conditions
        self.notams = notams
        self.transition_level = transition_level

    def to_dict(self):
        return {
            "name": self.name,
            "template": self.template,
            "airport_conditions": self.airport_conditions,
            "notams": self.notams,
            "transition_level": self.transition_level,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data.get("name", "默认"),
            data.get("template"),
            data.get("airport_conditions", ""),
            data.get("notams", ""),
            data.get("transition_level", ""),
        )


class Station:
    def __init__(self, identifier, name="", frequency="118.000",
                 atis_type=TYPE_COMBINED, code_range=None, presets=None,
                 contractions=None, latitude=0.0, longitude=0.0):
        self.identifier = identifier.strip().upper()
        self.name = name
        self.frequency = str(frequency).strip()
        self.atis_type = atis_type if atis_type in TYPE_SUFFIX else TYPE_COMBINED
        # 情报字母的可用范围，(起, 止) 闭区间
        self.code_range = tuple(code_range) if code_range else ("A", "Z")
        self.presets = presets if presets is not None else [Preset()]
        # 缩略语：模板里写 @变量名，渲染时替换。文字和语音两种形态，
        # 和气象要素一样（vATIS 的 Contractions）
        self.contractions = contractions or {}
        # 席位在 FSD 上报的位置。没填就按 ICAO 查机场坐标——留成 0/0 的话
        # 席位会显示在几内亚湾外海
        self.latitude = float(latitude or 0.0)
        self.longitude = float(longitude or 0.0)
        if not self.latitude and not self.longitude:
            import airports
            found = airports.coordinates(self.identifier)
            if found:
                self.latitude, self.longitude = found

        self.letter = self.code_range[0]

    # ---------- 派生 ----------
    @property
    def callsign(self):
        """网络上的呼号：ZSPD + _ATIS / _D_ATIS / _A_ATIS。"""
        return self.identifier + TYPE_SUFFIX[self.atis_type]

    @property
    def frequency_khz(self):
        return int(round(float(self.frequency) * 1000))

    @property
    def channel(self):
        """语音频道名，全网统一的约定。"""
        return f"FREQ_{str(self.frequency_khz).zfill(6)}"

    @property
    def label(self):
        return f"{self.callsign}  {self.frequency}"

    def preset(self, name):
        for preset in self.presets:
            if preset.name == name:
                return preset
        return self.presets[0] if self.presets else None

    # ---------- 情报字母 ----------
    def letters_in_range(self):
        start = LETTERS.find(self.code_range[0])
        end = LETTERS.find(self.code_range[1])
        if start < 0 or end < 0:
            return list(LETTERS)
        if start <= end:
            return list(LETTERS[start:end + 1])
        # 允许跨 Z 回绕，例如 X..C
        return list(LETTERS[start:]) + list(LETTERS[:end + 1])

    def advance_letter(self):
        """推进一格情报字母，到范围末尾就绕回开头。"""
        available = self.letters_in_range()
        try:
            index = available.index(self.letter)
        except ValueError:
            self.letter = available[0]
            return self.letter
        self.letter = available[(index + 1) % len(available)]
        return self.letter

    def set_letter(self, letter):
        letter = (letter or "").strip().upper()[:1]
        if letter in self.letters_in_range():
            self.letter = letter
            return True
        return False

    # ---------- 持久化 ----------
    def to_dict(self):
        return {
            "identifier": self.identifier,
            "name": self.name,
            "frequency": self.frequency,
            "atis_type": self.atis_type,
            "code_range": list(self.code_range),
            "letter": self.letter,
            "presets": [p.to_dict() for p in self.presets],
            "contractions": {k: list(v) for k, v in self.contractions.items()},
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    @classmethod
    def from_dict(cls, data):
        station = cls(
            data["identifier"],
            data.get("name", ""),
            data.get("frequency", "118.000"),
            data.get("atis_type", TYPE_COMBINED),
            data.get("code_range"),
            [Preset.from_dict(p) for p in data.get("presets", [])] or None,
            {k: tuple(v) for k, v in (data.get("contractions") or {}).items()},
            data.get("latitude", 0.0),
            data.get("longitude", 0.0),
        )
        station.set_letter(data.get("letter", station.code_range[0]))
        return station


class Profile:
    """一套席位配置，存成 JSON。"""

    def __init__(self, path="atis_profile.json"):
        self.path = path
        self.stations = []
        self.load()

    def add(self, station):
        if self.get(station.callsign):
            raise ValueError(f"{station.callsign} 已经存在了")
        self.stations.append(station)
        self.stations.sort(key=lambda s: s.callsign)
        return station

    def remove(self, callsign):
        station = self.get(callsign)
        if station:
            self.stations.remove(station)
            return True
        return False

    def get(self, callsign):
        for station in self.stations:
            if station.callsign == callsign:
                return station
        return None

    def __iter__(self):
        return iter(self.stations)

    def __len__(self):
        return len(self.stations)

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.stations = []
                for entry in data.get("stations", []):
                    try:
                        self.stations.append(Station.from_dict(entry))
                    except (KeyError, TypeError, ValueError) as e:
                        log.warning(f"跳过一个无法识别的席位: {e}")
                self.stations.sort(key=lambda s: s.callsign)
        except Exception as e:
            log.warning(f"读取失败: {e}")

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"stations": [s.to_dict() for s in self.stations]},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"保存失败: {e}")
