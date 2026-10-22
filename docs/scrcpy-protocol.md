# scrcpy 协议笔记（客户端视角）

本文记录 MobileCast 需要用到的 scrcpy 推流与控制协议细节。
**对齐版本：scrcpy 3.x（master，2026-10 校对）**；升级 scrcpy 时请重新核对上游
`app/src/control_msg.c`、`app/src/server.h`、`app/src/util/binary.h`。

## 1. 会话建立

```
1. adb push scrcpy-server-<ver>.jar /data/local/tmp/scrcpy-server.jar
2. 生成 scid（8 位十六进制），socket 名：
     video   : scrcpy_<scid>
     control : scrcpy_<scid>_control
3. adb forward tcp:<video_port>   localabstract:scrcpy_<scid>
   adb forward tcp:<control_port> localabstract:scrcpy_<scid>_control
4. 启动 server（adb shell）：
   CLASSPATH=/data/local/tmp/scrcpy-server.jar \
     app_process / com.genymobile.scrcpy.Server <version> \
       tunnel_forward=true control=true audio=false cleanup=true \
       video_codec=h264 max_size=1280 video_bit_rate=8000000
5. 连接 127.0.0.1:<video_port>   → 设备元信息 + 原始视频流
   连接 127.0.0.1:<control_port> → 控制通道
```

要点：

- `tunnel_forward=true` 表示走 `adb forward`（客户端连本地端口）；
  否则 server 会主动反向连接客户端。
- server 由 `app_process` 以 shell 权限运行；会话结束（socket 断开）后
  server 自行退出，`cleanup=true` 保证移除临时文件。

## 2. 视频流格式

连接建立后，server 首先发送**设备元信息**：

| 偏移 | 长度 | 内容 |
| --- | --- | --- |
| 0 | 64 | 设备名（UTF-8，NUL 填充，`SC_DEVICE_NAME_FIELD_LENGTH = 64`） |

随后是**原始 Annex-B H.264/H.265 字节流**（没有包头、没有长度前缀），
解码器直接喂入即可；NAL 边界由 start code（`00 00 00 01` / `00 00 01`）划分。
视频宽高由 SPS/PPS 决定，解码后从解码器读取。

> 注意：scrcpy 1.x 的「4B PTS + 4B 长度」分包格式已废弃，不要混用。

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

## 4. 常用 Android keycode

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

## 5. 参考

- scrcpy 源码：https://github.com/Genymobile/scrcpy
  - `app/src/control_msg.c` — 控制消息序列化（本文表格来源）
  - `app/src/server.h` — 设备名字段长度
  - `app/src/util/binary.h` — 大端写入与浮点定点转换
- scrcpy FAQ / 文档：https://github.com/Genymobile/scrcpy/blob/master/doc/
