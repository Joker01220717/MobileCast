# 第三方组件与许可说明

MobileCast 本身以 MIT 许可发布，但它依赖 / 分发若干第三方组件。
当你**分发**本软件（提供安装包、打包 exe 等）时，需要遵守下列许可条款。

| 组件 | 用途 | 许可 | 备注 |
| --- | --- | --- | --- |
| [scrcpy](https://github.com/Genymobile/scrcpy) | Android 推流 + 输入注入 | 客户端 Apache-2.0；**`scrcpy-server`（server.jar）为 GPL-3.0** | 本项目自行实现客户端协议，仅复用 server 端 jar；分发时必须一并提供其源码获取方式并保留 GPL 声明 |
| [Android platform-tools（adb）](https://developer.android.com/tools/releases/platform-tools) | 设备通道 | Apache-2.0 | 可由用户自行安装，不随包分发 |
| [FFmpeg](https://ffmpeg.org/) / [PyAV](https://pyav.org/) | H.264/H.265/AV1 解码 | LGPL-2.1+ 或 GPL-2+（取决于构建配置） | 使用 LGPL 构建可避免 GPL 传染；注意动态链接与源码提供义务 |
| [PySide6](https://doc.qt.io/qtforpython/) | 桌面 UI（Qt for Python） | LGPL-3.0 / GPL-3.0 | 保持动态链接即可满足 LGPL |
| [WebDriverAgent](https://github.com/appium/WebDriverAgent) | iOS 输入注入 | BSD 风格（以仓库内 LICENSE 为准） | 需自行编译签名后安装到 iOS 设备 |
| [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) | iOS 设备发现 / 通信 | 以仓库内 LICENSE 为准 | 规划中的 iOS 通道 |

## 发布时的合规清单

1. **scrcpy-server（GPL-3.0）**：若把 `scrcpy-server-*.jar` 打进发行包，
   需在包内附 GPL-3.0 全文与 scrcpy 源码地址（
   `https://github.com/Genymobile/scrcpy`），并确保接收者能获得对应版本的 server 源码。
2. **FFmpeg**：若静态链接 GPL 构建的 FFmpeg，整个发行包需按 GPL 分发；
   建议使用 LGPL 构建并动态链接。
3. 在安装包的「关于 / 许可」页面展示本文件内容。
4. 各组件的商标、名称仅用于描述兼容性，不表示其作者为本项目背书。

> 本文件是开发期的工程记录，不构成法律意见；正式发布前请逐项复核。
