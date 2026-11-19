"""Stable error types exposed by Soldeazy MCP tools.

字段形状：``code`` / ``message`` / ``status_code`` / ``retryable`` / ``details``。
``code`` 都是 ``SOLDEAZY_*``，调用方可以按前缀判断来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SoldeazyError(RuntimeError):
    code: str
    message: str
    status_code: int | None = None
    retryable: bool = False
    details: Any = None

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        error = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.status_code is not None:
            error["status_code"] = self.status_code
        if self.details not in (None, "", [], {}):
            error["details"] = self.details
        return error


def tool_error(operation: str, error: SoldeazyError) -> dict[str, Any]:
    return {
        "success": False,
        "operation": operation,
        "error": error.as_dict(),
    }
