# iOS 投屏方案调研与路线

iPhone / iPad 没有 scrcpy 这样的官方或通用低延迟通道，
Windows 上的可用路径各有权衡。本文记录调研结论与选型。

## 1. 候选方案对比

| 方案 | 视频 | 输入 | 延迟 | 难度 | 约束 |
| --- | --- | --- | --- | --- | --- |
| **A. WebDriverAgent（WDA）** | 截图流（JPEG，约 5~10 fps） | ✅ tap / drag / keys | 高延迟 | 中 | 需用 Xcode 编译签名 WDA 并安装到设备 |
| **B. pymobiledevice3（DVT 服务）** | screenshot / 部分版本支持 H.264 录屏流 | 🔲 需配合 WDA | 中 | 中高 | 依赖 usbmuxd；iOS 17+ 走 CoreDevice/RemoteXPC，差异大 |
| **C. AirPlay 接收端**（如 airplay2-receiver） | ✅ H.264 镜像流 | 🔲 | 低 | 高 | 需实现 AirPlay 协议与 FairPlay 解密；协议随 iOS 版本演进 |
| **D. QuickTime 镜像（usbmuxd 复用）** | ✅ H.264 | 🔲 | 低 | 高 | 仅 macOS 原生支持，Windows 需逆向 mux 协议，风险大 |

## 2. 选型结论

**Phase 3（近期）：A + B 组合**

- 视频：pymobiledevice3 优先（DVT screenshot / 录屏流），
  退化路径为 WDA 的 `/screenshot` 轮询。
- 输入：WebDriverAgent 的 `tap` / `dragfromtoforduration` / `keys` / `button`。
- 通道：usbmuxd（`pymobiledevice3 usbmux list`）发现设备，
  `iproxy` 或 pymobiledevice3 自带的端口转发把 WDA 的 8100 端口映射到本机。

理由：全部开源、无需逆向私有协议，能最快跑通「能看 + 能点」的最小闭环。

**长期（Phase 3.5+）：C（AirPlay 接收端）**

真正的低延迟镜像只能走 AirPlay mirroring。可行性验证点：
设备与 PC 同网段、iOS 屏幕镜像发现（Bonjour/mDNS）、FairPlay 握手与解密。
建议做成独立的可选模块（`core/ios/airplay/`），失败时自动回退方案 A+B。

## 3. 已知约束

1. **签名**：WDA 必须用 Apple ID（7 天有效）或开发者证书（1 年）签名安装；
   安装后首次运行还需在「设置 → 通用 → 设备管理」中信任。
   MobileCast 不代签，只提供图文引导。
2. **iOS 版本碎片**：
   - iOS 13~16：`pymobiledevice3` 走 lockdown/DVT。
   - iOS 17+：启用 CoreDevice（RemoteXPC），需要安装 `MobileDevice` 新驱动栈，
     pymobiledevice3 已支持但接口差异大，适配层要单独抽象。
3. **权限**：WDA 的按键注入无法绕过系统键盘弹窗；剪贴板受应用沙盒限制。
4. **性能**：WDA 截图流延迟约 200~500 ms，仅作兜底；
   可接受的体验必须等 AirPlay 通道。

## 4. 里程碑

- [ ] M1：设备发现（usbmuxd）+ WDA 状态检测 + 截图流预览
- [ ] M2：WDA 触控 / 拖拽 / 文本注入，鼠标键盘映射复用 Android 的 `InputEvent`
- [ ] M3：pymobiledevice3 帧通道替换截图流，提升帧率
- [ ] M4：AirPlay 接收端 PoC（发现 + 握手 + 首帧）
- [ ] M5：AirPlay 通道产品化，自动降级策略

## 5. 参考

- WebDriverAgent：https://github.com/appium/WebDriverAgent
- pymobiledevice3：https://github.com/doronz88/pymobiledevice3
- airplay2-receiver：https://github.com/openairplay/airplay2-receiver
- libimobiledevice / usbmuxd：https://libimobiledevice.org/
