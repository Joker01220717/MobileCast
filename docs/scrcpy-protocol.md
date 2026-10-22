# scrcpy 协议笔记（客户端视角）

本文记录 MobileCast 需要用到的 scrcpy 推流与控制协议细节。
**对齐版本：scrcpy v5.0.1（2026-10 真机校对）**；升级 scrcpy 时请重新核对上游：

- `app/src/control_msg.c`、`app/src/device_msg.c`、`app/src/util/binary.h`
- `server/src/main/java/com/genymobile/scrcpy/Options.java`（启动参数）
- `server/src/main/java/com/genymobile/scrcpy/device/DesktopConnection.java`（连接与元信息）
- `server/src/main/java/com/genymobile/scrcpy/device/Streamer.java`（帧封装）

## 1. 会话建立

```
1. adb push scrcpy-server-v<X.Y.Z> /data/local/tmp/scrcpy-server.jar
2. 生成 scid（8 位十六进制、31 位非负），socket 名：scrcpy_<scid>
3. adb forward tcp:<port> localabstract:scrcpy_<scid>     ← 只需一条转发
4. 启动 server（adb shell）：
   CLASSPATH=/data/local/tmp/scrcpy-server.jar \
     app_process / com.genymobile.scrcpy.Server <X.Y.Z> scid=<scid> \
       tunnel_forward=true control=true audio=false cleanup=true \
       video_codec=h264 max_size=1280 video_bit_rate=8000000
5. 连接 127.0.0.1:<port>（第一条）→ 视频通道
   连接 127.0.0.1:<port>（第二条）→ 控制通道
```

要点（都是踩过的坑）：

- **第一个参数是版本号**，必须与 server 的 `BuildConfig.VERSION_NAME` 完全一致
  （如 v5.0.1 传 `5.0.1`），否则 server 启动即退出。
- **scid 必须 < 0x80000000**：server 用 `Integer.parseInt(value, 16)` 有符号解析，
  超过 31 位会抛 `NumberFormatException`。
- **v5.x 用同一个 socket 名按序 accept**：第一条连接是视频，第二条是控制
  （audio=false 时）。这是 2.x `scrcpy_<scid>_control` 独立 socket 名的老格式，已废弃。
  顺序颠倒会把控制连接当成视频连接。
- **dummy byte**：server accept 视频连接后先发 1 个字节，客户端读到它才算真正连上。
  `adb forward` 会先接受本地 TCP 连接、之后才去连设备端 socket，server 未就绪时
  会得到「假连接」（connect 成功但立即 EOF）——必须用这 1 字节校验并重试。
- `cleanup=true` 时 server 退出会删除 jar 文件；连续快速重启时旧会话的 CleanUp
  可能删掉刚推送的新 jar，需要清理残留进程 + 启动重试。
- server 由 `app_process` 以 shell 权限运行；连接断开后自行退出。

## 2. 视频流格式（v5.x 成帧）

视频通道字节流布局：

```
[dummy byte 1B]                  send_dummy_byte=true 时（仅首条连接）
[设备名 64B]                     send_device_meta=true 时（NUL 填充）
[codec id u32]                   send_stream_meta=true 时
重复：
    [帧头 12B: ptsAndFlags u64 + size u32]   send_frame_meta=true 时
    [包体 size 字节]（MediaCodec 输出，通常含 1..n 个 Annex-B NAL）
```

帧头 `ptsAndFlags` 的高位标志：

| 位 | 含义 |
| --- | --- |
| bit63 | `PACKET_FLAG_SESSION`：这 12B 其实是 session meta（`flags:u32 width:u32 height:u32`，bit0 = 客户端触发的 resize） |
| bit62 | `PACKET_FLAG_CONFIG`：包体是编解码配置（CSD，如 SPS/PPS），此时 pts 无效 |
| bit61 | `PACKET_FLAG_KEY_FRAME`：关键帧 |

`codec id` 是 FourCC：H.264 = `0x68323634`（"h264"）。真机实测值一致。

session meta 会随旋转 / 分辨率变化重新发送，可用来更新窗口尺寸。

> 历史格式备忘：scrcpy 2.x 是「64B 设备名 + 裸 Annex-B 流（无帧头）」。
> 若服务端传 `raw_stream=true` 可退回裸流（同时关闭全部元信息），
> 此时需自行按 start code 切 NAL（见 `scrcpy_video.AnnexBSplitter`）。

## 3. 控制消息（客户端 → 设备）

所有整数为**大端序**。每条消息以 1 字节 type 开头，
序列化实现对应上游 `sc_control_msg_serialize()`。

| type | 消息 | 布局（type 之后） | 总长 |
| --- | --- | --- | --- |
| 0 | INJECT_KEYCODE | `action:u8 keycode:u32 repeat:u32 metastate:u32` | 14 |
| 1 | INJECT_TEXT | `len:u32 + utf8` | 5+len |
| 2 | INJECT_TOUCH | `action:u8 pointer_id:u64 x:u32 y:u32 w:u16 h:u16 pressure:u16fp action_button:u32 buttons:u32` | 32 |
| 3 | INJECT_SCROLL | `x:u32 y:u32 w:u16 h:u16 hscroll:i16fp vscroll:i16fp buttons:u32` | 21 |
| 4 | BACK_OR_SCREEN_ON | `action:u8` | 2 |
| 8 | GET_CLIPBOARD | `copy_key:u8`（0=none,1=copy,2=cut） | 2 |
| 9 | SET_CLIPBOARD | `sequence:u64 paste:u8 len:u32 + utf8` | 10+len |
| 10 | SET_DISPLAY_POWER | `on:u8` | 2 |
| 12/13/14 | UHID_CREATE / UHID_INPUT / UHID_DESTROY | 见上游 | 变长 |
| 16 | START_APP | `len:u8 + name` | 2+len |
| 17 | RESET_VIDEO | — | 1 |
| 22 | SCAN_FILE | `len:u32 + path` | 5+len |

### 3.1 固定点数编码

- `pressure`：0.0~1.0 → `u16fp`，即 `round? floor(f * 2^16)`，上限 0xFFFF。
- `hscroll / vscroll`：先把滚轮量除以 16 并 clamp 到 [-1, 1]，
  再转 `i16fp` = `floor(f * 2^15)`，上限 0x7FFF。

### 3.2 触摸语义

- `action`：`0=ACTION_DOWN, 1=ACTION_UP, 2=ACTION_MOVE`（多指还有
  pointer-down/up，本项目暂不使用）。
- `pointer_id`：单指固定用 `0xFFFFFFFFFFFFFFFF`（mouse）或
  `0xFFFFFFFFFFFFFFFE`（generic finger）；多指时用递增 id。
- `x/y` 为设备像素坐标，`w/h` 为设备屏幕尺寸（用于服务端归一化）。
- `action_button` 表示触发本次事件的鼠标键，`buttons` 表示当前按住的键集
  （`AMOTION_EVENT_BUTTON_*`，主键 = `0x1`）。

## 4. 设备 → 客户端消息

控制通道是双向的。设备端会主动推送下列消息（大端序），
解析实现对应上游 ``device_msg.c`` 的 ``sc_device_msg_deserialize()``：

| type | 消息 | 布局 | 总长 | 用途 |
| --- | --- | --- | --- | --- |
| 0 | CLIPBOARD | `len:u32 + utf8 text` | 5+len | 设备剪贴板变化时推送 |
| 1 | ACK_CLIPBOARD | `sequence:u64` | 9 | 确认 SET_CLIPBOARD 已生效 |
| 2 | UHID_OUTPUT | `id:u16 size:u16 + data` | 5+size | 虚拟 HID 外设回传（如键盘 LED） |

未知类型视为协议错误（无法恢复），应重连会话。
本项目据此实现剪贴板双向同步：设备复制 → `CLIPBOARD` → 写入 Windows 剪贴板；
PC 复制 → `SET_CLIPBOARD`（见上文）→ 设备。

## 5. 常用 Android keycode

| 键 | keycode |
| --- | --- |
| HOME | 3 |
| BACK | 4 |
| POWER | 26 |
| ENTER | 66 |
| DEL（退格） | 67 |
| MENU | 82 |
| VOLUME_UP / DOWN | 24 / 25 |
| APP_SWITCH | 187 |

完整列表见 AOSP `KeyEvent.java`（`KEYCODE_*`）。

## 6. 参考

- scrcpy 源码：https://github.com/Genymobile/scrcpy
  - `app/src/control_msg.c` — 控制消息序列化（本文表格来源）
  - `app/src/server.h` — 设备名字段长度
  - `app/src/util/binary.h` — 大端写入与浮点定点转换
- scrcpy FAQ / 文档：https://github.com/Genymobile/scrcpy/blob/master/doc/
