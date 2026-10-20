from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    """Replace NaN/inf with None recursively; convert tuples to lists."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def _default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def canonical_json(value: Any) -> str:
    """Stable JSON text: sorted keys, no whitespace, NaN/inf -> null."""
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"), default=_default, allow_nan=False)


def stable_id(*parts: Any, length: int = 32) -> str:
    """Content-addressed id over the canonical JSON of the parts (default 128-bit hex prefix)."""
    return sha256_text(canonical_json(list(parts)))[:length]


def scope_key(scope: dict[str, Any] | None) -> str:
    """Canonical scope string: '{}' for brand total, else sorted JSON of {dimension: value}."""
    clean = {str(k): v for k, v in (scope or {}).items() if v not in (None, "", [])}
    return canonical_json(clean)


def new_uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Timezone-naive UTC timestamp (Delta TIMESTAMP and DuckDB TIMESTAMP both store naive UTC here)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
