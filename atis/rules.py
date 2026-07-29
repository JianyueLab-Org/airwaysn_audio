"""开播前的拦截规则。

纯逻辑：不碰 Qt、不碰网络。给它席位、配置和当前在播的集合，它回答"能不能
开"，以及不能开的话该跟用户说什么。

**为什么单独拎出来。** 这几条原来在 `gui.py` 里各写了两遍——点按钮时查一遍
（`toggle_broadcast`），数据源核对回来真正开播前再查一遍（`start_broadcast`）。
两遍是必要的：核对要走网络，中间隔着几秒，这期间完全可能又开了一个同频率的
席位，只信点按钮那一刻的检查会漏。但**代码不该也写两遍**——四段警告文案各自
独立，改一处忘一处，用户就会在两条路径上看到不一样的说法。

判据本身也不是想当然的：语音账号是 `{cid}_atis{频率6位}`（server/login.py 就
按这个发 Murmur 用户 id），所以同一个频率上开两个通播用户名会撞，服务端的同名
踢人逻辑会把先连上的那个踢下去——表现出来是"我的通播莫名其妙断了"。
"""


def frequency_conflict(station, profile, broadcasting):
    """同频率上已经有别的通播在播吗？有就返回那个席位，没有返回 None。

    broadcasting 是正在播的呼号集合。跳过自己——自己已经在播的话，调用方走的
    是停播那条路。
    """
    for other_callsign in broadcasting:
        if other_callsign == station.callsign:
            continue
        other = profile.get(other_callsign)
        if other and other.frequency_khz == station.frequency_khz:
            return other
    return None


def conflict_message(other):
    return (f"{other.callsign} 已经在 {other.frequency} 上播出了。\n"
            f"两个通播用同一个频率会共用同一个语音账号，"
            f"后连上的会把先连上的踢掉。")


def blocking_reason(station, profile, broadcasting,
                    cid="", password="", rendered=None):
    """开播前的拦截理由。

    返回 (标题, 正文) 给界面弹窗用；None 表示可以开。顺序有讲究：先查填没填
    账号（最常见、最好改），再查频率冲突（会踢掉别人，最严重），最后查有没有
    稿子。
    """
    if station is None:
        return ("错误", "没有选中席位")

    if not (cid or "").strip() or not password:
        return ("错误", "请先填写用户名和密码")

    other = frequency_conflict(station, profile, broadcasting)
    if other is not None:
        return ("频率冲突", conflict_message(other))

    # rendered 是 script.render() 的结果：(文字通播, 语音稿)
    if not rendered or not (rendered[1] or "").strip():
        return ("错误", "还没有可播的内容，先刷新天气")

    return None
