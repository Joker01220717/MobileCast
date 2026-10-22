"""adb 封装测试（mock 子进程）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobilecast.core.android import adb as adb_mod
from mobilecast.core.android.adb import Adb, AdbDevice, AdbError


@pytest.fixture()
def fake_run(monkeypatch):
    def _install(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        calls: list[list[str]] = []

        def _run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

        monkeypatch.setattr(adb_mod.subprocess, "run", _run)
        return calls

    return _install


def test_devices_parsing(fake_run):
    fake_run(
        stdout=(
            b"List of devices attached\n"
            b"R5CT30ABC12\tdevice product:beyond1q model:SM_G973F device:beyond1\n"
            b"emulator-5554\toffline\n"
            b"\n"
        )
    )
    devices = Adb(adb_path="adb").devices()
    assert len(devices) == 2
    assert devices[0] == AdbDevice(
        serial="R5CT30ABC12",
        state="device",
        props={"product": "beyond1q", "model": "SM_G973F", "device": "beyond1"},
    )
    assert devices[0].ready
    assert devices[0].model == "SM_G973F"
    assert not devices[1].ready


def test_command_failure_raises_adb_error(fake_run):
    fake_run(stderr=b"error: device not found", returncode=1)
    with pytest.raises(AdbError, match="device not found"):
        Adb(adb_path="adb").shell("serial-1", "getprop", "ro.product.model")


def test_forward_and_push_args(fake_run):
    calls = fake_run()
    client = Adb(adb_path="adb")
    client.forward("serial-1", "tcp:27183", "localabstract:scrcpy_ab12cd34")
    client.push("serial-1", "server.jar", "/data/local/tmp/scrcpy-server.jar")

    assert calls[0][1:6] == [
        "-s",
        "serial-1",
        "forward",
        "tcp:27183",
        "localabstract:scrcpy_ab12cd34",
    ]
    assert calls[1][1:5] == ["-s", "serial-1", "push", "server.jar"]


def test_find_adb_without_platform_tools(monkeypatch):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(adb_mod.Path, "exists", lambda self: False)
    with pytest.raises(AdbError, match="未找到 adb"):
        adb_mod.find_adb()


def test_tcpip_and_connect_args(fake_run):
    calls = fake_run(stdout=b"restarting in TCP mode port: 5555\n")
    client = Adb(adb_path="adb")
    assert "5555" in client.tcpip("serial-1", 5555)
    assert calls[0][1:] == ["-s", "serial-1", "tcpip", "5555"]


def test_connect_address(fake_run):
    calls = fake_run(stdout=b"connected to 192.168.1.8:5555\n")
    client = Adb(adb_path="adb")
    assert "connected" in client.connect("192.168.1.8:5555")
    assert calls[0][1:] == ["connect", "192.168.1.8:5555"]


def test_device_ip_from_addr_show(monkeypatch):
    client = Adb(adb_path="adb")
    outputs = ["3: wlan0: <BROADCAST,MULTICAST> inet 192.168.1.8/24 scope global wlan0\n"]
    monkeypatch.setattr(client, "shell", lambda *a, **k: outputs.pop(0))
    assert client.device_ip("serial-1") == "192.168.1.8"


def test_device_ip_falls_back_to_ip_route(monkeypatch):
    client = Adb(adb_path="adb")
    outputs = [
        "3: wlan0: <BROADCAST,MULTICAST,UP>\n",
        "192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.99\n",
    ]
    monkeypatch.setattr(client, "shell", lambda *a, **k: outputs.pop(0))
    assert client.device_ip("serial-1") == "192.168.1.99"


def test_device_ip_returns_none_when_unavailable(monkeypatch):
    client = Adb(adb_path="adb")
    monkeypatch.setattr(client, "shell", lambda *a, **k: "")
    assert client.device_ip("serial-1") is None
