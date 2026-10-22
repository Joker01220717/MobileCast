"""AndroidSession 单元测试：socketpair + 假解码器，无需真机。

按 scrcpy v5.x 的成帧格式构造输入：设备名 64B + codec id u32 + 帧（12B 头 + 包体）。
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

from mobilecast.core.android.scrcpy_control import (
    encode_inject_keycode,
    encode_inject_scroll,
    encode_inject_text,
    encode_inject_touch,
    encode_scan_file,
    encode_set_clipboard,
)
from mobilecast.core.android.session import AndroidSession
from mobilecast.core.device import (
    KEYCODE_BACK,
    POINTER_ID_MOUSE,
    KeyAction,
    KeyEvent,
    ScrollEvent,
    SessionState,
    TextEvent,
    TouchAction,
    TouchEvent,
    VideoFrame,
)

META_LEN = 64
PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61


def _handshake(name: str = "Fake Device", codec_id: int = 0x0100) -> bytes:
    """设备名 + 流元信息（连接建立后最先到达的字节）。"""
    return name.encode("utf-8").ljust(META_LEN, b"\x00") + struct.pack(">I", codec_id)


def _packet(data: bytes, pts: int = 0, config: bool = False, key: bool = False) -> bytes:
    flags = (PACKET_FLAG_CONFIG if config else 0) | (PACKET_FLAG_KEY_FRAME if key else 0)
    return struct.pack(">QI", pts | flags, len(data)) + data


def _session_meta(width: int, height: int) -> bytes:
    return struct.pack(">III", 0x80000000, width, height)


class FakeDecoder:
    """把每个包变成一帧的假解码器，记录喂入的包。"""

    def __init__(self) -> None:
        self.packets: list[bytes] = []
        self.flushed = False

    def decode(self, data: bytes) -> list[VideoFrame]:
        self.packets.append(data)
        return [VideoFrame(data=b"\x00\x01", width=2, height=2)]

    def flush(self) -> list[VideoFrame]:
        self.flushed = True
        return []

    def close(self) -> None:
        pass


class FakeAdb:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str, str]] = []

    def push(self, serial, local, remote) -> None:
        self.pushed.append((serial, str(local), remote))


@pytest.fixture()
def env():
    video_dev, video_peer = socket.socketpair()
    ctrl_dev, ctrl_peer = socket.socketpair()
    decoder = FakeDecoder()
    session = AndroidSession(adb=None, serial="TEST-SERIAL", decoder=decoder, name="TestDevice")
    yield session, video_dev, video_peer, ctrl_dev, ctrl_peer, decoder
    session.stop()
    for sock in (video_dev, video_peer, ctrl_dev, ctrl_peer):
        try:
            sock.close()
        except OSError:
            pass


def test_attach_reads_meta_and_decodes_packets(env):
    session, video_dev, video_peer, _ctrl_dev, _ctrl_peer, decoder = env
    video_peer.sendall(
        _handshake() + _packet(b"\x65\x88", pts=1000, key=True) + _packet(b"\x41\x9a")
    )

    name = session.attach_sockets(video_dev, _ctrl_dev)
    assert name == "Fake Device"
    assert session.info.name == "Fake Device"
    assert session.codec_id == 0x0100
    assert session.state is SessionState.CONNECTED

    frame = next(session.frames())
    assert (frame.width, frame.height) == (2, 2)
    assert b"\x65\x88" in decoder.packets


def test_session_meta_updates_video_size(env):
    session, video_dev, video_peer, _ctrl_dev, _ctrl_peer, _decoder = env
    sizes: list[tuple[int, int]] = []
    session.on_video_size = lambda w, h: sizes.append((w, h))

    video_peer.sendall(_handshake() + _session_meta(1080, 2400))
    session.attach_sockets(video_dev, _ctrl_dev)

    deadline = time.time() + 2
    while not sizes and time.time() < deadline:
        time.sleep(0.01)
    assert sizes == [(1080, 2400)]
    assert (session.info.width, session.info.height) == (1080, 2400)


def test_input_events_are_encoded_to_control_socket(env):
    session, video_dev, video_peer, _ctrl_dev, ctrl_peer, _decoder = env
    video_peer.sendall(_handshake())
    session.attach_sockets(video_dev, _ctrl_dev)
    ctrl_peer.settimeout(2)

    session.send_touch(
        TouchEvent(
            action=TouchAction.DOWN,
            x=10,
            y=20,
            screen_width=1080,
            screen_height=2400,
            pointer_id=POINTER_ID_MOUSE,
            action_button=1,
            buttons=1,
        )
    )
    assert ctrl_peer.recv(4096) == encode_inject_touch(
        action=0,
        pointer_id=POINTER_ID_MOUSE,
        x=10,
        y=20,
        screen_width=1080,
        screen_height=2400,
        pressure=1.0,
        action_button=1,
        buttons=1,
    )

    session.send_text(TextEvent(text="你好"))
    assert ctrl_peer.recv(4096) == encode_inject_text("你好")

    session.send_scroll(
        ScrollEvent(x=1, y=2, screen_width=1080, screen_height=2400, hscroll=2.0, vscroll=-2.0)
    )
    assert ctrl_peer.recv(4096) == encode_inject_scroll(
        x=1, y=2, screen_width=1080, screen_height=2400, hscroll=2.0, vscroll=-2.0
    )

    session.send_key(KeyEvent(action=KeyAction.DOWN, keycode=KEYCODE_BACK))
    assert ctrl_peer.recv(4096) == encode_inject_keycode(action=0, keycode=KEYCODE_BACK)


def test_stream_end_marks_lost_and_flushes_decoder(env):
    session, video_dev, video_peer, _ctrl_dev, _ctrl_peer, decoder = env
    video_peer.sendall(_handshake() + _packet(b"\x65\x01"))
    session.attach_sockets(video_dev, _ctrl_dev)
    video_peer.close()

    frames = list(session.frames())  # 流结束后迭代器必须能终止
    assert frames, "已到达的包应被解码"
    assert decoder.flushed is True
    assert session.state is SessionState.LOST


def test_send_before_connect_raises(env):
    session, *_rest = env
    with pytest.raises(RuntimeError, match="会话未连接"):
        session.send_text(TextEvent(text="x"))


def test_device_clipboard_dispatched_to_callback(env):
    session, video_dev, video_peer, _ctrl_dev, ctrl_peer, _decoder = env
    video_peer.sendall(_handshake())
    session.attach_sockets(video_dev, _ctrl_dev)

    received: list[str] = []
    session.on_clipboard = received.append

    ctrl_peer.sendall(bytes([0]) + struct.pack(">I", 2) + b"hi")  # CLIPBOARD 消息
    deadline = time.time() + 2
    while not received and time.time() < deadline:
        time.sleep(0.01)
    assert received == ["hi"]


def test_send_clipboard_uses_incrementing_sequence(env):
    session, video_dev, video_peer, _ctrl_dev, ctrl_peer, _decoder = env
    video_peer.sendall(_handshake())
    session.attach_sockets(video_dev, _ctrl_dev)
    ctrl_peer.settimeout(2)

    assert session.send_clipboard("hi") == 1
    assert ctrl_peer.recv(4096) == encode_set_clipboard(1, "hi", paste=False)

    assert session.send_clipboard("again", paste=True) == 2
    assert ctrl_peer.recv(4096) == encode_set_clipboard(2, "again", paste=True)


def test_push_file_uploads_and_triggers_media_scan():
    video_dev, video_peer = socket.socketpair()
    ctrl_dev, ctrl_peer = socket.socketpair()
    adb = FakeAdb()
    session = AndroidSession(adb=adb, serial="TEST-SERIAL", decoder=FakeDecoder(), name="T")
    video_peer.sendall(_handshake())
    session.attach_sockets(video_dev, ctrl_dev)
    ctrl_peer.settimeout(2)

    remote = session.push_file("report.pdf")
    assert remote == "/sdcard/Download/report.pdf"
    assert adb.pushed == [("TEST-SERIAL", "report.pdf", "/sdcard/Download/report.pdf")]
    assert ctrl_peer.recv(4096) == encode_scan_file("/sdcard/Download/report.pdf")

    session.stop()
    for sock in (video_dev, video_peer, ctrl_dev, ctrl_peer):
        sock.close()


def test_stop_closes_session(env):
    session, video_dev, video_peer, _ctrl_dev, _ctrl_peer, _decoder = env
    video_peer.sendall(_handshake())
    session.attach_sockets(video_dev, _ctrl_dev)

    session.stop()
    assert session.state is SessionState.CLOSED
    assert list(session.frames()) == []  # 停止后帧迭代器立即结束

    with pytest.raises(RuntimeError):
        session.send_key(KeyEvent(action=KeyAction.UP, keycode=KEYCODE_BACK))

    session.stop()  # 幂等
