"""单台设备的投屏窗口：渲染 + 键鼠 + 剪贴板同步 + 文件拖拽。

关闭窗口即断开会话（通过 on_close 回调交给会话管理器）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QMainWindow

from ..core.device import InputEvent, VideoFrame
from .frame_pump import FramePump
from .mirror_view import MirrorView
from .tasks import FilePushTask

log = logging.getLogger(__name__)


class MirrorWindow(QMainWindow):
    device_clipboard = Signal(str)  # 设备剪贴板 → UI 线程

    def __init__(self, session, on_close: Callable[[str], None] | None = None) -> None:
        super().__init__(None)
        self._session = session
        self._on_close = on_close
        self._file_tasks: list[FilePushTask] = []
        self._suppress_clipboard = False
        self._last_sent_clipboard = ""

        self.setWindowTitle(f"MobileCast — {session.info.name}")
        self.resize(420, 840)
        self.setAcceptDrops(True)

        self._mirror = MirrorView()
        self.setCentralWidget(self._mirror)

        # 工具栏
        self._clipboard_sync = QAction("剪贴板同步", self, checkable=True, checked=True)
        self._paste_action = QAction("粘贴到设备", self)
        self._fullscreen_action = QAction("全屏", self)
        self._disconnect_action = QAction("断开", self)
        toolbar = self.addToolBar("main")
        toolbar.setMovable(False)
        for action in (
            self._clipboard_sync,
            self._paste_action,
            self._fullscreen_action,
            self._disconnect_action,
        ):
            toolbar.addAction(action)
        self._paste_action.triggered.connect(self._paste_to_device)
        self._fullscreen_action.triggered.connect(self._toggle_fullscreen)
        self._disconnect_action.triggered.connect(self.close)

        # 键鼠输入 → 会话
        self._mirror.touch_event.connect(self._send_input)
        self._mirror.key_event.connect(self._send_input)
        self._mirror.text_event.connect(self._send_input)
        self._mirror.scroll_event.connect(self._send_input)

        # 剪贴板桥接（设备 → PC 走信号切回 UI 线程）
        self._clipboard = QApplication.clipboard()
        self._clipboard.dataChanged.connect(self._on_local_clipboard_changed)
        self.device_clipboard.connect(self._apply_device_clipboard)
        session.on_clipboard = self.device_clipboard.emit

        # 视频
        self._pump = FramePump(session, self)
        self._pump.frame_ready.connect(self._on_frame)
        self._pump.failed.connect(self._on_stream_error)
        self._pump.start()

        self.statusBar().showMessage(f"已连接 {session.info.name} · 拖入文件即可传输")

    # ---- 帧渲染 --------------------------------------------------
    def _on_frame(self, frame: VideoFrame) -> None:
        self._mirror.set_rgb_frame(frame)

    def _on_stream_error(self, message: str) -> None:
        self.statusBar().showMessage(f"视频流中断: {message}")
        self.close()

    # ---- 输入 ----------------------------------------------------
    def _send_input(self, event: InputEvent) -> None:
        try:
            self._session.send_input(event)
        except RuntimeError as exc:
            log.warning("输入注入失败: %s", exc)

    # ---- 剪贴板 --------------------------------------------------
    def _on_local_clipboard_changed(self) -> None:
        if self._suppress_clipboard or not self._clipboard_sync.isChecked():
            return
        mime = self._clipboard.mimeData()
        if mime is None or not mime.hasText():
            return
        text = self._clipboard.text()
        if not text or text == self._last_sent_clipboard:
            return
        self._last_sent_clipboard = text
        try:
            self._session.send_clipboard(text)
        except RuntimeError as exc:
            log.warning("剪贴板同步失败: %s", exc)

    def _apply_device_clipboard(self, text: str) -> None:
        if not self._clipboard_sync.isChecked():
            return
        self._last_sent_clipboard = text
        self._suppress_clipboard = True
        self._clipboard.setText(text)
        # 延后解除抑制，避免 dataChanged 回声
        QTimer.singleShot(0, lambda: setattr(self, "_suppress_clipboard", False))

    def _paste_to_device(self) -> None:
        try:
            self._session.send_clipboard(self._clipboard.text(), paste=True)
            self.statusBar().showMessage("已粘贴到设备")
        except RuntimeError as exc:
            self.statusBar().showMessage(f"粘贴失败: {exc}")

    # ---- 全屏 ----------------------------------------------------
    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # ---- 文件拖拽 ------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        for path in paths:
            task = FilePushTask(self._session, path, self)
            task.pushed.connect(self._on_file_pushed)
            task.failed.connect(self._on_file_failed)
            self._file_tasks.append(task)
            task.start()
        if paths:
            self.statusBar().showMessage(f"正在推送 {len(paths)} 个文件…")

    def _on_file_pushed(self, local: str, remote: str) -> None:
        self.statusBar().showMessage(f"已推送 {local} → {remote}")

    def _on_file_failed(self, local: str, message: str) -> None:
        self.statusBar().showMessage(f"推送失败 {local}: {message}")

    # ---- 关闭 ----------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._session.on_clipboard = None
        self._pump.requestInterruption()
        if self._on_close is not None:
            self._on_close(self._session.serial)
        self._pump.wait(3000)
        super().closeEvent(event)
