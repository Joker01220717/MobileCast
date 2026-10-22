# 贡献指南

感谢你对 MobileCast 的兴趣！

## 开发环境

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[ui,video,dev]"
```

## 提交前检查

```powershell
ruff check src tests
pytest
```

CI 会在 Windows 与 Linux 上运行同样的检查，请保证本地通过。

## 代码约定

- `core/` 不允许 import Qt；`ui/` 不允许依赖具体协议实现。
- 协议相关改动（`scrcpy_*`）必须同步更新 [docs/scrcpy-protocol.md](docs/scrcpy-protocol.md)
  并注明上游版本。
- 新功能附带单元测试；协议编解码改动必须有字节级（golden bytes）测试。
- 提交信息使用祈使句、简洁明确，例如 `add scroll event encoder`。

## 报告问题

请提供：Windows 版本、Python 版本、Android/iOS 设备型号与系统版本、
复现步骤、`python -m mobilecast --debug` 的日志输出。

## 行为准则

保持友善与专业，尊重每一位参与者。
