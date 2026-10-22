"""帧泵线程：从会话拉取解码帧并转发给 UI（跨线程走 Qt 信号队列）。"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal

from ..core.device import DeviceSession

log = logging.getLogger(__name__)


class FramePump(QThread):
    frame_ready = Signal(object)  # VideoFrame
    failed = Signal(str)

    def __init__(self, session: DeviceSession, parent=None) -> None:
        super().__init__(parent)
        self._session = session

    def run(self) -> None:
        try:
            for frame in self._session.frames():
                if self.isInterruptionRequested():
                    break
                self.frame_ready.emit(frame)
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜底
            log.exception("帧读取线程异常")
            self.failed.emit(str(exc))
