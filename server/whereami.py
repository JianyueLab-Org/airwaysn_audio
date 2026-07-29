"""客户端"进不去频道、服务器也不报错"时，到服务器上取一次现场。

为什么需要这个脚本：Murmur 处理换频道的 `UserState` 时，**只有两条路径是完全
静默的**——目标频道在服务器上不存在，或者服务器认为你已经在里面了。除此之外
每一种拒绝（Enter、MoveUser、频道人满、令牌不对）都会回一条 `PermissionDenied`，
而四个客户端现在都接着那条回报。所以看到下面这种日志：

    → 发出进频道命令 FREQ_124550：会话号=111 从频道0 到频道1
    ← 进频道命令已入队 FREQ_124550
    发出了进入 FREQ_124550 的请求，但 5 秒内没有生效，稍后重试
    现场诊断 目标=FREQ_124550(id=1) 我的会话号=111 我在频道=0 表里有没有目标=True

客户端已经把它那一侧能知道的都说完了，再往下只能问服务器。这个脚本回答三件事：

    1. 服务器上到底有没有 id=1 这个频道？客户端的频道表是服务端推的，但推过来
       之后如果 ChannelRemove 丢了一条，本地就会留着一个服务器已经没有的号——
       客户端看着"表里有"，服务器收到 MoveCmd 却查不到，直接静默返回。
    2. 服务器认为这个用户在哪个频道？如果服务器已经把他放进去了、只是那条
       UserState 没回到客户端，客户端会一直重发一个"你已经在里面了"的请求，
       服务器每次都静默返回。
    3. 这个用户对这个频道到底有没有 Enter 权限？（用服务器自己的判定，不是猜
       ACL 位）

    python whereami.py                      # 全量：频道表 + 在线用户 + 权限矩阵
    python whereami.py FREQ_124550          # 只看这个频道
    python whereami.py FREQ_124550 1003     # 再指定要查的账号

在 Mumble 主机上跑（Ice 只监听 127.0.0.1），**要在客户端卡住的同时跑**——会话号
是一次连接一个，客户端一退这些就都没了。要先能 import MumbleServer，生成办法和
login.py 一样：

    slice2py /usr/share/mumble-server/MumbleServer.ice
"""

import sys

import Ice

import serverconf

try:
    import MumbleServer as MumbleIce          # Mumble 1.5+
except ImportError:                            # 1.4 及更早叫 Murmur
    import Murmur as MumbleIce

# 和 login.py / fix_acl.py 保持一致
ICE_PROXY = "Meta:tcp -h 127.0.0.1 -p 6502"
SERVER_ID = 1

ROOT_CHANNEL = 0

# 取自 mumble 的 src/ACL.h，和 fix_acl.py 同一套
PERM_ENTER = 0x4
PERM_SPEAK = 0x8
PERM_TRAVERSE = 0x2
PERM_MAKE_TEMP_CHANNEL = 0x400
PERM_LISTEN = 0x800

# 进一个频率频道真正要用到的几位
CHECKED = [
    (PERM_TRAVERSE, "Traverse"),
    (PERM_ENTER, "Enter"),
    (PERM_SPEAK, "Speak"),
    (PERM_LISTEN, "Listen"),
]

# 会让服务器拒绝进频道、但客户端很难猜到的几项配置
INTERESTING_CONF = [
    ("usersperchannel", "每个频道的人数上限，满了服务器会回 ChannelFull"),
    ("channelcountlimit", "服务器频道总数上限，满了建不出新的频率频道"),
    ("listenersperchannel", "每个频道能被多少人监听，管制端的 RX 靠它"),
    ("listenersperuser", "每个用户能监听多少频道，管制端的 RX 靠它"),
    ("messagelimit", "控制消息限流，超了服务器会直接丢消息"),
    ("messageburst", "限流的突发额度"),
]


def dump_channels(server, context, only=None):
    """频道表。回答"服务器上到底有没有这个号"。"""
    channels = server.getChannels(context)
    print(f"\n=== 服务器上的频道（共 {len(channels)} 个）===")
    for cid in sorted(channels):
        ch = channels[cid]
        if only and ch.name != only:
            continue
        kind = "临时" if ch.temporary else "永久"
        print(f"  id={cid:<5} {ch.name:<20} {kind}  父={ch.parent}")
    if only:
        hit = [c for c in channels.values() if c.name == only]
        if not hit:
            print(f"  ×  服务器上没有名叫 {only} 的频道。")
            print("     客户端的频道表里如果还有它，说明本地表比服务器旧——"
                  "MoveCmd 指向一个服务器查不到的号，服务器静默丢弃，"
                  "这就是那条不报错的失败。")
        elif len(hit) > 1:
            print(f"  ×  有 {len(hit)} 个频道都叫 {only}，"
                  f"号分别是 {[c.id for c in hit]}。")
            print("     客户端 find_by_name 只取第一个，很可能取到了错的那个。")
    return channels


def dump_users(server, context, channels, want_name=None):
    """在线用户，以及服务器认为他们各自在哪个频道。"""
    users = server.getUsers(context)
    print(f"\n=== 在线用户（共 {len(users)} 个）===")
    for session in sorted(users):
        u = users[session]
        if want_name and u.name != want_name:
            continue
        here = channels.get(u.channel)
        here_name = here.name if here else f"（表里没有 id={u.channel}）"
        print(f"  会话号={session:<5} 名字={u.name:<12} 账号id={u.userid:<6} "
              f"在频道={u.channel} {here_name}")
    return users


def dump_permissions(server, context, users, channels, only=None, want_name=None):
    """用服务器自己的判定问权限，不猜 ACL 位。"""
    if only:
        targets = [c for c in channels.values() if c.name == only]
    else:
        targets = [c for c in channels.values() if c.name.startswith("FREQ_")]
    if not targets:
        return
    print("\n=== 权限（服务器自己的判定）===")
    for session in sorted(users):
        u = users[session]
        if want_name and u.name != want_name:
            continue
        for ch in sorted(targets, key=lambda c: c.id):
            bits = []
            for bit, name in CHECKED:
                try:
                    ok = server.hasPermission(session, ch.id, bit, context)
                except Exception as e:
                    bits.append(f"{name}=?({e})")
                    continue
                bits.append(f"{name}={'有' if ok else '没有'}")
            print(f"  {u.name:<12} → {ch.name:<16}(id={ch.id})  " + "  ".join(bits))
    print("\n  注意：Enter 没有的话服务器会回 PermissionDenied，客户端是会报出来的。"
          "\n  所以如果这里 Enter 是'有'、客户端却一声不吭地进不去，那就不是权限问题，"
          "\n  要看上面的频道号和用户当前所在频道对不对得上。")


def dump_conf(server, context):
    print("\n=== 几项相关配置 ===")
    for key, why in INTERESTING_CONF:
        try:
            value = server.getConf(key, context)
        except Exception as e:
            value = f"取不到（{e}）"
        shown = value if value != "" else "（没设，用全局默认）"
        print(f"  {key:<20} = {shown:<24} {why}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    only = args[0] if args else None
    want_name = args[1] if len(args) > 1 else None

    init_data = Ice.InitializationData()
    init_data.properties = Ice.createProperties()
    # login.py 也这么设：服务端用的是 1.0 编码
    init_data.properties.setProperty("Ice.Default.EncodingVersion", "1.0")
    init_data.properties.setProperty("Ice.MessageSizeMax", "65536")

    try:
        ice_secret = serverconf.ice_secret()
    except serverconf.MissingSecret as e:
        print(f"启动失败: {e}")
        return 1

    with Ice.initialize(init_data) as communicator:
        meta = MumbleIce.MetaPrx.checkedCast(
            communicator.stringToProxy(ICE_PROXY))
        if not meta:
            print("连不上 Mumble 的 Ice 接口，检查 mumble-server.ini 里的 ice= 配置")
            return 1

        # 所有 Ice 调用都要带 secret，否则抛 InvalidSecretException
        context = {"secret": ice_secret}
        server = meta.getServer(SERVER_ID, context)
        if not server:
            print(f"拿不到 {SERVER_ID} 号虚拟服务器")
            return 1
        server = MumbleIce.ServerPrx.checkedCast(server.ice_timeout(5000))

        channels = dump_channels(server, context, only)
        users = dump_users(server, context, channels, want_name)
        dump_permissions(server, context, users, channels, only, want_name)
        dump_conf(server, context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
