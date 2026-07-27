"""给根频道的 ACL 补上 Listen 权限。

管制端报"没有权限（频道监听需要 Listen 权限）"就是缺这个。

**为什么会缺。** 管制端一个人要同时收好几个频率，用的是 Mumble 1.4 的频道监听
（UserState.listening_channel_add）——人还在主频率的频道里，其他频率靠"监听"
收。这需要目标频道的 `Listen` 权限（ACL 里的 0x800）。

而 `Listen` 是 Mumble 1.4 才加的位，**不在旧版的默认 ACL 里**。全新装的 1.4+
根频道默认给 all 组带上了它，但从 1.2/1.3 升级上来的服务器，数据库里那条 ACL
还是老的，就没有——服务器照常跑，只是监听请求一律被拒，管制员看到的就是某些
频率永远安静。

频率频道是客户端按需在根下建的临时频道，ACL 从根继承，所以在根上放开一次就够，
不用给每个 FREQ_* 单独配。

    python grant_listen.py            # 看当前 ACL，不改
    python grant_listen.py --apply    # 真的写进去

在 Mumble 主机上跑（Ice 只监听 127.0.0.1）。要先能 import MumbleServer，
生成办法和 login.py 一样：

    slice2py /usr/share/mumble-server/MumbleServer.ice
"""

import sys

import Ice

try:
    import MumbleServer as MumbleIce          # Mumble 1.5+
except ImportError:                            # 1.4 及更早叫 Murmur
    import Murmur as MumbleIce

# 和 login.py 保持一致
ICE_PROXY = "Meta:tcp -h 127.0.0.1 -p 6502"
ICE_SECRET = "yoyo14185721"
SERVER_ID = 1

ROOT_CHANNEL = 0

# ACL 权限位，取自 mumble 的 src/ACL.h（enum Perm）
PERM_WRITE = 0x1
PERM_TRAVERSE = 0x2
PERM_ENTER = 0x4
PERM_SPEAK = 0x8
PERM_WHISPER = 0x100
PERM_TEXT_MESSAGE = 0x200
PERM_MAKE_TEMP_CHANNEL = 0x400
PERM_LISTEN = 0x800

PERM_NAMES = [
    (PERM_WRITE, "Write"), (PERM_TRAVERSE, "Traverse"), (PERM_ENTER, "Enter"),
    (PERM_SPEAK, "Speak"), (0x10, "MuteDeafen"), (0x20, "Move"),
    (0x40, "MakeChannel"), (0x80, "LinkChannel"), (PERM_WHISPER, "Whisper"),
    (PERM_TEXT_MESSAGE, "TextMessage"),
    (PERM_MAKE_TEMP_CHANNEL, "MakeTempChannel"), (PERM_LISTEN, "Listen"),
]


def describe(mask):
    """把权限位翻译成人看得懂的名字。"""
    if not mask:
        return "无"
    names = [name for bit, name in PERM_NAMES if mask & bit]
    extra = mask & ~sum(bit for bit, _ in PERM_NAMES)
    if extra:
        names.append(f"0x{extra:x}")
    return " ".join(names) or f"0x{mask:x}"


def show(acls, groups, inherit):
    print(f"\n根频道当前 ACL（继承父级={inherit}，共 {len(acls)} 条）：")
    for i, acl in enumerate(acls):
        who = acl.group if acl.userid < 0 else f"用户#{acl.userid}"
        scope = []
        if acl.applyHere:
            scope.append("本频道")
        if acl.applySubs:
            scope.append("子频道")
        print(f"  [{i}] {who:12} 作用于 {'+'.join(scope) or '无'}")
        print(f"       允许: {describe(acl.allow)}")
        if acl.deny:
            print(f"       拒绝: {describe(acl.deny)}")
    if groups:
        print(f"组：{', '.join(g.name for g in groups)}")


def find_all_group_acl(acls):
    """找到给 all 组、且作用于子频道的那条——临时频率频道靠它继承。

    只认非继承的：继承来的 ACL 是只读的，改了写不回去。
    """
    for acl in acls:
        if (acl.userid < 0 and acl.group == "all"
                and acl.applySubs and not acl.inherited):
            return acl
    return None


def main():
    apply = "--apply" in sys.argv

    init_data = Ice.InitializationData()
    init_data.properties = Ice.createProperties()
    # login.py 也这么设：服务端用的是 1.0 编码
    init_data.properties.setProperty("Ice.Default.EncodingVersion", "1.0")
    init_data.properties.setProperty("Ice.MessageSizeMax", "65536")

    with Ice.initialize(init_data) as communicator:
        meta = MumbleIce.MetaPrx.checkedCast(
            communicator.stringToProxy(ICE_PROXY))
        if not meta:
            print("连不上 Mumble 的 Ice 接口，检查 mumble-server.ini 里的 ice= 配置")
            return 1

        # 所有 Ice 调用都要带 secret，否则抛 InvalidSecretException
        context = {"secret": ICE_SECRET}
        server = meta.getServer(SERVER_ID, context)
        if not server:
            print(f"拿不到 {SERVER_ID} 号虚拟服务器")
            return 1
        server = MumbleIce.ServerPrx.checkedCast(server.ice_timeout(5000))

        acls, groups, inherit = server.getACL(ROOT_CHANNEL, context)
        show(acls, groups, inherit)

        target = find_all_group_acl(acls)
        if target is None:
            print("\n根频道上没有给 all 组、作用于子频道的 ACL。"
                  "这不太正常，建议用 Mumble 客户端手工看一眼再决定怎么改。")
            return 1

        if target.allow & PERM_LISTEN:
            print("\nListen 权限已经放开了。管制端还报没权限的话，"
                  "看看是不是 mumble-server.ini 里的 listenersperuser / "
                  "listenersperchannel 限制了数量。")
            return 0

        print(f"\n缺 Listen（0x{PERM_LISTEN:x}）。"
              f"打算把 all 组的允许位从 {describe(target.allow)} "
              f"改成 {describe(target.allow | PERM_LISTEN)}。")

        if not apply:
            print("\n这是预览。确认没问题就加 --apply 真的写进去。")
            return 0

        # getACL 会把继承来的 ACL 一起返回，而那些是只读的，原样写回去会在本
        # 频道复制出一份。根频道没有父级所以本来就不会有，但别依赖这个巧合。
        own = [acl for acl in acls if not acl.inherited]
        if len(own) != len(acls):
            print(f"（略过 {len(acls) - len(own)} 条继承来的 ACL，它们是只读的）")

        target.allow |= PERM_LISTEN
        server.setACL(ROOT_CHANNEL, own, groups, inherit, context)
        print("已写入。")

        # 回读确认，别只信写入没抛异常
        acls, _, _ = server.getACL(ROOT_CHANNEL, context)
        again = find_all_group_acl(acls)
        if again and again.allow & PERM_LISTEN:
            print("回读确认：Listen 已生效。管制端重连一次即可。")
            return 0
        print("回读没看到 Listen，写入可能没成功。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
