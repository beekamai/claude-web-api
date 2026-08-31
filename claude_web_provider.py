"""Provider adapter for the existing authenticated claude.ai Camoufox session.

This module is deliberately a thin compatibility seam.  Browser ownership,
account verification, native tool injection, and tool-result delivery remain
inside :class:`claude_session.ClaudeSession`; the adapter only translates the
provider-neutral request, event, and result contracts.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from provider_contracts import (
    ProviderCapabilities,
    ProviderEvent,
    ProviderEventKind,
    ProviderEventSink,
    ProviderHealth,
    ProviderProfileIdentity,
    ProviderToolResult,
    ProviderToolUse,
    ProviderTurn,
    ProviderTurnRequest,
    ToolContinuation,
)


CLAUDE_WEB_PROVIDER_ID = "claude_web"


def _mapping_copy(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _native_turn_to_provider_turn(native: Any) -> ProviderTurn:
    """Convert a ``claude_session.NativeTurn`` without importing Camoufox."""

    tool_uses: list[ProviderToolUse] = []
    for native_tool in getattr(native, "tool_uses", ()) or ():
        tool_uses.append(
            ProviderToolUse(
                id=str(getattr(native_tool, "id", "") or ""),
                name=str(getattr(native_tool, "name", "") or ""),
                input=_mapping_copy(getattr(native_tool, "input", {})),
            )
        )
    content = getattr(native, "content", None)
    thinking = getattr(native, "thinking", None)
    model = getattr(native, "model", None)
    stop_reason = getattr(native, "stop_reason", None)
    return ProviderTurn(
        content=str(content) if content is not None else None,
        tool_uses=tuple(tool_uses),
        thinking=str(thinking) if thinking is not None else None,
        usage=_mapping_copy(getattr(native, "usage", {})),
        model=str(model) if model is not None else None,
        stop_reason=str(stop_reason) if stop_reason is not None else None,
    )


def _native_event_to_provider_event(
    native: Mapping[str, Any],
) -> ProviderEvent | None:
    """Translate the user-visible subset of Claude's native stream."""

    event_type = str(native.get("type") or "")
    metadata = {
        str(key): value
        for key, value in native.items()
        if key not in {"type", "text", "thinking", "model"}
    }
    if event_type == "text_delta":
        return ProviderEvent(
            kind=ProviderEventKind.TEXT_DELTA,
            text=str(native.get("text") or ""),
            metadata=metadata,
        )
    if event_type == "thinking_delta":
        return ProviderEvent(
            kind=ProviderEventKind.THINKING_DELTA,
            text=str(native.get("thinking") or ""),
            metadata=metadata,
        )
    if event_type == "model":
        model = str(native.get("model") or "")
        return ProviderEvent(
            kind=ProviderEventKind.MODEL,
            model=model or None,
            metadata=metadata,
        )
    if event_type == "retract":
        return ProviderEvent(
            kind=ProviderEventKind.RETRACT,
            reason="content_block_retract",
            metadata=metadata,
        )
    if event_type == "usage":
        return ProviderEvent(
            kind=ProviderEventKind.USAGE,
            metadata=metadata,
        )
    # Terminal bookkeeping stays authoritative on the final turn.
    return None


class ClaudeWebProviderAdapter:
    """Expose ``ClaudeSession`` through the provider-neutral contract."""

    def __init__(
        self,
        session: Any,
        *,
        internal_tool_names: Collection[str] = (),
    ) -> None:
        self.session = session
        self._internal_tool_names = frozenset(
            str(name) for name in internal_tool_names if str(name)
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            tool_continuation=ToolContinuation.SIDE_CHANNEL,
            streaming=True,
            thinking=True,
            profiles=True,
        )

    @property
    def profile_identity(self) -> ProviderProfileIdentity | None:
        try:
            profile = _mapping_copy(self.session.current_profile_spec())
            profile_id = str(
                self.session.current_profile_id()
                or profile.get("id")
                or ""
            )
        except Exception:
            return None
        if not profile_id:
            return None

        try:
            snapshot = _mapping_copy(self.session.health_snapshot())
        except Exception:
            snapshot = {}
        account = _mapping_copy(snapshot.get("account"))

        account_id = None
        get_account_id = getattr(
            self.session,
            "account_uuid_for_internal_use",
            None,
        )
        if callable(get_account_id):
            account_id = get_account_id()

        organization_id = None
        get_organization_id = getattr(
            self.session,
            "organization_uuid_for_internal_use",
            None,
        )
        if callable(get_organization_id):
            organization_id = get_organization_id()

        return ProviderProfileIdentity(
            provider=CLAUDE_WEB_PROVIDER_ID,
            profile_id=profile_id,
            display_name=str(profile.get("name") or profile_id),
            account_id=str(account_id) if account_id else None,
            account_name=(
                str(account.get("name")) if account.get("name") else None
            ),
            account_email_masked=(
                str(account.get("email")) if account.get("email") else None
            ),
            organization_id=(
                str(organization_id) if organization_id else None
            ),
        )

    def health(self) -> ProviderHealth:
        try:
            snapshot = _mapping_copy(self.session.health_snapshot())
        except Exception as exc:
            return ProviderHealth(
                live=False,
                ready=False,
                phase="health_error",
                detail=str(exc),
            )
        browser = _mapping_copy(snapshot.get("browser"))
        phase = str(browser.get("phase") or "unknown")
        live = phase not in {"stopped", "browser_dead"}
        detail = browser.get("last_error")
        return ProviderHealth(
            live=live,
            ready=bool(snapshot.get("ok")),
            phase=phase,
            detail=str(detail) if detail else None,
        )

    async def start(self) -> None:
        await self.session.start()

    async def stop(self) -> None:
        await self.session.stop()

    async def new_conversation(self) -> None:
        await self.session.new_chat()

    async def complete(
        self,
        request: ProviderTurnRequest,
        *,
        event_sink: ProviderEventSink | None = None,
    ) -> ProviderTurn:
        return await self.complete_native(request, event_sink=event_sink)

    async def complete_native(
        self,
        request: ProviderTurnRequest,
        *,
        internal_tool_names: Collection[str] | None = None,
        recovery_message: str | None = None,
        event_sink: ProviderEventSink | None = None,
    ) -> ProviderTurn:
        """Complete a turn while preserving Claude-only safety metadata.

        ``internal_tool_names`` and ``recovery_message`` are intentionally kept
        off the provider-neutral request.  The existing API layer can use this
        helper during migration without losing the trusted runtime tool carrier
        or safe browser-recovery context.
        """

        configured_internal_names = (
            self._internal_tool_names
            if internal_tool_names is None
            else frozenset(
                str(name) for name in internal_tool_names if str(name)
            )
        )
        native_sink = self._event_sink(event_sink)
        native = await self.session.native_chat(
            request.message,
            tools=[dict(tool) for tool in request.tools],
            internal_tool_names=set(configured_internal_names),
            timeout=request.timeout_seconds,
            new_chat=request.new_conversation,
            parallel_tool_calls=request.parallel_tool_calls,
            recovery_message=recovery_message,
            model=request.model,
            thinking_mode=request.reasoning_mode,
            effort=request.reasoning_effort,
            privacy_mode=request.privacy_mode,
            client_session_id=request.client_session_id,
            event_sink=native_sink,
        )
        return _native_turn_to_provider_turn(native)

    async def continue_with_tool_results(
        self,
        results: Sequence[ProviderToolResult],
        *,
        timeout_seconds: float = 300.0,
        client_session_id: str | None = None,
        event_sink: ProviderEventSink | None = None,
    ) -> ProviderTurn:
        native = await self.session.continue_native(
            [
                {
                    "tool_call_id": result.tool_use_id,
                    "name": result.name,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in results
            ],
            timeout=timeout_seconds,
            client_session_id=client_session_id,
            event_sink=self._event_sink(event_sink),
        )
        return _native_turn_to_provider_turn(native)

    @staticmethod
    def _event_sink(
        sink: ProviderEventSink | None,
    ):
        if sink is None:
            return None

        def emit(native: dict[str, Any]) -> None:
            normalized = _native_event_to_provider_event(native)
            if normalized is not None:
                sink(normalized)

        return emit


__all__ = [
    "CLAUDE_WEB_PROVIDER_ID",
    "ClaudeWebProviderAdapter",
]
