"""设备 → 客户端消息解析测试（golden bytes）。"""

from __future__ import annotations

import struct

import pytest

from mobilecast.core.android.device_msg import (
    AckClipboardMessage,
    ClipboardMessage,
    DeviceMsgError,
    DeviceMsgParser,
    UhidOutputMessage,
)


def test_clipboard_message():
    raw = bytes([0]) + struct.pack(">I", 6) + "中文".encode()
    assert DeviceMsgParser().feed(raw) == [ClipboardMessage(text="中文")]


def test_clipboard_message_partial_feed():
    raw = bytes([0]) + struct.pack(">I", 5) + b"hello"
    parser = DeviceMsgParser()
    assert parser.feed(raw[:3]) == []
    assert parser.feed(raw[3:7]) == []
    assert parser.feed(raw[7:]) == [ClipboardMessage(text="hello")]


def test_ack_clipboard_message():
    raw = bytes([1]) + struct.pack(">Q", 42)
    assert DeviceMsgParser().feed(raw) == [AckClipboardMessage(sequence=42)]


def test_uhid_output_message():
    raw = bytes([2]) + struct.pack(">HH", 7, 3) + b"\x01\x02\x03"
    assert DeviceMsgParser().feed(raw) == [UhidOutputMessage(uhid_id=7, data=b"\x01\x02\x03")]


def test_multiple_messages_in_one_chunk():
    raw = bytes([0]) + struct.pack(">I", 2) + b"hi" + bytes([1]) + struct.pack(">Q", 9)
    assert DeviceMsgParser().feed(raw) == [
        ClipboardMessage(text="hi"),
        AckClipboardMessage(sequence=9),
    ]


def test_unknown_type_raises():
    with pytest.raises(DeviceMsgError, match="无法识别"):
        DeviceMsgParser().feed(bytes([9]))
