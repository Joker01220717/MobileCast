"""设备 → 客户端消息解析（scrcpy 控制通道的反向消息）。

与上游 ``app/src/device_msg.c`` 的 ``sc_device_msg_deserialize()`` 对齐（大端序）：

============  ===============================================
CLIPBOARD     type:u8 | len:u32 | utf8 text        (5+len)
ACK_CLIPBOARD type:u8 | sequence:u64               (9)
UHID_OUTPUT   type:u8 | id:u16 | size:u16 | data   (5+size)
============  ===============================================
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

MAX_MESSAGE_SIZE = 1 << 18  # SC_DEVICE_MSG_MAX_SIZE

__all__ = [
    "DeviceMsgType",
    "ClipboardMessage",
    "AckClipboardMessage",
    "UhidOutputMessage",
    "DeviceMsg",
    "DeviceMsgError",
    "DeviceMsgParser",
]


class DeviceMsgType(IntEnum):
    CLIPBOARD = 0
    ACK_CLIPBOARD = 1
    UHID_OUTPUT = 2


@dataclass(frozen=True)
class ClipboardMessage:
    """设备剪贴板内容（用户在设备上复制时推送）。"""

    text: str


@dataclass(frozen=True)
class AckClipboardMessage:
    """设备端对 SET_CLIPBOARD 的确认，sequence 与发送时一致。"""

    sequence: int


@dataclass(frozen=True)
class UhidOutputMessage:
    """虚拟 HID 设备的输出（如键盘 LED 状态），暂不处理。"""

    uhid_id: int
    data: bytes


DeviceMsg = ClipboardMessage | AckClipboardMessage | UhidOutputMessage


class DeviceMsgError(RuntimeError):
    pass


class DeviceMsgParser:
    """增量解析器：喂字节流，吐完整消息（自动处理半包 / 粘包）。"""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[DeviceMsg]:
        self._buf += data
        if len(self._buf) > MAX_MESSAGE_SIZE:
            raise DeviceMsgError("设备消息缓冲溢出")

        messages: list[DeviceMsg] = []
        while True:
            message, consumed = _parse_one(self._buf)
            if consumed == 0:
                break
            if consumed < 0:
                raise DeviceMsgError(f"无法识别的设备消息类型: {self._buf[0]}")
            messages.append(message)
            self._buf = self._buf[consumed:]
        return messages


def _parse_one(buf: bytes) -> tuple[DeviceMsg | None, int]:
    """返回 (消息, 消费字节数)；0=数据不足，-1=无法解析。"""
    if not buf:
        return None, 0

    msg_type = buf[0]
    if msg_type == DeviceMsgType.CLIPBOARD:
        if len(buf) < 5:
            return None, 0
        (length,) = struct.unpack(">I", buf[1:5])
        if length > len(buf) - 5:
            return None, 0
        text = buf[5 : 5 + length].decode("utf-8", errors="replace")
        return ClipboardMessage(text=text), 5 + length

    if msg_type == DeviceMsgType.ACK_CLIPBOARD:
        if len(buf) < 9:
            return None, 0
        (sequence,) = struct.unpack(">Q", buf[1:9])
        return AckClipboardMessage(sequence=sequence), 9

    if msg_type == DeviceMsgType.UHID_OUTPUT:
        if len(buf) < 5:
            return None, 0
        uhid_id, size = struct.unpack(">HH", buf[1:5])
        if size > len(buf) - 5:
            return None, 0
        return UhidOutputMessage(uhid_id=uhid_id, data=bytes(buf[5 : 5 + size])), 5 + size

    return None, -1
