"""主窗口：设备列表 + 投屏画面。

当前完成 UI 骨架与设备枚举；会话接线（scrcpy 推流 + 输入注入）见 Phase 1 计划。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
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
from .mirror_view import MirrorView

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, adb: Adb | None = None) -> None:
        super().__init__()
        self._adb = adb
        self.setWindowTitle("MobileCast — 移动设备投屏与控制")
        self.resize(560, 780)

        self._device_list = QListWidget()
        self._device_list.setMinimumWidth(200)
        self._device_list.itemDoubleClicked.connect(self._connect_device)

        self._mirror = MirrorView()
        placeholder = QLabel("选择左侧设备并点击「连接」开始投屏\n（Phase 1 开发中）")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mirror_placeholder = placeholder

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._mirror, stretch=1)
        right_layout.addWidget(placeholder)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._device_list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        toolbar = QWidget()
        bar_layout = QHBoxLayout(toolbar)
        bar_layout.setContentsMargins(8, 8, 8, 0)
        self._refresh_btn = QPushButton("刷新设备")
        self._connect_btn = QPushButton("连接")
        self._disconnect_btn = QPushButton("断开")
        self._disconnect_btn.setEnabled(False)
        self._refresh_btn.clicked.connect(self.refresh_devices)
        self._connect_btn.clicked.connect(
            lambda: self._connect_device(self._device_list.currentItem())
        )
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

        self.statusBar().showMessage("就绪")
        self.refresh_devices()

    # ---- 设备 ----------------------------------------------------
    def refresh_devices(self) -> None:
        self._device_list.clear()
        try:
            adb = self._ensure_adb()
        except AdbError as exc:
            self.statusBar().showMessage(str(exc))
            return

        try:
            devices = adb.devices()
        except AdbError as exc:
            self.statusBar().showMessage(f"枚举设备失败: {exc}")
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

    # ---- 会话（Phase 1 接线） ------------------------------------
    def _connect_device(self, item: QListWidgetItem | None) -> None:
        if item is None:
            self.statusBar().showMessage("请先选择一台设备")
            return
        serial = item.data(Qt.ItemDataRole.UserRole)
        self.statusBar().showMessage(f"正在连接 {serial} …")
        # TODO(Phase 1): 创建 ScrcpyServer + AndroidSession，把 frames() 接到
        #  MirrorView.set_frame()，并把 MirrorView 的信号接到 session.send_input()。
        QMessageBox.information(
            self,
            "开发中",
            f"设备 {serial} 的投屏会话将在 Phase 1 接入。\n"
            "当前提交包含协议编解码、adb 封装与 UI 骨架。",
        )
        self.statusBar().showMessage("就绪")

    def _disconnect(self) -> None:
        # TODO(Phase 1): session.stop()
        self.statusBar().showMessage("已断开")
