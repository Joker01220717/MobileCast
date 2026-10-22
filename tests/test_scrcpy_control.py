"""scrcpy 控制协议编码的字节级测试（golden bytes）。

期望字节与 scrcpy 上游 app/src/control_msg.c 的 sc_control_msg_serialize() 对应。
升级 scrcpy 时如协议变更，本文件应同步更新并记录原因。
"""

from __future__ import annotations

import pytest

from mobilecast.core.android import scrcpy_control as sc


def test_inject_keycode_layout():
    msg = sc.encode_inject_keycode(sc.KeyAction.DOWN, keycode=4)
    assert len(msg) == 14
    assert msg.hex() == "00" "00" "00000004" "00000000" "00000000"

    msg_up = sc.encode_inject_keycode(sc.KeyAction.UP, keycode=26, repeat=1, metastate=0x1000)
    assert msg_up.hex() == "00" "01" "0000001a" "00000001" "00001000"


def test_inject_touch_layout():
    msg = sc.encode_inject_touch(
        action=sc.TouchAction.DOWN,
        pointer_id=0xFFFFFFFFFFFFFFFF,
        x=100,
        y=200,
        screen_width=1080,
        screen_height=2400,
        pressure=1.0,
        action_button=1,
        buttons=1,
    )
    assert len(msg) == 32
    assert msg.hex() == (
        "02"  # type
        "00"  # action DOWN
        "ffffffffffffffff"  # pointer id
        "00000064"  # x = 100
        "000000c8"  # y = 200
        "0438"  # width = 1080
        "0960"  # height = 2400
        "ffff"  # pressure 1.0 -> u16fp
        "00000001"  # action_button
        "00000001"  # buttons
    )


def test_touch_pressure_fixed_point():
    msg = sc.encode_inject_touch(
        action=sc.TouchAction.MOVE,
        pointer_id=0,
        x=0,
        y=0,
        screen_width=100,
        screen_height=100,
        pressure=0.5,
    )
    assert msg[22:24] == b"\x80\x00"  # 0.5 * 2^16 = 0x8000

    with pytest.raises(ValueError):
        sc.encode_inject_touch(
            action=sc.TouchAction.MOVE,
            pointer_id=0,
            x=0,
            y=0,
            screen_width=100,
            screen_height=100,
            pressure=1.5,
        )


def test_inject_scroll_layout_and_normalization():
    msg = sc.encode_inject_scroll(
        x=10,
        y=20,
        screen_width=1080,
        screen_height=1920,
        hscroll=8.0,
        vscroll=-16.0,
    )
    assert len(msg) == 21
    assert msg.hex() == (
        "03"  # type
        "0000000a"  # x
        "00000014"  # y
        "0438"  # width
        "0780"  # height
        "4000"  # 8/16 = 0.5 -> i16fp
        "8000"  # -1.0 -> i16fp
        "00000000"  # buttons
    )

    # 超出 [-16, 16] 的滚轮量会被 clamp
    clamped = sc.encode_inject_scroll(
        x=0, y=0, screen_width=1, screen_height=1, hscroll=100.0, vscroll=-100.0
    )
    assert clamped[13:15] == b"\x7f\xff"
    assert clamped[15:17] == b"\x80\x00"


def test_inject_text_utf8():
    msg = sc.encode_inject_text("中文")
    assert msg.hex() == "01" "00000006" "e4b8ade69687"

    # 超长文本按 UTF-8 字符边界截断，不产生半个字符
    long_msg = sc.encode_inject_text("好" * 200)
    text = long_msg[5:]
    assert len(text) <= sc.MAX_TEXT_LENGTH
    text.decode("utf-8")  # 不抛异常即为合法 UTF-8


def test_set_clipboard_layout():
    msg = sc.encode_set_clipboard(sequence=1, text="hi", paste=True)
    assert msg.hex() == "09" "0000000000000001" "01" "00000002" "6869"


def test_encode_scan_file_layout():
    path = "/sdcard/Download/a.txt"  # 22 字节
    msg = sc.encode_scan_file(path)
    assert msg.hex() == "16" "00000016" + path.encode().hex()


def test_no_payload_and_power():
    assert sc.encode_no_payload(sc.ControlMsgType.RESET_VIDEO) == b"\x11"
    assert sc.encode_no_payload(sc.ControlMsgType.ROTATE_DEVICE) == b"\x0b"
    assert sc.encode_set_display_power(False) == b"\x0a\x00"
    assert sc.encode_back_or_screen_on(sc.KeyAction.DOWN) == b"\x04\x00"


def test_control_writer_sends_raw_bytes():
    class FakeSocket:
        def __init__(self):
            self.sent = b""
            self.closed = False

        def sendall(self, data):
            self.sent += data

        def close(self):
            self.closed = True

    sock = FakeSocket()
    writer = sc.ScrcpyControlWriter(sock)
    writer.inject_keycode(sc.KeyAction.DOWN, sc.KEYCODES["HOME"])
    assert sock.sent == sc.encode_inject_keycode(sc.KeyAction.DOWN, 3)

    writer.close()
    with pytest.raises(RuntimeError):
        writer.inject_text("x")
