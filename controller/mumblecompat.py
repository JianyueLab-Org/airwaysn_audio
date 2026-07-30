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
import ssl

log = logging.getLogger("mumblecompat")


def install():
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
