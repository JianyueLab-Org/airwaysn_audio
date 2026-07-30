"""从 can-web 取全网通播配置，并进本地。

    GET https://airwaysn.org/api/v1/atis/config

**这才是"从网上取配置"，和「取在线席位」不是一回事。** 数据源（data.airwaysn.org
的 `atis[]`）给的是此刻谁在播，只有机场和频率，是运行状态；这个接口给的是配置
本身——席位、频率、跑道构型预设、模板、中文播报用词。以前网络上确实没有这种
东西，每个人都得把同样的模板和中文跑道词手打一遍，改了也传不到别人那里。

接口回的文档就是本客户端自己的 JSON 形状（`profile.Station.to_dict` 那套
snake_case 字段），所以每一项直接交给 `Station.from_dict`：

    {
      "version": "3f6d746b8451",   # 内容哈希，服务端算的，不是手填的版本号
      "updated": "2026-07-30",     # 给人看的日期
      "notes": "……",               # 一行说明，界面上显示
      "stations": [ {...}, ... ]   # 和 atis_profile.json 里的席位同形状
    }

三条原则，都是踩过的：

- **不认识的字段忽略，认识的字段照单全收。** 客户端版本比配置旧时，多出来的
  键直接跳过，而不是整份读不进来。
- **默认只补缺，不覆盖。** 本地那份可能是值班时手改过的（临时构型、NOTAM），
  网络版一律盖掉等于把人家的活删了。要覆盖必须调用方明确要求。
- **正在播出的席位一律不动。** 播出中的 Station 对象被 Broadcaster 和
  FSDClient 拿着，换掉它只会让稿子和实际在播的内容对不上。

版本号是服务端算的内容哈希（连 notes 改了都会变），所以「已经是最新」这个判断
不会因为谁忘了手动进位而失效。
"""

import json
import logging
import urllib.error
import urllib.request

from profile import Station

log = logging.getLogger("netconfig")

DEFAULT_CONFIG_URL = "https://airwaysn.org/api/v1/atis/config"

# 和 datafeed.py 同一个原因：airwaysn.org 前面挡着 Cloudflare，
# 非浏览器形态的 User-Agent 会被 403。带 Mozilla 前缀放行，后面照实说自己是谁。
_USER_AGENT = "Mozilla/5.0 (compatible; AirwaysnATIS/1.0)"


class NetConfigError(Exception):
    """取配置失败。消息是直接给用户看的中文。"""


class NetworkConfig:
    """解析好的一份网络配置。"""

    def __init__(self, version="", updated="", notes="", stations=None,
                 problems=None):
        self.version = version
        self.updated = updated
        self.notes = notes
        self.stations = list(stations or [])
        # 单个读不进来的席位在这里报出来，别让人以为全都拿到了
        self.problems = list(problems or [])

    def __len__(self):
        return len(self.stations)

    @property
    def label(self):
        """界面上显示的版本说明。"""
        if self.updated and self.version:
            return f"{self.updated}（{self.version}）"
        return self.updated or self.version or "未知版本"


def fetch(url=None, timeout=15):
    """取配置文档。失败抛 NetConfigError，消息可以直接弹给用户。

    这里不像 datafeed.fetch 那样静默返回 None：查等级失败不该影响播出，但用户
    明确按了「更新配置」，失败就必须告诉他为什么，否则只会得到一个什么都没发生
    的按钮。
    """
    target = url or DEFAULT_CONFIG_URL
    request = urllib.request.Request(target,
                                     headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise NetConfigError("请求太频繁，被服务器限流了，过一会儿再试")
        raise NetConfigError(f"服务器返回 {e.code}（{target}）")
    except urllib.error.URLError as e:
        raise NetConfigError(f"连不上 {target}：{e.reason}")
    except Exception as e:
        raise NetConfigError(f"取配置失败：{e}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise NetConfigError(f"返回的内容不是合法 JSON：{e}")
    if not isinstance(data, dict):
        raise NetConfigError("返回的内容不是一份配置文档")
    return data


def _station_entries(document):
    """文档里的席位列表。

    正常是 `stations`。也认 `profiles`——本客户端存盘用的就是那个形状，于是
    把 config_url 指向自己导出的 atis_profile.json（或分区自建的一份）也能用。
    """
    entries = document.get("stations")
    if entries is None:
        entries = []
        for profile in document.get("profiles") or []:
            if isinstance(profile, dict):
                entries.extend(profile.get("stations") or [])
    return entries if isinstance(entries, list) else []


def parse(document):
    """文档 → NetworkConfig。单个席位坏掉不连累整份。"""
    entries = _station_entries(document)
    if not entries:
        raise NetConfigError("配置里没有任何席位")

    stations, problems = [], []
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("有一项不是席位")
            continue
        identifier = str(entry.get("identifier", "") or "?").strip().upper()
        try:
            station = Station.from_dict(entry)
        except (KeyError, TypeError, ValueError) as e:
            problems.append(f"{identifier}：{e}")
            continue
        # 频率算不出来的席位留着只会在开播时才炸，这里就挡掉
        try:
            khz = station.frequency_khz
        except (TypeError, ValueError):
            problems.append(f"{identifier}：频率 {station.frequency!r} 无法识别")
            continue
        if not 100_000 <= khz <= 200_000:
            problems.append(f"{identifier}：频率 {station.frequency} 不在甚高频范围")
            continue
        stations.append(station)

    if not stations:
        raise NetConfigError("配置里没有能用的席位：" + "；".join(problems[:3]))

    stations.sort(key=lambda s: s.callsign)
    return NetworkConfig(str(document.get("version") or "").strip(),
                         str(document.get("updated") or "").strip(),
                         str(document.get("notes") or "").strip(),
                         stations, problems)


def compare(profile, stations):
    """和本地那份比一比，返回 (缺的, 同名但内容不同的, 一模一样的)。

    三个列表装的都是网络版的 Station。界面先把这个结果给用户看，再决定要不要
    动他的配置——「按一下就变了」在值班时是很难接受的。
    """
    missing, differing, same = [], [], []
    for station in stations:
        local = profile.get(station.callsign)
        if local is None:
            missing.append(station)
        elif _differs(local, station):
            differing.append(station)
        else:
            same.append(station)
    return missing, differing, same


def _differs(local, remote):
    """两个席位实质上是否不同。

    比的是**配置**，所以要先把运行状态摘掉：情报字母每几分钟就会推进一格，
    带着它比的话每个席位永远都"和网络版不一样"。
    """
    def config_only(station):
        data = station.to_dict()
        data.pop("letter", None)
        return data
    return config_only(local) != config_only(remote)


def merge(profile, stations, overwrite=False, protected=()):
    """把网络配置并进 profile。返回 (新增, 覆盖, 保留原样, 因播出跳过)。

    - 本地没有的，加进来。
    - 本地已有的，默认原样保留；`overwrite=True` 才换成网络版，且**保留本地
      当前的情报字母**——播了一半把字母退回 A，飞行员报的和听到的就对不上了。
    - `protected` 里的呼号（正在播出的那些）一律不动，并单独报出来。播出中的
      Station 被 Broadcaster 和 FSDClient 拿着，换掉它只会让在播内容和界面
      显示的稿子对不上。

    不存盘——调用方存，因为存盘走的是它那份 Profile/ProfileSet。
    """
    protected = {str(name) for name in protected}
    added, replaced, kept, skipped = [], [], [], []

    for station in stations:
        callsign = station.callsign
        local = profile.get(callsign)
        if local is None:
            profile.add(station)
            added.append(station)
            continue
        if callsign in protected:
            skipped.append(station)
            continue
        if not overwrite or not _differs(local, station):
            kept.append(station)
            continue
        station.set_letter(local.letter)     # 情报字母跟着本地走
        profile.remove(callsign)
        profile.add(station)
        replaced.append(station)

    if added or replaced:
        log.info("network configuration: %d added, %d overwritten, %d kept, "
                 "%d skipped while broadcasting", len(added), len(replaced),
                 len(kept), len(skipped))
    return added, replaced, kept, skipped
