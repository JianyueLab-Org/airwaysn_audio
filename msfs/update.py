"""查有没有新版。查到了也只是**告诉用户**，更不更新由他决定。

    GET https://ceruleanavi.net/api/v1/clients/latest?client=xpc-for-can&version=2.0.1

为什么不直接问 GitHub：大陆连 github.com 很不稳，60 MB 的包经常下到一半就断，
而 ceruleanavi.net 是成员本来就连得上的（通播配置就是从那儿取的）。所以查询和
下载都走自己的服务器——服务端那个 `/api/v1/clients/download/…` 是把 GitHub 的
资产中转出来，不是另存一份。

三条原则：

- **绝不因为查更新而影响启动。** 任何失败都返回 None 并记一行日志：查不到新版
  是小事，为它卡住启动或者弹一个错误框是大事。
- **绝不自动更新。** 这里只回报「有 x.y.z」，装不装、什么时候装是用户的事。
  一个正在值班的管制员最不需要的就是程序自作主张重启。
- **跳过的版本要记住。** 用户说了「跳过 2.0.2」，就不能每次启动再问一遍——那
  和自动更新一样烦人，区别只是它烦得更频繁。

比较交给服务端（它对四个客户端用同一套判据），但本地再挡一道：版本号和自己
一样就不提示。服务端要是哪天回错了，最坏也只是不提示，而不是天天催。
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("update")

DEFAULT_UPDATE_URL = "https://ceruleanavi.net/api/v1/clients/latest"

# 和 datafeed.py / netconfig.py 同一个原因：ceruleanavi.net 前面挡着 Cloudflare。
# 实测 /api/* 不会被挑战（HTML 页面会），但 UA 还是照着已有的写法来。
_USER_AGENT = "Mozilla/5.0 (compatible; CanClient/1.0)"


class Update:
    """一个可用的新版本。"""

    def __init__(self, version="", notes="", download="", size=0):
        self.version = version
        self.notes = notes            # release 说明页
        self.download = download      # 走自己服务器的下载地址
        self.size = int(size or 0)

    @property
    def size_label(self):
        if self.size <= 0:
            return ""
        return f"{self.size / (1024 * 1024):.1f} MB"

    def __repr__(self):
        return f"<Update {self.version} {self.size_label}>"


def parts(version):
    """版本号里的数字段。`v2.0.1`、`2.0.1`、`2.1.0-rc1` 都能拆。"""
    out = []
    for piece in "".join(c if c.isdigit() else " " for c in str(version or "")).split():
        try:
            out.append(int(piece))
        except ValueError:
            pass
    return out


def is_newer(candidate, current):
    """candidate 比 current 新吗。按数值逐段比。

    **不能按字符串比**：那样 `2.0.10` 会排在 `2.0.9` 前面，结果要么永远催更新，
    要么有了新版也不提示。
    """
    a, b = parts(candidate), parts(current)
    for i in range(max(len(a), len(b))):
        left = a[i] if i < len(a) else 0
        right = b[i] if i < len(b) else 0
        if left != right:
            return left > right
    return False


def check(client, current, url=None, timeout=10):
    """问服务器有没有比 `current` 新的版本。没有或者查不了都返回 None。

    `client` 是包名（`xpc-for-can` 这种），`current` 是本地版本号。
    """
    target = (url or DEFAULT_UPDATE_URL)
    # urlencode 编码参数值；自建源的 update_url 可能已经带了 ?token=…，
    # 那时要接 & 而不是再来一个 ?（否则每次检查都静默 400）
    query = urllib.parse.urlencode({"client": client, "version": current})
    joiner = "&" if "?" in target else "?"
    request = urllib.request.Request(target + joiner + query,
                                     headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        log.info("update check: the server answered %s", e.code)
        return None
    except urllib.error.URLError as e:
        log.info("update check: could not reach %s (%s)", target, e.reason)
        return None
    except Exception as e:
        log.info("update check failed: %s", e)
        return None

    if not isinstance(data, dict):
        return None
    if not data.get("update_available"):
        log.info("update check: %s is current", current)
        return None

    build = data.get("client") or {}
    version = str(build.get("version") or data.get("version") or "").strip()
    # 本地再挡一道：服务端说有新版，但版本号和自己一样就别提示
    if not version or not is_newer(version, current):
        log.info("update check: the server offered %r against %r, ignoring",
                 version, current)
        return None

    update = Update(version=version,
                    notes=str(data.get("notes") or ""),
                    download=str(build.get("download") or ""),
                    size=build.get("size") or 0)
    log.info("update available: %s (%s)", update.version, update.size_label)
    return update
