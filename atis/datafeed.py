"""从本网数据源查自己的登录等级。

通播席位在网络上显示的等级，应该和本人此刻管制用的那个一致——写死成 OBS 会
让雷达图上看起来像个观察员。can-web 没有查成员信息的公开接口，所以从 can-fsd
的数据源（/v1/data.json）里按 CID 找：`controllers[]` 和 `atis[]` 每项都带
`cid` 和 `rating`。

注意这里拿到的是**对方登录时报的等级**（can-fsd 的 NetworkRating），不是数据库
里的最高等级——这正是我们想要的：跟着本人这次的管制席位走。

人不在线时查不到，调用方退回配置里的值。只用标准库 urllib。
"""

import json
import logging
import urllib.request

log = logging.getLogger("datafeed")

DEFAULT_DATAFEED_URL = "https://data.ceruleanavi.net/v1/data.json"

# 数据源前面挡着 Cloudflare，非浏览器形态的 User-Agent 一律 403
# （"can-atis"、"python-requests/x.y" 都被拒）。带上 Mozilla 前缀才放行，
# 后面仍然标明自己是谁。
_USER_AGENT = "Mozilla/5.0 (compatible; CanATIS/1.0)"


def fetch(url=None, timeout=15):
    """拉取数据源。失败返回 None（查等级失败不该影响播出）。"""
    try:
        request = urllib.request.Request(url or DEFAULT_DATAFEED_URL,
                                         headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning("could not read the datafeed: %s", e)
        return None


FACILITY_OBSERVER = 0


def controller_for(cid, url=None, data=None):
    """找这个 CID 此刻正在管的席位，没有则返回 None。

    「正在管制」的判定和 can-fsd 自己的一致（handler.go 的 handleQueryATC）：
    要在 controllers[] 里，且席位类型高于观察员——挂着观察员不算在管制。
    通播席位（atis[]）当然也不算。
    """
    cid = str(cid or "").strip()
    if not cid:
        return None
    if data is None:
        data = fetch(url)
    if not data:
        return None

    for entry in data.get("controllers") or []:
        if str(entry.get("cid", "")).strip() != cid:
            continue
        try:
            facility = int(entry.get("facility", 0))
        except (TypeError, ValueError):
            facility = 0
        if facility > FACILITY_OBSERVER:
            return entry
    return None


# can-fsd 用这个频率表示"没设频率"。拿它建席位会得到一个谁也不在的频道。
NO_FREQUENCY = "199.998"

# 呼号后缀 → 通播类型。和 profile.TYPE_SUFFIX 是同一套约定，反过来查。
_SUFFIXES = (("_D_ATIS", "departure"), ("_A_ATIS", "arrival"),
             ("_ATIS", "combined"))


def atis_stations(url=None, data=None):
    """数据源上此刻在线的通播席位，[(ICAO, 呼号, 频率, 类型)]。

    **这不是"从 can 取配置"。** 配置在 can-web 的 `/api/v1/atis/config`，
    由 `netconfig.py` 取（席位、频率、跑道构型预设、模板、中文用词）。这里读的
    是 can-fsd 数据源里的 `atis[]`，那是**此刻在线的运行状态**：只有机场和
    频率，没有模板也没有预设。

    `/api/v1/atis` 是第三样东西——给 EuroScope 用的文本生成器，和这两个都无关。

    所以它能省掉的只是查 ICAO 和频率这一步。

    按 can-fsd 的约定过滤：只有呼号以 _ATIS 结尾的才在 atis[] 里；频率是整数
    兆赫字符串（"128.500"），199.998 表示没设，挡掉。
    """
    if data is None:
        data = fetch(url)
    found = []
    if not data:
        return found

    for entry in data.get("atis") or []:
        callsign = str(entry.get("callsign", "")).strip().upper()
        frequency = str(entry.get("frequency", "")).strip()
        if not callsign or not frequency or frequency == NO_FREQUENCY:
            continue
        for suffix, kind in _SUFFIXES:
            if callsign.endswith(suffix):
                icao = callsign[:-len(suffix)]
                break
        else:
            continue                     # 不是通播呼号，跳过
        if len(icao) != 4 or not icao.isalnum():
            continue
        found.append((icao, callsign, frequency, kind))

    found.sort(key=lambda item: item[1])
    return found


def rating_for(cid, url=None, data=None):
    """查这个 CID 此刻在网络上的等级。查不到返回 None。"""
    cid = str(cid or "").strip()
    if not cid:
        return None
    if data is None:
        data = fetch(url)
    if not data:
        return None

    # 管制席位和通播席位在数据源里是同一种结构
    for group in ("controllers", "atis"):
        for entry in data.get(group) or []:
            if str(entry.get("cid", "")).strip() != cid:
                continue
            try:
                rating = int(entry.get("rating"))
            except (TypeError, ValueError):
                continue
            if rating > 0:
                log.info("the datafeed gives CID %s the rating %s (%s)",
                         cid, rating, entry.get("callsign", group))
                return rating
    log.info("CID %s is not online on the datafeed, using the rating from "
             "the settings", cid)
    return None
