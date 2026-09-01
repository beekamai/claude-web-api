"""Local OpenAI-compatible API backed by an authenticated claude.ai session.

OpenAI functions are mapped to claude.ai native tools. Claude selects actions,
OpenClaude executes them, and their results return through Claude's real
``/tool_result`` side-channel while the original SSE remains open.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from claude_web_api import runtime
from claude_web_api.control.config import ControlConfig
from claude_web_api.paths import (
    PROJECT_INSTRUCTIONS,
    PROJECT_ROOT,
    WEB_ROOT,
)
from claude_web_api.protocol.openai import (
    OPENCLAUDE_CONTEXT_TOOL_NAME,
    ParsedAssistant,
    ToolCall,
    actionable_input,
    attach_runtime_context,
    chat_message,
    client_runtime_context,
    has_semantic_user_after_pending_tools,
    history_text,
    matching_tool_results,
    native_tools,
    trailing_tool_results,
    user_selected_persona_message,
)
from claude_web_api.protocol.openai_usage import openai_usage
from claude_web_api.providers.claude_web import (
    CLAUDE_WEB_PROVIDER_ID,
)
from claude_web_api.providers.contracts import (
    ProviderEvent,
    ProviderEventKind,
    ProviderToolResult,
    ProviderTurn,
    ProviderTurnRequest,
)
from claude_web_api.sanitize import public_error_message, sanitize_public_text
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
from claude_web_api.telemetry.store import TelemetryStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    await runtime.session.start()
    await runtime.session.start_watchdog()
    telemetry_settings = runtime.control.telemetry_settings()
    try:
        # Recovery is only safe after session.start() has acquired the
        # profile/runtime lease. A second worker must not interrupt the
        # first worker's in-flight rows.
        await runtime.telemetry.store_call_async("recover_interrupted")
        if not bool(telemetry_settings.get("store_content")):
            await runtime.telemetry.store_call_async("scrub_content")
        await runtime.telemetry.store_call_async(
            "prune",
            retention_days=int(
                telemetry_settings.get("retention_days") or 30
            ),
            max_requests=int(
                telemetry_settings.get("max_requests") or 5_000
            ),
        )
    except Exception:
        # Telemetry is auxiliary; its health is exposed in the control
        # panel, while the API remains available.
        pass
    telemetry_task = asyncio.create_task(
        runtime.telemetry_maintenance(),
        name="telemetry-maintenance",
    )
    runtime.persist_runtime_identity()
    runtime.telemetry.log("INFO", "API", "Сервер и Camoufox запущены")
    try:
        yield
    finally:
        telemetry_task.cancel()
        await asyncio.gather(telemetry_task, return_exceptions=True)
        await runtime.enrollment.stop()
        await runtime.session.stop()
        await runtime.telemetry.close_store_executor()


app = FastAPI(title="Claude Web API", version="3.1.0", lifespan=lifespan)
if WEB_ROOT.exists():
    app.mount(
        "/control/assets",
        StaticFiles(directory=str(WEB_ROOT)),
        name="control-assets",
    )


class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    new_chat: bool = False
    timeout: float = Field(default=300.0, ge=5.0, le=600.0)


class ChatOut(BaseModel):
    response: str


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


class BehaviorPatch(BaseModel):
    streaming: bool | None = None
    thinking: str | None = None
    privacy: str | None = None
    persona: str | None = None
    custom_persona: str | None = None
    actor: bool | None = None
    mature: bool | None = None


class TelemetryPatch(BaseModel):
    store_content: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=365)


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(default=CLAUDE_WEB_PROVIDER_ID, max_length=32)


class ModelSelect(BaseModel):
    model: str = Field(min_length=1, max_length=160)


@app.get("/")
async def root():
    return RedirectResponse("/control/")


@app.get("/control/")
async def control_index():
    index = WEB_ROOT / "index.html"
    if not index.exists():
        raise HTTPException(404, "control panel has not been installed")
    return FileResponse(index)


@app.get("/health")
async def health():
    return runtime.session.health_snapshot()


@app.get("/health/live")
async def health_live():
    """Event-loop liveness only; never waits for Playwright or its global lock."""
    if not runtime.session.watchdog_healthy():
        raise HTTPException(503, "Camoufox watchdog is unhealthy")
    return {"ok": True, "watchdog": True, "time": time.time()}


@app.get("/health/ready")
async def health_ready():
    """Non-blocking Camoufox readiness snapshot."""
    snapshot = runtime.session.health_snapshot()
    if not snapshot["ok"]:
        return JSONResponse(status_code=503, content=snapshot)
    return snapshot


@app.post("/new")
async def new_chat():
    try:
        await runtime.session.new_chat()
        return {"ok": True}
    except ClaudeBrowserUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn):
    request_id = f"legacy-{uuid.uuid4().hex[:12]}"
    _begin_request_telemetry(
        request_id,
        "claude-web",
        None,
        body.message,
        streaming=False,
    )
    try:
        text = await runtime.session.chat(
            body.message,
            timeout=body.timeout,
            new_chat=body.new_chat,
        )
        _finish_request_telemetry(
            request_id,
            status="completed",
            assistant_text=text,
            resolved_model="claude-web",
        )
        return ChatOut(response=text)
    except ClaudeTurnOutcomeUnknownError as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(409, str(exc)) from exc
    except ClaudeBrowserUnavailableError as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(500, str(exc)) from exc


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


def _parsed_native(turn: NativeTurn) -> ParsedAssistant:
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


async def _native_request(
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
        if has_semantic_user_after_pending_tools(body.messages, pending_ids):
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


async def _run_native_with_limits(
    body: CompletionsIn,
    *,
    client_session_id: str | None,
    client_working_directory: str | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None,
) -> NativeTurn:
    behavior, persona_instruction = runtime.control.behavior_snapshot()
    try:
        return await _native_request(
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






def _telemetry_content_enabled() -> bool:
    settings = runtime.control.telemetry_settings()
    privacy_mode = str(runtime.control.behavior().get("privacy") or "keep")
    return bool(settings.get("store_content")) and privacy_mode != "ephemeral"


def _telemetry_user_text(body: CompletionsIn) -> str | None:
    if trailing_tool_results(body.messages):
        return None
    try:
        return actionable_input(body.messages)
    except ValueError:
        return None


def _begin_request_telemetry(
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
        capture_content=_telemetry_content_enabled(),
    )


def _finish_request_telemetry(
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
    parsed = _parsed_native(native) if native is not None else None
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
        capture_content=_telemetry_content_enabled(),
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


def _assistant_message(turn: NativeTurn) -> dict[str, Any]:
    message = chat_message(_parsed_native(turn))
    if turn.thinking and runtime.control.behavior()["thinking"] != "off":
        message["reasoning_content"] = turn.thinking
    return message


def _finish_reason(turn: NativeTurn, parsed: ParsedAssistant) -> str:
    if parsed.tool_calls:
        return "tool_calls"
    if turn.stop_reason in {
        "max_tokens",
        "max_output_tokens",
        "model_context_window_exceeded",
    }:
        return "length"
    if turn.stop_reason in {"content_filter", "refusal"}:
        return "content_filter"
    return "stop"


def _completion_response(
    body: CompletionsIn,
    native: NativeTurn,
    completion_id: str,
    created: int,
) -> dict[str, Any]:
    parsed = _parsed_native(native)
    payload: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": (
            native.model
            or runtime.resolve_request_model(body.model)
            or body.model
        ),
        "choices": [
            {
                "index": 0,
                "message": _assistant_message(native),
                "finish_reason": _finish_reason(native, parsed),
            }
        ],
    }
    usage = openai_usage(native.usage)
    if usage is not None:
        payload["usage"] = usage
    return payload


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


def _chat_chunk(
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _usage_chunk(
    completion_id: str,
    created: int,
    model: str,
    usage: dict[str, Any],
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": usage,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _exception_status(exc: Exception) -> int:
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


def _stream_error(exc: Exception) -> str:
    status = _exception_status(exc)
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    payload = {
        "error": {
            "message": sanitize_public_text(detail),
            "type": "claude_web_error",
            "code": status,
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _chat_event_stream(
    body: CompletionsIn,
    completion_id: str,
    created: int,
    model: str,
    client_session_id: str | None,
    client_working_directory: str | None = None,
):
    request_id = completion_id.removeprefix("chatcmpl-")
    relay = StreamRelay(request_id)
    _begin_request_telemetry(
        request_id,
        model,
        client_session_id,
        _telemetry_user_text(body),
        streaming=True,
    )
    task = asyncio.create_task(
        _run_native_with_limits(
            body,
            client_session_id=client_session_id,
            client_working_directory=client_working_directory,
            event_sink=relay,
        ),
        name=f"openai-stream-{request_id}",
    )
    emitted_text = ""
    emitted_thinking = ""
    visible_emitted = False
    resolved_model = model
    try:
        yield _chat_chunk(
            completion_id,
            created,
            resolved_model,
            {"role": "assistant"},
        )
        while not task.done() or not relay.queue.empty():
            try:
                event = await asyncio.wait_for(relay.queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            event_type = event.get("type")
            if event_type == "model" and event.get("model"):
                resolved_model = str(event["model"])
            elif event_type == "text_delta":
                text = str(event.get("text") or "")
                if text:
                    emitted_text += text
                    visible_emitted = True
                    yield _chat_chunk(
                        completion_id,
                        created,
                        resolved_model,
                        {"content": text},
                    )
            elif (
                event_type == "thinking_delta"
                and runtime.control.behavior()["thinking"] != "off"
            ):
                thinking = str(event.get("thinking") or "")
                if thinking:
                    emitted_thinking += thinking
                    visible_emitted = True
                    yield _chat_chunk(
                        completion_id,
                        created,
                        resolved_model,
                        {"reasoning_content": thinking},
                    )
            elif event_type == "retract" and visible_emitted:
                raise RuntimeError(
                    "claude.ai retracted content after it was streamed; "
                    "the replacement was not appended to avoid duplicate output"
                )

        native = await task
        resolved_model = (
            native.model
            or runtime.resolve_request_model(body.model)
            or resolved_model
        )
        if native.content:
            if native.content.startswith(emitted_text):
                tail = native.content[len(emitted_text) :]
                if tail:
                    yield _chat_chunk(
                        completion_id,
                        created,
                        resolved_model,
                        {"content": tail},
                    )
            elif not emitted_text:
                yield _chat_chunk(
                    completion_id,
                    created,
                    resolved_model,
                    {"content": native.content},
                )
            else:
                raise RuntimeError(
                    "streamed text does not match Claude's final text block"
                )
        if (
            native.thinking
            and runtime.control.behavior()["thinking"] != "off"
            and not emitted_thinking
        ):
            yield _chat_chunk(
                completion_id,
                created,
                resolved_model,
                {"reasoning_content": native.thinking},
            )
        parsed = _parsed_native(native)
        for index, call in enumerate(parsed.tool_calls):
            yield _chat_chunk(
                completion_id,
                created,
                resolved_model,
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(
                                    call.arguments,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            },
                        }
                    ]
                },
            )
        usage = openai_usage(native.usage)
        # Persist completion before the terminal chunk. Some OpenAI clients
        # close the transport immediately after finish_reason and would
        # otherwise cancel this generator before telemetry is finalized.
        _finish_request_telemetry(
            request_id,
            status="completed",
            native=native,
            usage=usage,
            resolved_model=resolved_model,
        )
        runtime.telemetry.log(
            "INFO",
            "API",
            f"POST /v1/chat/completions завершён ({resolved_model})",
            request_id=request_id,
        )
        yield _chat_chunk(
            completion_id,
            created,
            resolved_model,
            {},
            _finish_reason(native, parsed),
        )
        if (
            usage is not None
            and body.stream_options is not None
            and body.stream_options.include_usage
        ):
            yield _usage_chunk(
                completion_id,
                created,
                resolved_model,
                usage,
            )
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _finish_request_telemetry(
            request_id,
            status="cancelled",
        )
        raise
    except Exception as exc:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if isinstance(exc, ClaudeAccountIdentityError):
            runtime.persist_runtime_identity()
        safe_error = public_error_message(exc)
        _finish_request_telemetry(
            request_id,
            status="error",
            error=safe_error,
        )
        yield _stream_error(exc)
        yield "data: [DONE]\n\n"
    finally:
        # StreamingResponse closes this generator with GeneratorExit when the
        # client disconnects. Always stop the browser turn in that path too.
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if (
            runtime.telemetry.has_active(request_id)
        ):
            _finish_request_telemetry(
                request_id,
                status="cancelled",
            )


async def _completed_event_stream(
    body: CompletionsIn,
    native: NativeTurn,
    completion_id: str,
    created: int,
    model: str,
):
    yield _chat_chunk(completion_id, created, model, {"role": "assistant"})
    if native.thinking and runtime.control.behavior()["thinking"] != "off":
        yield _chat_chunk(
            completion_id,
            created,
            model,
            {"reasoning_content": native.thinking},
        )
    if native.content:
        yield _chat_chunk(
            completion_id,
            created,
            model,
            {"content": native.content},
        )
    parsed = _parsed_native(native)
    for index, call in enumerate(parsed.tool_calls):
        yield _chat_chunk(
            completion_id,
            created,
            model,
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ]
            },
        )
    yield _chat_chunk(
        completion_id,
        created,
        model,
        {},
        _finish_reason(native, parsed),
    )
    usage = openai_usage(native.usage)
    if (
        usage is not None
        and body.stream_options is not None
        and body.stream_options.include_usage
    ):
        yield _usage_chunk(completion_id, created, model, usage)
    yield "data: [DONE]\n\n"


def _validated_client_header(
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


@app.post("/v1/chat/completions")
async def openai_compat(
    body: CompletionsIn,
    x_claude_code_session_id: Annotated[
        str | None,
        Header(alias="X-Claude-Code-Session-Id"),
    ] = None,
    x_openclaude_session_id: Annotated[
        str | None,
        Header(alias="X-OpenClaude-Session-Id"),
    ] = None,
    x_openclaude_working_directory: Annotated[
        str | None,
        Header(alias="X-OpenClaude-Working-Directory"),
    ] = None,
):
    legacy_session_id = _validated_client_header(
        x_claude_code_session_id,
        name="X-Claude-Code-Session-Id",
        max_length=256,
    )
    openclaude_session_id = _validated_client_header(
        x_openclaude_session_id,
        name="X-OpenClaude-Session-Id",
        max_length=256,
    )
    if (
        legacy_session_id
        and openclaude_session_id
        and legacy_session_id != openclaude_session_id
    ):
        raise HTTPException(400, "conflicting OpenClaude session headers")
    client_session_id = openclaude_session_id or legacy_session_id
    client_working_directory = _validated_client_header(
        x_openclaude_working_directory,
        name="X-OpenClaude-Working-Directory",
        max_length=4096,
    )
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = runtime.resolve_request_model(body.model) or body.model
    behavior = runtime.control.behavior()
    if body.stream and behavior["streaming"]:
        return StreamingResponse(
            _chat_event_stream(
                body,
                completion_id=completion_id,
                created=created,
                model=model,
                client_session_id=client_session_id,
                client_working_directory=client_working_directory,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    request_id = completion_id.removeprefix("chatcmpl-")
    _begin_request_telemetry(
        request_id,
        model,
        client_session_id,
        _telemetry_user_text(body),
        streaming=bool(body.stream),
    )
    try:
        native = await _run_native_with_limits(
            body,
            client_session_id=client_session_id,
            client_working_directory=client_working_directory,
            event_sink=None,
        )
        resolved_model = native.model or model
        usage = openai_usage(native.usage)
        _finish_request_telemetry(
            request_id,
            status="completed",
            native=native,
            usage=usage,
            resolved_model=resolved_model,
        )
        runtime.telemetry.log(
            "INFO",
            "API",
            f"POST /v1/chat/completions завершён ({resolved_model})",
            request_id=request_id,
        )
        if body.stream:
            return StreamingResponse(
                _completed_event_stream(
                    body,
                    native,
                    completion_id,
                    created,
                    resolved_model,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        return _completion_response(body, native, completion_id, created)
    except HTTPException as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=sanitize_public_text(exc.detail),
        )
        raise
    except ClaudeTurnOutcomeUnknownError as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(409, str(exc)) from exc
    except ClaudeAccountIdentityError as exc:
        runtime.persist_runtime_identity()
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(409, str(exc)) from exc
    except ClaudeBrowserUnavailableError as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(503, str(exc)) from exc
    except ClaudeServiceUnavailableError as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(503, str(exc)) from exc
    except ClaudeCompletionRejectedError as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(exc.status, str(exc)) from exc
    except ValueError as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        _finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(500, str(exc)) from exc


@app.get("/v1/models")
async def list_models():
    rows = [
        {
            "id": "claude-web",
            "object": "model",
            "created": 0,
            "owned_by": "local",
        }
    ]
    known = {"claude-web"}
    for item in runtime.session.selectable_models():
        if item.get("access_status") != "available":
            continue
        model_id = str(item["id"])
        if model_id in known:
            continue
        known.add(model_id)
        rows.append(
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "claude.ai",
            }
        )
    return {"object": "list", "data": rows}


@app.get("/api/control/state")
async def control_state():
    snapshot = runtime.control.snapshot()
    persona_compilation = ControlConfig.persona_compilation_for(
        snapshot.get("behavior", {}),
    )
    health = runtime.session.health_snapshot()
    for profile in snapshot["profiles"]:
        if profile["id"] == health.get("profile_id"):
            profile["runtime"] = {
                "active": True,
                "account": health.get("account"),
                "models": health.get("models"),
                "browser": health.get("browser"),
            }
    return {
        "config": snapshot,
        "persona_compilation": persona_compilation,
        "health": health,
        "providers": runtime.provider_capabilities_snapshot(),
        "activity": runtime.telemetry.snapshot(),
        "server": {
            "version": app.version,
            "port": int(os.getenv("PORT", "8765")),
            "project_root": str(PROJECT_ROOT),
            "working_directory": os.getcwd(),
            "streaming_is_live": bool(
                runtime.control.behavior().get("streaming")
            ),
            "thinking_note": (
                "Claude Web exposes provider summaries when available; "
                "hidden chain-of-thought is never fabricated or leaked."
            ),
        },
        "protocol": runtime.session.last_completion_shape(),
    }


TELEMETRY_PERIOD_SECONDS = {
    "1h": 3_600,
    "24h": 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
    "all": None,
}
TELEMETRY_STATUSES = {
    "running",
    "completed",
    "error",
    "cancelled",
    "interrupted",
}


def _telemetry_since(period: str) -> float | None:
    if period not in TELEMETRY_PERIOD_SECONDS:
        raise HTTPException(
            400,
            "period must be one of 1h, 24h, 7d, 30d or all",
        )
    seconds = TELEMETRY_PERIOD_SECONDS[period]
    return time.time() - seconds if seconds is not None else None


def _persistent_telemetry_store() -> TelemetryStore:
    if runtime.telemetry.store is None:
        raise HTTPException(
            503,
            runtime.telemetry.storage_error
            or "persistent telemetry storage is unavailable",
        )
    return runtime.telemetry.store


@app.get("/api/control/telemetry")
async def control_telemetry(
    period: str = Query(default="7d"),
    status: str = Query(default="all"),
    provider_id: str | None = Query(default=None, max_length=32),
    profile_id: str | None = Query(default=None, max_length=80),
    model: str | None = Query(default=None, max_length=160),
    q: str | None = Query(default=None, max_length=200),
    level: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    since = _telemetry_since(period)
    normalized_status = status.lower()
    if normalized_status != "all" and normalized_status not in TELEMETRY_STATUSES:
        raise HTTPException(400, "unsupported telemetry status")
    normalized_level = level.lower()
    if normalized_level not in {"all", "info", "warn", "error"}:
        raise HTTPException(400, "unsupported event level")
    _persistent_telemetry_store()
    try:
        requests, total = await runtime.telemetry.store_call_async(
            "list_requests",
            since=since,
            status=(
                None if normalized_status == "all" else normalized_status
            ),
            provider_id=provider_id or None,
            profile_id=profile_id or None,
            model=model or None,
            search=q or None,
            limit=limit,
            offset=offset,
        )
        summary = await runtime.telemetry.store_call_async(
            "summary",
            since=since,
            provider_id=provider_id or None,
            profile_id=profile_id or None,
            model=model or None,
            search=q or None,
        )
        events, event_total = await runtime.telemetry.store_call_async(
            "list_events",
            since=since,
            level=(
                None if normalized_level == "all" else normalized_level
            ),
            search=q or None,
            limit=limit,
            offset=offset,
        )
        runtime.telemetry.storage_error = None
    except Exception as exc:
        runtime.telemetry.storage_error = public_error_message(exc)
        raise HTTPException(
            503,
            "persistent telemetry query failed",
        ) from exc
    settings = runtime.control.telemetry_settings()
    payload = {
        "period": period,
        "summary": summary,
        "requests": {
            "items": requests,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(requests) < total,
        },
        "events": {
            "items": events,
            "total": event_total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(events) < event_total,
        },
        "settings": {
            **settings,
            "content_effective": _telemetry_content_enabled(),
            "ephemeral_suppresses_content": (
                str(runtime.control.behavior().get("privacy") or "keep")
                == "ephemeral"
            ),
        },
    }
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@app.patch("/api/control/telemetry/settings")
async def update_telemetry_settings(body: TelemetryPatch):
    updates = {
        key: value
        for key, value in body.model_dump().items()
        if value is not None
    }
    _persistent_telemetry_store()
    try:
        settings = runtime.control.update_telemetry(updates)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        if updates.get("store_content") is False:
            await runtime.telemetry.store_call_async("scrub_content")
        await runtime.telemetry.store_call_async(
            "prune",
            retention_days=int(settings["retention_days"]),
            max_requests=int(settings["max_requests"]),
        )
        runtime.telemetry.storage_error = None
    except Exception as exc:
        runtime.telemetry.storage_error = public_error_message(exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Настройка сохранена, но локальная очистка не завершена; "
                    "сервер повторит её автоматически"
                ),
                "settings": {
                    **settings,
                    "content_effective": _telemetry_content_enabled(),
                },
                "cleanup_pending": True,
            },
        )
    runtime.telemetry.log(
        "INFO",
        "Telemetry",
        "Настройки локальной истории обновлены",
    )
    return {
        "ok": True,
        "settings": {
            **settings,
            "content_effective": _telemetry_content_enabled(),
        },
    }


@app.delete("/api/control/telemetry")
async def clear_control_telemetry():
    _persistent_telemetry_store()
    runtime.telemetry.events.clear()
    runtime.telemetry.last = None
    try:
        await runtime.telemetry.store_call_async("clear_all")
    except Exception:
        raise HTTPException(503, "could not clear persistent telemetry") from None
    if runtime.telemetry.storage_error:
        raise HTTPException(503, "could not clear persistent telemetry")
    return {"ok": True}


@app.get("/api/control/telemetry/{request_id}")
async def control_telemetry_request(request_id: str):
    if not re.fullmatch(r"(?:chatcmpl-)?[a-zA-Z0-9_-]{6,80}", request_id):
        raise HTTPException(400, "invalid request id")
    _persistent_telemetry_store()
    try:
        item = await runtime.telemetry.store_call_async(
            "request_detail",
            request_id.removeprefix("chatcmpl-"),
        )
    except Exception as exc:
        runtime.telemetry.storage_error = public_error_message(exc)
        raise HTTPException(
            503,
            "persistent telemetry query failed",
        ) from exc
    if item is None:
        raise HTTPException(404, "telemetry request not found")
    return JSONResponse(
        content={"request": item},
        headers={"Cache-Control": "no-store"},
    )


@app.patch("/api/control/behavior")
async def update_behavior(body: BehaviorPatch):
    updates = {
        key: value
        for key, value in body.model_dump().items()
        if value is not None
    }
    try:
        behavior = runtime.control.update_behavior(updates)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    runtime.telemetry.log("INFO", "Settings", "Настройки поведения обновлены")
    return {
        "ok": True,
        "behavior": behavior,
        "persona_compilation": ControlConfig.persona_compilation_for(
            behavior,
        ),
    }


@app.post("/api/control/profiles")
async def create_profile(body: ProfileCreate):
    try:
        profile = runtime.control.create_profile(body.name, body.provider)
        runtime.bind_claude_profile_route(profile)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    runtime.telemetry.log(
        "INFO",
        "Profiles",
        f"Создан профиль «{profile['name']}»; требуется вход",
    )
    return {"ok": True, "profile": profile}


@app.post("/api/control/profiles/{profile_id}/login")
async def launch_profile_login(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    if runtime.is_active_claude_profile(profile):
        health = runtime.session.health_snapshot()
        browser = health.get("browser", {})
        if (
            isinstance(browser, dict)
            and browser.get("phase")
            in {"auth_required", "account_unknown", "account_changed"}
        ):
            if runtime.HEADLESS and runtime.session.headless:
                runtime.session.headless = False
                await runtime.session.sync_profiles(
                    runtime.runtime_profiles(),
                    profile_id,
                    restart=True,
                )
                await runtime.session.start_watchdog()
            else:
                await runtime.session.bring_to_front()
            runtime.control.update_profile(profile_id, {"status": "auth_pending"})
            runtime.telemetry.log(
                "INFO",
                "Profiles",
                f"Открыт Camoufox для повторного входа в профиль «{profile['name']}»",
            )
            return {
                "ok": True,
                "login": {
                    "profile_id": profile_id,
                    "status": "waiting_for_login",
                    "authenticated": False,
                    "browser_open": True,
                    "active_browser": True,
                    "ready": False,
                },
            }
        raise HTTPException(
            409,
            "the active Camoufox profile is already open and authenticated",
        )
    try:
        state = await runtime.enrollment.launch(
            profile_id,
            profile["path"],
            str(profile.get("provider") or CLAUDE_WEB_PROVIDER_ID),
        )
        runtime.control.update_profile(profile_id, {"status": "auth_pending"})
        if profile.get("provider") == runtime.GROK_WEB_PROVIDER_ID:
            log_message = (
                "Открыто диагностическое окно Playwright Chrome для проверки "
                f"доступа профиля «{profile['name']}»"
            )
        else:
            log_message = (
                f"Открыт Camoufox для входа в профиль «{profile['name']}»"
            )
        runtime.telemetry.log(
            "INFO",
            "Profiles",
            log_message,
        )
        return {"ok": True, "login": state}
    except Exception as exc:
        runtime.control.update_profile(profile_id, {"status": "error"})
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/control/profiles/{profile_id}/login")
async def inspect_profile_login(profile_id: str):
    task = runtime.profile_login_tasks.get(profile_id)
    if task is None or task.done():
        task = asyncio.create_task(
            _inspect_profile_login_once(profile_id),
            name=f"profile-login-{profile_id}",
        )
        runtime.profile_login_tasks[profile_id] = task
    try:
        return await asyncio.shield(task)
    finally:
        if runtime.profile_login_tasks.get(profile_id) is task and task.done():
            runtime.profile_login_tasks.pop(profile_id, None)


async def _inspect_profile_login_once(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    provider_id = str(
        profile.get("provider") or CLAUDE_WEB_PROVIDER_ID
    )
    active_claude_profile = runtime.is_active_claude_profile(profile)
    login_running = await runtime.enrollment.is_running(profile_id)
    if (
        provider_id != CLAUDE_WEB_PROVIDER_ID
        and not login_running
        and profile.get("status") in {"ready", "protocol_unverified"}
    ):
        # A browser login is not sufficient to make an experimental provider
        # executable. Normalize stale/legacy "ready" rows to the fail-closed
        # state and never expose them as an activatable completion profile.
        runtime.control.update_profile(
            profile_id,
            {
                "status": "protocol_unverified",
                "enabled": False,
                "last_checked_at": time.time(),
            },
        )
        profile = runtime.control.profile(profile_id)
        public_profile = next(
            row
            for row in runtime.control.snapshot()["profiles"]
            if row["id"] == profile_id
        )
        return {
            "ok": True,
            "login": {
                "profile_id": profile_id,
                "provider": provider_id,
                "status": "protocol_unverified",
                "authenticated": bool(
                    profile.get("account", {}).get("authenticated")
                ),
                "browser_open": False,
                "ready": False,
                "account": profile.get("account", {}),
                "models": profile.get("models", []),
                "profile": public_profile,
                "protocol_error": (
                    "Browser login alone does not verify this provider's "
                    "completion stream or tool continuation."
                ),
            },
        }
    if (
        not active_claude_profile
        and not login_running
        and provider_id == CLAUDE_WEB_PROVIDER_ID
        and profile.get("status") == "ready"
        and profile.get("project_id")
    ):
        public_profile = next(
            row
            for row in runtime.control.snapshot()["profiles"]
            if row["id"] == profile_id
        )
        return {
            "ok": True,
            "login": {
                "profile_id": profile_id,
                "status": "ready",
                "authenticated": bool(
                    profile.get("account", {}).get("authenticated")
                ),
                "browser_open": False,
                "ready": True,
                "account": profile.get("account", {}),
                "models": profile.get("models", []),
                "profile": public_profile,
            },
        }
    if (
        not active_claude_profile
        and not login_running
        and profile.get("status") == "duplicate"
    ):
        return {
            "ok": True,
            "login": {
                "profile_id": profile_id,
                "status": "duplicate",
                "authenticated": True,
                "browser_open": False,
                "ready": False,
                "account": profile.get("account", {}),
            },
        }
    if (
        active_claude_profile
        and not login_running
    ):
        health = runtime.session.health_snapshot()
        account = health.get("account")
        models = health.get("models")
        if not isinstance(account, dict):
            account = {}
        if not isinstance(models, dict):
            models = {}
        authenticated = bool(account.get("authenticated"))
        result = {
            "profile_id": profile_id,
            "status": "ready" if authenticated else "waiting_for_login",
            "authenticated": authenticated,
            "browser_open": health.get("browser", {}).get("phase")
            not in {"stopped", "browser_dead"},
            "active_browser": True,
            "ready": authenticated,
            "account": account,
            "models": models.get("available", []),
        }
        if authenticated:
            identity_ok = runtime.persist_runtime_identity()
            if not identity_ok:
                result["status"] = "account_changed"
                result["ready"] = False
                result["account_error"] = (
                    "This browser is logged into another or duplicate "
                    "account. Add it as a separate profile."
                )
            elif runtime.HEADLESS and not runtime.session.headless:
                runtime.session.headless = True
                await runtime.session.sync_profiles(
                    runtime.runtime_profiles(),
                    profile_id,
                    restart=True,
                )
                await runtime.session.start_watchdog()
                result["browser_open"] = False
                result["active_browser"] = True
        else:
            runtime.control.update_profile(
                profile_id,
                {
                    "status": "auth_pending",
                    "last_checked_at": time.time(),
                },
            )
        return {"ok": True, "login": result}
    result = await runtime.enrollment.inspect(profile_id)
    if not result.get("authenticated"):
        observed_status = str(result.get("status") or "checking")
        if observed_status in {"provider_blocked", "access_denied"}:
            # These are terminal provider/WAF outcomes, not an unfinished
            # login. Preserve the exact status so callers can stop polling and
            # present the provider's diagnostic instead of waiting forever.
            persisted_status = observed_status
        elif observed_status == "error":
            persisted_status = "error"
        elif observed_status in {"browser_closed", "not_running"}:
            persisted_status = "auth_required"
        else:
            persisted_status = "auth_pending"
        runtime.control.update_profile(
            profile_id,
            {
                "status": persisted_status,
                "last_checked_at": time.time(),
            },
        )
        result["ready"] = False
        return {"ok": True, "login": result}
    identity = await runtime.enrollment.internal_identity(profile_id)
    account_uuid = str(identity.get("account_uuid") or "")
    if not account_uuid:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "auth_pending",
                "account": result.get("account"),
                "models": result.get("models", []),
                "last_checked_at": time.time(),
            },
        )
        result["status"] = "identity_unverified"
        result["ready"] = False
        provider_name = (
            "Claude"
            if provider_id == CLAUDE_WEB_PROVIDER_ID
            else "Grok"
        )
        result["account_error"] = (
            f"The web account is visible, but {provider_name} has not "
            "exposed a stable account identity in the verified browser "
            "state yet."
        )
        return {"ok": True, "login": result}
    fingerprint_source = (
        account_uuid
        if provider_id == CLAUDE_WEB_PROVIDER_ID
        else f"{provider_id}:{account_uuid}"
    )
    fingerprint = runtime.control.account_fingerprint(fingerprint_source)
    duplicate = runtime.control.claim_account_fingerprint(
        profile_id,
        fingerprint,
    )
    if duplicate is not None:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "duplicate",
                "enabled": False,
                "account": result.get("account"),
                "last_checked_at": time.time(),
            },
        )
        await runtime.enrollment.finish(profile_id)
        result["status"] = "duplicate"
        result["ready"] = False
        result["duplicate"] = {
            "profile_id": duplicate["id"],
            "name": duplicate["name"],
        }
        runtime.telemetry.log(
            "WARN",
            "Profiles",
            f"Профиль «{profile['name']}» использует уже добавленный аккаунт",
        )
        return {"ok": True, "login": result}
    if provider_id != CLAUDE_WEB_PROVIDER_ID:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "protocol_unverified",
                "enabled": False,
                "account_fingerprint": fingerprint,
                "account": result.get("account"),
                "models": result.get("models", []),
                "last_checked_at": time.time(),
            },
        )
        await runtime.enrollment.finish(profile_id)
        result["status"] = "protocol_unverified"
        result["ready"] = False
        result["protocol_error"] = (
            "The browser login was observed, but Grok completion streaming "
            "and tool continuation have not been verified. The profile stays "
            "disabled."
        )
        result["profile"] = next(
            row
            for row in runtime.control.snapshot()["profiles"]
            if row["id"] == profile_id
        )
        runtime.telemetry.log(
            "WARN",
            "Profiles",
            (
                f"Профиль «{profile['name']}» виден в браузере, но "
                "completion-транспорт Grok не проверен и остаётся выключен"
            ),
        )
        return {"ok": True, "login": result}
    project: dict[str, Any]
    try:
        instructions = (
            PROJECT_INSTRUCTIONS.read_text(encoding="utf-8")
            if provider_id == CLAUDE_WEB_PROVIDER_ID
            else ""
        )
        project = await runtime.enrollment.ensure_project(profile_id, instructions)
    except Exception as exc:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "project_setup_error",
                "enabled": False,
                "account_fingerprint": fingerprint,
                "account": result.get("account"),
                "models": result.get("models", []),
                "last_checked_at": time.time(),
            },
        )
        result["status"] = "project_setup_error"
        result["ready"] = False
        result["project_error"] = public_error_message(exc)
        return {"ok": True, "login": result}
    identity = await runtime.enrollment.internal_identity(profile_id)
    runtime.control.update_profile(
        profile_id,
        {
            "status": "ready",
            "enabled": True,
            "account_fingerprint": fingerprint,
            "account": result.get("account"),
            "models": result.get("models", []),
            "organization_id": identity.get("organization_uuid"),
            "project_id": project["project_id"],
            "last_checked_at": time.time(),
        },
    )
    await runtime.enrollment.finish(profile_id)
    if provider_id == CLAUDE_WEB_PROVIDER_ID:
        await runtime.session.sync_profiles(
            runtime.runtime_profiles(),
            runtime.provider_profile_id(CLAUDE_WEB_PROVIDER_ID),
        )
    public_profile = next(
        row
        for row in runtime.control.snapshot()["profiles"]
        if row["id"] == profile_id
    )
    result["status"] = "ready"
    result["ready"] = True
    if project.get("required", True):
        result["project"] = {
            "name": project["name"],
            "project_id_suffix": project["project_id"][-8:],
            "organization_id_suffix": project.get(
                "organization_uuid_suffix"
            ),
        }
    else:
        result["project"] = {
            "required": False,
            "status": "not_required",
        }
    result["profile"] = public_profile
    runtime.telemetry.log(
        "INFO",
        "Profiles",
        f"Профиль «{profile['name']}» авторизован и готов",
    )
    return {"ok": True, "login": result}


@app.delete("/api/control/profiles/{profile_id}/login")
async def cancel_profile_login(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    finalize_task = runtime.profile_login_tasks.pop(profile_id, None)
    if finalize_task is not None and not finalize_task.done():
        finalize_task.cancel()
        await asyncio.gather(finalize_task, return_exceptions=True)
    if not await runtime.enrollment.is_running(profile_id):
        if (
            runtime.is_active_claude_profile(profile)
            and runtime.HEADLESS
            and not runtime.session.headless
        ):
            runtime.session.headless = True
            await runtime.session.sync_profiles(
                runtime.runtime_profiles(),
                profile_id,
                restart=True,
            )
            await runtime.session.start_watchdog()
            runtime.control.update_profile(profile_id, {"status": "auth_required"})
            return {"ok": True, "cancelled": True}
        return {"ok": True, "cancelled": False}
    await runtime.enrollment.finish(profile_id)
    runtime.control.update_profile(profile_id, {"status": "auth_required"})
    return {"ok": True, "cancelled": True}


@app.post("/api/control/profiles/{profile_id}/activate")
async def activate_profile(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    if (
        not profile.get("enabled", True)
        or profile.get("status") not in {"ready", "limited"}
    ):
        raise HTTPException(409, "profile is not ready for activation")
    provider_id = str(
        profile.get("provider") or CLAUDE_WEB_PROVIDER_ID
    )
    if provider_id != CLAUDE_WEB_PROVIDER_ID:
        raise HTTPException(
            409,
            "Grok Web cannot be activated until its authenticated Chrome "
            "stream protocol has been verified.",
        )
    runtime.bind_claude_profile_route(profile)
    limited_until = profile.get("limited_until")
    if isinstance(limited_until, (int, float)) and limited_until > time.time():
        raise HTTPException(
            409,
            "profile is temporarily limited and cannot be activated yet",
        )
    native_state = runtime.session.health_snapshot().get("native", {})
    if isinstance(native_state, dict) and native_state.get("active"):
        raise HTTPException(
            409,
            "cannot switch profile while Claude is waiting for tool_result",
        )
    old_profile_id = runtime.control.snapshot()["active_profile"]
    runtime_profiles = runtime.runtime_profiles()
    try:
        await runtime.session.sync_profiles(
            runtime_profiles,
            profile_id,
            restart=True,
        )
        if not runtime.session.health_snapshot().get("ok"):
            raise RuntimeError(
                "target profile requires authentication or account verification"
            )
        runtime.control.set_active_profile(profile_id)
        if not runtime.persist_runtime_identity():
            raise RuntimeError(
                "target profile is logged into a different or duplicate account"
            )
    except Exception as exc:
        if old_profile_id != profile_id:
            try:
                await runtime.session.sync_profiles(
                    runtime_profiles,
                    old_profile_id,
                    restart=True,
                )
                runtime.control.set_active_profile(old_profile_id)
            except Exception as rollback_exc:
                runtime.telemetry.log(
                    "ERROR",
                    "Profiles",
                    f"Не удалось вернуть предыдущий профиль: {rollback_exc}",
                )
        raise HTTPException(503, str(exc)) from exc
    runtime.telemetry.log(
        "INFO",
        "Profiles",
        f"Активирован профиль «{profile['name']}»",
    )
    return {"ok": True, "health": runtime.session.health_snapshot()}


@app.post("/api/control/profiles/{profile_id}/model")
async def select_profile_model(profile_id: str, body: ModelSelect):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    available = {
        str(item.get("id"))
        for item in profile.get("models", [])
        if (
            isinstance(item, dict)
            and item.get("available") is True
            and item.get("access_status") == "available"
        )
    }
    provider_alias = (
        "grok-web"
        if profile.get("provider") == runtime.GROK_WEB_PROVIDER_ID
        else "claude-web"
    )
    if body.model not in {"auto", provider_alias}:
        if not profile.get("models"):
            raise HTTPException(
                409,
                "account model catalog has not been discovered yet",
            )
        if body.model not in available:
            raise HTTPException(
                400,
                "model is not available to this authenticated account",
            )
    updated = runtime.control.update_profile(profile_id, {"model": body.model})
    if profile_id == runtime.session.current_profile_id():
        await runtime.session.sync_profiles(
            runtime.runtime_profiles(),
            profile_id,
            restart=False,
        )
    runtime.telemetry.log(
        "INFO",
        "Models",
        f"Для профиля «{profile['name']}» выбрана модель {body.model}",
    )
    return {"ok": True, "model": updated["model"]}


@app.delete("/api/control/events")
async def clear_control_events():
    _persistent_telemetry_store()
    runtime.telemetry.events.clear()
    try:
        await runtime.telemetry.store_call_async("clear_events")
    except Exception:
        raise HTTPException(503, "could not clear persistent event log") from None
    if runtime.telemetry.storage_error:
        raise HTTPException(503, "could not clear persistent event log")
    return {"ok": True}


def main() -> None:
    """Run the bridge on the loopback interface."""
    import uvicorn

    uvicorn.run(
        "claude_web_api.app:app",
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8765")),
        reload=False,
    )


if __name__ == "__main__":
    main()
