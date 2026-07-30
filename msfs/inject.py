"""把他机放进 MSFS。

这是 MSFS 版相对 xpc 最大的简化：**不需要插件**。X-Plane 的绘图 API 只有插件
够得着，所以 xpc 要拆成两个进程加一条 UDP 通道；SimConnect 允许外部进程直接
建 AI 飞机，一个进程就够了。

TCAS 也是白送的——AI 飞机是真的 SimObject，座舱的交通显示本来就看得见，不像
X-Plane 还要单独去填 sim/cockpit2/tcas/targets/*。

    创建   AICreateNonATCAircraft(title, 尾号, 初始位置, requestID)
    移动   SetDataOnSimObject(objectID, 位置定义)
    删除   AIRemoveObject(objectID, requestID)

**objectID 是异步回来的，而且必须自己关联。** 创建函数只是把请求发出去，真正的
objectID 通过 SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID 消息回来，靠 dwRequestID 对
上是哪一次创建。Python-SimConnect 自带的 dispatch 确实处理了这条消息，但它把结果
塞进 os.environ["SIMCONNECT_OBJECT_ID"]，**不带 RequestID**——同时建 20 架飞机
时全写同一个变量，根本分不清谁是谁。所以这里接管了 dispatch：先按 requestID 记
进自己的表，再把消息交回原来的处理函数。
"""

import ctypes
import logging
import threading
import time

log = logging.getLogger("inject")

# 我们自己的 requestID 从这里开始编，避开包里内建请求用的号段
REQUEST_BASE = 10000

# 一次最多放多少架。MSFS 对 AI 数量没有硬上限，但每架都是完整的飞机模型，
# 放太多会掉帧——按距离排序后截断，远的先扔。
MAX_AIRCRAFT = 40


class _Definition:
    """SetDataOnSimObject 用的数据定义。

    字段顺序必须和写进去的结构体逐项对应，错位了飞机会出现在地球另一边。
    """

    FIELDS = [
        (b"PLANE LATITUDE", b"degrees"),
        (b"PLANE LONGITUDE", b"degrees"),
        (b"PLANE ALTITUDE", b"feet"),
        (b"PLANE PITCH DEGREES", b"degrees"),
        (b"PLANE BANK DEGREES", b"degrees"),
        (b"PLANE HEADING DEGREES TRUE", b"degrees"),
        (b"AIRSPEED TRUE", b"knots"),
        (b"SIM ON GROUND", b"Bool"),
    ]


class TrafficInjector:
    """把 TrafficTable 的快照映射成 MSFS 里的 AI 飞机。"""

    def __init__(self, sim, definition_id=1000):
        self.sim = sim
        self.definition_id = definition_id
        self.aircraft = {}          # 呼号 -> {object_id, title, request_id}
        self.available = False

        self._lock = threading.Lock()
        self._pending = {}          # requestID -> 呼号（等 objectID 回来）
        self._requested_titles = {}  # requestID -> 模型名，出错时才知道是谁
        self._assigned = {}         # requestID -> objectID
        # 已经不要了、但 objectID 还在路上的那些请求。飞机在模拟器里是**已经建
        # 出来**的，等号码回来必须补一刀删掉，否则它会以最后的位置永久停在天上。
        self._orphaned = set()
        self.bad_titles = set()     # 模拟器拒绝生成过的模型；匹配时要排除掉
        self._next_request = REQUEST_BASE
        self._enums = None

        try:
            self._setup()
            self.available = True
        except Exception as e:
            log.warning("traffic injection is unavailable: %s", e)

    # ---------- 初始化 ----------
    def _setup(self):
        from SimConnect import Enum as sc_enum

        self._enums = sc_enum
        self._install_dispatch()
        self._define_position()

    def _install_dispatch(self):
        """接管 ASSIGNED_OBJECT_ID，按 requestID 关联。

        包自带的处理只写一个环境变量，不带 requestID，多架飞机同时创建时无法
        区分。这里在它前面插一层，记完再原样交回去。
        """
        sc = self.sim
        enums = self._enums
        original = sc.my_dispatch_proc

        def dispatch(pData, cbData, pContext):
            try:
                kind = pData.contents.dwID
                if (kind ==
                        enums.SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID):
                    body = ctypes.cast(
                        pData,
                        ctypes.POINTER(enums.SIMCONNECT_RECV_ASSIGNED_OBJECT_ID)
                    ).contents
                    with self._lock:
                        self._assigned[int(body.dwRequestID)] = int(body.dwObjectID)
                elif kind == enums.SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EXCEPTION:
                    self._note_exception(ctypes.cast(
                        pData,
                        ctypes.POINTER(enums.SIMCONNECT_RECV_EXCEPTION)).contents)
            except Exception as e:
                log.debug("handling a SimConnect message raised: %s", e)
            return original(pData, cbData, pContext)

        sc.my_dispatch_proc = dispatch

    def _note_exception(self, body):
        """SimConnect 的异步错误。

        建 AI 飞机失败是通过这条消息回来的，包自带的日志只打一个
        SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED，不说是哪架、哪个模型——
        实测日志里就是这样，完全没法查。这里把模型名补上。

        dwSendID 对应发出去的那个包，但要 GetLastSentPacketID 才能对上；我们
        没记那个，所以退而求其次：把最近一次请求过的模型都列出来。这已经足够
        指认是哪个模型建不出来了。
        """
        exceptions = self._enums.SIMCONNECT_EXCEPTION
        # 编号从枚举里取，别硬编码——CREATE_OBJECT_FAILED 是 22，我一开始
        # 按"排第 12 位"猜成了 12，那其实是 TOO_MANY_REQUESTS。
        interesting = {
            int(exceptions.SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED):
                "建不出来（模型名对不上，或这个机型不能作为 AI 生成）",
            int(exceptions.SIMCONNECT_EXCEPTION_OBJECT_OUTSIDE_REALITY_BUBBLE):
                "位置太远，超出模拟器的加载范围",
            int(exceptions.SIMCONNECT_EXCEPTION_OBJECT_CONTAINER):
                "模型容器有问题（装得不完整？）",
        }
        try:
            code = int(body.dwException)
        except Exception:
            return
        reason = interesting.get(code)
        if reason is None:
            return

        with self._lock:
            titles = list(self._requested_titles.values())
            waiting = list(self._pending.values())
        log.warning("the simulator refused to create traffic: %s. waiting: %s, "
                    "models in use: %s", reason, waiting or "none",
                    titles or "none")
        # 模型建不出来才拉黑；位置太远是暂时的，换个地方就好了，别把好模型
        # 永久排除掉
        blacklist = code == int(exceptions.SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED)
        with self._lock:
            if blacklist:
                self.bad_titles.update(titles)
            self._requested_titles.clear()
            for callsign in waiting:
                record = self.aircraft.get(callsign)
                if record is not None and record.get("object_id") is None:
                    self.aircraft.pop(callsign, None)
            # 这里把所有在等的请求都丢掉了，可其中有些是会建成功的——它们的
            # objectID 随后就到，没人认领就成了天上的幽灵。全部记成待补删，
            # 真没建出来的那几个 AIRemoveObject 会自己失败，无害。
            self._orphaned.update(self._pending)
            self._pending.clear()

    def _define_position(self):
        """注册位置的数据定义。只需要做一次。"""
        for name, unit in _Definition.FIELDS:
            hr = self.sim.dll.AddToDataDefinition(
                self.sim.hSimConnect, self.definition_id, name, unit,
                self._enums.SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64,
                0, self._enums.SIMCONNECT_UNUSED)
            if hr != 0:
                raise RuntimeError(f"AddToDataDefinition({name!r}) 失败: {hr}")

    def _request_id(self):
        with self._lock:
            self._next_request += 1
            return self._next_request

    # ---------- 同步 ----------
    def sync(self, entries):
        """让模拟器里的 AI 飞机和这份快照一致。

        entries 是 traffic.TrafficTable.snapshot() 的输出，已经按距离排好序。
        """
        if not self.available:
            return
        self._collect_assigned()

        entries = entries[:MAX_AIRCRAFT]
        seen = set()
        for entry in entries:
            callsign = entry.get("callsign")
            if not callsign:
                continue
            seen.add(callsign)
            try:
                self._sync_one(callsign, entry)
            except Exception as e:
                log.debug("updating %s raised: %s", callsign, e)

        for callsign in [c for c in self.aircraft if c not in seen]:
            self.remove(callsign)

    def _collect_assigned(self):
        """把已经回来的 objectID 认领到对应的飞机上。

        还要收拾"号码回来晚了"的那些：飞机一创建就飞出范围（或者中途来了个
        SimConnect 异常）时，remove() 那会儿 object_id 还是 None，删不了——可是
        模拟器那边是真的建出来了。不在这里补删的话，它会以最后的位置永远停在
        天上，而我们连它的号码都不再记得。
        """
        with self._lock:
            ready = [(rid, oid) for rid, oid in self._assigned.items()
                     if rid in self._pending]
            for rid, oid in ready:
                callsign = self._pending.pop(rid)
                self._assigned.pop(rid, None)
                self._requested_titles.pop(rid, None)
                record = self.aircraft.get(callsign)
                if record is not None:
                    record["object_id"] = oid
                    log.info("%s is in the simulator (object %d, model %s)",
                             callsign, oid, record.get("title", "?"))
            strays = [(rid, self._assigned.pop(rid))
                      for rid in list(self._assigned) if rid in self._orphaned]
            for rid, _ in strays:
                self._orphaned.discard(rid)
                self._requested_titles.pop(rid, None)

        # DLL 调用放到锁外面：_request_id() 自己也要拿这把锁
        for _, object_id in strays:
            try:
                self.sim.dll.AIRemoveObject(self.sim.hSimConnect, object_id,
                                            self._request_id())
                log.info("swept up an aircraft that was no longer needed (object %d)",
                         object_id)
            except Exception as e:
                log.debug("the traffic sweep raised: %s", e)

    def _sync_one(self, callsign, entry):
        record = self.aircraft.get(callsign)
        title = entry.get("model") or ""

        if record is not None and title and record.get("title") != title:
            # 机型问到了或者变了，换模型只能删了重建
            self.remove(callsign)
            record = None

        if record is None:
            if not title:
                return          # 还没匹配到模型，等下一轮
            self._create(callsign, entry, title)
            return

        object_id = record.get("object_id")
        if object_id is None:
            return              # 还在等 objectID
        self._move(object_id, entry)

    def _create(self, callsign, entry, title):
        if title in self.bad_titles:
            # 这个模型已经证明建不出来，别每轮都再试一次
            return

        init = self._enums.SIMCONNECT_DATA_INITPOSITION()
        init.Latitude = entry["latitude"]
        init.Longitude = entry["longitude"]
        init.Altitude = entry["altitude"]
        init.Pitch = entry.get("pitch", 0.0)
        init.Bank = entry.get("bank", 0.0)
        init.Heading = entry.get("heading", 0.0)
        init.OnGround = 1 if entry.get("on_ground") else 0
        init.Airspeed = int(entry.get("groundspeed", 0))

        request_id = self._request_id()
        hr = self.sim.dll.AICreateNonATCAircraft(
            self.sim.hSimConnect, title.encode("utf-8"),
            callsign.encode("utf-8")[:12], init, request_id)
        if hr != 0:
            log.warning("creating %s failed (model %r): HRESULT %s", callsign, title, hr)
            self.bad_titles.add(title)
            return

        with self._lock:
            self._pending[request_id] = callsign
            self._requested_titles[request_id] = title
        self.aircraft[callsign] = {"object_id": None, "title": title,
                                   "request_id": request_id,
                                   "requested_at": time.time()}
        log.info("asking the simulator to create %s: model %r, request %d",
                 callsign, title, request_id)

    def _move(self, object_id, entry):
        values = (ctypes.c_double * len(_Definition.FIELDS))(
            entry["latitude"], entry["longitude"], entry["altitude"],
            entry.get("pitch", 0.0), entry.get("bank", 0.0),
            entry.get("heading", 0.0), float(entry.get("groundspeed", 0)),
            1.0 if entry.get("on_ground") else 0.0)
        self.sim.dll.SetDataOnSimObject(
            self.sim.hSimConnect, self.definition_id, object_id,
            0, 0, ctypes.sizeof(values), values)

    def remove(self, callsign):
        record = self.aircraft.pop(callsign, None)
        if not record:
            return
        object_id = record.get("object_id")
        with self._lock:
            request_id = record.get("request_id")
            was_pending = self._pending.pop(request_id, None) is not None
            if object_id is None and was_pending:
                # 号码还没回来。飞机在模拟器里已经建出来了，这里删不掉，
                # 记一笔等 _collect_assigned 补删。
                self._orphaned.add(request_id)
        if object_id is None:
            return
        try:
            self.sim.dll.AIRemoveObject(self.sim.hSimConnect, object_id,
                                        self._request_id())
            log.info("removed %s from the simulator", callsign)
        except Exception as e:
            log.debug("removing %s raised: %s", callsign, e)

    def clear(self):
        """全部清掉。断开连接和退出时都要调，否则飞机会留在天上不动。"""
        for callsign in list(self.aircraft):
            self.remove(callsign)
