# 处理mumble的登录逻辑
# https://airwaysn.org/api/v1/public/auth
# json:{ "cid":"1000", "password": "1234"}
# 正确返回200，错误返回400
#
# Mumble 1.5 起 Ice 的 slice 模块由 Murmur 改名为 MumbleServer，接口本身没变
# （ServerAuthenticator 的方法和签名逐条对过 v1.5.735 的 MumbleServer.ice）。
# 生成绑定：
#     slice2py /usr/share/mumble-server/MumbleServer.ice     # Debian 13
import sys
import Ice
import requests
import json
import traceback
import re

try:
    import MumbleServer as MumbleIce          # Mumble 1.5+
except ImportError:                            # 1.4 及更早叫 Murmur
    import Murmur as MumbleIce

ICE_PROXY = "Meta:tcp -h 127.0.0.1 -p 6502"
ICE_SECRET = ""            # 对应配置文件里的 icesecretwrite
SERVER_ID = 1


class AuthenticatorI(MumbleIce.ServerAuthenticator):
    def __init__(self, server, adapter, context=None):
        self.server = server
        self.adapter = adapter
        self.context = context or {}
        self.online_users = {}  # 用户名 -> session，由 ServerCallbackI 维护

    def kick_previous_session(self, name):
        """同名账号已经在线就把旧会话踢掉。

        踢人要带上 secret context，否则服务端会抛 InvalidSecretException。
        """
        old_session = self.online_users.get(name)
        if old_session is None:
            return
        try:
            self.server.kickUser(old_session, "您的账号在其他位置登录", self.context)
        except Exception as e:
            print(f"踢出用户失败: {e}")

    def authenticate(self, name, pw, certificates, certhash, certstrong, current=None):
        try:
            print(f"认证用户: {name}")
            # 检查是否是ATIS登录
            atis_pattern = re.compile(r"^.*_atis\d{6}")
            if atis_pattern.match(name):
                print(f"匹配到ATIS登录: {name}")
                if login_ATIS(name, pw):
                    self.kick_previous_session(name)
                    # 提取atis后面的6位数字作为用户ID
                    atis_id = name.split("_atis")[1]
                    return (int(atis_id), name, [])
            else:
                if login(name, pw):
                    self.kick_previous_session(name)
                    return (int(name), name, [])
            return (-1, "", [])
        except Exception as e:
            print(f"认证异常: {e}")
            traceback.print_exc()
            return (-1, "", [])

    def nameToId(self, name, current=None):
        try:
            return int(name)
        except:
            return -2

    def idToName(self, id, current=None):
        return str(id)

    def getInfo(self, id, current=None):
        return (False, {})

    def idToTexture(self, id, current=None):
        return []


class ServerCallbackI(MumbleIce.ServerCallback):
    """在线用户的进出通知。

    userConnected / userDisconnected 属于 ServerCallback，不属于
    ServerAuthenticator——以前把它们写在认证器里，但从没调用 addCallback，
    所以那两个方法从来没被触发过，online_users 一直是空的，"同一账号在别处
    登录就踢掉旧会话"其实从未生效。这里单独注册回调把这条链路补上。

    ServerCallback 的七个方法都要实现：Ice 会按接口分发，缺一个就会在服务端
    触发那个事件时报错。
    """

    def __init__(self, authenticator):
        self.authenticator = authenticator

    def userConnected(self, user, current=None):
        self.authenticator.online_users[user.name] = user.session
        print(f"用户 {user.name} 已连接，session: {user.session}")

    def userDisconnected(self, user, current=None):
        self.authenticator.online_users.pop(user.name, None)
        print(f"用户 {user.name} 已断开连接")

    def userStateChanged(self, user, current=None):
        pass

    def userTextMessage(self, user, message, current=None):
        pass

    def channelCreated(self, state, current=None):
        pass

    def channelRemoved(self, state, current=None):
        pass

    def channelStateChanged(self, state, current=None):
        pass


url = "https://airwaysn.org/api/v1/public/auth"
def login(cid, password):
    global url
    # url = "https://airwaysn.org/api/v1/public/auth"
    headers = {
        "Content-Type": "application/json",
    }
    data = {
        "cid": str(cid),
        "password": str(password),
    }
    print(f"登录请求: {data}")
    try:
        response = requests.post(url, headers=headers, data=json.dumps(data))
        if response.status_code == 200:
            print (f"登录成功: {response.status_code}, {response.text}")
            return True

        else:
            print (f"登录失败: {response.status_code}, {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"请求错误: {e}")
        return False

def login_ATIS(cid, password):
    # ATIS登录逻辑，ATIS登录时遵循用户名：DDDD_atisDDDDDD的格式
    print(f"ATIS登录: {cid}, {password}")
    # 获取真正的用户ID（ATIS前面的数字）
    cid = cid.split("_atis")[0]

    # 特殊处理ID
    if cid == "900" and password == "p@ssw0rd":
        return True

    headers = {
        "Content-Type": "application/json",
    }
    data = {
        "cid": str(cid),
        "password": str(password),
    }
    print(f"登录请求: {data}")
    try:
        response = requests.post(url, headers=headers, data=json.dumps(data))
        if response.status_code == 200:
            print (f"ATIS登录成功: {response.status_code}, {response.text}")
            return True

        else:
            print (f"登录失败: {response.status_code}, {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"请求错误: {e}")
        return False



def main():
    # 显式设置 Ice 编码版本为 1.0
    init_data = Ice.InitializationData()
    init_data.properties = Ice.createProperties()
    init_data.properties.setProperty("Ice.Default.EncodingVersion", "1.0")

    with Ice.initialize(init_data) as communicator:
        # 设置Ice连接，强制代理使用 1.0 编码
        base = communicator.stringToProxy(ICE_PROXY)
        meta = MumbleIce.MetaPrx.checkedCast(base)
        if not meta:
            print("无法连接到 Mumble 服务器")
            return

        # 使用正确的ice secret
        context = {"secret": ICE_SECRET}
        server = meta.getServer(SERVER_ID, context)
        if not server:
            print("无法获取服务器实例")
            return

        # 创建Ice适配器
        adapter = communicator.createObjectAdapterWithEndpoints(
            "Authenticator", "tcp -h 127.0.0.1")
        adapter.activate()

        # 创建并注册认证器
        auth = AuthenticatorI(server, adapter, context)
        auth_prx = MumbleIce.ServerAuthenticatorPrx.checkedCast(adapter.addWithUUID(auth))

        # 在线用户的进出通知，认证时靠它判断要不要踢掉旧会话
        callback = ServerCallbackI(auth)
        callback_prx = MumbleIce.ServerCallbackPrx.checkedCast(adapter.addWithUUID(callback))

        try:
            server.setAuthenticator(auth_prx, context)
            print("认证器已设置")

            server.addCallback(callback_prx, context)
            print("服务器回调已注册")

            # 补上注册回调之前就已经在线的用户
            try:
                for user in server.getUsers(context).values():
                    auth.online_users[user.name] = user.session
                print(f"当前在线 {len(auth.online_users)} 人")
            except Exception as e:
                print(f"获取在线用户失败: {e}")

            # 保持程序运行
            communicator.waitForShutdown()
        except Ice.Exception as e:
            print(f"Ice异常: {e}")
            traceback.print_exc()
        finally:
            # 清理
            try:
                server.removeCallback(callback_prx, context)
            except:
                pass
            try:
                server.setAuthenticator(None, context)
            except:
                pass

if __name__ == "__main__":
    main()
