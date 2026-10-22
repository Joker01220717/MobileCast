"""SessionManager 单元测试（注入假会话工厂）。"""

from __future__ import annotations

import pytest

from mobilecast.core.device import DeviceInfo, Platform
from mobilecast.core.session_manager import SessionError, SessionManager


class FakeSession:
    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.info = DeviceInfo(serial=serial, name=serial, platform=Platform.ANDROID)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def manager():
    def factory(serial: str) -> FakeSession:
        return FakeSession(serial)

    return SessionManager(adb=None, session_factory=factory)


def test_open_starts_and_registers(manager):
    session = manager.open("A")
    assert session.started
    assert manager.is_open("A")
    assert manager.get("A") is session
    assert manager.serials() == ["A"]

    with pytest.raises(SessionError, match="已有会话"):
        manager.open("A")


def test_close_stops_session_and_is_idempotent(manager):
    session = manager.open("A")
    manager.close("A")
    assert session.stopped
    assert not manager.is_open("A")
    manager.close("A")  # 幂等


def test_close_all(manager):
    first = manager.open("A")
    second = manager.open("B")
    assert len(manager.sessions()) == 2

    manager.close_all()

    assert first.stopped and second.stopped
    assert manager.serials() == []
