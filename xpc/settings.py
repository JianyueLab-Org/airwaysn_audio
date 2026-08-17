"""配置，存成当前目录下的 xpc_settings.json。

密码是明文存的——和这个仓库里其他几个客户端一样。这不是好做法，但网络的
FSD 密码就是网站密码，改法要三个仓库一起动，先照旧。
"""

import json
import logging
import os

import ptt

log = logging.getLogger("settings")

SETTINGS_FILE = "xpc_settings.json"

MUMBLE_HOST = "audio.ceruleanavi.net"
FSD_HOST = "fsd.ceruleanavi.net"
FSD_PORT = 6809

# 语音服务器的旧域名。mumble_host 是存进配置文件的，所以光换上面那个默认值
# 只对全新安装有效——老用户的 json 里还是旧域名，等旧域名停掉那天他们看到的
# 是"连不上语音服务器"，而设置界面上那一行看着完全正常。读配置时换掉。
# 只认这一个旧值：用户自己填的别的地址是有意为之，不能动。
OLD_MUMBLE_HOSTS = {"hjdczy.top"}

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
    # PTT 现在是一串绑定（键盘 / 鼠标侧键 / 摇杆），任意一个按住即发话。
    # 老配置里的 ptt_key + joystick_ptt 两个字段读得进来，见 load()。
    "ptt_bindings": None,
    "ptt_key": "`",
    "joystick_ptt": None,
    # 界面语言。空字符串表示"还没选过"，第一次启动跟系统走
    "language": "",
    "debug": False,
    "input_device_index": None,
    "output_device_index": None,
    "mic_volume": 100,
    "speaker_volume": 100,
    # 收到管制消息时响一声。默认开：飞行员盯着的是窗外，消息区多一行没人看得见。
    # message_sound_all 打开的话频率上每条消息都响；默认只有私聊、以及正文里
    # 点到自己呼号的那种才响（见 chime.wants_alert）。
    "message_sound": True,
    "message_sound_all": False,
    "message_sound_volume": 100,
    "connect_fsd": True,
    "connect_voice": True,
    # 观察员模式（双人机组的右座）：只连语音，不连 FSD，网络上不会多一架
    # 飞机。observer_frequency 是手输的频率，留空就跟着 COM1 走。见 observer.py。
    "observer_mode": False,
    "observer_frequency": "",
    "flight_plan": {},
    # 他机渲染。csl_path 指向装好的 CSL 模型包所在目录（Bluebell 等）；
    # 留空就只送 TCAS，不画模型。
    "render_traffic": True,
    "csl_path": "",
    # 用户指定的 X-Plane 安装目录。留空就每次自动探测——只有探测不出来（绿色版、
    # 搬过目录）时才需要手工指，所以这里不预填。
    "xplane_path": "",
    "traffic_range_nm": 60,
    # 更新检查。启动时问一次 can 有没有新版；查到了也只是弹一句，
    # 装不装由用户决定。skipped_version 记住"这一版我不要"，免得每次
    # 启动再问一遍——那和自动更新一样烦人，只是烦得更频繁。
    "update_check": True,
    "skipped_version": "",
    "update_url": "",
}


def _clamp_volume(value, default=100):
    """夹到 0-200。0 是合法的（静音），None 和坏值才回默认。"""
    try:
        return max(0, min(200, int(value)))
    except (TypeError, ValueError):
        return default


class Settings:
    def __init__(self, path=SETTINGS_FILE):
        self.path = path
        for key, value in DEFAULTS.items():
            setattr(self, key, value)
        self.load()
        if self.ptt_bindings is None:
            # 全新安装，连配置文件都还没有：用默认的 PTT 键起一条绑定
            self.ptt_bindings = ptt.load(None, legacy_key=self.ptt_key,
                                         legacy_joystick=self.joystick_ptt)

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
        if (self.mumble_host or "").strip().lower() in OLD_MUMBLE_HOSTS:
            log.info("the voice server was renamed, using %s instead of %s",
                     MUMBLE_HOST, self.mumble_host)
            self.mumble_host = MUMBLE_HOST
        # 音量存坏了会让整条音频链路失灵，夹一下
        # `or 100` 会把用户特意拉到 0 的静音在重启后悄悄变回 100（麦克风
        # 开着而用户不知道）；0 是滑条上真实可取的值，只有 None/坏值才回默认
        self.mic_volume = _clamp_volume(self.mic_volume)
        self.speaker_volume = _clamp_volume(self.speaker_volume)
        self.message_sound_volume = _clamp_volume(self.message_sound_volume)
        # 绑定表从 JSON 变成 ptt.Binding。老配置里没有 ptt_bindings，就拿
        # ptt_key + joystick_ptt 升上来——升不上来的话，用户原来设的 PTT 会在
        # 升级之后悄悄失效，而界面上一切正常，只是没人听得见。
        self.ptt_bindings = ptt.load(data.get("ptt_bindings"),
                                     legacy_key=self.ptt_key,
                                     legacy_joystick=self.joystick_ptt)
        log.info("read the settings from %s", os.path.abspath(self.path))

    def save(self):
        data = {key: getattr(self, key, DEFAULTS[key]) for key in DEFAULTS}
        data["ptt_bindings"] = ptt.dump(self.ptt_bindings)
        # ptt_key / joystick_ptt 不再写回去：留着就有两个说了算的地方，而且它们
        # 加起来也表达不了鼠标侧键这一种。老版本读到没有这两个键会用自己的默认，
        # 那是降级时能接受的行为。
        data.pop("ptt_key", None)
        data.pop("joystick_ptt", None)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("could not save the settings: %s", e)
