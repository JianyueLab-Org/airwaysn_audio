"""pymumble 在新版 Python 上的兼容补丁。

pymumble 1.6.1 建 TLS 用的是 `ssl.wrap_socket()`——这个函数 Python 3.7 起弃用、
**3.12 已经删除**。而它那段调用长这样（mumble.py 的 connect）：

    try:
        self.control_socket = ssl.wrap_socket(...)          # 3.12 上抛 AttributeError
    except AttributeError:
        self.control_socket = ssl.wrap_socket(..., ssl.PROTOCOL_TLSv1)   # 同一个函数，再抛一次

兜底分支调的是同一个已被删除的函数，所以异常直接冲出去。又因为这一切发生在
pymumble 自己的线程里，调用方只看到"线程没了"，界面上就显示成"服务器拒绝了
连接"——把人往密码上引，而实际上连 TLS 握手都没开始（服务器日志里只会看到
连上又立刻断开）。

这里补一个等价实现。Mumble 用的是自签证书，官方客户端也是靠证书指纹而不是 CA
链来认服务器，所以这里不做校验——和被删掉的 `ssl.wrap_socket` 的默认行为
（cert_reqs=CERT_NONE）保持一致，不会比原来更宽松。
"""

import logging
import select
import ssl
import time

log = logging.getLogger("mumblecompat")

# 发不动时最多等多久再判定为真的断了。等待期间 pymumble 的主循环是停着的
# （收不到别人的话音），所以不能太长；但比起丢连接重连（实测五秒起步，还要重新
# 进频道）又划算得多。
SEND_TIMEOUT = 2.0


class _SendStats:
    """上行堵了多少次、最久一次等了多久。

    单独统计而不是每次都记日志：上行紧张时一秒能进几十次重试，逐条记只会把
    真正的线索淹掉。调用方（voice.py / broadcast.py）在掉线时和定期各取一次
    汇总——**「掉线前这一分钟堵过 37 次、最久 0.8 秒」比任何一行流水都说明问题**，
    它直接区分了"上行真的不行"和"只是一次瞬时抖动"。
    """

    def __init__(self):
        self.blocked = 0          # 写不动过多少次
        self.gave_up = 0          # 其中彻底放弃（判定断线）的次数
        self.worst = 0.0          # 最久的一次等待
        self.total = 0.0          # 累计等待时间

    def record(self, waited, gave_up=False):
        self.blocked += 1
        self.total += waited
        self.worst = max(self.worst, waited)
        if gave_up:
            self.gave_up += 1

    def drain(self):
        """取走并清零。返回 None 表示这段时间一次都没堵过。"""
        if not self.blocked:
            return None
        summary = (f"the send buffer was full {self.blocked} time(s) "
                   f"(worst {self.worst * 1000:.0f} ms, {self.total:.1f} s total"
                   + (f", {self.gave_up} of them gave up" if self.gave_up else "")
                   + ")")
        self.__init__()
        return summary


_stats = _SendStats()


def send_stats():
    """取走自上次调用以来的上行拥塞汇总，没有就返回 None。"""
    return _stats.drain()


class _RetryingSocket:
    """给非阻塞 socket 的 `send()` 补上"等它可写再续发"。

    **这是语音"一发话就掉线"的病根。** pymumble 没有 UDP 通道：话音被塞进
    `UDPTUNNEL` 消息，和控制消息走**同一条 TCP**（soundoutput.py 的
    `tcppacket = ... # encapsulate in tcp tunnel`）。而那条 socket 在
    `connect()` 里被设成了非阻塞，发送循环却是照 C 的写法兜错的：

        sent = self.mumble_object.control_socket.send(tcppacket)
        if sent < 0:                     # Python 里 send 是抛异常，不是返回 -1
            raise socket.error(...)

    于是上行稍微堵一下、内核发送缓冲一满，`send()` 抛 `BlockingIOError`
    （TLS 上是 `SSLWantWriteError`），一路穿到 `Mumble.run()` 的
    `except socket.error`，连接被判死、DISCONNECTED 发出去——**而网络上其实
    什么都没断**。实测日志里的形状正是这样：掉线都发生在按下 PTT 两三秒后，
    0.2 秒的短发话从不掉，而且每次都是第一次重连就立刻成功。

    这一层只改"写不动"这一种情况：等这条 socket 可写再续发，等满
    SEND_TIMEOUT 才把异常放出去（那时候是真的堵死了，当断线处理是对的）。
    部分写入不用管——pymumble 两处发送循环本来就按返回值切片重发。
    """

    def __init__(self, sock, timeout=SEND_TIMEOUT):
        self._sock = sock
        self._timeout = timeout

    def __getattr__(self, name):
        # recv / close / fileno / getpeercert … 一律原样转交。select.select()
        # 认 fileno()，所以主循环那句 select 照常工作。
        return getattr(self._sock, name)

    def send(self, data):
        deadline = time.monotonic() + self._timeout
        waited = 0.0
        while True:
            try:
                sent = self._sock.send(data)
                if waited:
                    # 只统计，不每次都打日志——上行紧张时这里一秒能进几十次，
                    # 记成日志反而把真正的线索淹掉。汇总由调用方定期打印。
                    _stats.record(waited)
                return sent
            except (BlockingIOError, ssl.SSLWantWriteError) as e:
                error, readable = e, False
            except ssl.SSLWantReadError as e:
                # TLS 在重新协商时会要求先读到东西才写得出去
                error, readable = e, True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stats.record(waited, gave_up=True)
                log.warning("the control socket stayed unwritable for %.0f s, "
                            "treating it as a lost connection", self._timeout)
                raise error

            started = time.monotonic()
            if readable:
                select.select([self._sock], [], [], remaining)
            else:
                select.select([], [self._sock], [], remaining)
            waited += time.monotonic() - started


def guard_control_socket(mumble):
    """把某个 Mumble 对象的控制 socket 换成会重试的那个。

    幂等：已经换过就不再套一层。
    """
    sock = getattr(mumble, "control_socket", None)
    if sock is None or isinstance(sock, _RetryingSocket):
        return False
    mumble.control_socket = _RetryingSocket(sock)
    return True


def patch_send():
    """让每一条新建的连接都自动带上上面那层。返回是否真的打了补丁。

    挂在 `Mumble.connect()` 之后而不是让每个客户端自己去包：连接是 pymumble
    在**它自己的线程里**建的，而且每次重连都会重建一次 socket——漏掉任何一次
    重连，那条连接就退回旧行为，而症状（发话时掉线）只在网络不好的用户那里出现，
    自己这边根本复现不了。
    """
    try:
        from pymumble_py3.mumble import Mumble
    except Exception as e:                       # 打包环境里理论上不会发生
        log.warning("could not patch the pymumble send path: %s", e)
        return False

    if getattr(Mumble.connect, "_airwaysn_guarded", False):
        return False

    original = Mumble.connect

    def connect(self):
        state = original(self)
        try:
            guard_control_socket(self)
        except Exception as e:
            # 包不上就照旧跑，只是会退回"发不动就掉线"的老行为
            log.warning("could not guard the control socket: %s", e)
        return state

    connect._airwaysn_guarded = True
    Mumble.connect = connect
    log.info("patched pymumble so a full send buffer no longer looks like a "
             "lost connection")
    return True


def install():
    """打上 pymumble 需要的两个补丁。返回值只说 ssl.wrap_socket 补没补。

    发送那个补丁在这里一并打上：所有客户端都在 import pymumble **之前**调
    install()，这是唯一一处四个组件都必经的地方。
    """
    patched_ssl = _install_wrap_socket()
    patch_send()
    return patched_ssl


def _install_wrap_socket():
    """需要的话补上 ssl.wrap_socket。返回是否真的打了补丁。"""
    if hasattr(ssl, "wrap_socket"):
        return False

    def wrap_socket(sock, keyfile=None, certfile=None, cert_reqs=ssl.CERT_NONE,
                    ca_certs=None, **kwargs):
        # 忽略调用方传进来的 ssl_version：那些常量在新版里也已弃用，
        # 交给 SSLContext 自己协商更稳妥
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # 顺序不能反：check_hostname 还是 True 时设 CERT_NONE 会抛 ValueError
        context.check_hostname = False
        context.verify_mode = cert_reqs
        if ca_certs:
            context.load_verify_locations(ca_certs)
        if certfile:
            context.load_cert_chain(certfile, keyfile)
        return context.wrap_socket(sock)

    ssl.wrap_socket = wrap_socket
    log.info("reinstated ssl.wrap_socket for pymumble (removed from Python "
             "3.12 onwards)")
    return True
