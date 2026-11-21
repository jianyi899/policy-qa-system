"""工具错误。对外只暴露 error_type，不带出会话以外的用户标识来源。"""

from __future__ import annotations


class ToolError(RuntimeError):
    def __init__(self, error_type: str, *, detail: str | None = None):
        self.error_type = error_type
        self.detail = detail
        super().__init__(error_type)

    def to_dict(self) -> dict:
        data = {"ok": False, "error_type": self.error_type}
        if self.detail:
            data["detail"] = self.detail
        return data
