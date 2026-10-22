"""Android 投屏会话：串联 scrcpy-server、解码器与输入注入。

线程模型：

- **视频线程**（daemon）：读视频 socket → Annex-B 切 NAL → 解码 → 最新帧槽位
- **frames()**：调用方（如 UI 的 QThread）阻塞拉取解码帧
- **send_***：任意线程可调用，内部加锁写控制 socket

帧槽位采用 ``queue.Queue(maxsize=1)`` 的「最新帧优先」语义：
渲染端永远取最新画面，落后帧直接丢弃，保证低延迟。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from ..device import (
    DeviceInfo,
    DeviceSession,
    KeyEvent,
    Platform,
    ScrollEvent,
    SessionState,
    TextEvent,
    TouchEvent,
    VideoFrame,
)
from ..video.decoder import VideoDecoder, create_decoder
from .adb import Adb
from .device_msg import (
    AckClipboardMessage,
    ClipboardMessage,
    DeviceMsg,
    DeviceMsgParser,
)
from .scrcpy_control import (
    CopyKey,
    ScrcpyControlWriter,
    encode_get_clipboard,
    encode_inject_keycode,
    encode_inject_scroll,
    encode_inject_text,
    encode_inject_touch,
    encode_scan_file,
    encode_set_clipboard,
)
from .scrcpy_server import ScrcpyOptions, ScrcpyServer
from .scrcpy_video import AnnexBSplitter, read_device_meta

log = logging.getLogger(__name__)

__all__ = ["AndroidSession"]

_READ_CHUNK = 65536


class AndroidSession(DeviceSession):
    """一台 Android 设备的投屏会话。"""

    def __init__(
        self,
        adb: Adb | None,
        serial: str,
        server_jar: str | Path | None = None,
        options: ScrcpyOptions | None = None,
        decoder: VideoDecoder | None = None,
        name: str | None = None,
    ) -> None:
        self._adb = adb
        self._serial = serial
        self._server_jar = Path(server_jar) if server_jar is not None else None
        self._options = options or ScrcpyOptions()
        self._decoder = decoder
        self._name = name or serial

        self._server: ScrcpyServer | None = None
        self._writer: ScrcpyControlWriter | None = None
        self._video_socket = None
        self._control_socket = None
        self._state = SessionState.CREATED

        self._frames: queue.Queue[VideoFrame] = queue.Queue(maxsize=1)
        self._ended = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None
        self._clipboard_seq = 0

        # 设备 → PC 的事件回调（由控制通道线程调用，UI 层需自行切线程）
        self.on_clipboard: Callable[[str], None] | None = None
        self.on_clipboard_ack: Callable[[int], None] | None = None

    # ---- 属性 ----------------------------------------------------
    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(serial=self._serial, name=self._name, platform=Platform.ANDROID)

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def serial(self) -> str:
        return self._serial

    # ---- 生命周期 -------------------------------------------------
    def start(self) -> None:
        if self._state is SessionState.CONNECTED:
            return
        if self._adb is None or self._server_jar is None:
            raise RuntimeError("缺少 adb 或 scrcpy-server 路径，无法启动会话")

        self._state = SessionState.CONNECTING
        server = ScrcpyServer(self._adb, self._serial, self._server_jar, self._options)
        try:
            video_socket, control_socket = server.start()
        except Exception:
            self._state = SessionState.LOST
            raise
        self._server = server
        self.attach_sockets(video_socket, control_socket)
        log.info("会话已启动: %s（%s）", self._serial, self._name)

    def attach_sockets(self, video_socket, control_socket) -> str:
        """接管已建立的视频 / 控制 socket（真实连接与测试共用入口）。

        读取 64 字节设备元信息后启动视频线程，返回设备名。
        """
        self._video_socket = video_socket
        self._control_socket = control_socket
        self._writer = ScrcpyControlWriter(control_socket)

        self._name = read_device_meta(video_socket) or self._name
        if self._decoder is None:
            self._decoder = create_decoder(self._options.video_codec)

        self._ended.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._video_loop, name=f"mobilecast-video-{self._serial}", daemon=True
        )
        self._thread.start()
        self._control_thread = threading.Thread(
            target=self._control_loop, name=f"mobilecast-control-{self._serial}", daemon=True
        )
        self._control_thread.start()
        self._state = SessionState.CONNECTED
        return self._name

    def stop(self) -> None:
        self._stop_event.set()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        for sock in (self._video_socket, self._control_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._video_socket = None
        self._control_socket = None
        if self._server is not None:
            self._server.stop()
            self._server = None
        for thread in (self._thread, self._control_thread):
            if thread is not None:
                thread.join(timeout=3)
        self._thread = None
        self._control_thread = None
        self._ended.set()
        self._state = SessionState.CLOSED

    # ---- 视频 ----------------------------------------------------
    def frames(self) -> Iterator[VideoFrame]:
        """阻塞式拉取解码帧，流结束 / 会话停止后返回。"""
        while True:
            try:
                frame = self._frames.get(timeout=0.5)
            except queue.Empty:
                if self._ended.is_set():
                    return
                continue
            yield frame

    def _video_loop(self) -> None:
        splitter = AnnexBSplitter()
        try:
            while not self._stop_event.is_set():
                chunk = self._video_socket.recv(_READ_CHUNK)
                if not chunk:
                    log.info("视频流结束: %s", self._serial)
                    break
                for nal in splitter.feed(chunk):
                    self._decode_and_store(nal)
            tail = splitter.flush()
            if tail is not None:
                self._decode_and_store(tail)
            assert self._decoder is not None
            for frame in self._decoder.flush():
                self._store_frame(frame)
        except OSError as exc:
            if not self._stop_event.is_set():
                log.warning("视频读取异常（%s）: %s", self._serial, exc)
        finally:
            if self._state is SessionState.CONNECTED:
                self._state = SessionState.LOST
            self._ended.set()

    def _decode_and_store(self, nal: bytes) -> None:
        assert self._decoder is not None
        for frame in self._decoder.decode(nal):
            self._store_frame(frame)

    def _store_frame(self, frame: VideoFrame) -> None:
        """最新帧优先：槽位已满时丢弃旧帧。"""
        while True:
            try:
                self._frames.put_nowait(frame)
                return
            except queue.Full:
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass

    # ---- 输入注入 -------------------------------------------------
    def _require_writer(self) -> ScrcpyControlWriter:
        if self._writer is None:
            raise RuntimeError("会话未连接，无法注入输入事件")
        return self._writer

    def send_touch(self, event: TouchEvent) -> None:
        self._require_writer().send(
            encode_inject_touch(
                action=event.action,
                pointer_id=event.pointer_id,
                x=event.x,
                y=event.y,
                screen_width=event.screen_width,
                screen_height=event.screen_height,
                pressure=event.pressure,
                action_button=event.action_button,
                buttons=event.buttons,
            )
        )

    def send_key(self, event: KeyEvent) -> None:
        self._require_writer().send(
            encode_inject_keycode(
                action=event.action,
                keycode=event.keycode,
                repeat=event.repeat,
                metastate=event.metastate,
            )
        )

    def send_text(self, event: TextEvent) -> None:
        self._require_writer().send(encode_inject_text(event.text))

    def send_scroll(self, event: ScrollEvent) -> None:
        self._require_writer().send(
            encode_inject_scroll(
                x=event.x,
                y=event.y,
                screen_width=event.screen_width,
                screen_height=event.screen_height,
                hscroll=event.hscroll,
                vscroll=event.vscroll,
                buttons=event.buttons,
            )
        )

    # ---- 剪贴板 ---------------------------------------------------
    def send_clipboard(self, text: str, paste: bool = False) -> int:
        """把 PC 剪贴板文本同步到设备；paste=True 时写入后立即粘贴。返回 sequence。"""
        self._clipboard_seq += 1
        self._require_writer().send(encode_set_clipboard(self._clipboard_seq, text, paste))
        return self._clipboard_seq

    def request_clipboard(self, copy_key: int = CopyKey.NONE) -> None:
        """请求读取设备剪贴板（结果经 on_clipboard 回调返回）。"""
        self._require_writer().send(encode_get_clipboard(copy_key))

    # ---- 文件传输 -------------------------------------------------
    def push_file(self, local_path: str | Path, remote_dir: str = "/sdcard/Download/") -> str:
        """推送文件到设备并触发媒体库扫描，返回设备上的完整路径。"""
        if self._adb is None:
            raise RuntimeError("缺少 adb，无法推送文件")
        local = Path(local_path)
        remote = f"{remote_dir.rstrip('/')}/{local.name}"
        self._adb.push(self._serial, local, remote)
        self._require_writer().send(encode_scan_file(remote))
        return remote

    # ---- 控制通道反向消息 ------------------------------------------
    def _control_loop(self) -> None:
        parser = DeviceMsgParser()
        sock = self._control_socket
        try:
            while not self._stop_event.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                for message in parser.feed(chunk):
                    self._dispatch_device_msg(message)
        except (OSError, RuntimeError):
            pass  # socket 被 stop() 关闭属于正常退出

    def _dispatch_device_msg(self, message: DeviceMsg) -> None:
        if isinstance(message, ClipboardMessage):
            if self.on_clipboard is not None:
                self.on_clipboard(message.text)
        elif isinstance(message, AckClipboardMessage):
            if self.on_clipboard_ack is not None:
                self.on_clipboard_ack(message.sequence)
        # UhidOutputMessage 暂不处理（虚拟外设回传）
