"""WebDriverAgent（WDA）HTTP 客户端 —— iOS 输入注入与截图。

WDA 是一个安装在 iOS 设备上的测试代理，暴露 HTTP 接口。使用前提：

1. 用 Xcode 编译并签名 WebDriverAgent，安装到设备（见 docs/ios-plan.md）；
2. 启动 WDA 后，通过 usbmuxd / iproxy 把设备的 8100 端口映射到本机。

本模块只做协议封装，不处理端口转发与签名流程。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

__all__ = ["WdaError", "WdaClient"]

DEFAULT_BASE_URL = "http://127.0.0.1:8100"


class WdaError(RuntimeError):
    pass


class WdaClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session_id: str | None = None

    # ---- 基础 HTTP -----------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise WdaError(f"WDA 请求失败 {method} {path}: {exc}") from exc
        return json.loads(body) if body else {}

    def _session_path(self, suffix: str = "") -> str:
        if not self._session_id:
            raise WdaError("尚未创建 WDA 会话，请先调用 new_session()")
        return f"/session/{self._session_id}{suffix}"

    # ---- 会话 ----------------------------------------------------
    def status(self) -> dict:
        return self._request("GET", "/status")

    def new_session(self) -> str:
        resp = self._request("POST", "/session", {"capabilities": {}})
        self._session_id = resp.get("sessionId") or resp.get("value", {}).get("sessionId")
        if not self._session_id:
            raise WdaError(f"WDA 未返回 sessionId: {resp}")
        return self._session_id

    def delete_session(self) -> None:
        if self._session_id:
            self._request("DELETE", self._session_path())
            self._session_id = None

    # ---- 输入注入 ------------------------------------------------
    def tap(self, x: int, y: int) -> None:
        self._request("POST", self._session_path("/wda/tap/0"), {"x": x, "y": y})

    def drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.3
    ) -> None:
        self._request(
            "POST",
            self._session_path("/wda/dragfromtoforduration"),
            {
                "fromX": start_x,
                "fromY": start_y,
                "toX": end_x,
                "toY": end_y,
                "duration": duration,
            },
        )

    def keys(self, text: str) -> None:
        self._request("POST", self._session_path("/wda/keys"), {"value": list(text)})

    def press_button(self, name: str) -> None:
        """name: home / volumeUp / volumeDown / ..."""
        self._request("POST", self._session_path("/wda/pressButton"), {"name": name})

    def orientation(self) -> str:
        resp = self._request("GET", self._session_path("/orientation"))
        return str(resp.get("value", "PORTRAIT"))

    # ---- 视频（截图流，低帧率兜底方案） ----------------------------
    def screenshot(self) -> bytes:
        """返回 PNG 字节。适合 5~10 fps 的预览，不足以作为正式投屏通道。"""
        resp = self._request("GET", "/screenshot")
        value = resp.get("value")
        if not value:
            raise WdaError("WDA 未返回截图数据")
        return base64.b64decode(value)
