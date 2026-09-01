"""What one native turn returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NativeToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class NativeTurn:
    content: str | None
    tool_uses: list[NativeToolUse]
    thinking: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    stop_reason: str | None = None
