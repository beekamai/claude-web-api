"""Provider-neutral contracts for completion backends.

The current claude.ai browser session can be wrapped behind this interface
without exposing Playwright, SSE, or Claude-specific parser state to the API
layer.  Other providers may implement the same contract with a different tool
continuation mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


class ToolContinuation(str, Enum):
    """How a provider accepts results for a tool call."""

    SIDE_CHANNEL = "side_channel"
    NEXT_REQUEST = "next_request"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ProviderCapabilities:
    tool_continuation: ToolContinuation
    streaming: bool = True
    thinking: bool = False
    profiles: bool = False


@dataclass(frozen=True)
class ProviderToolUse:
    id: str
    name: str
    input: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderToolResult:
    tool_use_id: str
    content: str
    name: str = ""
    is_error: bool = False


@dataclass(frozen=True)
class ProviderTurn:
    content: str | None
    tool_uses: tuple[ProviderToolUse, ...] = ()
    thinking: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    stop_reason: str | None = None


class ProviderEventKind(str, Enum):
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    MODEL = "model"
    USAGE = "usage"
    RETRACT = "retract"


@dataclass(frozen=True)
class ProviderEvent:
    kind: ProviderEventKind
    text: str | None = None
    model: str | None = None
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


ProviderEventSink = Callable[[ProviderEvent], None]


@dataclass(frozen=True)
class ProviderHealth:
    live: bool
    ready: bool
    phase: str
    detail: str | None = None


@dataclass(frozen=True)
class ProviderProfileIdentity:
    provider: str
    profile_id: str
    display_name: str
    account_id: str | None = None
    account_name: str | None = None
    account_email_masked: str | None = None
    organization_id: str | None = None


@dataclass(frozen=True)
class ProviderTurnRequest:
    message: str
    tools: tuple[Mapping[str, Any], ...] = ()
    timeout_seconds: float = 300.0
    new_conversation: bool = False
    parallel_tool_calls: bool = True
    model: str | None = None
    reasoning_mode: str = "auto"
    reasoning_effort: str | None = None
    privacy_mode: str = "keep"
    client_session_id: str | None = None


@runtime_checkable
class CompletionProvider(Protocol):
    """Structural interface implemented by a provider adapter."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        ...

    @property
    def profile_identity(self) -> ProviderProfileIdentity | None:
        ...

    def health(self) -> ProviderHealth:
        ...

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def new_conversation(self) -> None:
        ...

    async def complete(
        self,
        request: ProviderTurnRequest,
        *,
        event_sink: ProviderEventSink | None = None,
    ) -> ProviderTurn:
        ...

    async def continue_with_tool_results(
        self,
        results: Sequence[ProviderToolResult],
        *,
        timeout_seconds: float = 300.0,
        client_session_id: str | None = None,
        event_sink: ProviderEventSink | None = None,
    ) -> ProviderTurn:
        ...


__all__ = [
    "CompletionProvider",
    "ProviderCapabilities",
    "ProviderEvent",
    "ProviderEventKind",
    "ProviderEventSink",
    "ProviderHealth",
    "ProviderProfileIdentity",
    "ProviderToolResult",
    "ProviderToolUse",
    "ProviderTurn",
    "ProviderTurnRequest",
    "ToolContinuation",
]
