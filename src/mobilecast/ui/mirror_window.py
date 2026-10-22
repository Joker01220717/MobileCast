"""单台设备的投屏窗口：渲染 + 键鼠 + 剪贴板同步 + 文件拖拽 + 画质设置。

**文本输入默认走「剪贴板 + 粘贴」**：scrcpy 的按键模拟（INJECT_TEXT）依赖
KeyCharacterMap，中文无法注入，且设备端输入法会把数字 / 符号当候选词吞掉
（真机实测）。因此默认把连续敲入的字符聚合后经 SET_CLIPBOARD(paste=True)
一次粘贴；玩需要真实按键的游戏时可切换到「按键模拟」模式。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QMainWindow,
)

from ..core.android.scrcpy_server import ScrcpyOptions, estimate_bit_rate
from ..core.device import InputEvent, TextEvent, VideoFrame
from .frame_pump import FramePump
from .mirror_view import MirrorView
from .tasks import FilePushTask

log = logging.getLogger(__name__)

TEXT_FLUSH_INTERVAL_MS = 150


class MirrorWindow(QMainWindow):
    device_clipboard = Signal(str)  # 设备剪贴板 → UI 线程

    def __init__(
        self,
        session,
        on_close: Callable[[str], None] | None = None,
        on_reconnect: Callable[[str, ScrcpyOptions], None] | None = None,
        display_info=None,
    ) -> None:
        super().__init__(None)
        self._session = session
        self._on_close = on_close
        self._on_reconnect = on_reconnect
        self._display_info = display_info
        self._file_tasks: list[FilePushTask] = []
        self._suppress_clipboard = False
        self._last_sent_clipboard = ""
        self._pending_text = ""

        self._text_timer = QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(TEXT_FLUSH_INTERVAL_MS)
        self._text_timer.timeout.connect(self._flush_text)

        self._frame_count = 0
        self._fps_t0 = time.monotonic()

        self.setWindowTitle(f"MobileCast — {session.info.name}")
        self.resize(420, 840)
        self.setAcceptDrops(True)

        self._mirror = MirrorView()
        self._mirror.setFocus()
        self.setCentralWidget(self._mirror)

        # 工具栏
        self._paste_input_mode = QAction("输入：剪贴板粘贴", self, checkable=True, checked=True)
        self._clipboard_sync = QAction("剪贴板同步", self, checkable=True, checked=True)
        self._paste_action = QAction("粘贴到设备", self)
        self._quality_action = QAction("画质…", self)
        self._fullscreen_action = QAction("全屏", self)
        self._disconnect_action = QAction("断开", self)
        toolbar = self.addToolBar("main")
        toolbar.setMovable(False)
        for action in (
            self._paste_input_mode,
            self._clipboard_sync,
            self._paste_action,
            self._quality_action,
            self._fullscreen_action,
            self._disconnect_action,
        ):
            toolbar.addAction(action)
        self._paste_input_mode.toggled.connect(self._on_input_mode_toggled)
        self._paste_action.triggered.connect(self._paste_to_device)
        self._quality_action.triggered.connect(self._choose_quality)
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
        self._pump.finished.connect(self._on_pump_finished)
        self._pump.start()

        self.statusBar().showMessage(f"已连接 {session.info.name} · 拖入文件即可传输")

    # ---- 帧渲染 --------------------------------------------------
    def _on_frame(self, frame: VideoFrame) -> None:
        self._mirror.set_rgb_frame(frame)
        self._frame_count += 1
        now = time.monotonic()
        elapsed = now - self._fps_t0
        if elapsed >= 1.0:
            fps = self._frame_count / elapsed
            self.statusBar().showMessage(
                f"设备 {self._native_label()} · 串流 {frame.width}x{frame.height} · {fps:.0f} fps"
            )
            self._fps_t0 = now
            self._frame_count = 0

    def _native_label(self) -> str:
        info = self._display_info
        if info is None or info.width <= 0:
            return "未知"
        rate = f"@{info.refresh_rate:g}Hz" if info.refresh_rate > 0 else ""
        return f"{info.width}x{info.height}{rate}"

    def _on_stream_error(self, message: str) -> None:
        self.statusBar().showMessage(f"视频流中断: {message}")
        self.close()

    def _on_pump_finished(self) -> None:
        from ..core.device import SessionState

        if self._session.state is SessionState.LOST:
            self.statusBar().showMessage("连接已断开（设备离线或会话被终止）· 点「断开」关闭窗口")

    # ---- 输入 ----------------------------------------------------
    def _send_input(self, event: InputEvent) -> None:
        if isinstance(event, TextEvent):
            self._handle_text(event)
            return
        # 先提交待粘贴的文本，再发功能键 / 点击，顺序不能颠倒
        self._flush_text()
        try:
            self._session.send_input(event)
        except Exception as exc:  # noqa: BLE001 - 断连后忽略输入并限频提示
            self._note_send_failure(str(exc))

    def _handle_text(self, event: TextEvent) -> None:
        if not self._paste_input_mode.isChecked():
            # 按键模拟模式（游戏场景）；中文会被输入法/字符映射限制
            try:
                self._session.send_text(event)
            except Exception as exc:  # noqa: BLE001
                self._note_send_failure(str(exc))
            return
        self._pending_text += event.text
        self._text_timer.start()

    def _flush_text(self) -> None:
        """把聚合的文本经剪贴板一次性粘贴到设备。"""
        self._text_timer.stop()
        if not self._pending_text:
            return
        text, self._pending_text = self._pending_text, ""
        try:
            self._session.send_clipboard(text, paste=True)
        except Exception as exc:  # noqa: BLE001
            self._note_send_failure(str(exc))

    def _note_send_failure(self, message: str) -> None:
        """连接断开后输入会持续失败：限频提示，避免刷屏。"""
        now = time.monotonic()
        if now - getattr(self, "_last_send_failure", 0.0) < 2.0:
            return
        self._last_send_failure = now
        log.warning("输入注入失败: %s", message)
        self.statusBar().showMessage("输入注入失败（连接可能已断开）")

    def _on_input_mode_toggled(self, checked: bool) -> None:
        self._flush_text()
        self._paste_input_mode.setText("输入：剪贴板粘贴" if checked else "输入：按键模拟")
        # 粘贴模式：启用 PC 输入法组合（支持中文上屏）；
        # 按键模式：禁用 IME，让原始按键直达手机（由手机端输入法组合）
        self._mirror.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, checked)
        self._mirror.clearFocus()
        self._mirror.setFocus()
        self.statusBar().showMessage(
            "文本经剪贴板粘贴（支持中文）"
            if checked
            else "文本经按键模拟（适合游戏，手机端输入法生效）"
        )

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
        self._flush_text()
        try:
            self._session.send_clipboard(self._clipboard.text(), paste=True)
            self.statusBar().showMessage("已粘贴到设备")
        except RuntimeError as exc:
            self.statusBar().showMessage(f"粘贴失败: {exc}")

    # ---- 画质 ----------------------------------------------------
    def _choose_quality(self) -> None:
        info = self._display_info
        max_dim = info.max_dimension if info and info.width > 0 else 0
        rate = info.refresh_rate if info else 0

        size_items = [f"原始（长边 {max_dim}）"] if max_dim else []
        size_items += ["1080p（长边 1920）", "720p（长边 1280）", "480p（长边 854）"]
        size_text, ok = QInputDialog.getItem(self, "画质", "分辨率:", size_items, 0, False)
        if not ok:
            return
        fps_items = ([f"设备最大（{rate:g} Hz）"] if rate > 0 else []) + [
            "60 fps",
            "30 fps",
            "不限",
        ]
        fps_text, ok = QInputDialog.getItem(self, "画质", "帧率:", fps_items, 0, False)
        if not ok:
            return

        chosen_max = max_dim
        if "1920" in size_text:
            chosen_max = 1920
        elif "1280" in size_text:
            chosen_max = 1280
        elif "854" in size_text:
            chosen_max = 854

        if "60" in fps_text:
            chosen_fps: float | None = 60.0
        elif "30" in fps_text:
            chosen_fps = 30.0
        elif "不限" in fps_text:
            chosen_fps = None
        else:
            chosen_fps = rate if rate > 0 else None

        options = self._build_options(chosen_max, chosen_fps)
        self.statusBar().showMessage(f"应用画质：{size_text} / {fps_text}，重新连接…")
        if self._on_reconnect is not None:
            self._on_reconnect(self._session.serial, options)

    def _build_options(self, max_dim: int, max_fps: float | None) -> ScrcpyOptions:
        info = self._display_info
        if info is not None and info.width > 0 and max_dim > 0:
            scale = max_dim / info.max_dimension
            width, height = int(info.width * scale), int(info.height * scale)
        else:
            width, height = (1920, 1080)
        fps = max_fps if max_fps else (info.refresh_rate if info and info.refresh_rate else 60.0)
        return ScrcpyOptions(
            max_size=max_dim,
            max_fps=max_fps,
            video_bit_rate=estimate_bit_rate(width, height, fps),
        )

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
        self._flush_text()
        self._session.on_clipboard = None
        self._pump.requestInterruption()
        if self._on_close is not None:
            self._on_close(self._session.serial)
        self._pump.wait(3000)
        super().closeEvent(event)
