"""scrcpy-server 的部署、启动与连接管理。

流程（tunnel_forward 模式）::

    adb push scrcpy-server.jar /data/local/tmp/scrcpy-server.jar
    adb forward tcp:<video_port>   localabstract:scrcpy_<scid>
    adb forward tcp:<control_port> localabstract:scrcpy_<scid>_control
    adb shell CLASSPATH=/data/local/tmp/scrcpy-server.jar \
        app_process / com.genymobile.scrcpy.Server <version> tunnel_forward=true ...
    客户端连接 127.0.0.1:<video_port>   → 视频流（首部 64B 设备名由会话层读取）
    客户端连接 127.0.0.1:<control_port> → 控制通道

`scrcpy-server.jar` 不随本仓库分发（GPL-3.0），请从 scrcpy 官方发布页下载后
放入 ``third_party/``：https://github.com/Genymobile/scrcpy/releases
"""

from __future__ import annotations

import logging
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .adb import Adb, AdbError

log = logging.getLogger(__name__)

DEFAULT_SERVER_VERSION = "3.0"
SERVER_REMOTE_PATH = "/data/local/tmp/scrcpy-server.jar"

__all__ = ["ScrcpyError", "ScrcpyOptions", "ScrcpyServer", "find_server_jar"]


class ScrcpyError(RuntimeError):
    pass


@dataclass
class ScrcpyOptions:
    """server 启动参数（对应 scrcpy 命令行选项的 key=value 形式）。"""

    max_size: int = 1280
    video_bit_rate: int = 8_000_000
    video_codec: str = "h264"
    max_fps: int | None = None
    control: bool = True
    audio: bool = False
    stay_awake: bool = False
    power_off_on_close: bool = False
    show_touches: bool = False
    cleanup: bool = True

    def to_server_args(self) -> list[str]:
        args = [
            "tunnel_forward=true",
            f"control={'true' if self.control else 'false'}",
            f"audio={'true' if self.audio else 'false'}",
            f"cleanup={'true' if self.cleanup else 'false'}",
            f"video_codec={self.video_codec}",
            f"max_size={self.max_size}",
            f"video_bit_rate={self.video_bit_rate}",
            f"stay_awake={'true' if self.stay_awake else 'false'}",
            f"power_off_on_close={'true' if self.power_off_on_close else 'false'}",
            f"show_touches={'true' if self.show_touches else 'false'}",
        ]
        if self.max_fps is not None:
            args.append(f"max_fps={self.max_fps}")
        return args


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def find_server_jar() -> Path:
    """定位 scrcpy-server jar。查找顺序：

    1. 环境变量 ``MOBILECAST_SCRCPY_SERVER``
    2. 仓库根目录 ``third_party/scrcpy-server*.jar``
    3. 包目录 ``data/scrcpy-server*.jar``（打包发布时随包附带）

    找不到时抛 :class:`ScrcpyError` 并给出下载指引。
    """
    import os

    env_path = os.environ.get("MOBILECAST_SCRCPY_SERVER")
    if env_path:
        path = Path(env_path)
        if path.exists():
            return path

    search_dirs = [
        Path(__file__).resolve().parents[4] / "third_party",  # <repo>/third_party
        Path(__file__).resolve().parents[2] / "data",  # <package>/data
    ]
    for directory in search_dirs:
        if directory.is_dir():
            matches = sorted(directory.glob("scrcpy-server*.jar"))
            if matches:
                return matches[-1]

    raise ScrcpyError(
        "未找到 scrcpy-server.jar。请从 https://github.com/Genymobile/scrcpy/releases "
        "下载后放入 third_party/ 目录，或设置环境变量 MOBILECAST_SCRCPY_SERVER 指向它。"
    )


class ScrcpyServer:
    """一次 scrcpy-server 会话：负责子进程与视频 / 控制 socket 的生命周期。"""

    def __init__(
        self,
        adb: Adb,
        serial: str,
        server_jar: str | Path,
        options: ScrcpyOptions | None = None,
        version: str = DEFAULT_SERVER_VERSION,
    ) -> None:
        self._adb = adb
        self._serial = serial
        self._server_jar = Path(server_jar)
        self._options = options or ScrcpyOptions()
        self._version = version
        self._scid = secrets.token_hex(4)

        self._process: subprocess.Popen | None = None
        self._video_socket: socket.socket | None = None
        self._control_socket: socket.socket | None = None
        self._video_port: int | None = None
        self._control_port: int | None = None

    # ---- 生命周期 -------------------------------------------------
    def start(self, connect_timeout: float = 5.0) -> tuple[socket.socket, socket.socket]:
        """启动 server 并建立连接，返回 (video_socket, control_socket)。"""
        if not self._server_jar.exists():
            raise ScrcpyError(
                f"缺少 scrcpy-server: {self._server_jar}。"
                "请从 https://github.com/Genymobile/scrcpy/releases 下载后放入 third_party/"
            )

        video_name = f"scrcpy_{self._scid}"
        control_name = f"{video_name}_control"

        try:
            self._adb.push(self._serial, self._server_jar, SERVER_REMOTE_PATH)

            self._video_port = _free_port()
            self._adb.forward(
                self._serial, f"tcp:{self._video_port}", f"localabstract:{video_name}"
            )
            self._control_port = _free_port()
            self._adb.forward(
                self._serial, f"tcp:{self._control_port}", f"localabstract:{control_name}"
            )

            self._process = self._adb.popen_shell(self._serial, *self._server_command())
        except AdbError as exc:
            self.stop()
            raise ScrcpyError(f"启动 scrcpy-server 失败: {exc}") from exc

        try:
            self._video_socket = self._connect("127.0.0.1", self._video_port, connect_timeout)
            self._control_socket = self._connect("127.0.0.1", self._control_port, connect_timeout)
            log.info("scrcpy-server 隧道已建立: %s (scid=%s)", self._serial, self._scid)
        except OSError as exc:
            self.stop()
            raise ScrcpyError(f"连接 scrcpy-server 失败: {exc}") from exc

        return self._video_socket, self._control_socket

    def stop(self) -> None:
        """关闭 socket、终止 server 进程并清理端口转发。幂等。"""
        for sock in (self._control_socket, self._video_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._control_socket = None
        self._video_socket = None

        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                self._process.kill()
            self._process = None

        if self._video_port is not None:
            self._adb.forward_remove(self._serial, f"tcp:{self._video_port}")
            self._video_port = None
        if self._control_port is not None:
            self._adb.forward_remove(self._serial, f"tcp:{self._control_port}")
            self._control_port = None

    # ---- 内部 ----------------------------------------------------
    def _server_command(self) -> list[str]:
        parts = [
            f"CLASSPATH={SERVER_REMOTE_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            self._version,
            *self._options.to_server_args(),
        ]
        return [" ".join(parts)]

    @staticmethod
    def _connect(host: str, port: int, timeout: float) -> socket.socket:
        """连接本地隧道端口；server 就绪需要一点时间，做有限重试。"""
        deadline = time.monotonic() + timeout
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            try:
                return socket.create_connection((host, port), timeout=timeout)
            except OSError as exc:  # noqa: PERF203
                last_error = exc
                time.sleep(0.1)
        raise OSError(f"连接 {host}:{port} 超时: {last_error}")
