"""视频解码层：H.264/H.265/AV1 NAL → RGB 帧。

core 层只依赖 `VideoDecoder` 协议，默认实现 `PyAvDecoder`（FFmpeg 绑定），
后续可替换为 MediaFoundation 硬解而不影响上层。
"""

from __future__ import annotations

from typing import Protocol

from ..device import VideoFrame

__all__ = ["VideoDecoder", "PyAvDecoder", "create_decoder"]


class VideoDecoder(Protocol):
    """解码器协议：喂 NAL，出帧。"""

    def decode(self, data: bytes) -> list[VideoFrame]:
        """喂入一个 NAL 单元（含 start code），返回解出的 0..n 帧。"""
        ...

    def flush(self) -> list[VideoFrame]:
        """流结束时排空解码器内部缓冲的帧。"""
        ...

    def close(self) -> None: ...


class PyAvDecoder:
    """基于 PyAV 的软解实现。"""

    def __init__(self, codec: str = "h264") -> None:
        try:
            import av
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示路径
            raise RuntimeError('缺少视频解码依赖，请执行: pip install -e ".[video]"') from exc
        self._av = av
        self._ctx = av.CodecContext.create(codec, "r")

    def decode(self, data: bytes) -> list[VideoFrame]:
        if self._ctx is None:
            return []
        frames: list[VideoFrame] = []
        for packet in self._ctx.parse(data):
            for frame in self._ctx.decode(packet):
                frames.append(self._to_video_frame(frame))
        return frames

    def flush(self) -> list[VideoFrame]:
        if self._ctx is None:
            return []
        return [self._to_video_frame(frame) for frame in self._ctx.decode(None)]

    def close(self) -> None:
        self._ctx = None

    @staticmethod
    def _to_video_frame(frame) -> VideoFrame:
        rgb = frame.to_ndarray(format="rgb24")
        return VideoFrame(
            data=rgb.tobytes(),
            width=frame.width,
            height=frame.height,
            pts=frame.pts if frame.pts is not None else 0,
            format="rgb24",
        )


def create_decoder(codec: str = "h264") -> VideoDecoder:
    """按编解码类型创建默认解码器。"""
    return PyAvDecoder(codec)
