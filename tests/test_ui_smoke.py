"""UI 冒烟测试（需要 PySide6，缺依赖时跳过；离屏渲染）。"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from mobilecast.core.device import (  # noqa: E402
    KEYCODE_BACK,
    KeyEvent,
    TextEvent,
    TouchAction,
    TouchEvent,
    VideoFrame,
)
from mobilecast.ui.mirror_view import MirrorView  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


def _make_view() -> MirrorView:
    view = MirrorView()
    view.resize(300, 600)
    view.show()
    # 64x128（1:2）在 300x600（1:2）中恰好铺满，便于断言坐标映射
    view.set_rgb_frame(VideoFrame(data=bytes(64 * 128 * 3), width=64, height=128))
    return view


def test_letterbox_coordinate_mapping(app):
    view = _make_view()
    assert view.widget_to_device(QPoint(0, 0)) == (0, 0)
    assert view.widget_to_device(QPoint(150, 300)) == (32, 64)
    assert view.widget_to_device(QPoint(299, 599)) == (63, 127)


def test_letterbox_ignores_black_borders(app):
    view = MirrorView()
    view.resize(400, 800)  # 1:1 视频 → 画面 400x400 居中，上下各 200px 黑边
    view.show()
    view.set_rgb_frame(VideoFrame(data=bytes(64 * 64 * 3), width=64, height=64))

    assert view.widget_to_device(QPoint(200, 100)) is None  # 上黑边
    assert view.widget_to_device(QPoint(200, 700)) is None  # 下黑边
    assert view.widget_to_device(QPoint(200, 400)) == (32, 32)  # 画面中心


def test_mouse_and_keyboard_emit_input_events(app):
    view = _make_view()
    events: list = []
    view.touch_event.connect(events.append)
    view.key_event.connect(events.append)
    view.text_event.connect(events.append)

    QTest.mousePress(
        view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(150, 300)
    )
    QTest.mouseRelease(
        view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(150, 300)
    )
    QTest.mouseClick(
        view, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(150, 300)
    )
    QTest.keyClick(view, Qt.Key.Key_A)

    touches = [e for e in events if isinstance(e, TouchEvent)]
    assert [t.action for t in touches] == [TouchAction.DOWN, TouchAction.UP]
    assert (touches[0].x, touches[0].y) == (32, 64)
    assert (touches[0].screen_width, touches[0].screen_height) == (64, 128)

    keys = [e for e in events if isinstance(e, KeyEvent)]
    assert [k.keycode for k in keys] == [KEYCODE_BACK, KEYCODE_BACK]  # 右键按下/抬起

    texts = [e for e in events if isinstance(e, TextEvent)]
    assert [t.text for t in texts] == ["a"]


def test_main_window_constructs_offscreen(app):
    from mobilecast.ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    assert window.windowTitle().startswith("MobileCast")
    assert window._connect_btn.isEnabled()
    window.close()
