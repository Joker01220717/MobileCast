"""主窗口：设备列表 + 投屏画面 + 会话管理。

数据流：
    AndroidSession（视频线程解码）→ FramePump(QThread) → MirrorView 渲染
    MirrorView 键鼠信号 → MainWindow 转发 → session.send_input()
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.android.adb import Adb, AdbError
from ..core.android.scrcpy_server import ScrcpyError, ScrcpyOptions, find_server_jar
from ..core.android.session import AndroidSession
from ..core.device import InputEvent, SessionState, VideoFrame
from .mirror_view import MirrorView

log = logging.getLogger(__name__)


class FramePump(QThread):
    """在后台线程里拉取会话解码帧并转发给 UI（跨线程走 Qt 信号队列）。"""

    frame_ready = Signal(object)  # VideoFrame
    failed = Signal(str)

    def __init__(self, session: AndroidSession, parent=None) -> None:
        super().__init__(parent)
        self._session = session

    def run(self) -> None:
        try:
            for frame in self._session.frames():
                self.frame_ready.emit(frame)
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜底
            log.exception("帧读取线程异常")
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, adb: Adb | None = None) -> None:
        super().__init__()
        self._adb = adb
        self._session: AndroidSession | None = None
        self._pump: FramePump | None = None
        self._frame_count = 0
        self._fps_t0 = time.monotonic()

        self.setWindowTitle("MobileCast — 移动设备投屏与控制")
        self.resize(560, 780)

        self._device_list = QListWidget()
        self._device_list.setMinimumWidth(200)
        self._device_list.itemDoubleClicked.connect(self._connect_device)

        self._mirror = MirrorView()
        self._mirror_placeholder = QLabel("选择左侧设备并点击「连接」开始投屏")
        self._mirror_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._mirror, stretch=1)
        right_layout.addWidget(self._mirror_placeholder)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._device_list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        toolbar = QWidget()
        bar_layout = QHBoxLayout(toolbar)
        bar_layout.setContentsMargins(8, 8, 8, 0)
        self._refresh_btn = QPushButton("刷新设备")
        self._connect_btn = QPushButton("连接")
        self._disconnect_btn = QPushButton("断开")
        self._disconnect_btn.setEnabled(False)
        self._refresh_btn.clicked.connect(self.refresh_devices)
        self._connect_btn.clicked.connect(self._connect_current)
        self._disconnect_btn.clicked.connect(self._disconnect)
        bar_layout.addWidget(self._refresh_btn)
        bar_layout.addWidget(self._connect_btn)
        bar_layout.addWidget(self._disconnect_btn)
        bar_layout.addStretch(1)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(toolbar)
        layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(container)

        # 输入事件统一转发给会话
        self._mirror.touch_event.connect(self._send_input)
        self._mirror.key_event.connect(self._send_input)
        self._mirror.text_event.connect(self._send_input)
        self._mirror.scroll_event.connect(self._send_input)

        self.statusBar().showMessage("就绪")
        self.refresh_devices()

    # ---- 设备列表 ------------------------------------------------
    def refresh_devices(self) -> None:
        self._device_list.clear()
        try:
            adb = self._ensure_adb()
            devices = adb.devices()
        except AdbError as exc:
            self.statusBar().showMessage(str(exc))
            return

        for device in devices:
            item = QListWidgetItem(f"{device.model}  [{device.serial}]  ({device.state})")
            item.setData(Qt.ItemDataRole.UserRole, device.serial)
            self._device_list.addItem(item)
        self.statusBar().showMessage(f"发现 {len(devices)} 台设备")

    def _ensure_adb(self) -> Adb:
        if self._adb is None:
            self._adb = Adb()
        return self._adb

    # ---- 会话 ----------------------------------------------------
    def _connect_current(self) -> None:
        item = self._device_list.currentItem()
        if item is None:
            self.statusBar().showMessage("请先选择一台设备")
            return
        self._connect_device(item)

    def _connect_device(self, item: QListWidgetItem) -> None:
        self.connect_serial(item.data(Qt.ItemDataRole.UserRole))

    def connect_serial(self, serial: str) -> None:
        """连接指定设备（供双击、按钮与 --serial 启动参数共用）。"""
        if self._session is not None and self._session.state is SessionState.CONNECTED:
            self.statusBar().showMessage("已有会话连接中，请先断开")
            return

        try:
            adb = self._ensure_adb()
        except AdbError as exc:
            self.statusBar().showMessage(str(exc))
            return

        try:
            server_jar = find_server_jar()
        except ScrcpyError as exc:
            QMessageBox.warning(self, "缺少 scrcpy-server", str(exc))
            return

        session = AndroidSession(adb, serial, server_jar, options=ScrcpyOptions())
        self.statusBar().showMessage(f"正在连接 {serial} …")
        try:
            session.start()
        except (ScrcpyError, OSError, RuntimeError) as exc:
            session.stop()
            log.warning("连接 %s 失败: %s", serial, exc)
            QMessageBox.warning(self, "连接失败", str(exc))
            self.statusBar().showMessage("连接失败")
            return

        self._session = session
        self._mirror_placeholder.hide()
        self._fps_t0 = time.monotonic()
        self._frame_count = 0

        self._pump = FramePump(session, self)
        self._pump.frame_ready.connect(self._on_frame)
        self._pump.failed.connect(self._on_stream_error)
        self._pump.start()

        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self.statusBar().showMessage(f"已连接 {session.info.name}（{serial}）")

    def _disconnect(self) -> None:
        session, self._session = self._session, None
        pump, self._pump = self._pump, None
        if session is not None:
            session.stop()
        if pump is not None:
            pump.wait(3000)
        self._mirror_placeholder.show()
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self.statusBar().showMessage("已断开")

    # ---- 帧渲染 --------------------------------------------------
    def _on_frame(self, frame: VideoFrame) -> None:
        self._mirror.set_rgb_frame(frame)
        self._frame_count += 1
        now = time.monotonic()
        elapsed = now - self._fps_t0
        if elapsed >= 1.0 and self._session is not None:
            fps = self._frame_count / elapsed
            self.statusBar().showMessage(
                f"已连接 {self._session.info.name} · "
                f"{frame.width}x{frame.height} · {fps:.0f} fps · "
                f"右键=返回 中键=Home"
            )
            self._fps_t0 = now
            self._frame_count = 0

    def _on_stream_error(self, message: str) -> None:
        log.error("视频流异常: %s", message)
        self.statusBar().showMessage(f"视频流中断: {message}")
        self._disconnect()

    # ---- 输入转发 ------------------------------------------------
    def _send_input(self, event: InputEvent) -> None:
        if self._session is None:
            return
        try:
            self._session.send_input(event)
        except RuntimeError as exc:
            log.warning("输入注入失败: %s", exc)

    # ---- 生命周期 ------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._session is not None:
            self._disconnect()
        super().closeEvent(event)
