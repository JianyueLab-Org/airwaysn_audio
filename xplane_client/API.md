API
===
-----
# X-Plane 无线电客户端

X-Plane 版本的 Mumble 无线电客户端。与 SimConnect（MSFS）版本功能相同，但通过 X-Plane UDP 协议获取无线电频率。

## 架构概览

```
gui.py          — PyQt6 图形界面（登录、频率显示、PTT 指示灯、设置）
radio.py        — 核心客户端（Mumble 连接、X-Plane UDP 频率读取、音频 I/O）
settings.py     — 设置持久化与管理对话框
xplane.py       — X-Plane UDP 协议示例（独立脚本）
```

## X-Plane 通信协议

### 发现 X-Plane
- 监听多播组 `239.255.1.1:49707`
- 接收 `BECN` 信标，解析出 X-Plane 的数据端口和 IP

### 读取频率
- 发送 `RREF` 请求到 X-Plane 的数据端口
- 数据引用：`sim/cockpit/radios/com1_freq_hz`
- 返回值单位：百 MHz（12540 = 125.40 MHz）

## 依赖

| 包 | 用途 |
|---|---|
| `PyQt6` | GUI 框架 |
| `pymumble_py3` | Mumble 语音通信 |
| `pyaudio` | 音频采集与播放 |
| `numpy` | 音频数据处理 |
| `pygame` | 摇杆输入 |
| `keyboard` | 键盘 PTT 检测 |

## 使用方式

```bash
python gui.py
```

1. 确保 X-Plane 正在运行且已进入飞行
2. 在 X-Plane 设置 → Data Output → IPs for UDP network 中添加本机 IP
3. 启动 `gui.py`，输入 Mumble 账号密码登录
4. 主界面显示 COM1 实时频率，按住 PTT 按键说话

## 与 SimConnect 版本的区别

| 项目 | SimConnect 版本 | X-Plane 版本 |
|---|---|---|
| 模拟器通信 | SimConnect SDK | X-Plane UDP 协议 |
| 频率读取 | `AircraftRequests.get("COM_ACTIVE_FREQUENCY:1")` | UDP RREF 请求 |
| 发现机制 | 无需（进程内通信） | 多播信标自动发现 |
| 配置文件 | `radio_settings.json` | `xplane_radio_settings.json` |
