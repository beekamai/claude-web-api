"""One Claude Web turn, independent of the wire protocol that asked for it.

Both the OpenAI and the Anthropic surfaces translate their request into this
layer, which owns tool wiring, retries, profile rotation on a usage limit, and
the request journal entries around a turn.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import BaseModel, Field

from claude_web_api import runtime
from claude_web_api.protocol.openai import (
    OPENCLAUDE_CONTEXT_TOOL_NAME,
    ParsedAssistant,
    ToolCall,
    actionable_input,
    attach_runtime_context,
    carries_tool_results,
    client_runtime_context,
    has_semantic_user_after_pending_tools,
    history_text,
    matching_tool_results,
    native_tools,
    trailing_tool_results,
    user_selected_persona_message,
)
from claude_web_api.protocol.openai_usage import openai_usage
from claude_web_api.providers.contracts import (
    ProviderEvent,
    ProviderEventKind,
    ProviderToolResult,
    ProviderTurn,
    ProviderTurnRequest,
)
from claude_web_api.sanitize import sanitize_public_text
from claude_web_api.session.claude import (
    ClaudeAccountIdentityError,
    ClaudeBrowserUnavailableError,
    ClaudeCompletionRejectedError,
    ClaudeConversationLimitError,
    ClaudeServiceUnavailableError,
    ClaudeTurnOutcomeUnknownError,
    ClaudeUsageLimitError,
    NativeToolUse,
    NativeTurn,
)


class StreamOptions(BaseModel):
    include_usage: bool = False

class CompletionsIn(BaseModel):
    messages: list[dict[str, Any]]
    new_chat: bool = False
    model: str = "claude-web"
    stream: bool = False
    stream_options: StreamOptions | None = None
    timeout: float = Field(default=300.0, ge=5.0, le=600.0)
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool = True
    reasoning_effort: str | None = None

def _client_starts_fresh_chat(body: CompletionsIn) -> bool:
    non_system = [
        message
        for message in body.messages
        if message.get("role") not in ("system", "developer")
    ]
    return body.new_chat or bool(
        len(non_system) == 1
        and non_system[0].get("role") == "user"
    )

def _request_starts_fresh_chat(
    body: CompletionsIn,
    client_session_id: str | None,
) -> bool:
    if client_session_id:
        return bool(
            body.new_chat
            or runtime.session.client_session_requires_new(client_session_id)
        )
    return _client_starts_fresh_chat(body)

def parsed_native(turn: NativeTurn) -> ParsedAssistant:
    return ParsedAssistant(
        content=turn.content,
        tool_calls=[
            ToolCall(
                id=tool.id,
                name=tool.name,
                arguments=tool.input,
            )
            for tool in turn.tool_uses
        ],
    )

def _provider_turn_as_native(turn: ProviderTurn) -> NativeTurn:
    return NativeTurn(
        content=turn.content,
        tool_uses=[
            NativeToolUse(
                id=tool.id,
                name=tool.name,
                input=dict(tool.input),
            )
            for tool in turn.tool_uses
        ],
        thinking=turn.thinking,
        usage=dict(turn.usage),
        model=turn.model,
        stop_reason=turn.stop_reason,
    )

def _provider_event_sink(
    sink: Callable[[dict[str, Any]], None] | None,
):
    if sink is None:
        return None

    def emit(event: ProviderEvent) -> None:
        if event.kind is ProviderEventKind.TEXT_DELTA:
            payload = {
                "type": "text_delta",
                "text": event.text or "",
                **dict(event.metadata),
            }
        elif event.kind is ProviderEventKind.THINKING_DELTA:
            payload = {
                "type": "thinking_delta",
                "thinking": event.text or "",
                **dict(event.metadata),
            }
        elif event.kind is ProviderEventKind.MODEL:
            payload = {
                "type": "model",
                "model": event.model,
                **dict(event.metadata),
            }
        elif event.kind is ProviderEventKind.USAGE:
            payload = {
                "type": "usage",
                **dict(event.metadata),
            }
        elif event.kind is ProviderEventKind.RETRACT:
            payload = {
                "type": "retract",
                **dict(event.metadata),
            }
        else:
            return
        sink(payload)

    return emit

def _rollover_message(body: CompletionsIn, reason: str) -> str:
    """Build an honest user-visible context rebuild for a new web chat."""
    history = history_text(body.messages[:-1])
    if len(history) > 60_000:
        history = history[-60_000:]
    return (
        "OpenClaude restored this IDE task in a new claude.ai conversation.\n"
        f"Reason: {reason}\n\n"
        "EARLIER_IDE_CONVERSATION\n"
        + (history or "(no earlier non-system messages)")
        + "\n\nCURRENT_USER_REQUEST\n"
        + actionable_input(body.messages)
    )

def _native_tools_with_runtime(
    body: CompletionsIn,
    profile_id: str | None = None,
    *,
    client_working_directory: str | None = None,
) -> list[dict[str, Any]]:
    resolved_model = runtime.resolve_request_model(body.model, profile_id)
    mapped_tools = native_tools(body.tools, body.tool_choice)
    runtime_context = client_runtime_context(
        body.messages,
        mapped_tools,
        working_directory=client_working_directory,
    )
    selected_runtime_model = (
        resolved_model
        or runtime.session.selected_model_for_runtime()
    )
    runtime_context += (
        f"\nrequested_model_alias: {body.model}"
        + (
            f"\nselected_browser_model: {selected_runtime_model}"
            if selected_runtime_model
            else ""
        )
    )
    return attach_runtime_context(
        mapped_tools,
        runtime_context,
    )

def _internal_tool_names(
    tools: list[dict[str, Any]],
) -> set[str]:
    return {
        OPENCLAUDE_CONTEXT_TOOL_NAME
        for tool in tools
        if tool.get("name") == OPENCLAUDE_CONTEXT_TOOL_NAME
    }

async def native_request(
    body: CompletionsIn,
    *,
    client_session_id: str | None = None,
    client_working_directory: str | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    behavior_snapshot: dict[str, Any] | None = None,
    persona_instruction: str | None = None,
) -> NativeTurn:
    if not runtime.persist_runtime_identity():
        raise HTTPException(
            409,
            "the active Camoufox profile is logged into a different or "
            "duplicate account; add that account as a separate profile",
        )
    pending_ids, recovery_required = await runtime.session.native_request_state(
        client_session_id
    )
    if pending_ids:
        # A client that asks for a fresh chat, or sends no tool result at all,
        # is not going to answer the pending call — it interrupted the turn, or
        # started over. Holding the browser hostage until the lease expires
        # would fail every request in between, so the turn is abandoned here.
        client_moved_on = (
            _request_starts_fresh_chat(body, client_session_id)
            or not carries_tool_results(body.messages)
        )
        if client_moved_on or has_semantic_user_after_pending_tools(
            body.messages, pending_ids
        ):
            recovery_required = await runtime.session.abandon_pending_native(
                pending_ids,
                client_session_id=client_session_id,
            )
        else:
            results = matching_tool_results(body.messages, pending_ids)
            continued = await runtime.claude_provider.continue_with_tool_results(
                tuple(
                    ProviderToolResult(
                        tool_use_id=result.tool_call_id,
                        name=result.name,
                        content=result.content,
                        is_error=result.is_error,
                    )
                    for result in results
                ),
                timeout_seconds=body.timeout,
                client_session_id=client_session_id,
                event_sink=_provider_event_sink(event_sink),
            )
            return _provider_turn_as_native(continued)
    if trailing_tool_results(body.messages):
        raise HTTPException(
            409,
            "tool results belong to a native Claude stream that is no longer "
            "pending; start or retry the IDE turn instead of replaying the "
            "completed tool result",
        )

    if behavior_snapshot is None:
        behavior, resolved_persona = runtime.control.behavior_snapshot()
    else:
        behavior = dict(behavior_snapshot)
        resolved_persona = runtime.control.persona_prompt_for(behavior)
    if persona_instruction is None:
        persona_instruction = resolved_persona

    user_input = actionable_input(body.messages)
    outbound_message = user_input
    if recovery_required:
        fresh_chat = False
        outbound_message = _rollover_message(
            body,
            "The previous native host-tool result lease expired and the "
            "claude.ai chat was reset. Recover the IDE task from the history "
            "below and continue from the newest user instruction.",
        )
    else:
        fresh_chat = _request_starts_fresh_chat(
            body,
            client_session_id,
        ) or runtime.session.privacy_mode_requires_new(
            str(behavior["privacy"])
        )
        if fresh_chat:
            history = history_text(body.messages[:-1])
            if history:
                outbound_message = _rollover_message(
                    body,
                    "OpenClaude opened a fresh web conversation while "
                    "preserving the IDE transcript supplied by the client.",
                )

    if runtime.DEBUG_REQUESTS:
        print(
            "OPENAI_NATIVE_TURN "
            + json.dumps(
                {
                    "roles": [
                        message.get("role") for message in body.messages
                    ],
                    "fresh_chat": fresh_chat,
                    "prompt_length": len(user_input),
                    "submitted_prompt_length": len(outbound_message),
                    "tools": [
                        tool.get("function", {}).get("name")
                        for tool in (body.tools or [])
                        if tool.get("type") == "function"
                    ],
                    "client_session_suffix": (
                        client_session_id[-8:] if client_session_id else None
                    ),
                },
                ensure_ascii=True,
            ),
            flush=True,
        )

    resolved_request_model = runtime.resolve_request_model(body.model)
    request_tools = _native_tools_with_runtime(
        body,
        client_working_directory=client_working_directory,
    )
    recovery_message = _rollover_message(
        body,
        "Camoufox was restarted before the IDE turn could be submitted. "
        "Rebuild the same task context from the history below, then "
        "continue from the newest user instruction.",
    )
    outbound_message = user_selected_persona_message(
        outbound_message,
        persona_instruction,
    )
    recovery_message = user_selected_persona_message(
        recovery_message,
        persona_instruction,
    )
    provider_turn = await runtime.claude_provider.complete_native(
        ProviderTurnRequest(
            message=outbound_message,
            tools=tuple(request_tools),
            timeout_seconds=body.timeout,
            new_conversation=fresh_chat,
            parallel_tool_calls=body.parallel_tool_calls,
            model=resolved_request_model,
            reasoning_mode=str(behavior["thinking"]),
            reasoning_effort=body.reasoning_effort,
            privacy_mode=str(behavior["privacy"]),
            client_session_id=client_session_id,
        ),
        internal_tool_names=_internal_tool_names(request_tools),
        recovery_message=recovery_message,
        event_sink=_provider_event_sink(event_sink),
    )
    native = _provider_turn_as_native(provider_turn)
    if recovery_required:
        await runtime.session.mark_history_recovered()
    requires_tool = body.tool_choice == "required" or isinstance(
        body.tool_choice,
        dict,
    )
    if requires_tool and not native.tool_uses:
        raise RuntimeError(
            "Claude completed without the host tool required by tool_choice"
        )
    return native

def _native_retry_kwargs(
    body: CompletionsIn,
    client_session_id: str | None,
    event_sink: Callable[[dict[str, Any]], None] | None,
    profile_id: str | None = None,
    client_working_directory: str | None = None,
    behavior_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if behavior_snapshot is None:
        behavior, _ = runtime.control.behavior_snapshot()
    else:
        behavior = dict(behavior_snapshot)
    tools = _native_tools_with_runtime(
        body,
        profile_id,
        client_working_directory=client_working_directory,
    )
    return {
        "tools": tools,
        "internal_tool_names": _internal_tool_names(tools),
        "timeout": body.timeout,
        "new_chat": True,
        "parallel_tool_calls": body.parallel_tool_calls,
        "model": runtime.resolve_request_model(body.model, profile_id),
        "thinking_mode": str(behavior["thinking"]),
        "effort": body.reasoning_effort,
        "privacy_mode": str(behavior["privacy"]),
        "client_session_id": client_session_id,
        "event_sink": event_sink,
    }

async def run_native_with_limits(
    body: CompletionsIn,
    *,
    client_session_id: str | None,
    client_working_directory: str | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None,
) -> NativeTurn:
    behavior, persona_instruction = runtime.control.behavior_snapshot()
    try:
        return await native_request(
            body,
            client_session_id=client_session_id,
            client_working_directory=client_working_directory,
            event_sink=event_sink,
            behavior_snapshot=behavior,
            persona_instruction=persona_instruction,
        )
    except ClaudeConversationLimitError as exc:
        if not exc.replay_safe or bool(
            getattr(event_sink, "visible_seen", False)
        ):
            raise HTTPException(
                409,
                "Claude reported a conversation limit after output or a tool "
                "path had started; the turn was not replayed.",
            ) from exc
        try:
            retry_message = _rollover_message(
                body,
                "The previous claude.ai conversation reached its length "
                "limit. Continue the same IDE task in this new chat.",
            )
            retry_message = user_selected_persona_message(
                retry_message,
                persona_instruction,
            )
            return await runtime.session.native_chat(
                retry_message,
                recovery_message=retry_message,
                **_native_retry_kwargs(
                    body,
                    client_session_id,
                    event_sink,
                    runtime.session.current_profile_id(),
                    client_working_directory,
                    behavior,
                ),
            )
        except ClaudeUsageLimitError as limit_error:
            return await _rotate_after_usage_limit(
                body,
                client_session_id=client_session_id,
                client_working_directory=client_working_directory,
                event_sink=event_sink,
                limit_error=limit_error,
                behavior_snapshot=behavior,
                persona_instruction=persona_instruction,
            )
    except ClaudeUsageLimitError as exc:
        return await _rotate_after_usage_limit(
            body,
            client_session_id=client_session_id,
            client_working_directory=client_working_directory,
            event_sink=event_sink,
            limit_error=exc,
            behavior_snapshot=behavior,
            persona_instruction=persona_instruction,
        )

async def _rotate_after_usage_limit(
    body: CompletionsIn,
    *,
    client_session_id: str | None,
    client_working_directory: str | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None,
    limit_error: ClaudeUsageLimitError,
    behavior_snapshot: dict[str, Any],
    persona_instruction: str,
) -> NativeTurn:
    if not limit_error.replay_safe or bool(
        getattr(event_sink, "visible_seen", False)
    ):
        raise HTTPException(
            409,
            "Claude reported an account limit after output or a tool path "
            "had already started; profile rotation did not replay it.",
        ) from limit_error
    current_id = runtime.session.current_profile_id()
    rotation_succeeded = False
    try:
        try:
            runtime.control.update_profile(
                current_id,
                {
                    "status": "limited",
                    "limited_until": time.time() + 3600,
                },
            )
        except KeyError:
            pass
        eligible = runtime.eligible_rotation_ids()
        eligible.discard(current_id)
        attempts = len(eligible)
        for _ in range(attempts):
            try:
                if not await runtime.session.rotate_profile(eligible):
                    break
            except ClaudeBrowserUnavailableError as rotate_exc:
                failed_id = runtime.session.current_profile_id()
                eligible.discard(failed_id)
                try:
                    runtime.control.update_profile(
                        failed_id,
                        {
                            "status": "error",
                            "last_checked_at": time.time(),
                        },
                    )
                except KeyError:
                    pass
                runtime.telemetry.log(
                    "WARN",
                    "Profiles",
                    f"Профиль {failed_id} пропущен: {rotate_exc}",
                )
                continue
            candidate_id = runtime.session.current_profile_id()
            if not runtime.persist_runtime_identity():
                eligible.discard(candidate_id)
                runtime.telemetry.log(
                    "WARN",
                    "Profiles",
                    f"Профиль {candidate_id} использует другой или "
                    "дублирующий аккаунт",
                )
                continue
            try:
                runtime.control.set_active_profile(candidate_id)
            except Exception as commit_exc:
                try:
                    await runtime.session.sync_profiles(
                        runtime.runtime_profiles(),
                        current_id,
                        restart=True,
                    )
                except Exception:
                    pass
                raise HTTPException(
                    503,
                    f"failed to persist rotated profile: {commit_exc}",
                ) from commit_exc
            try:
                retry_message = _rollover_message(
                    body,
                    "OpenClaude rotated to another authenticated browser "
                    "profile. Continue the same IDE task from the supplied "
                    "history.",
                )
                retry_message = user_selected_persona_message(
                    retry_message,
                    persona_instruction,
                )
                native = await runtime.session.native_chat(
                    retry_message,
                    recovery_message=retry_message,
                    **_native_retry_kwargs(
                        body,
                        client_session_id,
                        event_sink,
                        candidate_id,
                        client_working_directory,
                        behavior_snapshot,
                    ),
                )
                runtime.persist_runtime_identity()
                runtime.telemetry.log(
                    "WARN",
                    "Profiles",
                    "Профиль автоматически сменён после лимита",
                )
                rotation_succeeded = True
                return native
            except ClaudeUsageLimitError as alternate_limit:
                if (
                    not alternate_limit.replay_safe
                    or bool(getattr(event_sink, "visible_seen", False))
                ):
                    raise HTTPException(
                        409,
                        "Claude reported an account limit after output or a "
                        "tool path had started on a rotated profile; the turn "
                        "was not replayed again.",
                    ) from alternate_limit
                try:
                    runtime.control.update_profile(
                        candidate_id,
                        {
                            "status": "limited",
                            "limited_until": time.time() + 3600,
                        },
                    )
                except KeyError:
                    pass
                eligible.discard(candidate_id)
                continue
            except ValueError as candidate_exc:
                eligible.discard(candidate_id)
                runtime.telemetry.log(
                    "WARN",
                    "Models",
                    f"Профиль {candidate_id} не подходит: {candidate_exc}",
                )
                continue
            except ClaudeAccountIdentityError as candidate_exc:
                eligible.discard(candidate_id)
                runtime.persist_runtime_identity()
                runtime.telemetry.log(
                    "WARN",
                    "Profiles",
                    f"Профиль {candidate_id} сменил аккаунт: {candidate_exc}",
                )
                continue
            except ClaudeBrowserUnavailableError as candidate_exc:
                health = runtime.session.health_snapshot()
                browser = health.get("browser", {})
                status = (
                    "auth_required"
                    if isinstance(browser, dict)
                    and browser.get("phase") == "auth_required"
                    else "error"
                )
                try:
                    runtime.control.update_profile(
                        candidate_id,
                        {
                            "status": status,
                            "last_checked_at": time.time(),
                        },
                    )
                except KeyError:
                    pass
                eligible.discard(candidate_id)
                runtime.telemetry.log(
                    "WARN",
                    "Profiles",
                    f"Профиль {candidate_id} пропущен: {candidate_exc}",
                )
                continue
        raise HTTPException(
            429,
            "Claude account usage limit reached on every eligible "
            "authenticated profile.",
        ) from limit_error
    finally:
        if not rotation_succeeded:
            try:
                await runtime.session.sync_profiles(
                    runtime.runtime_profiles(),
                    current_id,
                    restart=True,
                )
                runtime.control.set_active_profile(current_id)
            except Exception as restore_exc:
                runtime.telemetry.log(
                    "ERROR",
                    "Profiles",
                    f"Не удалось вернуть профиль {current_id}: {restore_exc}",
                )

def telemetry_user_text(body: CompletionsIn) -> str | None:
    if trailing_tool_results(body.messages):
        return None
    try:
        return actionable_input(body.messages)
    except ValueError:
        return None

def begin_request_telemetry(
    request_id: str,
    model: str,
    client_session_id: str | None,
    user_text: str | None,
    *,
    streaming: bool,
) -> None:
    privacy_mode = str(runtime.control.behavior().get("privacy") or "keep")
    profile_id = runtime.session.current_profile_id()
    runtime.telemetry.begin(
        request_id,
        model,
        profile_id,
        provider_id=runtime.profile_provider_id(profile_id),
        client_session_id=client_session_id,
        session_key=runtime.control.telemetry_session_key(
            client_session_id,
            request_id,
        ),
        user_text=user_text,
        streaming=streaming,
        privacy_mode=privacy_mode,
        capture_content=runtime.telemetry_content_enabled(),
    )

def finish_request_telemetry(
    request_id: str,
    *,
    status: str,
    native: NativeTurn | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
    assistant_text: str | None = None,
    resolved_model: str | None = None,
) -> None:
    settings = runtime.control.telemetry_settings()
    parsed = parsed_native(native) if native is not None else None
    if native is not None:
        if usage is None:
            usage = openai_usage(native.usage)
        if assistant_text is None:
            assistant_text = native.content
        resolved_model = native.model or resolved_model
    runtime.telemetry.finish(
        request_id,
        status=status,
        usage=usage,
        error=sanitize_public_text(error) if error else None,
        assistant_text=assistant_text,
        thinking_text=native.thinking if native is not None else None,
        tool_call_count=len(parsed.tool_calls) if parsed is not None else 0,
        resolved_model=resolved_model,
        final_profile_id=runtime.session.current_profile_id(),
        final_provider_id=runtime.profile_provider_id(
            runtime.session.current_profile_id()
        ),
        capture_content=runtime.telemetry_content_enabled(),
        retention_days=int(settings.get("retention_days") or 30),
        max_requests=int(settings.get("max_requests") or 5_000),
    )
    if status == "error" and error:
        runtime.telemetry.log(
            "ERROR",
            "API",
            sanitize_public_text(error),
            request_id=request_id,
        )

class StreamRelay:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.visible_seen = False

    def __call__(self, event: dict[str, Any]) -> None:
        if event.get("type") in {"text_delta", "thinking_delta"}:
            self.visible_seen = True
        runtime.telemetry.native_event(self.request_id, event)
        self.queue.put_nowait(dict(event))

def exception_status(exc: Exception) -> int:
    if isinstance(exc, HTTPException):
        return int(exc.status_code)
    if isinstance(exc, ValueError):
        return 400
    if isinstance(exc, ClaudeCompletionRejectedError):
        return exc.status
    if isinstance(exc, ClaudeUsageLimitError):
        return 429
    if isinstance(exc, ClaudeAccountIdentityError):
        return 409
    if isinstance(exc, ClaudeBrowserUnavailableError):
        return 503
    if isinstance(exc, ClaudeServiceUnavailableError):
        return 503
    if isinstance(
        exc,
        (
            ClaudeConversationLimitError,
            ClaudeTurnOutcomeUnknownError,
        ),
    ):
        return 409
    return 500

def validated_client_header(
    value: str | None,
    *,
    name: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if (
        len(normalized) > max_length
        or any(ord(char) < 32 for char in normalized)
    ):
        raise HTTPException(400, f"invalid {name} header")
    return normalized
