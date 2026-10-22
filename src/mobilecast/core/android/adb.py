"""adb 命令行封装（纯标准库，Windows / Linux 通用）。

统一负责：
- 查找 adb 可执行文件（PATH → ANDROID_HOME → ANDROID_SDK_ROOT → 默认安装路径）
- 设备枚举、文件推送、端口转发、shell 执行
- 子进程的创建与清理（Windows 下隐藏控制台窗口）
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

__all__ = ["AdbError", "AdbDevice", "Adb", "find_adb"]


class AdbError(RuntimeError):
    """adb 调用失败。message 中包含 adb 的 stderr 输出。"""


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str  # device / offline / unauthorized / ...
    props: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def model(self) -> str:
        return self.props.get("model", self.serial)


def find_adb() -> str:
    """定位 adb 可执行文件，找不到时抛 AdbError。"""
    exe = "adb.exe" if os.name == "nt" else "adb"

    found = shutil.which("adb")
    if found:
        return found

    candidates: list[Path] = []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            candidates.append(Path(root) / "platform-tools" / exe)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates.append(Path(local) / "Android" / "Sdk" / "platform-tools" / exe)
        candidates.append(
            Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / exe
        )

    for path in candidates:
        if path.exists():
            return str(path)

    raise AdbError(
        "未找到 adb。请安装 Android SDK platform-tools 并加入 PATH，"
        "参考 https://developer.android.com/tools/releases/platform-tools"
    )


class Adb:
    """轻量 adb 客户端。所有方法都以 serial 指定目标设备。"""

    def __init__(self, adb_path: str | None = None, timeout: float = 15.0) -> None:
        self.adb_path = adb_path or find_adb()
        self.timeout = timeout

    # ---- 基础执行 -------------------------------------------------
    def _run(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        cmd = [self.adb_path, *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout or self.timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            stdout = proc.stdout.decode("utf-8", errors="replace").strip()
            raise AdbError(
                f"adb {' '.join(args)} 失败（exit={proc.returncode}）: {stderr or stdout}"
            )
        return proc

    def version(self) -> str:
        out = self._run("version").stdout.decode("utf-8", errors="replace")
        return out.splitlines()[0] if out else ""

    # ---- 设备 ----------------------------------------------------
    def devices(self) -> list[AdbDevice]:
        """枚举设备。返回所有状态（含 offline / unauthorized）。"""
        out = self._run("devices", "-l").stdout.decode("utf-8", errors="replace")
        result: list[AdbDevice] = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            props: dict[str, str] = {}
            for item in parts[2:]:
                if ":" in item:
                    key, _, value = item.partition(":")
                    props[key] = value
            result.append(AdbDevice(serial=serial, state=state, props=props))
        return result

    # ---- 文件 / 端口 ---------------------------------------------
    def push(self, serial: str, local: str | Path, remote: str) -> None:
        self._run("-s", serial, "push", str(local), remote, timeout=120.0)

    def forward(self, serial: str, local_spec: str, remote_spec: str) -> None:
        """例如 forward(serial, 'tcp:27183', 'localabstract:scrcpy_1234abcd')。"""
        self._run("-s", serial, "forward", local_spec, remote_spec)

    def forward_remove(self, serial: str, local_spec: str) -> None:
        try:
            self._run("-s", serial, "forward", "--remove", local_spec)
        except AdbError:
            pass  # 清理失败不影响主流程

    def forward_remove_all(self) -> None:
        try:
            self._run("forward", "--remove-all")
        except AdbError:
            pass

    # ---- 无线连接 ------------------------------------------------
    def tcpip(self, serial: str, port: int = 5555) -> str:
        """让设备的 adbd 在 TCP 端口上监听（之后可用 adb connect 无线连接）。

        注意：设备端 adbd 会重启，USB 连接可能短暂断开。
        """
        out = self._run("-s", serial, "tcpip", str(port), timeout=30.0)
        return out.stdout.decode("utf-8", errors="replace").strip()

    def connect(self, address: str) -> str:
        """连接无线设备，address 形如 ``192.168.1.8:5555``。"""
        out = self._run("connect", address, timeout=15.0)
        return out.stdout.decode("utf-8", errors="replace").strip()

    def disconnect(self, address: str) -> str:
        try:
            out = self._run("disconnect", address, timeout=15.0)
            return out.stdout.decode("utf-8", errors="replace").strip()
        except AdbError:
            return ""

    def device_ip(self, serial: str, interface: str = "wlan0") -> str | None:
        """读取设备在无线网络上的 IPv4 地址；读不到返回 None。"""
        try:
            out = self.shell(serial, "ip", "-f", "inet", "addr", "show", interface)
        except AdbError:
            out = ""
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if match:
            return match.group(1)

        # 回退：从路由表里找 src 地址
        try:
            out = self.shell(serial, "ip", "route")
        except AdbError:
            return None
        match = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
        return match.group(1) if match else None

    # ---- shell ---------------------------------------------------
    def shell(self, serial: str, *cmd: str, timeout: float | None = None) -> str:
        out = self._run("-s", serial, "shell", *cmd, timeout=timeout)
        return out.stdout.decode("utf-8", errors="replace")

    def popen_shell(self, serial: str, *cmd: str) -> subprocess.Popen:
        """启动长驻 shell 进程（如 scrcpy-server），返回 Popen 供调用方管理。"""
        args = [self.adb_path, "-s", serial, "shell", *cmd]
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )

    def getprop(self, serial: str, name: str) -> str:
        return self.shell(serial, "getprop", name).strip()
