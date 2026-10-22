"""scrcpy 视频流解析测试。"""

from __future__ import annotations

from mobilecast.core.android.scrcpy_video import (
    DEVICE_NAME_FIELD_LENGTH,
    AnnexBSplitter,
    nal_type,
    read_device_meta,
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
