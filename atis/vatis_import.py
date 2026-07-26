"""导入 vATIS 的配置文件。

vATIS 的 profile 是 JSON，字段名 camelCase、枚举序列化成字符串
（vATIS.Desktop/SourceGenerationContext.cs 里的 JsonSourceGenerationOptions）。
结构大致是：

    {
      "name": "配置名",
      "stations": [                     // 旧版叫 composites
        {
          "identifier": "KLAX",
          "name": "Los Angeles",
          "atisType": "Combined",       // Combined / Departure / Arrival
          "codeRange": {"low": "A", "high": "Z"},
          "frequency": 135700000,       // uint，单位是赫兹
          "presets": [
            {"name": "...", "template": "...",
             "airportConditions": "...", "notams": "..."}
          ],
          "contractions": [
            {"variableName": "...", "text": "...", "voice": "..."}
          ]
        }
      ]
    }

能对上的都对上；vATIS 里我们没有对应实现的部分（atisFormat 的细粒度格式设置、
IDS 端点、静态定义、语音录制）会原样跳过，并在结果里报出来，免得用户以为
全都导进来了。
"""

import json

from profile import (Preset, Station, TYPE_ARRIVAL, TYPE_COMBINED,
                     TYPE_DEPARTURE)

# vATIS 的枚举既可能是字符串也可能是数字（旧版本），两种都认
ATIS_TYPES = {
    "combined": TYPE_COMBINED, "0": TYPE_COMBINED, 0: TYPE_COMBINED,
    "departure": TYPE_DEPARTURE, "1": TYPE_DEPARTURE, 1: TYPE_DEPARTURE,
    "arrival": TYPE_ARRIVAL, "2": TYPE_ARRIVAL, 2: TYPE_ARRIVAL,
}

# vATIS 里有、我们这边没有对应实现的字段，导入时跳过并提示
UNSUPPORTED = {
    "atisFormat": "细粒度的格式设置（风、能见度等各自的读法选项）",
    "idsEndpoint": "IDS 推送端点",
    "airportConditionDefinitions": "预置的机场条件短语库",
    "notamDefinitions": "预置的 NOTAM 短语库",
    "atisVoice": "语音录制设置",
    "externalGenerator": "外部生成器",
}


class ImportError_(Exception):
    pass


def parse_frequency(value):
    """vATIS 存的是赫兹（135700000）。也容忍千赫和兆赫的写法。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ImportError_(f"频率 {value!r} 无法识别")

    if number >= 1_000_000:          # 赫兹
        mhz = number / 1_000_000
    elif number >= 100_000:          # 千赫
        mhz = number / 1000
    else:                            # 已经是兆赫
        mhz = number

    if not 100 <= mhz <= 200:
        raise ImportError_(f"频率 {mhz:.3f} 超出甚高频范围")
    return f"{mhz:.3f}"


def _code_range(data):
    code_range = data.get("codeRange") or {}
    low = str(code_range.get("low", "A")).strip().upper()[:1] or "A"
    high = str(code_range.get("high", "Z")).strip().upper()[:1] or "Z"
    return (low, high)


def _contractions(entries):
    """[{variableName, text, voice}] → {变量名: (文字, 语音)}"""
    result = {}
    for entry in entries or []:
        name = (entry.get("variableName") or "").strip()
        if not name:
            continue
        # 旧版字段叫 string / spoken
        text = entry.get("text") or entry.get("string") or ""
        voice = entry.get("voice") or entry.get("spoken") or ""
        result[name] = (text.strip(), voice.strip())
    return result


def _presets(entries):
    presets = []
    for entry in sorted(entries or [], key=lambda e: e.get("ordinal") or 0):
        template = entry.get("template")
        if not template:
            continue                 # 没有模板的预设导进来也没用
        presets.append(Preset(
            name=(entry.get("name") or "未命名").strip(),
            template=template,
            airport_conditions=(entry.get("airportConditions") or "").strip(),
            notams=(entry.get("notams") or "").strip(),
        ))
    return presets


def parse_station(data):
    """一个 vATIS 席位 → 我们的 Station。返回 (station, 跳过的说明列表)。"""
    identifier = (data.get("identifier") or "").strip()
    if not identifier:
        raise ImportError_("席位缺少 identifier")

    station = Station(
        identifier,
        (data.get("name") or "").strip(),
        parse_frequency(data.get("frequency")),
        ATIS_TYPES.get(str(data.get("atisType", "")).strip().lower(), TYPE_COMBINED),
        _code_range(data),
        _presets(data.get("presets")) or None,
        _contractions(data.get("contractions")),
    )

    skipped = [description for key, description in UNSUPPORTED.items()
               if data.get(key)]
    return station, skipped


def load_profile(path):
    """读一个 vATIS profile 文件。

    返回 (配置名, [Station], 提示列表)。提示是给用户看的，说明哪些东西没导进来。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as e:
        raise ImportError_(f"打不开文件: {e}")
    except json.JSONDecodeError as e:
        raise ImportError_(f"不是合法的 JSON: {e}")

    if not isinstance(data, dict):
        raise ImportError_("这个文件看起来不是 vATIS 的配置")

    # 旧版把席位叫 composites
    entries = data.get("stations")
    if entries is None:
        entries = data.get("composites")
    if not entries:
        raise ImportError_("配置里没有任何席位（stations / composites 都是空的）")

    stations, notes = [], set()
    failures = []
    for entry in entries:
        try:
            station, skipped = parse_station(entry)
        except ImportError_ as e:
            failures.append(str(e))
            continue
        stations.append(station)
        notes.update(skipped)

    if not stations:
        raise ImportError_("没有能导入的席位：" + "；".join(failures[:3]))

    messages = []
    if failures:
        messages.append(f"{len(failures)} 个席位无法导入：" + "；".join(failures[:3]))
    if notes:
        messages.append("以下 vATIS 设置本客户端没有对应功能，已跳过：" +
                        "、".join(sorted(notes)))

    return (data.get("name") or "").strip(), stations, messages
