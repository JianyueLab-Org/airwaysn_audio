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

log = logging.getLogger("数据源")

DEFAULT_DATAFEED_URL = "https://data.airwaysn.org/v1/data.json"

# 数据源前面挡着 Cloudflare，非浏览器形态的 User-Agent 一律 403
# （"airwaysn-atis"、"python-requests/x.y" 都被拒）。带上 Mozilla 前缀才放行，
# 后面仍然标明自己是谁。
_USER_AGENT = "Mozilla/5.0 (compatible; AirwaysnATIS/1.0)"


def fetch(url=None, timeout=15):
    """拉取数据源。失败返回 None（查等级失败不该影响播出）。"""
    try:
        request = urllib.request.Request(url or DEFAULT_DATAFEED_URL,
                                         headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning("读取数据源失败: %s", e)
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
                log.info("从数据源查到 CID %s 的等级: %s（%s）",
                         cid, rating, entry.get("callsign", group))
                return rating
    log.info("数据源上没有 CID %s 在线，等级用配置里的值", cid)
    return None
