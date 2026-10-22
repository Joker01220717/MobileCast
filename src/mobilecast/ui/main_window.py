"""主窗口：设备仪表盘（设备列表、无线连接、多窗口投屏管理）。

每台设备的投屏画面在独立的 :class:`MirrorWindow` 中，一设备一窗口。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.android.adb import Adb, AdbError
from ..core.android.scrcpy_server import ScrcpyError, ScrcpyOptions
from ..core.session_manager import SessionError, SessionManager
from .mirror_window import MirrorWindow
from .tasks import AsyncTask

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        adb: Adb | None = None,
        manager: SessionManager | None = None,
    ) -> None:
        super().__init__()
        self._adb = adb
        self._manager = manager or SessionManager(adb)
        self._windows: dict[str, MirrorWindow] = {}
        self._tasks: list[AsyncTask] = []

        self.setWindowTitle("MobileCast — 移动设备投屏与控制")
        self.resize(520, 560)

        self._device_list = QListWidget()
        self._device_list.itemDoubleClicked.connect(lambda _item: self._connect_current())

        hint = QLabel("双击设备或点「连接」开始投屏（一设备一窗口）\n"
                      "无线调试：USB 设备选中后点「启用无线调试」，之后可拔线使用")
        hint.setWordWrap(True)

        self._refresh_btn = QPushButton("刷新设备")
        self._connect_btn = QPushButton("连接")
        self._wireless_btn = QPushButton("启用无线调试")
        self._connect_ip_btn = QPushButton("连接网络设备…")
        self._close_all_btn = QPushButton("断开全部")
        self._refresh_btn.clicked.connect(self.refresh_devices)
        self._connect_btn.clicked.connect(self._connect_current)
        self._wireless_btn.clicked.connect(self._enable_wireless)
        self._connect_ip_btn.clicked.connect(self._connect_by_ip)
        self._close_all_btn.clicked.connect(self._close_all)

        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        for btn in (
            self._refresh_btn,
            self._connect_btn,
            self._wireless_btn,
            self._connect_ip_btn,
            self._close_all_btn,
        ):
            button_layout.addWidget(btn)
        button_layout.addStretch(1)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(buttons)
        layout.addWidget(self._device_list, stretch=1)
        layout.addWidget(hint)
        self.setCentralWidget(container)

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
            marker = " [投屏中]" if self._manager.is_open(device.serial) else ""
            item = QListWidgetItem(f"{device.model}  [{device.serial}]  ({device.state}){marker}")
            item.setData(Qt.ItemDataRole.UserRole, device.serial)
            self._device_list.addItem(item)
        self.statusBar().showMessage(f"发现 {len(devices)} 台设备")

    def _ensure_adb(self) -> Adb:
        if self._adb is None:
            self._adb = Adb()
        return self._adb

    def _selected_serial(self) -> str | None:
        item = self._device_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    # ---- 投屏会话 ------------------------------------------------
    def _connect_current(self) -> None:
        serial = self._selected_serial()
        if serial is None:
            self.statusBar().showMessage("请先选择一台设备")
            return
        self.connect_serial(serial)

    def connect_serial(self, serial: str, options: ScrcpyOptions | None = None) -> None:
        """连接指定设备并打开投屏窗口（供双击、按钮、--serial 与画质重连共用）。"""
        if self._manager.is_open(serial):
            self.statusBar().showMessage(f"设备 {serial} 已在投屏中")
            return

        try:
            adb = self._ensure_adb()
        except AdbError as exc:
            self.statusBar().showMessage(str(exc))
            return

        # 设备状态预检查：给出可操作的提示，而不是底层 adb 报错
        try:
            known = {d.serial: d for d in adb.devices()}
        except AdbError as exc:
            self.statusBar().showMessage(f"枚举设备失败: {exc}")
            return
        device = known.get(serial)
        if device is None:
            self.statusBar().showMessage(f"设备 {serial} 已不在列表中，请点「刷新设备」后重试")
            return
        if not device.ready:
            QMessageBox.warning(
                self,
                "设备不可用",
                f"设备 {serial} 当前状态：{device.state}\n\n"
                "请检查：\n"
                "· USB 线是否插好（无线设备请确认同一 Wi-Fi）\n"
                "· 手机屏幕是否已解锁\n"
                "· 「开发者选项 → USB 调试」是否开启\n\n"
                "处理后点「刷新设备」重试。",
            )
            return

        # 屏幕参数：决定默认画质（原始分辨率 + 峰值刷新率）并显示在状态栏
        display_info = None
        try:
            display_info = adb.device_display_info(serial)
        except AdbError as exc:
            log.warning("读取屏幕参数失败 %s: %s", serial, exc)
        if options is None:
            options = ScrcpyOptions.for_display(display_info)

        self.statusBar().showMessage(f"正在连接 {serial} …")
        try:
            session = self._manager.open(serial, options)
        except (SessionError, ScrcpyError, AdbError, OSError, RuntimeError) as exc:
            log.warning("连接 %s 失败: %s", serial, exc)
            if isinstance(exc, ScrcpyError):
                QMessageBox.warning(self, "无法连接", str(exc))
            self.statusBar().showMessage(f"连接失败: {exc}")
            return

        window = MirrorWindow(
            session,
            on_close=self._on_window_closed,
            on_reconnect=self._reconnect,
            display_info=display_info,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._windows[serial] = window
        window.show()
        self.refresh_devices()

    def _reconnect(self, serial: str, options: ScrcpyOptions) -> None:
        """按新画质重连（关闭旧窗口与会话后重新建立）。"""
        window = self._windows.get(serial)
        if window is not None:
            window.close()  # 触发 _on_window_closed → 会话关闭
        self.connect_serial(serial, options)

    def _on_window_closed(self, serial: str) -> None:
        self._windows.pop(serial, None)
        self._manager.close(serial)
        self.refresh_devices()

    def _close_all(self) -> None:
        for window in list(self._windows.values()):
            window.close()
        self._manager.close_all()
        self.refresh_devices()

    # ---- 无线连接 ------------------------------------------------
    def _enable_wireless(self) -> None:
        serial = self._selected_serial()
        if serial is None:
            self.statusBar().showMessage("请先选择一台 USB 连接的设备")
            return
        try:
            adb = self._ensure_adb()
        except AdbError as exc:
            self.statusBar().showMessage(str(exc))
            return

        def flow() -> str:
            adb.tcpip(serial, 5555)
            import time

            time.sleep(1.0)  # 等待设备端 adbd 重启
            ip = adb.device_ip(serial)
            if not ip:
                raise RuntimeError("无法读取设备无线 IP，请确认设备已连接 Wi-Fi")
            address = f"{ip}:5555"
            adb.connect(address)
            return address

        self.statusBar().showMessage("正在启用无线调试…")
        self._run_task(flow, self._on_wireless_ready)

    def _on_wireless_ready(self, address: str) -> None:
        self.statusBar().showMessage(f"无线调试已启用，可用 {address} 连接（现在可以拔掉 USB 线）")
        self.refresh_devices()

    def _connect_by_ip(self) -> None:
        text, ok = QInputDialog.getText(
            self, "连接网络设备", "设备地址（IP:端口）:", text="192.168.1.8:5555"
        )
        if not ok or not text.strip():
            return
        address = text.strip()
        try:
            adb = self._ensure_adb()
        except AdbError as exc:
            self.statusBar().showMessage(str(exc))
            return

        def flow() -> str:
            return adb.connect(address)

        self.statusBar().showMessage(f"正在连接 {address} …")
        self._run_task(flow, self._on_ip_connected)

    def _on_ip_connected(self, result: str) -> None:
        self.statusBar().showMessage(result)
        self.refresh_devices()

    # ---- 后台任务 ------------------------------------------------
    def _run_task(self, fn, on_done) -> None:
        task = AsyncTask(fn, self)
        task.done.connect(on_done)
        task.failed.connect(lambda msg: self.statusBar().showMessage(f"操作失败: {msg}"))
        self._tasks.append(task)
        task.finished.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)
        task.start()

    # ---- 生命周期 ------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        self._close_all()
        super().closeEvent(event)
