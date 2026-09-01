"""Local OpenAI-compatible API backed by an authenticated claude.ai session.

OpenAI functions are mapped to claude.ai native tools. Claude selects actions,
OpenClaude executes them, and their results return through Claude's real
``/tool_result`` side-channel while the original SSE remains open.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from claude_web_api import __version__, runtime
from claude_web_api.api import control as control_api
from claude_web_api.paths import (
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


app = FastAPI(
    title="Claude Web API",
    version=__version__,
    lifespan=lifespan,
)
app.include_router(control_api.router)

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
        capture_content=runtime.telemetry_content_enabled(),
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
