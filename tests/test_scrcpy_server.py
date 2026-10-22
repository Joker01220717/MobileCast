"""ScrcpyServer 单元测试（不触及设备）。"""

from __future__ import annotations

from pathlib import Path

from mobilecast.core.android.adb import DisplayInfo
from mobilecast.core.android.scrcpy_server import (
    ScrcpyOptions,
    ScrcpyServer,
    estimate_bit_rate,
    server_version_from_jar,
)


def _make_server() -> ScrcpyServer:
    return ScrcpyServer(
        adb=None,  # type: ignore[arg-type]
        serial="TEST-SERIAL",
        server_jar=Path("third_party/scrcpy-server-v5.0.1"),
    )


def test_scid_is_31_bit_non_negative():
    """server 端 Integer.parseInt(scid, 16) 是有符号解析，scid 不能超过 0x7FFFFFFF。

    历史 bug：用 secrets.token_hex(4) 生成的 scid 可能大于 0x7FFFFFFF，
    导致 server 启动即抛 NumberFormatException。
    """
    for _ in range(200):
        server = _make_server()
        scid = server._scid
        assert len(scid) == 8
        assert int(scid, 16) < 0x80000000
        assert scid == f"{int(scid, 16):08x}"  # 与 server 端 String.format("_%08x") 一致


def test_server_version_parsed_from_jar_name():
    assert server_version_from_jar("third_party/scrcpy-server-v5.0.1") == "5.0.1"
    assert server_version_from_jar("scrcpy-server-v2.4.jar") == "2.4"
    assert server_version_from_jar("unknown.jar") == "5.0.1"  # 回退默认


def test_server_command_contains_required_options():
    server = _make_server()
    command = server._server_command()
    assert len(command) == 1
    cmd = command[0]
    assert "com.genymobile.scrcpy.Server" in cmd
    assert f"scid={server._scid}" in cmd
    assert "tunnel_forward=true" in cmd
    assert "video_codec=h264" in cmd
    assert "control=true" in cmd
    assert "audio=false" in cmd


def test_options_serialization():
    args = ScrcpyOptions(max_size=1920, max_fps=30).to_server_args()
    assert "max_size=1920" in args
    assert "max_fps=30" in args
    assert "max_fps" not in " ".join(ScrcpyOptions().to_server_args())


def test_options_for_display_uses_native_resolution_and_peak_rate():
    info = DisplayInfo(width=1200, height=2670, refresh_rate=120.0)
    options = ScrcpyOptions.for_display(info)
    assert options.max_size == 2670  # 原始分辨率（长边）
    assert options.max_fps == 120.0  # 设备峰值刷新率
    assert 2_000_000 <= options.video_bit_rate <= 30_000_000


def test_estimate_bit_rate_scales_and_clamps():
    assert estimate_bit_rate(1920, 1080, 60) == 7_464_960
    assert estimate_bit_rate(10, 10, 1) == 2_000_000  # 下限
    assert estimate_bit_rate(8000, 8000, 240) == 30_000_000  # 上限
