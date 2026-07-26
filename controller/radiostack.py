"""电台栈：管制员同时守听/发话的一组频率。

模型照搬 TrackAudio 的 radioStore：每个频率是一部"电台"，各自有 RX / TX / XC
三个开关，PTT 按下时对所有开了 TX 的频率同时发话。三个开关之间的联动规则也和
TrackAudio 一致（src/renderer/src/components/radio/radio.tsx）：

    关掉 RX  →  TX、XC 一起关（收都不收了，发和耦合无从谈起）
    打开 TX  →  自动打开 RX（不可能只发不收）
    打开 XC  →  自动打开 RX 和 TX

XC（cross couple，交叉耦合）指把这个频率和其它同样开了 XC 的频率连起来：在其中
一个频率上收到的话音会转发到另一个上，管制员合并席位时用。

这里只有状态，没有任何 I/O —— 语音那一侧在 voice.py。
"""


def parse_frequency(text):
    """把 "118.000" 这样的频率解析成整数千赫（118000）。"""
    value = str(text).strip()
    if not value:
        raise ValueError("频率不能为空")
    try:
        khz = int(round(float(value) * 1000))
    except (TypeError, ValueError):
        raise ValueError(f"频率格式无效: {text}")
    if not 100000 <= khz <= 199999:
        raise ValueError(f"频率超出甚高频范围: {text}")
    return khz


def format_frequency(khz):
    """118000 → "118.000"。"""
    return f"{khz / 1000:.3f}"


def channel_name(khz):
    """频率对应的 Mumble 频道名，全网统一的约定。"""
    return f"FREQ_{str(khz).zfill(6)}"


class Radio:
    """栈里的一部电台。"""

    def __init__(self, frequency_khz, callsign="", rx=False, tx=False, xc=False,
                 volume=100, muted=False):
        self.frequency_khz = frequency_khz
        self.callsign = callsign
        self.rx = rx
        self.tx = tx
        self.xc = xc
        self.volume = volume            # 0-100，和主音量相乘
        self.muted = muted

        # 运行时状态，不持久化
        self.currently_rx = False
        self.currently_tx = False
        self.last_received_callsign = ""
        self.last_received_at = 0.0

    @property
    def frequency(self):
        return format_frequency(self.frequency_khz)

    @property
    def channel(self):
        return channel_name(self.frequency_khz)

    @property
    def label(self):
        return f"{self.callsign} {self.frequency}" if self.callsign else self.frequency

    def effective_volume(self):
        return 0 if self.muted else self.volume

    def to_dict(self):
        return {
            "frequency": self.frequency_khz,
            "callsign": self.callsign,
            "rx": self.rx,
            "tx": self.tx,
            "xc": self.xc,
            "volume": self.volume,
            "muted": self.muted,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            int(data["frequency"]),
            data.get("callsign", ""),
            bool(data.get("rx", False)),
            bool(data.get("tx", False)),
            bool(data.get("xc", False)),
            int(data.get("volume", 100)),
            bool(data.get("muted", False)),
        )


class RadioStack:
    """一组电台。on_change 在任何状态变化后被调用，界面据此重画。"""

    def __init__(self, on_change=None):
        self._radios = []
        self.on_change = on_change
        self.selected_khz = None      # 主频率：真正进入的那个 Mumble 频道

    # ---------- 查询 ----------
    def __iter__(self):
        return iter(self._radios)

    def __len__(self):
        return len(self._radios)

    def get(self, khz):
        for radio in self._radios:
            if radio.frequency_khz == khz:
                return radio
        return None

    def radios(self):
        return list(self._radios)

    def rx_frequencies(self):
        return [r.frequency_khz for r in self._radios if r.rx]

    def tx_frequencies(self):
        return [r.frequency_khz for r in self._radios if r.tx]

    def xc_frequencies(self):
        return [r.frequency_khz for r in self._radios if r.xc]

    def selected(self):
        return self.get(self.selected_khz) if self.selected_khz else None

    # ---------- 增删 ----------
    def add(self, frequency, callsign=""):
        """加一部电台。频率重复时抛 ValueError。"""
        khz = parse_frequency(frequency) if not isinstance(frequency, int) else frequency
        if self.get(khz):
            raise ValueError(f"{format_frequency(khz)} 已经在电台栈里了")

        radio = Radio(khz, callsign.strip().upper())
        self._radios.append(radio)
        self._radios.sort(key=lambda r: r.frequency_khz)
        if self.selected_khz is None:
            self.selected_khz = khz
        self._changed()
        return radio

    def remove(self, khz):
        radio = self.get(khz)
        if not radio:
            return False
        self._radios.remove(radio)
        if self.selected_khz == khz:
            self.selected_khz = self._radios[0].frequency_khz if self._radios else None
        self._changed()
        return True

    def clear(self):
        self._radios = []
        self.selected_khz = None
        self._changed()

    # ---------- 开关（联动规则同 TrackAudio） ----------
    def set_rx(self, khz, value):
        radio = self.get(khz)
        if not radio:
            return None
        radio.rx = value
        if not value:
            # 不收了，发和耦合也就无从谈起
            radio.tx = False
            radio.xc = False
            radio.currently_rx = False
        elif self.selected_khz is None:
            self.selected_khz = khz
        self._changed()
        return radio

    def set_tx(self, khz, value):
        radio = self.get(khz)
        if not radio:
            return None
        radio.tx = value
        if value:
            radio.rx = True          # 不存在只发不收
        else:
            radio.xc = False
            radio.currently_tx = False
        self._changed()
        return radio

    def set_xc(self, khz, value):
        radio = self.get(khz)
        if not radio:
            return None
        radio.xc = value
        if value:
            radio.rx = True
            radio.tx = True
        self._changed()
        return radio

    def toggle_rx(self, khz):
        radio = self.get(khz)
        return self.set_rx(khz, not radio.rx) if radio else None

    def toggle_tx(self, khz):
        radio = self.get(khz)
        return self.set_tx(khz, not radio.tx) if radio else None

    def toggle_xc(self, khz):
        radio = self.get(khz)
        return self.set_xc(khz, not radio.xc) if radio else None

    def select(self, khz):
        """选主频率——真正进入的那个 Mumble 频道。"""
        if self.get(khz):
            self.selected_khz = khz
            self._changed()

    def set_volume(self, khz, volume):
        radio = self.get(khz)
        if radio:
            radio.volume = max(0, min(100, int(volume)))
            self._changed()

    def set_muted(self, khz, muted):
        radio = self.get(khz)
        if radio:
            radio.muted = bool(muted)
            self._changed()

    # ---------- 运行时状态 ----------
    def set_currently_rx(self, khz, value, callsign="", timestamp=0.0):
        radio = self.get(khz)
        if not radio:
            return None
        radio.currently_rx = value
        if value and callsign:
            radio.last_received_callsign = callsign
            radio.last_received_at = timestamp
        self._changed()
        return radio

    def set_currently_tx(self, value):
        for radio in self._radios:
            radio.currently_tx = value and radio.tx
        self._changed()

    # ---------- 持久化 ----------
    def to_list(self):
        return [radio.to_dict() for radio in self._radios]

    def load(self, entries):
        """从设置里恢复电台栈。运行时状态不恢复。"""
        self._radios = []
        for entry in entries or []:
            try:
                self._radios.append(Radio.from_dict(entry))
            except (KeyError, TypeError, ValueError) as e:
                print(f"[电台栈] 跳过一条无法识别的记录: {e}")
        self._radios.sort(key=lambda r: r.frequency_khz)
        active = [r for r in self._radios if r.rx]
        self.selected_khz = (active or self._radios)[0].frequency_khz if self._radios else None
        self._changed()

    def _changed(self):
        if self.on_change:
            self.on_change()
