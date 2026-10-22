"""多设备会话管理：一台设备一个会话，多会话并发。

core 层保持 Qt 无关；窗口的创建 / 销毁由 UI 层负责，本类只管会话生命周期。
"""

from __future__ import annotations

from collections.abc import Callable

from .android.adb import Adb
from .android.session import AndroidSession

__all__ = ["SessionError", "SessionManager"]

SessionFactory = Callable[[str], AndroidSession]


class SessionError(RuntimeError):
    pass


class SessionManager:
    def __init__(
        self,
        adb: Adb | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._adb = adb
        self._factory = session_factory or self._default_factory
        self._sessions: dict[str, AndroidSession] = {}

    def _default_factory(self, serial: str) -> AndroidSession:
        from .android.scrcpy_server import find_server_jar

        if self._adb is None:
            self._adb = Adb()
        return AndroidSession(self._adb, serial, find_server_jar())

    # ---- 会话生命周期 ---------------------------------------------
    def open(self, serial: str) -> AndroidSession:
        """为指定设备建立会话；同一设备不允许重复开启。"""
        if serial in self._sessions:
            raise SessionError(f"设备 {serial} 已有会话在运行")
        session = self._factory(serial)
        session.start()
        self._sessions[serial] = session
        return session

    def close(self, serial: str) -> None:
        session = self._sessions.pop(serial, None)
        if session is not None:
            session.stop()

    def close_all(self) -> None:
        for serial in list(self._sessions):
            self.close(serial)

    # ---- 查询 -----------------------------------------------------
    def get(self, serial: str) -> AndroidSession | None:
        return self._sessions.get(serial)

    def is_open(self, serial: str) -> bool:
        return serial in self._sessions

    def sessions(self) -> list[AndroidSession]:
        return list(self._sessions.values())

    def serials(self) -> list[str]:
        return list(self._sessions)
