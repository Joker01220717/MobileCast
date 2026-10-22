# MobileCast

**在 Windows 上把 Android 手机 / iPhone / iPad 投屏到桌面窗口，并用鼠标和键盘直接操控它们。**

> 🚧 项目处于早期开发阶段（当前：Phase 1 — Android 基础链路）。

## 特性

- **多设备投屏**：每台设备一个独立窗口，可同时连接多台设备
- **键鼠操控**：鼠标 → 触摸 / 点击 / 滚轮，键盘 → 文本输入与系统快捷键
- **低延迟视频**：H.264 / H.265 / AV1 硬解推流（基于 scrcpy-server 协议）
- **剪贴板双向同步**：PC 复制 → 设备粘贴，设备复制 → PC 粘贴
- **文件拖拽传输**：把文件拖进投屏窗口即推送至设备并触发媒体库扫描
- **无线连接**：一键启用 adb tcpip 无线调试，拔掉 USB 线继续投屏
- **iOS 跟进**：iPhone / iPad 通过 usbmuxd + WebDriverAgent（规划中，见 [docs/ios-plan.md](docs/ios-plan.md)）
- **纯本地运行**：不经任何云端中转，设备数据不出本机

## 支持矩阵

| 平台 | 投屏 | 触控 | 键盘 | 剪贴板 | 状态 |
| --- | --- | --- | --- | --- | --- |
| Android 5.0+（API 21+） | ✅ | ✅ | ✅ | ✅ | 链路已真机验证 |
| iPhone / iPad（iOS 13+） | 🔲 | 🔲 | 🔲 | 🔲 | 规划中 |

## 快速开始

### 环境要求

- Windows 10 / 11 x64
- Python 3.11+
- Android SDK platform-tools（`adb`）已加入 `PATH`
- Android 设备已开启「开发者选项 → USB 调试」
- `scrcpy-server-*.jar`（GPL-3.0，不随仓库分发）：
  从 [scrcpy releases](https://github.com/Genymobile/scrcpy/releases) 下载后放入 `third_party/`
  目录，或用环境变量 `MOBILECAST_SCRCPY_SERVER` 指向它的路径

### 安装与运行

```powershell
git clone https://github.com/Joker01220717/MobileCast.git
cd MobileCast
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[ui,video]"
python -m mobilecast
```

连接设备后点击「连接」即可投屏；也可以直接指定设备：

```powershell
python -m mobilecast --serial <设备序列号>
```

### 使用说明

| 操作 | 效果 |
| --- | --- |
| 鼠标左键点击 / 拖动 | 触摸 / 滑动 |
| 鼠标滚轮 | 页面滚动 |
| 鼠标右键 | 返回键 |
| 鼠标中键 | Home 键 |
| 键盘打字（含中文输入法） | 文本输入 |
| 方向键 / Enter / Backspace 等 | 对应 Android 按键 |
| 拖文件进投屏窗口 | 推送到 `/sdcard/Download/` 并刷新媒体库 |

**无线连接**：USB 连接状态下选中设备 → 点「启用无线调试」→ 提示地址后即可拔线，
之后用「连接网络设备…」输入 `IP:5555` 连接；同一 Wi-Fi 下即可使用。

**剪贴板**：工具栏「剪贴板同步」默认开启，双向自动同步；
「粘贴到设备」按钮会把 PC 剪贴板内容粘贴到设备当前输入框。

### 运行测试

```powershell
pip install -e ".[dev]"
pytest
```

### 出问题先自检

```powershell
python -m mobilecast --doctor
```

检查 Python 依赖、adb、scrcpy-server 与设备连接状态；
手动验证清单见 [docs/validation.md](docs/validation.md)。

## 架构

```
┌─────────────────────────── UI 层（PySide6）───────────────────────────┐
│   MainWindow     DeviceList     MirrorView（渲染 + 键鼠捕获）           │
└────────────┬───────────────────────────────▲──────────────────────────┘
             │ InputEvent                    │ VideoFrame
┌────────────▼───────────────────────────────┴──────────────────────────┐
│                会话层 core/device.py（DeviceSession）                   │
├──────────────────────────┬────────────────────────────────────────────┤
│   Android 适配器          │   iOS 适配器                                │
│   scrcpy_server/…        │   ios/wda.py + usbmuxd                     │
├──────────────────────────┴────────────────────────────────────────────┤
│   传输层：adb（USB / TCP）· usbmuxd · 本地 socket 隧道                   │
├────────────────────────────────────────────────────────────────────────┤
│   解码层：PyAV / MediaFoundation → RGB 帧                               │
└────────────────────────────────────────────────────────────────────────┘
```

详细设计见 [docs/architecture.md](docs/architecture.md)。

## 仓库结构

```
MobileCast/
├── docs/                      设计文档
│   ├── architecture.md        分层架构与线程 / 数据流模型
│   ├── scrcpy-protocol.md     scrcpy 推流与控制协议笔记
│   └── ios-plan.md            iOS 投屏方案调研与路线
├── src/mobilecast/
│   ├── app.py                 程序入口
│   ├── core/                  与 UI 无关的核心逻辑
│   │   ├── device.py          设备 / 会话 / 输入事件抽象
│   │   ├── video/             解码层（PyAV，可替换硬解）
│   │   ├── android/           adb、scrcpy-server、控制协议与会话
│   │   └── ios/               WebDriverAgent 客户端
│   └── ui/                    PySide6 界面（渲染 + 键鼠捕获 + 会话接线）
├── tests/                     单元测试（协议编解码、会话、解码链路）
└── .github/workflows/ci.yml   CI（ruff + pytest）
```

## 开发路线

- [x] **Phase 0** — 仓库、工程骨架、协议编解码与单测
- [x] **Phase 1** — Android：USB 投屏窗口 + 鼠标触摸 + 键盘输入
- [x] **Phase 2** — Android：多设备多窗口、无线连接、剪贴板同步、文件拖拽
- [x] **真机验证（链路）** — Xiaomi Android 16 / Redmi Android 9 实测通过（[记录](docs/validation.md)）
- [ ] **真机验证（GUI）** — 手动清单验证中，见 [docs/validation.md](docs/validation.md)
- [ ] **Phase 3** — iOS：usbmuxd 发现设备 + WebDriverAgent 视频流与触控
- [ ] **Phase 4** — 打包发布（PyInstaller 单文件安装包）、快捷键自定义、录制

## 依赖与许可

本仓库代码采用 [MIT License](LICENSE)。

投屏能力依赖第三方组件（各自的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）：

- [scrcpy](https://github.com/Genymobile/scrcpy)（`scrcpy-server` 为 GPL-3.0）— Android 推流与注入
- [FFmpeg](https://ffmpeg.org/) / [PyAV](https://pyav.org/) — 视频解码
- [PySide6](https://doc.qt.io/qtforpython/) — 桌面 UI
- [WebDriverAgent](https://github.com/appium/WebDriverAgent) / [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) — iOS 通道

## 致谢

MobileCast 的 Android 链路站在 [scrcpy](https://github.com/Genymobile/scrcpy) 的肩膀上——
没有它出色的逆向与工程实现，就没有本项目。
