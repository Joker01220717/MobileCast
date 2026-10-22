"""真实 H.264 解码链路测试（需要 PyAV + numpy，缺依赖时跳过）。"""

from __future__ import annotations

from fractions import Fraction

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

from mobilecast.core.android.scrcpy_video import AnnexBSplitter  # noqa: E402
from mobilecast.core.video.decoder import PyAvDecoder  # noqa: E402


def _make_low_latency_h264(frame_count: int = 4) -> bytes:
    """生成 scrcpy 风格的低延迟 Annex-B 流（bf=0、zerolatency）。"""
    encoder = av.CodecContext.create("h264", "w")
    encoder.width, encoder.height = 64, 64
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 25)
    encoder.max_b_frames = 0
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}

    stream = bytearray()

    def emit(packet) -> None:
        raw = bytes(packet)
        stream.extend(raw if raw.startswith(b"\x00\x00") else b"\x00\x00\x00\x01" + raw)

    for i in range(frame_count):
        arr = np.full((64, 64, 3), i * 40, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        for packet in encoder.encode(frame):
            emit(packet)
    for packet in encoder.encode(None):
        emit(packet)
    return bytes(stream)


def test_decode_stream_through_annexb_splitter():
    stream = _make_low_latency_h264()
    splitter = AnnexBSplitter()
    decoder = PyAvDecoder("h264")

    frames = []
    for nal in splitter.feed(stream):
        frames.extend(decoder.decode(nal))
    tail = splitter.flush()
    if tail is not None:
        frames.extend(decoder.decode(tail))
    frames.extend(decoder.flush())

    assert frames, "低延迟流应即时解出帧"
    for frame in frames:
        assert frame.width == 64 and frame.height == 64
        assert len(frame.data) == 64 * 64 * 3  # RGB24
        assert frame.format == "rgb24"


def test_decoder_flush_drains_buffered_frames():
    """含 B 帧的流需要 flush 才能取回缓冲帧。"""
    encoder = av.CodecContext.create("h264", "w")
    encoder.width, encoder.height = 64, 64
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 25)

    stream = bytearray()

    def emit(packet) -> None:
        raw = bytes(packet)
        stream.extend(raw if raw.startswith(b"\x00\x00") else b"\x00\x00\x00\x01" + raw)

    for i in range(3):
        arr = np.full((64, 64, 3), i * 40, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        for packet in encoder.encode(frame):
            emit(packet)
    for packet in encoder.encode(None):
        emit(packet)

    decoder = PyAvDecoder("h264")
    for nal in AnnexBSplitter().feed(bytes(stream)):
        decoder.decode(nal)
    assert decoder.flush(), "flush 应排空解码器缓冲"


def test_session_pipeline_with_real_h264_stream():
    """端到端：socketpair 注入真实 H.264 流（v5 成帧）→ AndroidSession → 解码帧。"""
    import socket
    import struct
    import threading

    from mobilecast.core.android.session import AndroidSession

    def packet(data: bytes, config: bool = False) -> bytes:
        flags = 1 << 62 if config else 0
        return struct.pack(">QI", flags, len(data)) + data

    stream = _make_low_latency_h264(4)
    splitter = AnnexBSplitter()
    nals = splitter.feed(stream)
    tail = splitter.flush()
    if tail is not None:
        nals.append(tail)

    video_dev, video_peer = socket.socketpair()
    ctrl_dev, ctrl_peer = socket.socketpair()
    session = AndroidSession(
        adb=None, serial="TEST-SERIAL", decoder=PyAvDecoder("h264"), name="Pipeline"
    )
    # 设备名 64B + codec id 4B + 每个 NAL 一包
    video_peer.sendall(b"Pipeline".ljust(64, b"\x00") + struct.pack(">I", 0))
    for index, nal in enumerate(nals):
        video_peer.sendall(packet(nal, config=index < 2))
    session.attach_sockets(video_dev, ctrl_dev)

    result: list = []

    def collect() -> None:
        for index, frame in enumerate(session.frames()):
            result.append(frame)
            if index >= 1:
                return

    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    worker.join(10)
    session.stop()
    for sock in (video_dev, video_peer, ctrl_dev, ctrl_peer):
        sock.close()

    assert not worker.is_alive(), "帧拉取超时，视频链路疑似中断"
    assert len(result) == 2
    assert all(f.width == 64 and f.height == 64 for f in result)
    assert all(len(f.data) == 64 * 64 * 3 for f in result)
