"""AndroidSession 单元测试：socketpair + 假解码器，无需真机。"""

from __future__ import annotations

import socket

import pytest

from mobilecast.core.android.scrcpy_control import (
    encode_inject_keycode,
    encode_inject_scroll,
    encode_inject_text,
    encode_inject_touch,
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


class FakeDecoder:
    """把每个 NAL 变成一帧的假解码器，记录调用顺序。"""

    def __init__(self) -> None:
        self.nals: list[bytes] = []
        self.flushed = False

    def decode(self, data: bytes) -> list[VideoFrame]:
        self.nals.append(data)
        return [VideoFrame(data=b"\x00\x01", width=2, height=2)]

    def flush(self) -> list[VideoFrame]:
        self.flushed = True
        return []

    def close(self) -> None:
        pass


@pytest.fixture()
def env():
    video_dev, video_peer = socket.socketpair()
    ctrl_dev, ctrl_peer = socket.socketpair()
    decoder = FakeDecoder()
    session = AndroidSession(
        adb=None, serial="TEST-SERIAL", decoder=decoder, name="TestDevice"
    )
    yield session, video_dev, video_peer, ctrl_dev, ctrl_peer, decoder
    session.stop()
    for sock in (video_dev, video_peer, ctrl_dev, ctrl_peer):
        try:
            sock.close()
        except OSError:
            pass


def test_attach_reads_device_meta_and_decodes(env):
    session, video_dev, video_peer, _ctrl_dev, _ctrl_peer, decoder = env
    meta = b"Fake Device".ljust(META_LEN, b"\x00")
    nal1 = b"\x00\x00\x00\x01\x65\x88"
    nal2 = b"\x00\x00\x00\x01\x41\x9a"
    video_peer.sendall(meta + nal1 + nal2)

    name = session.attach_sockets(video_dev, _ctrl_dev)
    assert name == "Fake Device"
    assert session.info.name == "Fake Device"
    assert session.state is SessionState.CONNECTED

    frame = next(session.frames())
    assert (frame.width, frame.height) == (2, 2)
    assert nal1 in decoder.nals  # 第一个 NAL 在第二个 start code 到达时被切出


def test_input_events_are_encoded_to_control_socket(env):
    session, video_dev, video_peer, _ctrl_dev, ctrl_peer, _decoder = env
    video_peer.sendall(b"\x00" * META_LEN)
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
    video_peer.sendall(b"\x00" * META_LEN + b"\x00\x00\x00\x01\x65\x01")
    session.attach_sockets(video_dev, _ctrl_dev)
    video_peer.close()

    frames = list(session.frames())  # 流结束后迭代器必须能终止
    assert frames, "尾部 NAL 应在 flush 时解出"
    assert decoder.flushed is True
    assert session.state is SessionState.LOST


def test_send_before_connect_raises(env):
    session, *_rest = env
    with pytest.raises(RuntimeError, match="会话未连接"):
        session.send_text(TextEvent(text="x"))


def test_stop_closes_session(env):
    session, video_dev, video_peer, _ctrl_dev, _ctrl_peer, _decoder = env
    video_peer.sendall(b"\x00" * META_LEN)
    session.attach_sockets(video_dev, _ctrl_dev)

    session.stop()
    assert session.state is SessionState.CLOSED
    assert list(session.frames()) == []  # 停止后帧迭代器立即结束

    with pytest.raises(RuntimeError):
        session.send_key(KeyEvent(action=KeyAction.UP, keycode=KEYCODE_BACK))

    session.stop()  # 幂等
