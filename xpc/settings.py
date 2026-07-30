"""配置，存成当前目录下的 xpc_settings.json。

密码是明文存的——和这个仓库里其他几个客户端一样。这不是好做法，但网络的
FSD 密码就是网站密码，改法要三个仓库一起动，先照旧。
"""

import json
import logging
import os

log = logging.getLogger("settings")

SETTINGS_FILE = "xpc_settings.json"

MUMBLE_HOST = "hjdczy.top"
FSD_HOST = "fsd.airwaysn.org"
FSD_PORT = 6809

DEFAULTS = {
    "cid": "",
    "password": "",
    "real_name": "",
    "callsign": "",
    "aircraft": "",
    "rating": 1,
    "mumble_host": MUMBLE_HOST,
    "fsd_host": FSD_HOST,
    "fsd_port": FSD_PORT,
    "ptt_key": "`",
    "joystick_ptt": None,
    "input_device_index": None,
    "output_device_index": None,
    "mic_volume": 100,
    "speaker_volume": 100,
    "connect_fsd": True,
    "connect_voice": True,
    "flight_plan": {},
    # 他机渲染。csl_path 指向装好的 CSL 模型包所在目录（Bluebell 等）；
    # 留空就只送 TCAS，不画模型。
    "render_traffic": True,
    "csl_path": "",
    "traffic_range_nm": 60,
}


class Settings:
    def __init__(self, path=SETTINGS_FILE):
        self.path = path
        for key, value in DEFAULTS.items():
            setattr(self, key, value)
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("could not read the settings, using the defaults: %s", e)
            return
        for key in DEFAULTS:
            if key in data:
                setattr(self, key, data[key])
        # 音量存坏了会让整条音频链路失灵，夹一下
        self.mic_volume = max(0, min(200, int(self.mic_volume or 100)))
        self.speaker_volume = max(0, min(200, int(self.speaker_volume or 100)))
        log.info("read the settings from %s", os.path.abspath(self.path))

    def save(self):
        data = {key: getattr(self, key, DEFAULTS[key]) for key in DEFAULTS}
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("could not save the settings: %s", e)
