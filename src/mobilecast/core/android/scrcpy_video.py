"""scrcpy 视频流解析（设备 → 客户端），对齐 **scrcpy v5.x**。

视频 socket 上的字节流布局（由 ``Streamer`` / ``DesktopConnection`` 写出）::

    [dummy byte 1B]                     send_dummy_byte=true 时（本项目关闭）
    [设备名 64B]                        send_device_meta=true 时
    [codec id u32]                      send_stream_meta=true 时（流元信息）
    重复:
        [帧头 12B: ptsAndFlags u64 + size u32]   send_frame_meta=true 时
        [包体 size 字节]

帧头 ptsAndFlags 的高位标志（大端序）::

    bit63 PACKET_FLAG_SESSION   置位时这 12B 其实是 session meta：
                                flags:u32 width:u32 height:u32
    bit62 PACKET_FLAG_CONFIG    包体是编解码配置（CSD，如 SPS/PPS）
    bit61 PACKET_FLAG_KEY_FRAME 关键帧

会话建立（tunnel_forward=true）：server 在唯一 socket 名 ``scrcpy_<scid>``
上按顺序 accept —— 第一条连接是视频，第二条是控制（audio=false 时）。

参考：scrcpy v5.0.1 ``server/.../device/DesktopConnection.java``、``Streamer.java``。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

DEVICE_NAME_FIELD_LENGTH = 64
FRAME_HEADER_SIZE = 12

PACKET_FLAG_SESSION = 1 << 63
PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61

# H.264 NAL unit type（裸流模式 / Annex-B 工具使用）
NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9

__all__ = [
    "DEVICE_NAME_FIELD_LENGTH",
    "FRAME_HEADER_SIZE",
    "PACKET_FLAG_SESSION",
    "PACKET_FLAG_CONFIG",
    "PACKET_FLAG_KEY_FRAME",
    "VideoStreamError",
    "SessionMeta",
    "VideoPacket",
    "read_device_meta",
    "read_stream_meta",
    "read_frame",
    "AnnexBSplitter",
    "nal_type",
]


class VideoStreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionMeta:
    """画面尺寸会话信息（旋转 / 分辨率变化时重新发送）。"""

    width: int
    height: int
    client_resize: bool = False


@dataclass(frozen=True)
class VideoPacket:
    """一包编码数据（通常含 1..n 个 NAL）。"""

    data: bytes
    pts: int = 0
    config: bool = False
    key_frame: bool = False


def _recv_exact(sock, size: int) -> bytes:
    """从 socket 精确读取 size 字节。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise VideoStreamError("视频流连接已断开")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_device_meta(sock) -> str:
    """读取 64 字节设备名。"""
    raw = _recv_exact(sock, DEVICE_NAME_FIELD_LENGTH)
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def read_stream_meta(sock) -> int:
    """读取流元信息头部：4 字节 codec id。"""
    (codec_id,) = struct.unpack(">I", _recv_exact(sock, 4))
    return codec_id


def read_frame(sock) -> SessionMeta | VideoPacket:
    """读取一帧：12 字节帧头 + 包体；帧头也可能是 session meta。"""
    header = _recv_exact(sock, FRAME_HEADER_SIZE)
    first = struct.unpack(">I", header[:4])[0]

    if first & (PACKET_FLAG_SESSION >> 32):
        flags, width, height = struct.unpack(">III", header)
        return SessionMeta(width=width, height=height, client_resize=bool(flags & 1))

    pts_and_flags, size = struct.unpack(">QI", header)
    data = _recv_exact(sock, size) if size else b""
    return VideoPacket(
        data=data,
        pts=pts_and_flags & ~(PACKET_FLAG_CONFIG | PACKET_FLAG_KEY_FRAME),
        config=bool(pts_and_flags & PACKET_FLAG_CONFIG),
        key_frame=bool(pts_and_flags & PACKET_FLAG_KEY_FRAME),
    )


def _find_start_code(buf: bytes, pos: int) -> tuple[int, int] | None:
    """从 pos 起查找下一个 start code，返回 (起始偏移, start code 长度)。"""
    idx = buf.find(b"\x00\x00\x01", pos)
    while idx != -1:
        if idx > 0 and buf[idx - 1] == 0:
            return idx - 1, 4
        return idx, 3
    return None


def nal_type(nal: bytes) -> int:
    """返回 H.264 NAL 类型（nal[0] & 0x1F，跳过 start code）。"""
    if nal.startswith(b"\x00\x00\x00\x01"):
        header = nal[4:]
    elif nal.startswith(b"\x00\x00\x01"):
        header = nal[3:]
    else:
        header = nal
    if not header:
        raise ValueError("空 NAL")
    return header[0] & 0x1F


class AnnexBSplitter:
    """把 Annex-B 字节流切成完整 NAL 单元（每个含 start code）。

    用于裸流模式（服务端 ``raw_stream=true``）或工具/测试场景；
    v5.x 默认成帧模式下请用 :func:`read_frame`。
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        nals: list[bytes] = []

        first = _find_start_code(self._buf, 0)
        if first is None:
            # 可能只缓冲到半个 start code，保留末尾 3 字节
            if len(self._buf) > 3:
                self._buf = self._buf[-3:]
            return nals
        first_pos, _first_len = first
        if first_pos > 0:
            self._buf = self._buf[first_pos:]  # 丢弃起始垃圾数据

        while True:
            cur = _find_start_code(self._buf, 0)
            assert cur is not None
            cur_pos, cur_len = cur
            nxt = _find_start_code(self._buf, cur_pos + cur_len)
            if nxt is None:
                break
            nals.append(self._buf[: nxt[0]])
            self._buf = self._buf[nxt[0] :]
        return nals

    def flush(self) -> bytes | None:
        """流结束时取出最后一个 NAL（若完整）。"""
        if not self._buf:
            return None
        nal, self._buf = self._buf, b""
        return nal
