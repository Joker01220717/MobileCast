"""MobileCast 程序入口。"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mobilecast", description="移动设备投屏与控制")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--serial", help="启动后自动连接指定设备序列号")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print('缺少 UI 依赖，请先执行: pip install -e ".[ui,video]"', file=sys.stderr)
        return 1

    from .ui.main_window import MainWindow

    app = QApplication([sys.argv[0]])
    app.setApplicationName("MobileCast")
    window = MainWindow()
    window.show()

    if args.serial:
        # TODO(Phase 1): 直接建立会话
        window.statusBar().showMessage(f"--serial {args.serial} 将在 Phase 1 支持自动连接")

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
