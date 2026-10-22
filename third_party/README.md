# third_party

本目录存放**不入库**的第三方组件（见根目录 `.gitignore` 与
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）：

| 文件 | 来源 | 用途 |
| --- | --- | --- |
| `platform-tools/`（含 `adb.exe`） | https://developer.android.com/tools/releases/platform-tools | 设备通道 |
| `scrcpy-server-v<X.Y.Z>` | https://github.com/Genymobile/scrcpy/releases | Android 推流与输入注入（GPL-3.0） |

## 准备方法

```powershell
# 1. adb
Invoke-WebRequest https://dl.google.com/android/repository/platform-tools-latest-windows.zip -OutFile pt.zip
Expand-Archive pt.zip -DestinationPath . -Force; Remove-Item pt.zip

# 2. scrcpy-server（把 <X.Y.Z> 换成实际版本，如 5.0.1）
#    也可用环境变量 MOBILECAST_SCRCPY_SERVER 指向任意位置的文件
Invoke-WebRequest https://github.com/Genymobile/scrcpy/releases/download/v<X.Y.Z>/scrcpy-server-v<X.Y.Z> -OutFile scrcpy-server-v<X.Y.Z>
```

或直接解压 scrcpy 官方发布包，把其中的 `scrcpy-server-*` 拷到本目录。

准备完成后运行 `python -m mobilecast --doctor` 检查。

> 注意：server 文件名里的版本号会被解析为启动参数（必须与 server 自身版本一致），
> 请保持官方命名 `scrcpy-server-v<X.Y.Z>`。
