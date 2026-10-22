"""scrcpy-server 的部署、启动与连接管理（对齐 scrcpy v5.x）。

流程（tunnel_forward 模式）::

    adb push scrcpy-server-v<X.Y.Z> /data/local/tmp/scrcpy-server.jar
    adb forward tcp:<port> localabstract:scrcpy_<scid>     # 只需一条转发
    adb shell CLASSPATH=/data/local/tmp/scrcpy-server.jar \
        app_process / com.genymobile.scrcpy.Server <X.Y.Z> scid=<scid> \
        tunnel_forward=true ...
    客户端连接 127.0.0.1:<port>（第一条）→ 视频流
    客户端连接 127.0.0.1:<port>（第二条）→ 控制通道

⚠ v5.x 的 server 在**同一个 socket 名**上按顺序 accept 各通道连接
（video → audio → control），不是 2.x 的 ``_control`` 独立 socket 名。
第一条连接必须是视频，顺序不能颠倒。

`scrcpy-server` 不随本仓库分发（GPL-3.0），请从 scrcpy 官方发布页下载后
放入 ``third_party/``：https://github.com/Genymobile/scrcpy/releases
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .adb import Adb, AdbError

log = logging.getLogger(__name__)

DEFAULT_SERVER_VERSION = "5.0.1"
SERVER_REMOTE_PATH = "/data/local/tmp/scrcpy-server.jar"

__all__ = [
    "ScrcpyError",
    "ScrcpyOptions",
    "ScrcpyServer",
    "find_server_jar",
    "server_version_from_jar",
]


class ScrcpyError(RuntimeError):
    pass


@dataclass
class ScrcpyOptions:
    """server 启动参数（对应 scrcpy server 的 key=value 选项）。"""

    max_size: int = 0  # 0 = 设备原始分辨率
    video_bit_rate: int = 8_000_000
    video_codec: str = "h264"
    max_fps: float | None = None
    control: bool = True
    audio: bool = False
    stay_awake: bool = False
    power_off_on_close: bool = False
    show_touches: bool = False
    cleanup: bool = True

    @classmethod
    def for_display(cls, info, **kwargs) -> ScrcpyOptions:
        """按设备屏幕参数生成默认画质：原始分辨率 + 峰值刷新率。"""
        options = cls()
        if info is not None and info.width > 0:
            options.max_size = info.max_dimension
            fps = info.refresh_rate if info.refresh_rate > 0 else 60.0
            options.video_bit_rate = estimate_bit_rate(info.width, info.height, fps)
            if info.refresh_rate > 0:
                options.max_fps = info.refresh_rate
        for key, value in kwargs.items():
            setattr(options, key, value)
        return options

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
            # send_dummy_byte 保持默认 true：server 会在视频通道先发 1 字节，
            # 客户端用它区分「真连上」与「adb forward 的假连接」
        ]
        if self.max_fps is not None:
            args.append(f"max_fps={self.max_fps:g}")
        return args


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def estimate_bit_rate(width: int, height: int, fps: float) -> int:
    """按像素数 × 帧率估算码率（1080p@60 ≈ 7.5 Mbps，接近 scrcpy 默认）。"""
    return max(2_000_000, min(30_000_000, int(width * height * fps * 0.06)))


def server_version_from_jar(path: str | Path) -> str:
    """从文件名解析 server 版本，如 ``scrcpy-server-v5.0.1`` → ``5.0.1``。

    版本号必须与 server 的 ``BuildConfig.VERSION_NAME`` 完全一致，
    否则 server 会直接退出（Options.parse 的第一个参数校验）。
    """
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", Path(path).name)
    return match.group(1) if match else DEFAULT_SERVER_VERSION


def find_server_jar() -> Path:
    """定位 scrcpy-server 文件。查找顺序：

    1. 环境变量 ``MOBILECAST_SCRCPY_SERVER``
    2. 仓库根目录 ``third_party/scrcpy-server*``
    3. 包目录 ``data/scrcpy-server*``（打包发布时随包附带）

    找不到时抛 :class:`ScrcpyError` 并给出下载指引。
    """
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
            matches = sorted(directory.glob("scrcpy-server*"))
            matches = [m for m in matches if m.is_file()]
            if matches:
                return matches[-1]

    raise ScrcpyError(
        "未找到 scrcpy-server。请从 https://github.com/Genymobile/scrcpy/releases "
        "下载 scrcpy-server-v<X.Y.Z> 放入 third_party/ 目录，"
        "或设置环境变量 MOBILECAST_SCRCPY_SERVER 指向它。"
    )


class ScrcpyServer:
    """一次 scrcpy-server 会话：负责子进程与视频 / 控制 socket 的生命周期。"""

    def __init__(
        self,
        adb: Adb,
        serial: str,
        server_jar: str | Path,
        options: ScrcpyOptions | None = None,
        version: str | None = None,
    ) -> None:
        self._adb = adb
        self._serial = serial
        self._server_jar = Path(server_jar)
        self._options = options or ScrcpyOptions()
        self._version = version or server_version_from_jar(self._server_jar)
        # scid 必须是 31 位非负数：server 端用 Integer.parseInt(value, 16) 解析，
        # 超过 0x7FFFFFFF 会抛 NumberFormatException（负数保留给 "none"）
        self._scid = f"{secrets.randbelow(0x80000000):08x}"

        self._process: subprocess.Popen | None = None
        self._video_socket: socket.socket | None = None
        self._control_socket: socket.socket | None = None
        self._port: int | None = None
        self._log_tail: deque[str] = deque(maxlen=20)

    @property
    def version(self) -> str:
        return self._version

    # ---- 生命周期 -------------------------------------------------
    def start(
        self, connect_timeout: float = 10.0, attempts: int = 3
    ) -> tuple[socket.socket, socket.socket]:
        """启动 server 并建立连接，返回 (video_socket, control_socket)。

        失败自动重试：上一个会话的 CleanUp 线程可能延迟删除
        ``/data/local/tmp/scrcpy-server.jar``，与新推送的 jar 产生竞争。
        """
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._start_once(connect_timeout)
            except (ScrcpyError, OSError) as exc:
                last_error = exc
                self.stop()
                if attempt < attempts:
                    # 仅在失败重试时清理残留 server：
                    # 预清理会误杀其它并行会话（如另一实例 / scrcpy 官方客户端）
                    self._kill_stale_servers()
                    log.warning(
                        "启动 scrcpy-server 失败（第 %d/%d 次），重试: %s", attempt, attempts, exc
                    )
                    time.sleep(1.0)
        raise ScrcpyError(
            f"启动 scrcpy-server 失败: {last_error}\n--- server 日志 ---\n"
            + "\n".join(self._log_tail)
        )

    def _start_once(self, connect_timeout: float) -> tuple[socket.socket, socket.socket]:
        if not self._server_jar.exists():
            raise ScrcpyError(f"缺少 scrcpy-server 文件: {self._server_jar}")

        socket_name = f"scrcpy_{self._scid}"

        try:
            self._adb.push(self._serial, self._server_jar, SERVER_REMOTE_PATH)

            self._port = _free_port()
            self._adb.forward(self._serial, f"tcp:{self._port}", f"localabstract:{socket_name}")

            self._process = self._adb.popen_shell(self._serial, *self._server_command())
            self._drain_server_output(self._process)
        except AdbError as exc:
            raise ScrcpyError(f"启动 scrcpy-server 失败: {exc}") from exc

        # 顺序不能颠倒：server 先 accept 视频，再 accept 控制。
        # 视频连接必须读到 dummy byte 才算真正建立（adb forward 会先接受
        # 本地连接、后探设备端 socket，server 未就绪时会得到"假连接"）。
        self._video_socket = self._connect_alive("127.0.0.1", self._port, connect_timeout)
        self._control_socket = self._connect("127.0.0.1", self._port, connect_timeout)
        log.info(
            "scrcpy-server %s 已就绪: %s (scid=%s)",
            self._version,
            self._serial,
            self._scid,
        )
        return self._video_socket, self._control_socket

    def _kill_stale_servers(self) -> None:
        """清理设备上残留的 scrcpy-server 进程（上个会话异常退出时可能遗留）。"""
        try:
            self._adb.shell(
                self._serial, "pkill", "-f", "com.genymobile.scrcpy.Server", timeout=5.0
            )
        except AdbError:
            pass  # 没有残留进程 / 设备不支持 pkill 都没关系
        time.sleep(0.3)  # 给 CleanUp 留出时间，避免它删掉接下来推送的 jar

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

        if self._port is not None:
            self._adb.forward_remove(self._serial, f"tcp:{self._port}")
            self._port = None

    # ---- 内部 ----------------------------------------------------
    def _drain_server_output(self, process: subprocess.Popen) -> None:
        """后台排空 server 的输出，防止管道写满阻塞设备端进程。"""

        def drain(stream, tag: str) -> None:
            try:
                for line in iter(stream.readline, b""):
                    text = line.decode("utf-8", errors="replace").rstrip()
                    self._log_tail.append(f"[{tag}] {text}")
                    log.debug("scrcpy-server[%s]: %s", tag, text)
            except OSError:
                pass

        for stream, tag in ((process.stdout, "out"), (process.stderr, "err")):
            if stream is not None:
                threading.Thread(
                    target=drain, args=(stream, tag), name=f"scrcpy-server-{tag}", daemon=True
                ).start()

    def _server_command(self) -> list[str]:
        parts = [
            f"CLASSPATH={SERVER_REMOTE_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            self._version,
            f"scid={self._scid}",
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
                sock = socket.create_connection((host, port), timeout=timeout)
                sock.settimeout(None)  # 超时只用于建连；之后阻塞读
                return sock
            except OSError as exc:  # noqa: PERF203
                last_error = exc
                time.sleep(0.1)
        raise OSError(f"连接 {host}:{port} 超时: {last_error}")

    @staticmethod
    def _connect_alive(host: str, port: int, timeout: float) -> socket.socket:
        """连接并确认对端真的在服务（读到 dummy byte 才算成功）。

        ``adb forward`` 会立即接受本地 TCP 连接，之后才去连设备端 socket；
        server 尚未启动时连接会直接断开。收到 dummy byte 说明 accept 已发生。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((host, port), timeout=timeout)
            except OSError:
                time.sleep(0.1)
                continue
            sock.settimeout(timeout)
            try:
                first_byte = sock.recv(1)
            except OSError:
                first_byte = b""
            if first_byte:
                # 之后是长驻流：切回阻塞模式，否则静态画面（无新帧）会读超时断线
                sock.settimeout(None)
                return sock  # dummy byte 已被消费，后续即为设备元信息
            sock.close()
            time.sleep(0.1)
        raise OSError(f"连接 {host}:{port} 超时（server 未就绪）")
