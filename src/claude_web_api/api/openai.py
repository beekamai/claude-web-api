"""OpenAI-compatible API.

Chat completions and the model list, translated from the shared completions
core into OpenAI request and response shapes.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from claude_web_api import completions, runtime
from claude_web_api.protocol.openai import ParsedAssistant, chat_message
from claude_web_api.protocol.openai_usage import openai_usage
from claude_web_api.sanitize import public_error_message, sanitize_public_text
from claude_web_api.session.claude import (
    ClaudeAccountIdentityError,
    ClaudeBrowserUnavailableError,
    ClaudeCompletionRejectedError,
    ClaudeServiceUnavailableError,
    ClaudeTurnOutcomeUnknownError,
    NativeTurn,
)

router = APIRouter()


def _assistant_message(turn: NativeTurn) -> dict[str, Any]:
    message = chat_message(completions.parsed_native(turn))
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
    body: completions.CompletionsIn,
    native: NativeTurn,
    completion_id: str,
    created: int,
) -> dict[str, Any]:
    parsed = completions.parsed_native(native)
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

def _stream_error(exc: Exception) -> str:
    status = completions.exception_status(exc)
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
    body: completions.CompletionsIn,
    completion_id: str,
    created: int,
    model: str,
    client_session_id: str | None,
    client_working_directory: str | None = None,
):
    request_id = completion_id.removeprefix("chatcmpl-")
    relay = completions.StreamRelay(request_id)
    completions.begin_request_telemetry(
        request_id,
        model,
        client_session_id,
        completions.telemetry_user_text(body),
        streaming=True,
    )
    task = asyncio.create_task(
        completions.run_native_with_limits(
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
        parsed = completions.parsed_native(native)
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
        completions.finish_request_telemetry(
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
        completions.finish_request_telemetry(
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
        completions.finish_request_telemetry(
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
            completions.finish_request_telemetry(
                request_id,
                status="cancelled",
            )

async def _completed_event_stream(
    body: completions.CompletionsIn,
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
    parsed = completions.parsed_native(native)
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

@router.post("/v1/chat/completions")
async def openai_compat(
    body: completions.CompletionsIn,
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
    legacy_session_id = completions.validated_client_header(
        x_claude_code_session_id,
        name="X-Claude-Code-Session-Id",
        max_length=256,
    )
    openclaude_session_id = completions.validated_client_header(
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
    client_working_directory = completions.validated_client_header(
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
    completions.begin_request_telemetry(
        request_id,
        model,
        client_session_id,
        completions.telemetry_user_text(body),
        streaming=bool(body.stream),
    )
    try:
        native = await completions.run_native_with_limits(
            body,
            client_session_id=client_session_id,
            client_working_directory=client_working_directory,
            event_sink=None,
        )
        resolved_model = native.model or model
        usage = openai_usage(native.usage)
        completions.finish_request_telemetry(
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
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=sanitize_public_text(exc.detail),
        )
        raise
    except ClaudeTurnOutcomeUnknownError as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(409, str(exc)) from exc
    except ClaudeAccountIdentityError as exc:
        runtime.persist_runtime_identity()
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(409, str(exc)) from exc
    except ClaudeBrowserUnavailableError as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(503, str(exc)) from exc
    except ClaudeServiceUnavailableError as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(503, str(exc)) from exc
    except ClaudeCompletionRejectedError as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(exc.status, str(exc)) from exc
    except ValueError as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(500, str(exc)) from exc

@router.get("/v1/models")
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
