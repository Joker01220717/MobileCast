# 架构设计

本文描述 MobileCast 的分层结构、线程模型与关键数据流。目标平台为 **Windows 10/11**。

## 1. 分层

```
┌─────────────────────────── UI 层（PySide6）───────────────────────────┐
│ MainWindow · DeviceList · MirrorView（视频渲染 + 键鼠捕获）             │
└────────────┬────────────────────────────────▲─────────────────────────┘
             │ InputEvent（Qt 信号）            │ VideoFrame（队列）
┌────────────▼────────────────────────────────┴─────────────────────────┐
│              会话层 core/device.py（DeviceSession 生命周期）             │
├───────────────────────────┬───────────────────────────────────────────┤
│ Android 适配器             │ iOS 适配器                                 │
│ core/android/*            │ core/ios/*                                 │
│ scrcpy-server 推流+注入    │ WDA 注入 + 帧采集                          │
├───────────────────────────┴───────────────────────────────────────────┤
│ 传输层：adb（USB / TCP）· usbmuxd · 本地 socket 隧道                     │
├────────────────────────────────────────────────────────────────────────┤
│ 解码层：PyAV（FFmpeg）→ RGB 帧；后续可切换 MediaFoundation 硬解           │
└────────────────────────────────────────────────────────────────────────┘
```

分层原则：

- **core 不依赖 Qt**。协议编解码、adb 封装都是纯标准库，方便单元测试与 CLI 复用。
- **UI 只认识抽象接口**（`DeviceSession`、`VideoFrame`、`InputEvent`），
  不直接接触 scrcpy / WDA 的细节。新增 iOS 通道时 UI 层零改动。
- **传输层可替换**：同一套会话逻辑可以跑在 USB、adb tcpip、未来的无线配对之上。

## 2. 线程 / 异步模型

| 执行单元 | 职责 | 说明 |
| --- | --- | --- |
| UI 主线程（Qt） | 渲染、键鼠事件捕获、输入注入写入 | 只做轻量工作，绝不阻塞 |
| 视频线程（每会话一条） | 读视频 socket → 切 NAL → 解码 → 最新帧槽位 | 读取与解码同线程，负载高时可拆分 |
| FramePump（QThread） | 从帧槽位拉帧 → 信号转发给 UI 渲染 | 跨线程走 Qt 信号队列 |
| adb 子进程 | `adb` / `scrcpy-server` 均为子进程 | 统一由 `Adb` 类管理，退出时清理 |

控制 socket 的写入由 UI 线程直接完成（`ScrcpyControlWriter` 内部加锁），
单条消息只有几十字节，同步写入的开销可忽略。

帧传递使用**单槽最新帧队列**（`queue.Queue(maxsize=1)` + 丢旧帧），
渲染永远追最新画面，保证低延迟而不是高流畅度。

## 3. 关键数据流

### 3.1 建立 Android 会话

```
UI: 连接(serial)
 └─ Adb.push(scrcpy-server.jar → /data/local/tmp)
 └─ Adb.forward(tcp:PORT1 → localabstract:scrcpy_<scid>)
 └─ Adb.forward(tcp:PORT2 → localabstract:scrcpy_<scid>_control)
 └─ Adb.popen_shell(app_process … com.genymobile.scrcpy.Server …)
 └─ socket.connect(127.0.0.1:PORT1)  → 读 64B 设备名 → 原始 H.264 流
 └─ socket.connect(127.0.0.1:PORT2)  → 控制通道（客户端 → 设备）
```

细节见 [scrcpy-protocol.md](scrcpy-protocol.md)。

### 3.2 输入注入

```
Qt mouseMoveEvent(x, y)
 └─ MirrorView.widget_to_device()      # letterbox 坐标映射
 └─ TouchEvent(action=MOVE, x, y, screen_w, screen_h)
 └─ MainWindow._send_input() → AndroidSession.send_touch()
 └─ scrcpy_control.encode_inject_touch()   # 32 字节大端消息
 └─ ScrcpyControlWriter → 控制 socket
```

坐标映射规则：视频按比例缩放并居中（letterbox），窗口留黑边部分直接忽略；
映射回设备像素坐标时 clamp 到 `[0, width-1] × [0, height-1]`。

### 3.3 键盘

两级策略：

1. **文本输入**（无修饰键、可打印字符）→ `INJECT_TEXT`，直接 UTF-8 下发，
   兼容中文、emoji 与输入法无关。
2. **功能键 / 组合键**（Ctrl+C、方向键、Home…）→ `INJECT_KEYCODE`
   （Android keycode + metastate）。

## 4. 错误处理与恢复

- 所有子进程失败均抛 `AdbError`，会话层转为状态机事件
  （`CONNECTING → CONNECTED → LOST → CLOSED`），UI 显示横幅并提供「重连」。
- 视频 socket 断开 = 设备端 server 退出：自动重连一次（指数退避），仍失败则上报。
- 设备旋转 / 分辨率变化：解码器输出尺寸变化时重建渲染目标。

## 5. 性能预算（目标值）

| 指标 | 目标 |
| --- | --- |
| 端到端延迟（USB，1080p） | < 70 ms |
| CPU 占用（1080p@60fps，硬解） | < 8% |
| 内存（单会话） | < 300 MB |
| 首帧时间 | < 1.5 s |

达成手段：控制消息合帧、帧队列丢旧、硬解优先、UI 渲染走 GPU（QOpenGLWidget / QRhi）。

## 6. 目录与模块约定

- `core/` 禁止 import Qt；`ui/` 禁止直接 import `scrcpy_*` 内部结构。
- 协议常量集中在各自模块顶部，注明对应的上游版本（升级 scrcpy 时同步校验）。
- 每个新通道（Android / iOS / 未来的虚拟设备）实现 `DeviceSession` 子类即可接入。
