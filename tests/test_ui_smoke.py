"""UI 冒烟测试（需要 PySide6，缺依赖时跳过；离屏渲染）。"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mobilecast.core.device import (  # noqa: E402
    KEYCODE_BACK,
    DeviceInfo,
    KeyEvent,
    Platform,
    TextEvent,
    TouchAction,
    TouchEvent,
    VideoFrame,
)
from mobilecast.ui.mirror_view import MirrorView  # noqa: E402
from mobilecast.ui.mirror_window import MirrorWindow  # noqa: E402


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
    assert window._wireless_btn.isEnabled()
    window.close()


class FakeSessionStub:
    """MirrorWindow 依赖的最小会话接口。"""

    def __init__(self) -> None:
        self.serial = "FAKE-SERIAL"
        self.info = DeviceInfo(serial="FAKE-SERIAL", name="Fake Device", platform=Platform.ANDROID)
        self.on_clipboard = None
        self.sent: list = []
        self.stopped = False

    def frames(self):
        return iter(())

    def send_input(self, event) -> None:
        self.sent.append(event)

    def send_text(self, event) -> None:
        self.sent.append(event)

    def send_clipboard(self, text: str, paste: bool = False) -> int:
        self.sent.append(("clipboard", text, paste))
        return 1

    def push_file(self, path):
        return "/sdcard/Download/x"

    def stop(self) -> None:
        self.stopped = True


def test_mirror_window_lifecycle_and_clipboard_bridge(app):
    session = FakeSessionStub()
    closed: list[str] = []
    window = MirrorWindow(session, on_close=closed.append)
    window.show()

    assert window._clipboard_sync.isChecked()
    assert window._clipboard_sync.text() == "剪贴板同步"

    # 设备 → PC：设备剪贴板回传应写入系统剪贴板
    window.device_clipboard.emit("from device")
    app.processEvents()
    assert window._clipboard.text() == "from device"

    # PC → device：本地剪贴板变化应同步到设备
    window._suppress_clipboard = False  # 兜底：确保不被回声抑制挡住
    window._clipboard.setText("to device")
    assert ("clipboard", "to device", False) in session.sent

    window.close()
    assert closed == ["FAKE-SERIAL"]  # 关闭窗口 → 通知管理器断开会话


def test_text_input_aggregates_and_pastes(app):
    """默认「剪贴板粘贴」模式：连续字符聚合后一次粘贴（支持中文/数字）。"""
    from mobilecast.core.device import TextEvent

    session = FakeSessionStub()
    window = MirrorWindow(session)
    window.show()

    window._send_input(TextEvent(text="你"))
    window._send_input(TextEvent(text="好"))
    window._send_input(TextEvent(text="123"))
    assert session.sent == []  # 聚合期间不发任何消息

    window._flush_text()
    assert ("clipboard", "你好123", True) in session.sent

    # 切到按键模拟模式：文本直接作为 TextEvent 下发
    window._paste_input_mode.setChecked(False)
    window._send_input(TextEvent(text="abc"))
    assert isinstance(session.sent[-1], TextEvent)
    assert session.sent[-1].text == "abc"
    window.close()


def test_text_flushed_before_key_events(app):
    """功能键之前必须先提交待粘贴文本，保证输入顺序正确。"""
    from mobilecast.core.device import KEYCODE_BACK, KeyAction, KeyEvent, TextEvent

    session = FakeSessionStub()
    window = MirrorWindow(session)
    window.show()

    window._send_input(TextEvent(text="hi"))
    window._send_input(KeyEvent(action=KeyAction.DOWN, keycode=KEYCODE_BACK))

    assert session.sent[0] == ("clipboard", "hi", True)  # 文本先到
    assert isinstance(session.sent[1], KeyEvent)  # 按键后到
    window.close()


def test_ime_commit_produces_text_event(app):
    """输入法：组合中不发文本，提交（commitString）才发。"""
    from PySide6.QtGui import QInputMethodEvent

    view = _make_view()
    events: list = []
    preedits: list = []
    view.text_event.connect(events.append)
    view.preedit_changed.connect(preedits.append)

    # 组合中（拼音 "nihao"）
    composing = QInputMethodEvent("nihao", [])
    QApplication.sendEvent(view, composing)
    assert preedits == ["nihao"]
    assert events == []  # 组合中不产生文本事件

    # 提交
    committed = QInputMethodEvent("", [])
    committed.setCommitString("你好")
    QApplication.sendEvent(view, committed)
    assert [e.text for e in events] == ["你好"]
    assert view._preedit == ""


def test_keys_ignored_during_ime_composition(app):
    """输入法组合期间的按键不应重复注入文本（防双输）。"""
    from PySide6.QtGui import QInputMethodEvent

    view = _make_view()
    events: list = []
    view.text_event.connect(events.append)

    composing = QInputMethodEvent("abc", [])
    QApplication.sendEvent(view, composing)

    QTest.keyClick(view, Qt.Key.Key_A)  # 组合中的按键应被忽略
    assert events == []
