"""共享返回契约。

统一形状：``{success, operation, source, data, meta.retrieved_at}``。
Soldeazy 侧用站点代码（US/UK/DE…），不涉及 eBay marketplace 概念。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def success(
    operation: str,
    source: str,
    data: Any,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        **(meta or {}),
    }
    return {
        "success": True,
        "operation": operation,
        "source": source,
        "data": data,
        "meta": metadata,
    }
