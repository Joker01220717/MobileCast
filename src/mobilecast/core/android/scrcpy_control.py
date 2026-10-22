"""scrcpy 控制协议编码器（客户端 → 设备）。

与 scrcpy 3.x ``app/src/control_msg.c`` 的 ``sc_control_msg_serialize()`` 对齐，
所有整数为**大端序**。消息布局（type 之后）::

    INJECT_KEYCODE   action:u8 keycode:u32 repeat:u32 metastate:u32      (总 14 B)
    INJECT_TEXT      len:u32 + utf8                                      (总 5+len)
    INJECT_TOUCH     action:u8 pointer_id:u64 x:u32 y:u32 w:u16 h:u16
                     pressure:u16fp action_button:u32 buttons:u32        (总 32 B)
    INJECT_SCROLL    x:u32 y:u32 w:u16 h:u16 hscroll:i16fp vscroll:i16fp
                     buttons:u32                                         (总 21 B)
    BACK_OR_SCREEN_ON action:u8                                          (总 2 B)
    SET_CLIPBOARD    sequence:u64 paste:u8 len:u32 + utf8                (总 10+len)
    SET_DISPLAY_POWER on:u8                                              (总 2 B)

固定点编码：
    pressure(0..1) -> u16fp = floor(f * 2^16)，上限 0xFFFF
    scroll 量先除以 16 并 clamp 到 [-1, 1]，再转 i16fp = floor(f * 2^15)

⚠ 协议随 scrcpy 版本演进；升级 scrcpy-server 时请对照上游 control_msg.c
  重新校验本文件与 docs/scrcpy-protocol.md。
"""

from __future__ import annotations

import struct
import threading
from enum import IntEnum

__all__ = [
    "ControlMsgType",
    "KeyAction",
    "TouchAction",
    "CopyKey",
    "KEYCODES",
    "encode_inject_keycode",
    "encode_inject_text",
    "encode_inject_touch",
    "encode_inject_scroll",
    "encode_back_or_screen_on",
    "encode_set_display_power",
    "encode_set_clipboard",
    "encode_get_clipboard",
    "encode_scan_file",
    "encode_no_payload",
    "ScrcpyControlWriter",
]


class ControlMsgType(IntEnum):
    INJECT_KEYCODE = 0
    INJECT_TEXT = 1
    INJECT_TOUCH_EVENT = 2
    INJECT_SCROLL_EVENT = 3
    BACK_OR_SCREEN_ON = 4
    EXPAND_NOTIFICATION_PANEL = 5
    EXPAND_SETTINGS_PANEL = 6
    COLLAPSE_PANELS = 7
    GET_CLIPBOARD = 8
    SET_CLIPBOARD = 9
    SET_DISPLAY_POWER = 10
    ROTATE_DEVICE = 11
    UHID_CREATE = 12
    UHID_INPUT = 13
    UHID_DESTROY = 14
    OPEN_HARD_KEYBOARD_SETTINGS = 15
    START_APP = 16
    RESET_VIDEO = 17
    CAMERA_SET_TORCH = 18
    CAMERA_ZOOM_IN = 19
    CAMERA_ZOOM_OUT = 20
    RESIZE_DISPLAY = 21
    SCAN_FILE = 22


class TouchAction(IntEnum):
    DOWN = 0
    UP = 1
    MOVE = 2


class KeyAction(IntEnum):
    DOWN = 0
    UP = 1


class CopyKey(IntEnum):
    NONE = 0
    COPY = 1
    CUT = 2


# 常用 Android keycode（见 AOSP KeyEvent.java）
KEYCODES = {
    "HOME": 3,
    "BACK": 4,
    "CALL": 5,
    "ENDCALL": 6,
    "POWER": 26,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "ENTER": 66,
    "DEL": 67,  # 退格
    "FORWARD_DEL": 112,
    "TAB": 61,
    "SPACE": 62,
    "MENU": 82,
    "SEARCH": 84,
    "MEDIA_PLAY_PAUSE": 85,
    "MEDIA_NEXT": 87,
    "MEDIA_PREVIOUS": 88,
    "MOVE_HOME": 122,
    "MOVE_END": 123,
    "PAGE_UP": 92,
    "PAGE_DOWN": 93,
    "DPAD_UP": 19,
    "DPAD_DOWN": 20,
    "DPAD_LEFT": 21,
    "DPAD_RIGHT": 22,
    "DPAD_CENTER": 23,
    "APP_SWITCH": 187,
    "CAPS_LOCK": 115,
}

MAX_TEXT_LENGTH = 300  # SC_CONTROL_MSG_INJECT_TEXT_MAX_LENGTH


def _u16fp(value: float) -> int:
    """0.0~1.0 → 无符号 16 位定点（对应 sc_float_to_u16fp）。"""
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"pressure 必须在 [0, 1] 内: {value}")
    u = int(value * 0x10000)
    return min(u, 0xFFFF)


def _i16fp(value: float) -> int:
    """-1.0~1.0 → 有符号 16 位定点（对应 sc_float_to_i16fp）。"""
    if not -1.0 <= value <= 1.0:
        raise ValueError(f"滚动量必须在 [-1, 1] 内: {value}")
    i = int(value * 0x8000)
    return max(min(i, 0x7FFF), -0x8000)


def encode_inject_keycode(action: int, keycode: int, repeat: int = 0, metastate: int = 0) -> bytes:
    """注入按键（14 字节）。keycode 为 Android keycode。"""
    return struct.pack(">BBIII", ControlMsgType.INJECT_KEYCODE, action, keycode, repeat, metastate)


def encode_inject_text(text: str) -> bytes:
    """注入整段文本（5+len 字节）。超长部分按上游行为截断到 300 字节。"""
    raw = text.encode("utf-8")
    if len(raw) > MAX_TEXT_LENGTH:
        raw = raw[:MAX_TEXT_LENGTH]
        # 避免截断出半个 UTF-8 序列：逐步回退到可解码的边界
        while raw:
            try:
                raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
    return struct.pack(">BI", ControlMsgType.INJECT_TEXT, len(raw)) + raw


def encode_inject_touch(
    action: int,
    pointer_id: int,
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    pressure: float = 1.0,
    action_button: int = 0,
    buttons: int = 0,
) -> bytes:
    """注入触摸事件（32 字节）。x/y 为设备像素坐标。"""
    return struct.pack(
        ">BBQIIHHHII",
        ControlMsgType.INJECT_TOUCH_EVENT,
        action,
        pointer_id,
        x,
        y,
        screen_width,
        screen_height,
        _u16fp(pressure),
        action_button,
        buttons,
    )


def encode_inject_scroll(
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    hscroll: float,
    vscroll: float,
    buttons: int = 0,
) -> bytes:
    """注入滚轮事件（21 字节）。hscroll/vscroll 为原始滚轮量（典型 [-16, 16]）。"""
    h_norm = max(-1.0, min(1.0, hscroll / 16.0))
    v_norm = max(-1.0, min(1.0, vscroll / 16.0))
    return struct.pack(
        ">BIIHHhhI",
        ControlMsgType.INJECT_SCROLL_EVENT,
        x,
        y,
        screen_width,
        screen_height,
        _i16fp(h_norm),
        _i16fp(v_norm),
        buttons,
    )


def encode_back_or_screen_on(action: int) -> bytes:
    """返回键；若屏幕熄灭则先点亮（2 字节）。"""
    return struct.pack(">BB", ControlMsgType.BACK_OR_SCREEN_ON, action)


def encode_set_display_power(on: bool) -> bytes:
    """开关屏幕（2 字节）。"""
    return struct.pack(">BB", ControlMsgType.SET_DISPLAY_POWER, 1 if on else 0)


def encode_get_clipboard(copy_key: int = CopyKey.NONE) -> bytes:
    """请求读取设备剪贴板（2 字节）。"""
    return struct.pack(">BB", ControlMsgType.GET_CLIPBOARD, copy_key)


def encode_set_clipboard(sequence: int, text: str, paste: bool = False) -> bytes:
    """设置设备剪贴板（10+len 字节）。sequence 用于匹配设备端回执。"""
    raw = text.encode("utf-8")
    return (
        struct.pack(">BQBI", ControlMsgType.SET_CLIPBOARD, sequence, 1 if paste else 0, len(raw))
        + raw
    )


def encode_no_payload(msg_type: ControlMsgType) -> bytes:
    """无负载消息（1 字节），如 RESET_VIDEO / ROTATE_DEVICE / 面板开关等。"""
    return bytes((msg_type,))


def encode_scan_file(path: str) -> bytes:
    """触发设备端媒体库扫描该文件（5+len 字节），配合 adb push 使用。"""
    raw = path.encode("utf-8")
    return struct.pack(">BI", ControlMsgType.SCAN_FILE, len(raw)) + raw


class ScrcpyControlWriter:
    """线程安全的控制 socket 写入器。"""

    def __init__(self, sock) -> None:
        self._sock = sock
        self._lock = threading.Lock()
        self._closed = False

    def send(self, payload: bytes) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("控制通道已关闭")
            try:
                self._sock.sendall(payload)
            except OSError as exc:
                self._closed = True
                raise RuntimeError(f"控制通道写入失败: {exc}") from exc

    # ---- 便捷方法 -------------------------------------------------
    def inject_keycode(
        self, action: int, keycode: int, repeat: int = 0, metastate: int = 0
    ) -> None:
        self.send(encode_inject_keycode(action, keycode, repeat, metastate))

    def inject_text(self, text: str) -> None:
        self.send(encode_inject_text(text))

    def inject_touch(self, **kwargs) -> None:
        self.send(encode_inject_touch(**kwargs))

    def inject_scroll(self, **kwargs) -> None:
        self.send(encode_inject_scroll(**kwargs))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                self._sock.close()
            except OSError:
                pass
