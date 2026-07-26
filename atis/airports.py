"""机场坐标表。

FSD 的位置包（`%`）要经纬度，否则席位会落在 0/0——几内亚湾外海。vATIS 是从它
自己的 NavData 仓库拿坐标的；我们直接用本网站已经在用的那份：
can-web 的 `public/airports.json`，格式是 `{"RJAA": [纬度, 经度]}`，一万七千
多个机场，源头是 VATSpy 数据。

表随程序一起打包，查表不联网。用户在席位里手填的坐标优先——机场基准点未必是
塔台位置，想精确定位时可以覆盖。
"""

import json
import logging
import os
import sys

log = logging.getLogger("机场")

DATA_FILE = "airports.json"

_table = None


def _data_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, DATA_FILE)


def table():
    """整张表，第一次调用时加载。"""
    global _table
    if _table is None:
        try:
            with open(_data_path(), encoding="utf-8") as f:
                _table = json.load(f)
            log.info("已加载 %d 个机场的坐标", len(_table))
        except Exception as e:
            log.warning("机场坐标表加载失败，席位位置将需要手填: %s", e)
            _table = {}
    return _table


def coordinates(icao):
    """查机场坐标，返回 (纬度, 经度)；查不到返回 None。"""
    entry = table().get((icao or "").strip().upper())
    if not entry or len(entry) < 2:
        return None
    try:
        return float(entry[0]), float(entry[1])
    except (TypeError, ValueError):
        return None
