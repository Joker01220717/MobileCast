"""设备抽象层。

UI 与会话层只依赖本模块定义的接口（`DeviceSession`、`VideoFrame`、`InputEvent`），
不感知 scrcpy / WebDriverAgent 等具体协议，方便后续接入新的设备通道。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import IntEnum

# 鼠标主键（AMOTION_EVENT_BUTTON_PRIMARY）
BUTTON_PRIMARY = 0x1
BUTTON_SECONDARY = 0x2
BUTTON_MIDDLE = 0x4

# scrcpy 约定的虚拟指针 id
POINTER_ID_MOUSE = 0xFFFFFFFFFFFFFFFF
POINTER_ID_GENERIC_FINGER = 0xFFFFFFFFFFFFFFFE


class Platform(IntEnum):
    ANDROID = 1
    IOS = 2


class SessionState(IntEnum):
    CREATED = 0
    CONNECTING = 1
    CONNECTED = 2
    LOST = 3
    CLOSED = 4


class TouchAction(IntEnum):
    DOWN = 0
    UP = 1
    MOVE = 2


class KeyAction(IntEnum):
    DOWN = 0
    UP = 1


@dataclass(frozen=True)
class DeviceInfo:
    """一台已发现的设备。"""

    serial: str
    name: str
    platform: Platform
    width: int = 0
    height: int = 0

    @property
    def screen_size(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True)
class TouchEvent:
    """触摸事件。x/y 为设备像素坐标，screen_* 为设备屏幕尺寸。"""

    action: TouchAction
    x: int
    y: int
    screen_width: int
    screen_height: int
    pressure: float = 1.0
    pointer_id: int = POINTER_ID_MOUSE
    action_button: int = 0
    buttons: int = 0


@dataclass(frozen=True)
class KeyEvent:
    """按键事件。keycode 为 Android keycode（AKEYCODE_*）。"""

    action: KeyAction
    keycode: int
    repeat: int = 0
    metastate: int = 0


@dataclass(frozen=True)
class TextEvent:
    """文本输入事件，整段 UTF-8 文本下发，与输入法无关。"""

    text: str


@dataclass(frozen=True)
class ScrollEvent:
    """滚轮事件。hscroll/vscroll 为原始滚轮量（典型范围 [-16, 16]）。"""

    x: int
    y: int
    screen_width: int
    screen_height: int
    hscroll: float
    vscroll: float
    buttons: int = 0


InputEvent = TouchEvent | KeyEvent | TextEvent | ScrollEvent


@dataclass(frozen=True)
class VideoFrame:
    """解码后的视频帧。data 为连续内存（默认 RGB24）。"""

    data: bytes
    width: int
    height: int
    pts: int = 0
    format: str = "rgb24"
    extra: dict = field(default_factory=dict)


class DeviceSession(ABC):
    """一台设备的会话生命周期与输入输出通道。"""

    @property
    @abstractmethod
    def info(self) -> DeviceInfo: ...

    @property
    @abstractmethod
    def state(self) -> SessionState: ...

    @abstractmethod
    def start(self) -> None:
        """建立连接并开始推流。失败抛出异常，状态转为 LOST/CLOSED。"""

    @abstractmethod
    def stop(self) -> None:
        """断开连接并释放所有子进程 / socket。幂等。"""

    @abstractmethod
    def frames(self) -> Iterator[VideoFrame]:
        """阻塞式拉取解码后的帧，直到会话结束。"""

    @abstractmethod
    def send_touch(self, event: TouchEvent) -> None: ...

    @abstractmethod
    def send_key(self, event: KeyEvent) -> None: ...

    @abstractmethod
    def send_text(self, event: TextEvent) -> None: ...

    @abstractmethod
    def send_scroll(self, event: ScrollEvent) -> None: ...

    def send_input(self, event: InputEvent) -> None:
        """按事件类型分发（默认实现）。"""
        if isinstance(event, TouchEvent):
            self.send_touch(event)
        elif isinstance(event, KeyEvent):
            self.send_key(event)
        elif isinstance(event, TextEvent):
            self.send_text(event)
        elif isinstance(event, ScrollEvent):
            self.send_scroll(event)
        else:
            raise TypeError(f"未知输入事件类型: {type(event)!r}")
