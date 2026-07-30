"""他机航迹表。

对应 xPilot 的 `src/aircrafts/`。FSD 的 `@` 位置包一秒才 5 个，直接照着画飞机
会一跳一跳，所以这里保留每架飞机的前后两个采样，由渲染端按时间插值。

这个模块**不碰 X-Plane、不碰网络**，只有数据和时间——所以它是这套东西里唯一
能完整测到的部分。插件和 FSD 各自把数据递进来：

    fsdpilot  --on_traffic-->  TrafficTable  --snapshot-->  插件（画 + TCAS）

机型从哪来：`@` 包里没有。要靠 `#SB … PIR` 问对方，对方回
`PI:GEN:EQUIPMENT=B738:AIRLINE=CCA`。在拿到之前先按通用模型画，拿到之后再换
——所以 `Aircraft.model_dirty` 存在，让渲染端知道该重新匹配了。
"""

import logging
import math
import threading
import time

log = logging.getLogger("traffic")

# 超过这么久没有新位置就认为对方掉线了。FSD 正常 5 Hz，给足余量。
STALE_AFTER = 15.0
# 插值最多外推这么久。再久就不如让飞机停住，免得跑出天际。
MAX_EXTRAPOLATE = 2.0
# 机型问不到就别一直问，隔这么久重试一次
PLANE_INFO_RETRY = 30.0

FEET_PER_METRE = 3.280839895
NM_PER_DEGREE = 60.0


class Sample:
    """一个时刻的位置和姿态。"""

    __slots__ = ("time", "latitude", "longitude", "altitude",
                 "pitch", "bank", "heading", "on_ground", "groundspeed")

    def __init__(self, time, latitude, longitude, altitude,
                 pitch, bank, heading, on_ground, groundspeed):
        self.time = time
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude          # 英尺
        self.pitch = pitch
        self.bank = bank
        self.heading = heading
        self.on_ground = on_ground
        self.groundspeed = groundspeed    # 节


def _interpolate_angle(a, b, ratio):
    """按最短弧插值。359° 到 1° 应当往前走 2°，而不是倒着走 358°。"""
    difference = (b - a + 180.0) % 360.0 - 180.0
    return (a + difference * ratio) % 360.0


class Aircraft:
    """一架他机。"""

    def __init__(self, callsign):
        self.callsign = callsign
        self.squawk = 0
        self.transponder_mode = "S"
        self.previous = None
        self.latest = None

        # 机型匹配信息，来自 #SB PI:GEN
        self.equipment = ""          # ICAO 机型码，如 B738
        self.airline = ""            # 航司码，如 CCA
        self.livery = ""
        self.csl = ""                # 对方直接指定的 CSL 名
        self.model_dirty = True      # 渲染端该（重新）匹配模型了
        self.info_requested = 0.0    # 上次发 PIR 的时刻

        # 来自 $CQ … ACC 的配置，用来驱动动画
        self.gear_down = None
        self.flaps = 0.0
        self.spoilers = False
        self.lights = {}
        self.engines_on = True

    @property
    def has_plane_info(self):
        return bool(self.equipment or self.csl)

    def update(self, sample, squawk=None, mode=None):
        # 同一个时刻的重复包会让插值除零，直接丢掉
        if self.latest and sample.time <= self.latest.time:
            return
        self.previous = self.latest
        self.latest = sample
        if squawk is not None:
            self.squawk = squawk
        if mode is not None:
            self.transponder_mode = mode

    def set_plane_info(self, equipment=None, airline=None, livery=None, csl=None):
        """收到 PI:GEN。有变化才置脏，免得渲染端反复重新加载模型。"""
        changed = False
        for name, value in (("equipment", equipment), ("airline", airline),
                            ("livery", livery), ("csl", csl)):
            if value is None:
                continue
            value = value.strip().upper()
            if value and getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed:
            self.model_dirty = True
        return changed

    @property
    def vertical_speed(self):
        """英尺每分钟。只有两个采样才算得出来。"""
        if not (self.previous and self.latest):
            return 0.0
        span = self.latest.time - self.previous.time
        if span <= 0:
            return 0.0
        return (self.latest.altitude - self.previous.altitude) / span * 60.0

    def position_at(self, now):
        """插值出 now 时刻的位置。没有数据返回 None。

        两个采样之间走线性插值；超过最后一个采样就按最后的速度外推，但最多
        外推 MAX_EXTRAPOLATE 秒——对方掉线时飞机应当停在原地，不是一直飞下去。
        """
        latest = self.latest
        if not latest:
            return None
        previous = self.previous
        if not previous or latest.time <= previous.time:
            return self._as_dict(latest)

        span = latest.time - previous.time
        ratio = (now - previous.time) / span
        # 往前不外推（收到乱序包时会出现），往后最多外推固定秒数
        limit = 1.0 + min(MAX_EXTRAPOLATE, span * 2) / span
        ratio = max(0.0, min(limit, ratio))

        return {
            "latitude": previous.latitude + (latest.latitude - previous.latitude) * ratio,
            "longitude": previous.longitude + (latest.longitude - previous.longitude) * ratio,
            "altitude": previous.altitude + (latest.altitude - previous.altitude) * ratio,
            "pitch": previous.pitch + (latest.pitch - previous.pitch) * ratio,
            "bank": previous.bank + (latest.bank - previous.bank) * ratio,
            "heading": _interpolate_angle(previous.heading, latest.heading, ratio),
            "on_ground": latest.on_ground,
            "groundspeed": latest.groundspeed,
        }

    @staticmethod
    def _as_dict(sample):
        return {
            "latitude": sample.latitude, "longitude": sample.longitude,
            "altitude": sample.altitude, "pitch": sample.pitch,
            "bank": sample.bank, "heading": sample.heading,
            "on_ground": sample.on_ground, "groundspeed": sample.groundspeed,
        }


class TrafficTable:
    """所有他机。FSD 线程写，渲染线程读，所以整体上锁。"""

    def __init__(self, on_request_info=None):
        # on_request_info(callsign) —— 需要向对方要机型时调用
        self.on_request_info = on_request_info
        self.aircraft = {}
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self.aircraft)

    def __contains__(self, callsign):
        with self._lock:
            return callsign in self.aircraft

    def get(self, callsign):
        with self._lock:
            return self.aircraft.get(callsign)

    def update_position(self, callsign, latitude, longitude, altitude,
                        pitch, bank, heading, on_ground=False,
                        groundspeed=0, squawk=None, mode=None, now=None):
        """收到一个 `@` 位置包。"""
        now = now if now is not None else time.time()
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                aircraft = Aircraft(callsign)
                self.aircraft[callsign] = aircraft
                log.info("new aircraft %s", callsign)
            aircraft.update(Sample(now, latitude, longitude, altitude, pitch,
                                   bank, heading, on_ground, groundspeed),
                            squawk=squawk, mode=mode)
            needs_info = (not aircraft.has_plane_info
                          and now - aircraft.info_requested > PLANE_INFO_RETRY)
            if needs_info:
                aircraft.info_requested = now

        # 回调放在锁外面：它会去发包，别把网络 IO 圈进锁里
        if needs_info and self.on_request_info:
            try:
                self.on_request_info(callsign)
            except Exception as e:
                log.warning("could not ask %s for its aircraft type: %s", callsign, e)
        return aircraft

    def set_plane_info(self, callsign, **info):
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                # 机型先于位置到达也要留住，别丢
                aircraft = Aircraft(callsign)
                self.aircraft[callsign] = aircraft
            changed = aircraft.set_plane_info(**info)
        if changed:
            log.info("%s is a %s/%s", callsign,
                     aircraft.equipment or "?", aircraft.airline or "?")
        return aircraft

    def set_config(self, callsign, config):
        """$CQ … ACC 的 config JSON，用来驱动动画。"""
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft is None:
                return None
            if "gear_down" in config:
                aircraft.gear_down = bool(config["gear_down"])
            if "flaps_pct" in config:
                try:
                    aircraft.flaps = max(0.0, min(1.0, float(config["flaps_pct"]) / 100.0))
                except (TypeError, ValueError):
                    pass
            if "spoilers_out" in config:
                aircraft.spoilers = bool(config["spoilers_out"])
            if isinstance(config.get("lights"), dict):
                aircraft.lights.update(config["lights"])
            engines = config.get("engines")
            if isinstance(engines, dict) and engines:
                aircraft.engines_on = any(
                    isinstance(e, dict) and e.get("on") for e in engines.values())
            return aircraft

    def remove(self, callsign):
        with self._lock:
            if self.aircraft.pop(callsign, None):
                log.info("%s went offline", callsign)
                return True
            return False

    def prune(self, now=None):
        """清掉太久没消息的。返回被清掉的呼号。"""
        now = now if now is not None else time.time()
        with self._lock:
            gone = [callsign for callsign, aircraft in self.aircraft.items()
                    if not aircraft.latest or now - aircraft.latest.time > STALE_AFTER]
            for callsign in gone:
                del self.aircraft[callsign]
        for callsign in gone:
            log.info("dropped %s, no updates for too long", callsign)
        return gone

    def snapshot(self, now=None, origin=None, limit=None, max_range_nm=None):
        """给渲染端的一份数据，已经插值到 now。

        origin 是本机 (纬度, 经度)。给了就按距离排序并可以截断——TCAS 只有 64
        个位置，飞机比这多的时候必须先扔远的，不能随便扔。
        """
        now = now if now is not None else time.time()
        with self._lock:
            aircraft = list(self.aircraft.values())

        entries = []
        for one in aircraft:
            position = one.position_at(now)
            if not position:
                continue
            entry = {
                "callsign": one.callsign,
                "squawk": one.squawk,
                "mode": one.transponder_mode,
                "equipment": one.equipment,
                "airline": one.airline,
                "livery": one.livery,
                "csl": one.csl,
                "model_dirty": one.model_dirty,
                "vertical_speed": one.vertical_speed,
                "gear_down": one.gear_down,
                "flaps": one.flaps,
                "spoilers": one.spoilers,
                "lights": dict(one.lights),
                "engines_on": one.engines_on,
            }
            entry.update(position)
            if origin:
                entry["range_nm"] = distance_nm(
                    origin[0], origin[1], position["latitude"], position["longitude"])
            entries.append(entry)

        if origin:
            entries.sort(key=lambda e: e["range_nm"])
            if max_range_nm is not None:
                entries = [e for e in entries if e["range_nm"] <= max_range_nm]
        if limit is not None:
            entries = entries[:limit]
        return entries

    def mark_model_clean(self, callsign):
        """渲染端匹配过模型之后回来清标记。"""
        with self._lock:
            aircraft = self.aircraft.get(callsign)
            if aircraft:
                aircraft.model_dirty = False


def distance_nm(lat1, lon1, lat2, lon2):
    """两点距离（海里）。平面近似，几百海里内够用，比 haversine 便宜。"""
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * NM_PER_DEGREE
    dx = (lon2 - lon1) * NM_PER_DEGREE * math.cos(mean_lat)
    return math.hypot(dx, dy)
