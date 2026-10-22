"""scrcpy 视频流解析测试（v5.x 成帧格式 + Annex-B 工具）。"""

from __future__ import annotations

import struct

import pytest

from mobilecast.core.android.scrcpy_video import (
    DEVICE_NAME_FIELD_LENGTH,
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    AnnexBSplitter,
    SessionMeta,
    VideoPacket,
    VideoStreamError,
    nal_type,
    read_device_meta,
    read_frame,
    read_stream_meta,
)


class FakeSocket:
    """按固定块大小返回数据的假 socket。"""

    def __init__(self, data: bytes, chunk: int = 8) -> None:
        self._data = data
        self._chunk = chunk

    def recv(self, size: int) -> bytes:
        if not self._data:
            return b""
        take = min(size, self._chunk)
        chunk, self._data = self._data[:take], self._data[take:]
        return chunk


def test_read_device_meta():
    name = "Pixel 8 Pro"
    raw = name.encode("utf-8").ljust(DEVICE_NAME_FIELD_LENGTH, b"\x00")
    sock = FakeSocket(raw + b"\x00\x00\x00\x01\x67", chunk=5)
    assert read_device_meta(sock) == name


def test_annexb_splitter_basic():
    splitter = AnnexBSplitter()
    stream = (
        b"\x00\x00\x00\x01\x67\x42\x00\x1e"  # SPS
        b"\x00\x00\x00\x01\x68\xce\x06"  # PPS
        b"\x00\x00\x01\x65\x88\x80"  # IDR（3 字节 start code）
    )
    # 最后一个 NAL 要等下一个 start code 或 flush() 才会吐出
    nals = splitter.feed(stream)
    assert [nal_type(n) for n in nals] == [7, 8]
    assert nals[0] == b"\x00\x00\x00\x01\x67\x42\x00\x1e"

    tail = splitter.flush()
    assert tail is not None
    assert nal_type(tail) == 5
    assert splitter.flush() is None


def test_annexb_splitter_incremental_feed():
    splitter = AnnexBSplitter()
    stream = b"\x00\x00\x00\x01\x67\x42" + b"\x00\x00\x00\x01\x68\xce" + b"\x00\x00\x00\x01\x65\x01"

    collected: list[bytes] = []
    for i in range(len(stream)):
        collected.extend(splitter.feed(stream[i : i + 1]))

    assert [nal_type(n) for n in collected] == [7, 8]
    tail = splitter.flush()
    assert tail is not None
    assert nal_type(tail) == 5


def test_annexb_splitter_skips_leading_garbage():
    splitter = AnnexBSplitter()
    nals = splitter.feed(b"\xff\xfe\x00\x00\x00\x01\x67\x42\x00\x00\x00\x01\x68\xce")
    assert [nal_type(n) for n in nals] == [7]
    tail = splitter.flush()
    assert tail is not None
    assert nal_type(tail) == 8


# ---- v5.x 成帧格式 ------------------------------------------------


def test_read_stream_meta():
    sock = FakeSocket(struct.pack(">I", 0x01020304), chunk=2)
    assert read_stream_meta(sock) == 0x01020304


def test_read_frame_video_packet():
    payload = b"\x00\x00\x00\x01\x65\x88"
    header = struct.pack(">QI", 90000 | PACKET_FLAG_KEY_FRAME, len(payload))
    packet = read_frame(FakeSocket(header + payload, chunk=5))
    assert packet == VideoPacket(data=payload, pts=90000, config=False, key_frame=True)


def test_read_frame_config_packet():
    payload = b"\x00\x00\x00\x01\x67\x42"
    header = struct.pack(">QI", PACKET_FLAG_CONFIG, len(payload))
    packet = read_frame(FakeSocket(header + payload))
    assert packet.config is True
    assert packet.key_frame is False
    assert packet.pts == 0
    assert packet.data == payload


def test_read_frame_session_meta():
    meta = read_frame(FakeSocket(struct.pack(">III", 0x80000000, 1080, 2400)))
    assert meta == SessionMeta(width=1080, height=2400, client_resize=False)

    resized = read_frame(FakeSocket(struct.pack(">III", 0x80000001, 720, 1280)))
    assert resized.client_resize is True


def test_read_frame_truncated_stream_raises():
    with pytest.raises(VideoStreamError, match="断开"):
        read_frame(FakeSocket(b"\x00\x00"))
