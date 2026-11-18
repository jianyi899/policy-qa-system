"""服务端会话上下文。user_id 只允许由此注入，不得出现在工具签名里。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContext:
    user_id: int
    external_id: str | None = None
    display_name: str | None = None


class MissingSessionContext(RuntimeError):
    error_type = "missing_session_context"

    def __init__(self) -> None:
        super().__init__(self.error_type)
