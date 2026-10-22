"""UI 层的后台任务（QThread 封装）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class AsyncTask(QThread):
    """在后台线程执行任意可调用对象，完成后发信号。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], object], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜底
            log.exception("后台任务失败")
            self.failed.emit(str(exc))


class FilePushTask(QThread):
    """后台推送文件到设备。"""

    pushed = Signal(str, str)  # (本地路径, 设备路径)
    failed = Signal(str, str)  # (本地路径, 错误信息)

    def __init__(self, session, local_path: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._session = session
        self._local_path = Path(local_path)

    def run(self) -> None:
        try:
            remote = self._session.push_file(self._local_path)
            self.pushed.emit(str(self._local_path), remote)
        except Exception as exc:  # noqa: BLE001
            log.warning("文件推送失败 %s: %s", self._local_path, exc)
            self.failed.emit(str(self._local_path), str(exc))
