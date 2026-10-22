"""投屏视图：视频渲染 + 鼠标键盘捕获。

- 渲染按 letterbox（等比缩放、居中、留黑边）显示；
- 鼠标坐标按同一映射换算回设备像素坐标后再注入；
- 键盘：可打印文本走 TextEvent，功能键走 KeyEvent（Android keycode）。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from ..core.device import (
    BUTTON_MIDDLE,
    BUTTON_PRIMARY,
    BUTTON_SECONDARY,
    POINTER_ID_MOUSE,
    KeyAction,
    KeyEvent,
    ScrollEvent,
    TextEvent,
    TouchAction,
    TouchEvent,
)

# Qt key → Android keycode（常用功能键）
QT_TO_ANDROID_KEYCODE: dict[int, int] = {
    Qt.Key.Key_Home: 3,
    Qt.Key.Key_Backspace: 67,
    Qt.Key.Key_Tab: 61,
    Qt.Key.Key_Return: 66,
    Qt.Key.Key_Enter: 66,
    Qt.Key.Key_Space: 62,
    Qt.Key.Key_Delete: 112,
    Qt.Key.Key_Escape: 4,
    Qt.Key.Key_Up: 19,
    Qt.Key.Key_Down: 20,
    Qt.Key.Key_Left: 21,
    Qt.Key.Key_Right: 22,
    Qt.Key.Key_PageUp: 92,
    Qt.Key.Key_PageDown: 93,
    Qt.Key.Key_VolumeUp: 24,
    Qt.Key.Key_VolumeDown: 25,
}

# Qt 鼠标键 → AMOTION_EVENT_BUTTON_*
QT_TO_ANDROID_BUTTON = {
    Qt.MouseButton.LeftButton: BUTTON_PRIMARY,
    Qt.MouseButton.RightButton: BUTTON_SECONDARY,
    Qt.MouseButton.MiddleButton: BUTTON_MIDDLE,
}


class MirrorView(QWidget):
    """单台设备的投屏画面。通过 Qt 信号把输入事件交给会话层。"""

    touch_event = Signal(object)  # TouchEvent
    key_event = Signal(object)  # KeyEvent
    text_event = Signal(object)  # TextEvent
    scroll_event = Signal(object)  # ScrollEvent

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._device_size = QSize(0, 0)
        self._buttons_down = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(240, 320)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    # ---- 画面 ----------------------------------------------------
    def set_frame(self, image: QImage, device_size: QSize | None = None) -> None:
        self._image = image
        if device_size is not None and device_size.isValid():
            self._device_size = device_size
        self.update()

    def _target_rect(self):
        """letterbox 后画面在控件中的位置与尺寸。"""
        if self._image is None or self._image.isNull():
            return None
        img_size = self._image.size()
        if not self._device_size.isValid() or self._device_size.isEmpty():
            self._device_size = img_size
        scaled = img_size.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        return x, y, scaled.width(), scaled.height()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        target = self._target_rect()
        if target is not None and self._image is not None:
            x, y, w, h = target
            painter.drawImage(x, y, self._image.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio))
        painter.end()

    # ---- 坐标映射 ------------------------------------------------
    def widget_to_device(self, pos: QPoint) -> tuple[int, int] | None:
        """窗口坐标 → 设备像素坐标；点在黑边区域时返回 None。"""
        target = self._target_rect()
        if target is None:
            return None
        x, y, w, h = target
        dx, dy = pos.x() - x, pos.y() - y
        if not (0 <= dx < w and 0 <= dy < h):
            return None
        dev_w = self._device_size.width()
        dev_h = self._device_size.height()
        return int(dx * dev_w / w), int(dy * dev_h / h)

    # ---- 鼠标 ----------------------------------------------------
    def _emit_touch(self, action: TouchAction, event: QMouseEvent) -> None:
        point = self.widget_to_device(event.position().toPoint())
        if point is None:
            return
        x, y = point
        buttons = 0
        for qt_button, android_button in QT_TO_ANDROID_BUTTON.items():
            if event.buttons() & qt_button:
                buttons |= android_button
        self.touch_event.emit(
            TouchEvent(
                action=action,
                x=x,
                y=y,
                screen_width=self._device_size.width(),
                screen_height=self._device_size.height(),
                pointer_id=POINTER_ID_MOUSE,
                action_button=QT_TO_ANDROID_BUTTON.get(event.button(), 0),
                buttons=buttons,
            )
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._emit_touch(TouchAction.DOWN, event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._emit_touch(TouchAction.MOVE, event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._emit_touch(TouchAction.UP, event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        point = self.widget_to_device(event.position().toPoint())
        if point is None:
            return
        delta = event.angleDelta()
        self.scroll_event.emit(
            ScrollEvent(
                x=point[0],
                y=point[1],
                screen_width=self._device_size.width(),
                screen_height=self._device_size.height(),
                hscroll=delta.x() / 120.0 * 2,
                vscroll=delta.y() / 120.0 * 2,
            )
        )

    # ---- 键盘 ----------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        self._handle_key(KeyAction.DOWN, event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        self._handle_key(KeyAction.UP, event)

    def _handle_key(self, action: KeyAction, event: QKeyEvent) -> None:
        if action is KeyAction.DOWN:
            modifiers = event.modifiers()
            shortcut_mods = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
            if not (modifiers & shortcut_mods):
                if event.text() and event.key() not in QT_TO_ANDROID_KEYCODE:
                    self.text_event.emit(TextEvent(text=event.text()))
                    return
        keycode = QT_TO_ANDROID_KEYCODE.get(event.key())
        if keycode is None:
            return
        self.key_event.emit(
            KeyEvent(action=action, keycode=keycode, repeat=event.count() - 1)
        )
