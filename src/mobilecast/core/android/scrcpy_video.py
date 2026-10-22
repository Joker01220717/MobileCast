"""scrcpy 视频流解析（设备 → 客户端）。

连接建立后 server 首先发送设备元信息：64 字节设备名（UTF-8，NUL 填充），
随后是**原始 Annex-B H.264/H.265 字节流**（scrcpy 2.x 起无包头 / 无长度前缀）。
NAL 边界由 start code（00 00 00 01 或 00 00 01）划分。

参考：scrcpy ``app/src/server.h``（SC_DEVICE_NAME_FIELD_LENGTH = 64）。
"""

from __future__ import annotations

from collections.abc import Iterator

DEVICE_NAME_FIELD_LENGTH = 64

# H.264 NAL unit type
NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9


class VideoStreamError(RuntimeError):
    pass


def _recv_exact(sock, size: int) -> bytes:
    """从 socket 精确读取 size 字节。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise VideoStreamError("视频流在读取设备元信息时断开")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_device_meta(sock) -> str:
    """读取 64 字节设备名。调用后 sock 上即为原始视频流。"""
    raw = _recv_exact(sock, DEVICE_NAME_FIELD_LENGTH)
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


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
    """把字节流切成完整的 Annex-B NAL 单元（每个含 start code）。

    用法::

        splitter = AnnexBSplitter()
        for nal in splitter.feed(chunk):
            handle(nal)
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
        first_pos, first_len = first
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

    def iter_stream(self, chunks: Iterator[bytes]) -> Iterator[bytes]:
        """把字节块迭代器转换为 NAL 迭代器。"""
        for chunk in chunks:
            yield from self.feed(chunk)
        tail = self.flush()
        if tail is not None:
            yield tail
