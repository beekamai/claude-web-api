"""Anthropic Messages API.

The surface Claude Code talks to when ANTHROPIC_BASE_URL points at the bridge.
Requests are translated into the shared completions core, and both the buffered
response and the SSE stream are built from the native turn, so ``tool_use``
blocks survive the round trip and a ``tool_result`` continues the same turn.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from claude_web_api import completions, runtime
from claude_web_api.protocol import anthropic as protocol
from claude_web_api.sanitize import public_error_message, sanitize_public_text

router = APIRouter()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def error_payload(status: int, message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {
            "type": ERROR_TYPES.get(status, "api_error"),
            "message": message,
        },
    }


def _bridge_request(body: protocol.MessagesIn) -> completions.CompletionsIn:
    return completions.CompletionsIn(
        messages=protocol.bridge_messages(body),
        model=body.model,
        stream=body.stream,
        tools=protocol.bridge_tools(body.tools),
        tool_choice=protocol.bridge_tool_choice(body.tool_choice),
        parallel_tool_calls=protocol.parallel_tool_calls(body.tool_choice),
        new_chat=body.new_chat,
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _client_headers(
    anthropic_session_id: str | None,
    openclaude_session_id: str | None,
    working_directory: str | None,
) -> tuple[str | None, str | None]:
    session_id = completions.validated_client_header(
        openclaude_session_id or anthropic_session_id,
        name="X-OpenClaude-Session-Id",
        max_length=256,
    )
    directory = completions.validated_client_header(
        working_directory,
        name="X-OpenClaude-Working-Directory",
        max_length=4096,
    )
    return session_id, directory


async def _message_stream(
    body: protocol.MessagesIn,
    inner: completions.CompletionsIn,
    response_id: str,
    model: str,
    client_session_id: str | None,
    client_working_directory: str | None,
):
    request_id = response_id.removeprefix("msg_")
    relay = completions.StreamRelay(request_id)
    completions.begin_request_telemetry(
        request_id,
        model,
        client_session_id,
        completions.telemetry_user_text(inner),
        streaming=True,
    )
    task = asyncio.create_task(
        completions.run_native_with_limits(
            inner,
            client_session_id=client_session_id,
            client_working_directory=client_working_directory,
            event_sink=relay,
        ),
        name=f"anthropic-stream-{request_id}",
    )
    resolved_model = model
    emitted_text = ""
    text_block_open = False
    try:
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": response_id,
                    "type": "message",
                    "role": "assistant",
                    "model": resolved_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        while not task.done() or not relay.queue.empty():
            try:
                event = await asyncio.wait_for(relay.queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            kind = event.get("type")
            if kind == "model" and event.get("model"):
                resolved_model = str(event["model"])
            elif kind == "text_delta":
                text = str(event.get("text") or "")
                if not text:
                    continue
                if not text_block_open:
                    yield _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    text_block_open = True
                emitted_text += text
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            elif kind == "retract" and emitted_text:
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
        tail = ""
        if native.content:
            if native.content.startswith(emitted_text):
                tail = native.content[len(emitted_text) :]
            elif not emitted_text:
                tail = native.content
            else:
                raise RuntimeError(
                    "streamed text does not match Claude's final text block"
                )
        if tail:
            if not text_block_open:
                yield _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                text_block_open = True
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": tail},
                },
            )
        if text_block_open:
            yield _sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            )

        index = 1 if text_block_open else 0
        for call in native.tool_uses:
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": {},
                    },
                },
            )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            call.input, ensure_ascii=False
                        ),
                    },
                },
            )
            yield _sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
            index += 1

        usage = protocol.anthropic_usage(native.usage)
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": protocol.stop_reason(native.tool_uses),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": usage["output_tokens"]},
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})
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
            f"POST /v1/messages завершён ({resolved_model})",
            request_id=request_id,
        )
    except asyncio.CancelledError:
        task.cancel()
        completions.finish_request_telemetry(request_id, status="cancelled")
        raise
    except Exception as exc:
        task.cancel()
        status = completions.exception_status(exc)
        message = (
            sanitize_public_text(exc.detail)
            if isinstance(exc, HTTPException)
            else public_error_message(exc)
        )
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=message,
        )
        yield _sse("error", error_payload(status, message))


@router.post("/v1/messages")
async def create_message(
    body: protocol.MessagesIn,
    x_anthropic_session_id: Annotated[
        str | None,
        Header(alias="X-Anthropic-Session-Id"),
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
    client_session_id, client_working_directory = _client_headers(
        x_anthropic_session_id,
        x_openclaude_session_id,
        x_openclaude_working_directory,
    )
    try:
        inner = _bridge_request(body)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content=error_payload(400, public_error_message(exc)),
        )

    response_id = protocol.message_id()
    model = runtime.resolve_request_model(body.model) or body.model
    if body.stream and runtime.control.behavior()["streaming"]:
        return StreamingResponse(
            _message_stream(
                body,
                inner,
                response_id,
                model,
                client_session_id,
                client_working_directory,
            ),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    request_id = response_id.removeprefix("msg_")
    completions.begin_request_telemetry(
        request_id,
        model,
        client_session_id,
        completions.telemetry_user_text(inner),
        streaming=bool(body.stream),
    )
    try:
        native = await completions.run_native_with_limits(
            inner,
            client_session_id=client_session_id,
            client_working_directory=client_working_directory,
            event_sink=None,
        )
    except Exception as exc:
        status = completions.exception_status(exc)
        message = (
            sanitize_public_text(exc.detail)
            if isinstance(exc, HTTPException)
            else public_error_message(exc)
        )
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=message,
        )
        return JSONResponse(
            status_code=status,
            content=error_payload(status, message),
        )

    resolved_model = native.model or model
    payload = protocol.message_response(
        response_id=response_id,
        model=resolved_model,
        text=native.content,
        tool_uses=native.tool_uses,
        usage=native.usage,
    )
    completions.finish_request_telemetry(
        request_id,
        status="completed",
        native=native,
        usage=payload["usage"],
        resolved_model=resolved_model,
    )
    runtime.telemetry.log(
        "INFO",
        "API",
        f"POST /v1/messages завершён ({resolved_model})",
        request_id=request_id,
    )
    if body.stream:
        return StreamingResponse(
            _replay_stream(response_id, resolved_model, native, payload),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    return payload


async def _replay_stream(
    response_id: str,
    model: str,
    native: Any,
    payload: dict[str, Any],
):
    """Emit a finished turn as SSE, for clients that asked to stream while
    live streaming is switched off in the control panel."""
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": response_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": payload["usage"]["input_tokens"],
                    "output_tokens": 0,
                },
            },
        },
    )
    for index, block in enumerate(payload["content"]):
        if block["type"] == "text":
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            if block["text"]:
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "text_delta",
                            "text": block["text"],
                        },
                    },
                )
        else:
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
            )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            block["input"], ensure_ascii=False
                        ),
                    },
                },
            )
        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": index},
        )
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": payload["stop_reason"],
                "stop_sequence": None,
            },
            "usage": {"output_tokens": payload["usage"]["output_tokens"]},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})


@router.post("/v1/messages/count_tokens")
async def count_tokens(body: protocol.CountTokensIn):
    """Estimate a prompt's size.

    claude.ai reports token counts only after a turn has run and exposes no
    tokenizer, so this is a documented character-length approximation rather
    than an upstream measurement.
    """
    return {"input_tokens": protocol.estimated_input_tokens(body)}
