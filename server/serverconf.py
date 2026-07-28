"""口令不写进仓库。

按顺序找一个口令：

1. 环境变量
2. 同目录下的 ``server_secrets.json``（已经在 .gitignore 里）
3. Mumble 的 ini 文件——只对 Ice 口令有效，``icesecretwrite`` 本来就住在那儿，
   服务端上早就有一份，没必要再抄一遍

找不到就抛 :class:`MissingSecret`，让调用方当场大声失败。**不留默认值**：源码
里一旦有一个能用的默认口令，它就永远不会被改，而且会跟着仓库到处走——git
历史里也会永远留着。

这三条顺序是有讲究的：环境变量最灵活（systemd 的 Environment= 就够），文件其
次（start.sh 不用动），ini 兜底（等于零配置）。
"""

import json
import os

DEFAULT_INI = "/etc/mumble/mumble-server.ini"
SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "server_secrets.json")

# Murmur 的 ini 里这两个键都可能是写口令，按这个顺序找
_ICE_INI_KEYS = ("icesecretwrite", "icesecret")


class MissingSecret(Exception):
    """找遍了也没有。消息里要说清楚找过哪些地方。"""


def from_env(name):
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def from_file(key, path=None):
    """从 server_secrets.json 里取。文件不存在或坏了都当作没有。"""
    path = path or SECRETS_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        # 坏掉的配置文件要说出来，否则会表现成"口令没配"，查错方向完全不对
        print(f"读取 {path} 失败（当作没有配）: {e}")
        return None
    value = data.get(key)
    return str(value).strip() if value else None


def from_ini(path=None, keys=_ICE_INI_KEYS):
    """从 mumble-server.ini 里读 Ice 口令。

    这个文件没有 section 头，configparser 直接读会报错，所以按行扫。
    """
    path = path or DEFAULT_INI
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    found = {}
    for line in lines:
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key in keys:
            value = value.strip()
            if value:
                found[key] = value
    for key in keys:                 # 按 keys 的优先级挑
        if key in found:
            return found[key]
    return None


def ice_secret(ini_path=None):
    """Murmur 的 Ice 写口令。"""
    value = (from_env("MUMBLE_ICE_SECRET")
             or from_file("ice_secret")
             or from_ini(ini_path))
    if not value:
        raise MissingSecret(
            "没有找到 Mumble 的 Ice 口令。三个地方都可以，任选其一：\n"
            "  1. 环境变量 MUMBLE_ICE_SECRET\n"
            f"  2. {SECRETS_FILE} 里的 \"ice_secret\"\n"
            f"  3. {ini_path or DEFAULT_INI} 里的 icesecretwrite\n"
            "（第 3 条通常已经有了，确认这个进程读得到那个文件即可）")
    return value


def atis_account():
    """服务端那队通播机用的保留账号。不是秘密，缺省 900。"""
    return from_env("ATIS_CID") or from_file("atis_cid") or "900"


def atis_password(required=False):
    """保留账号的口令。

    缺了不该让 login.py 起不来——普通用户的登录不依赖它。所以默认返回 None，
    由调用方决定要不要因此失败：通播机自己没有它就没法上线，那边才是 required。
    """
    value = from_env("ATIS_PASSWORD") or from_file("atis_password")
    if not value and required:
        raise MissingSecret(
            "没有找到通播保留账号的口令。两个地方都可以：\n"
            "  1. 环境变量 ATIS_PASSWORD\n"
            f"  2. {SECRETS_FILE} 里的 \"atis_password\"")
    return value
