"""环境自检：``python -m mobilecast --doctor``。

排查真机验证问题时先跑这个命令，输出各项检查结果。
不需要 Qt / 设备也能运行。
"""

from __future__ import annotations

import sys

__all__ = ["run_doctor"]


def _check(name: str, fn) -> bool:
    try:
        detail = fn()
        print(f"  [ OK ] {name}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as exc:  # noqa: BLE001 - 自检要展示任何失败原因
        print(f"  [FAIL] {name} — {exc}")
        return False


def run_doctor() -> int:
    print("MobileCast 环境自检")
    print("=" * 40)
    results: list[bool] = []

    def python_version() -> str:
        return sys.version.split()[0]

    def pyside() -> str:
        import PySide6

        return PySide6.__version__

    def av() -> str:
        import av

        return av.__version__

    def numpy() -> str:
        import numpy

        return numpy.__version__

    def adb() -> str:
        from .core.android.adb import Adb

        client = Adb()
        return f"{client.version()}（{client.adb_path}）"

    def server_jar() -> str:
        from .core.android.scrcpy_server import find_server_jar, server_version_from_jar

        path = find_server_jar()
        return f"{path.name}（版本 {server_version_from_jar(path)}）"

    def devices() -> str:
        from .core.android.adb import Adb

        devices_ = Adb().devices()
        if not devices_:
            raise RuntimeError("未发现设备，请用 USB 连接并开启 USB 调试")
        return ", ".join(f"{d.model}[{d.state}]" for d in devices_)

    print("\n[运行环境]")
    results.append(_check("Python", python_version))
    results.append(_check("PySide6（界面）", pyside))
    results.append(_check("PyAV（解码）", av))
    results.append(_check("numpy", numpy))

    print("\n[投屏组件]")
    results.append(_check("adb", adb))
    results.append(_check("scrcpy-server", server_jar))

    print("\n[设备]")
    results.append(_check("已连接设备", devices))

    print()
    if all(results):
        print("[OK] 全部检查通过，可以运行: python -m mobilecast")
        return 0
    print("[FAIL] 存在未通过项，请按上面的提示处理")
    return 1
