"""取 METAR 报文。

只用标准库 urllib，不额外引入依赖。地址可以在设置里改——默认这个也是
can-fsd 自己在用的气象源（config.json 的 weather 项），所以和网络上其它
地方看到的天气是同一份。
"""

import re
import urllib.request

DEFAULT_METAR_URL = "https://metar.vatsim.net/metar.php?id="
_USER_AGENT = "airwaysn-atis"


class WeatherError(Exception):
    pass


def normalize(line, icao):
    """去掉 METAR/SPECI 前缀，确认确实是这个机场的报文。"""
    line = line.strip()
    for prefix in ("METAR ", "SPECI "):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
    return line if line.startswith(icao) else None


def fetch_metar(icao, url=None, timeout=15):
    """取一份原始 METAR 电码。失败抛 WeatherError。"""
    icao = (icao or "").strip().upper()
    if not re.match(r'^[A-Z]{4}$', icao):
        raise WeatherError(f"{icao} 不是 4 位 ICAO 代码")

    request = urllib.request.Request((url or DEFAULT_METAR_URL) + icao,
                                     headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise WeatherError(f"取 {icao} 的 METAR 失败: {e}")

    for line in text.splitlines():
        normalized = normalize(line, icao)
        if normalized:
            return normalized
    raise WeatherError(f"气象源里没有 {icao} 的报文")
