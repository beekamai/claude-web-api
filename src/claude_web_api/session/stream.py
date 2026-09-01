"""Intercepting claude.ai's completion stream and parsing its frames."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from claude_web_api.session.errors import (
    ClaudeBrowserUnavailableError,
    ClaudeCompletionRejectedError,
    ClaudeConversationLimitError,
    ClaudeServiceUnavailableError,
    ClaudeUsageLimitError,
)
from claude_web_api.session.models import NativeToolUse
from claude_web_api.session.patterns import (
    COMPLETION_IDS_RE,
    PASSTHROUGH_HEADER_NAMES,
)
from claude_web_api.session.state import SessionState


class NativeStreamMixin(SessionState):
    """Intercepting claude.ai's completion stream and parsing its frames."""

    async def _receive_sse(self, source: Any, payload: Any) -> None:
        del source
        if (
            not self._native_active
            or self._native_queue is None
            or not isinstance(payload, dict)
        ):
            return
        url = str(payload.get("url", ""))
        event = str(payload.get("event", ""))
        self._sse_tap_last_at = time.time()
        self._sse_tap_last_event = event
        self._sse_tap_last_url = url
        if event == "__tap_http_error":
            try:
                diagnostic = json.loads(str(payload.get("data", "")))
            except json.JSONDecodeError:
                diagnostic = {}
            self._sse_tap_last_data = json.dumps(
                {"status": diagnostic.get("status")},
                ensure_ascii=True,
            )
        elif event.startswith("__tap_"):
            self._sse_tap_last_data = str(payload.get("data", ""))[:500]
        if (
            self._native_completion_url
            and url.split("?", 1)[0]
            != self._native_completion_url.split("?", 1)[0]
        ):
            self._sse_tap_rejected_count += 1
            return
        self._sse_tap_event_count += 1
        self._set_phase("waiting_sse")
        await self._native_queue.put(
            {
                "event": event,
                "data": str(payload.get("data", "")),
            }
        )
    async def _route_conversation_create(
        self,
        route: Any,
        request: Any,
        epoch: int | None = None,
    ) -> None:
        if (
            epoch is not None
            and epoch != self._session_epoch
        ) or request.method != "POST":
            await route.continue_()
            return
        if self._privacy_mode != "ephemeral":
            await route.continue_()
            return
        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            await route.continue_()
            return
        payload["is_temporary"] = True
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if project_id:
            if "project_id" in payload and "project_uuid" not in payload:
                payload["project_id"] = project_id
            else:
                payload["project_uuid"] = project_id
        headers = dict(request.headers)
        headers.pop("content-length", None)
        headers["content-type"] = "application/json"
        await route.continue_(
            headers=headers,
            post_data=json.dumps(payload, ensure_ascii=False),
        )
    async def _route_completion(
        self,
        route: Any,
        request: Any,
        epoch: int | None = None,
    ) -> None:
        if epoch is not None and epoch != self._session_epoch:
            await route.continue_()
            return
        self._debug(
            "route:"
            f"{request.method}:{request.url}:"
            f"active={self._native_active}:tools={len(self._native_tools)}"
        )
        if request.method != "POST" or not self._native_active:
            await route.continue_()
            return
        match = COMPLETION_IDS_RE.search(request.url)
        if not match:
            await route.continue_()
            return

        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            await route.continue_()
            return

        original_model = payload.get("model")
        if isinstance(original_model, str) and original_model:
            self._observed_models.add(original_model)
        create_params = payload.get("create_conversation_params")
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if isinstance(create_params, dict):
            if project_id:
                if (
                    "project_id" in create_params
                    and "project_uuid" not in create_params
                ):
                    create_params["project_id"] = project_id
                else:
                    create_params["project_uuid"] = project_id
            if self._privacy_mode == "ephemeral":
                create_params["is_temporary"] = True
        self._last_completion_shape = {
            "keys": sorted(str(key) for key in payload),
            "model": original_model,
            "has_thinking": any(
                key in payload
                for key in (
                    "thinking",
                    "thinking_enabled",
                    "extended_thinking",
                    "effort",
                )
            ),
            "thinking_keys": [
                key
                for key in (
                    "thinking",
                    "thinking_enabled",
                    "extended_thinking",
                    "effort",
                )
                if key in payload
            ],
            "create_conversation": (
                {
                    "keys": sorted(str(key) for key in create_params),
                    "has_project": bool(
                        create_params.get("project_uuid")
                        or create_params.get("project_id")
                    ),
                    "is_temporary": bool(
                        create_params.get("is_temporary")
                    ),
                }
                if isinstance(create_params, dict)
                else None
            ),
            "context_channel": "native_tool_description",
        }
        if self._native_requested_model:
            payload["model"] = self._native_requested_model
            self._observed_models.add(self._native_requested_model)
        payload["thinking_mode"] = {
            "off": "off",
            "show": "extended",
        }.get(self._native_thinking_mode, "auto")
        if self._native_thinking_mode == "off":
            payload.pop("effort", None)
        elif self._native_effort:
            payload["effort"] = {
                "xhigh": "max",
                "ultra": "max",
            }.get(self._native_effort, self._native_effort)

        if (
            self._native_completion_url
            and request.url != self._native_completion_url
            and "retry_completion" in request.url
        ):
            if bool(
                getattr(self._native_event_sink, "visible_seen", False)
            ):
                self._emit_native_event(
                    {
                        "type": "retract",
                        "from_index": 0,
                        "reason": "claude_retry_completion",
                    }
                )
            # A retry is a replacement stream. Drop partial blocks and queued
            # frames from the failed generation before accepting its events.
            self._native_queue = asyncio.Queue()
            self._native_blocks.clear()
            self._native_text_blocks.clear()
            self._native_tool_blocks.clear()
            self._native_thinking_blocks.clear()
            self._native_usage.clear()
            self._native_model = None
            self._native_stop_reason = None
            self._native_saw_content = False
            self._native_saw_tool = False
            self._native_terminal_seen = False

        custom_names = {
            tool["name"]
            for tool in self._native_tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        original_tools = [
            tool
            for tool in payload.get("tools", [])
            if isinstance(tool, dict) and tool.get("name") not in custom_names
        ]
        payload["tools"] = [*self._native_tools, *original_tools]
        self._debug(
            "route:inject:"
            + ",".join(
                str(tool.get("name", ""))
                for tool in self._native_tools
            )
        )

        self._native_completion_url = request.url
        self._native_org_uuid = match.group("org")
        self._native_conversation_uuid = match.group("conversation")
        self._set_phase("completion_intercepted")
        self._native_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in PASSTHROUGH_HEADER_NAMES
        }

        headers = dict(request.headers)
        headers.pop("content-length", None)
        headers["content-type"] = "application/json"
        await route.continue_(
            headers=headers,
            post_data=json.dumps(payload, ensure_ascii=False),
        )
        self._set_phase("waiting_first_sse")
    def _reset_native_parser(self) -> None:
        self._native_queue = asyncio.Queue()
        self._native_completion_url = None
        self._native_org_uuid = None
        self._native_conversation_uuid = None
        self._native_headers = {}
        self._native_pending_ids = set()
        self._native_pending_deadline = None
        self._native_blocks = {}
        self._native_text_blocks = {}
        self._native_tool_blocks = {}
        self._native_thinking_blocks = {}
        self._native_usage = {}
        self._native_model = None
        self._native_stop_reason = None
        self._tool_result_delivery = {}
        self._native_internal_tool_acks = 0
        self._native_internal_text_prefix = []
        self._native_internal_thinking_prefix = []
        self._native_saw_content = False
        self._native_saw_tool = False
        self._native_terminal_seen = False
        self._native_conversation_verified = False
        self._sse_tap_event_count = 0
        self._sse_tap_rejected_count = 0
        self._sse_tap_last_at = None
        self._sse_tap_last_event = None
        self._sse_tap_last_url = None
        self._sse_tap_last_data = None
    def _emit_native_event(self, payload: dict[str, Any]) -> None:
        sink = self._native_event_sink
        if sink is None:
            return
        try:
            sink(dict(payload))
        except Exception:
            # Streaming observers must never be able to corrupt Claude's
            # authoritative parser/tool state.
            return
    def _update_native_usage(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        changed = False
        for key, raw in value.items():
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                self._native_usage[str(key)] = int(raw)
                changed = True
            elif isinstance(raw, dict):
                current = self._native_usage.get(str(key))
                if not isinstance(current, dict):
                    current = {}
                for nested_key, nested_value in raw.items():
                    if isinstance(nested_value, (int, float)) and not isinstance(
                        nested_value,
                        bool,
                    ):
                        current[str(nested_key)] = int(nested_value)
                        changed = True
                self._native_usage[str(key)] = current
        if changed:
            self._emit_native_event(
                {"type": "usage", "usage": dict(self._native_usage)}
            )
    def _process_native_event(self, envelope: dict[str, str]) -> bool:
        event = envelope.get("event", "")
        raw = envelope.get("data", "")
        if event == "__browser_dead":
            raise ClaudeBrowserUnavailableError(raw or "Camoufox became unavailable")
        if event == "__tap_error":
            if "abort" not in raw.lower():
                raise RuntimeError(f"claude.ai SSE tap failed: {raw}")
            return False
        if event == "__tap_http_error":
            try:
                error_payload = json.loads(raw)
            except json.JSONDecodeError:
                error_payload = {}
            try:
                status = int(error_payload.get("status") or 500)
            except (TypeError, ValueError):
                status = 500
            message = str(
                error_payload.get("message")
                or "completion returned no SSE frames"
            )
            if status == 429:
                raise ClaudeUsageLimitError(
                    "claude.ai rejected the completion because the current "
                    "account reached a usage or rate limit",
                    replay_safe=True,
                )
            if status == 529:
                raise ClaudeServiceUnavailableError(
                    "claude.ai rejected the completion because the service "
                    "is overloaded"
                )
            raise ClaudeCompletionRejectedError(status, message)
        if event == "__tap_eof":
            if self._native_terminal_seen:
                return False
            raise RuntimeError("claude.ai SSE ended before message_stop")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            if event == "error":
                raise RuntimeError(f"claude.ai stream error: {raw}") from None
            return False
        if event in ("", "message"):
            event = str(payload.get("type", event))
        if os.getenv("CLAUDE_DEBUG_SSE", "0").lower() in ("1", "true", "yes"):
            delta = payload.get("delta")
            delta_type = (
                str(delta.get("type", ""))
                if isinstance(delta, dict)
                else None
            )
            lengths: dict[str, int] = {}
            if isinstance(delta, dict):
                for key in ("text", "thinking", "summary", "partial_json"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        lengths[key] = len(value)
            print(
                "CLAUDE_SSE "
                + json.dumps(
                    {
                        "event": event,
                        "index": payload.get("index"),
                        "delta_type": delta_type,
                        "lengths": lengths,
                        "usage": (
                            payload.get("usage")
                            if event in {"message_start", "message_delta"}
                            else None
                        ),
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )

        if event == "message_start":
            message = payload.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model:
                    self._native_model = model
                    self._observed_models.add(model)
                    self._emit_native_event({"type": "model", "model": model})
                self._update_native_usage(message.get("usage"))
            return False

        if event == "message_delta":
            self._update_native_usage(payload.get("usage"))
            delta = payload.get("delta")
            if isinstance(delta, dict):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    self._native_stop_reason = stop_reason
                self._emit_native_event(
                    {
                        "type": "message_delta",
                        "stop_reason": delta.get("stop_reason"),
                    }
                )
            return False

        if event in {"model_update", "model_fallback"}:
            candidate = (
                payload.get("model")
                or payload.get("to_model")
                or (
                    payload.get("to", {}).get("model")
                    if isinstance(payload.get("to"), dict)
                    else None
                )
            )
            if isinstance(candidate, str) and candidate:
                self._native_model = candidate
                self._observed_models.add(candidate)
                self._emit_native_event(
                    {"type": "model", "model": candidate}
                )
            return False

        if event == "content_block_start":
            index = payload.get("index")
            block = payload.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                return False
            block_type = block.get("type")
            if block_type == "text":
                self._native_saw_content = True
                self._native_blocks[index] = {
                    "type": "text",
                    "text": str(block.get("text", "") or ""),
                }
                initial_text = str(block.get("text", "") or "")
                if initial_text:
                    self._emit_native_event(
                        {
                            "type": "text_delta",
                            "index": index,
                            "text": initial_text,
                        }
                    )
            elif block_type in {"thinking", "thinking_summary"}:
                # Expose only provider-authored summaries, never raw/opaque
                # chain-of-thought fields.
                initial_thinking = (
                    str(block.get("summary") or "")
                    if block_type == "thinking_summary"
                    else ""
                )
                self._native_blocks[index] = {
                    "type": "thinking",
                    "thinking": initial_thinking,
                }
                if initial_thinking:
                    self._emit_native_event(
                        {
                            "type": "thinking_delta",
                            "index": index,
                            "thinking": initial_thinking,
                        }
                    )
            elif block_type == "redacted_thinking":
                # ``data`` in this block is provider-owned opaque material,
                # not a user-visible summary. Never surface or log it.
                self._native_blocks[index] = {
                    "type": "redacted_thinking",
                }
            elif block_type == "tool_use":
                self._native_saw_tool = True
                self._native_blocks[index] = {
                    "type": "tool_use",
                    "id": str(block.get("id", "") or ""),
                    "name": str(block.get("name", "") or ""),
                    "initial_input": block.get("input"),
                    "json_parts": [],
                }
            return False

        if event == "content_block_delta":
            index = payload.get("index")
            block = self._native_blocks.get(index)
            delta = payload.get("delta")
            if not isinstance(block, dict) or not isinstance(delta, dict):
                return False
            if block.get("type") == "text" and delta.get("type") == "text_delta":
                text_delta = str(delta.get("text", "") or "")
                block["text"] += text_delta
                if text_delta:
                    self._emit_native_event(
                        {
                            "type": "text_delta",
                            "index": index,
                            "text": text_delta,
                        }
                    )
            elif (
                block.get("type") == "thinking"
                and delta.get("type") == "thinking_summary_delta"
            ):
                thinking_delta = str(
                    delta.get("summary")
                    or delta.get("text")
                    or ""
                )
                block["thinking"] += thinking_delta
                if thinking_delta:
                    self._emit_native_event(
                        {
                            "type": "thinking_delta",
                            "index": index,
                            "thinking": thinking_delta,
                        }
                    )
            elif (
                block.get("type") == "tool_use"
                and delta.get("type") == "input_json_delta"
            ):
                block["json_parts"].append(str(delta.get("partial_json", "") or ""))
            return False

        if event == "content_block_stop":
            index = payload.get("index")
            if not isinstance(index, int):
                return False
            block = self._native_blocks.pop(index, None)
            if not isinstance(block, dict):
                return False
            if block.get("type") == "text":
                self._native_text_blocks[index] = str(block.get("text", "") or "")
            elif block.get("type") == "thinking":
                self._native_thinking_blocks[index] = str(
                    block.get("thinking", "") or ""
                )
            elif block.get("type") == "tool_use":
                raw_input = "".join(block.get("json_parts", []))
                if not raw_input:
                    buffered = payload.get("buffered_input")
                    raw_input = str(buffered or "")
                if raw_input:
                    try:
                        tool_input = json.loads(raw_input)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            "Claude returned invalid native tool input JSON"
                        ) from exc
                else:
                    tool_input = block.get("initial_input") or {}
                if not isinstance(tool_input, dict):
                    raise RuntimeError("Claude native tool input must be a JSON object")
                tool_id = str(block.get("id", "") or "")
                tool_name = str(block.get("name", "") or "")
                allowed_names = {
                    str(tool.get("name"))
                    for tool in self._native_tools
                    if isinstance(tool, dict) and tool.get("name")
                }
                if not tool_id:
                    raise RuntimeError("Claude native tool_use is missing an id")
                if tool_name not in allowed_names:
                    # Browser-owned built-ins remain in the completion catalog.
                    # Claude's UI executes those itself; their tool_result and
                    # continuation stay on this same SSE, so the host bridge
                    # must simply observe and wait.
                    return False
                self._native_tool_blocks[index] = NativeToolUse(
                    id=tool_id,
                    name=tool_name,
                    input=tool_input,
                )
            return False

        if event == "content_block_retract":
            from_index = payload.get("from_index")
            if isinstance(from_index, int):
                self._emit_native_event(
                    {"type": "retract", "from_index": from_index}
                )
                self._native_blocks = {
                    index: block
                    for index, block in self._native_blocks.items()
                    if index < from_index
                }
                self._native_text_blocks = {
                    index: text
                    for index, text in self._native_text_blocks.items()
                    if index < from_index
                }
                self._native_tool_blocks = {
                    index: tool
                    for index, tool in self._native_tool_blocks.items()
                    if index < from_index
                }
                self._native_thinking_blocks = {
                    index: text
                    for index, text in self._native_thinking_blocks.items()
                    if index < from_index
                }
            return False

        if event == "message_limit":
            lowered = raw.lower()
            if any(
                marker in lowered
                for marker in (
                    '"status":"exceeded"',
                    '"status": "exceeded"',
                    '"type":"exceeded"',
                    '"type": "exceeded"',
                )
            ):
                raise ClaudeUsageLimitError(
                    "claude.ai reports that the current account "
                    "reached its usage limit",
                    replay_safe=not self._native_saw_tool,
                )
            return False

        if event == "error":
            lowered = raw.lower()
            if "maximum conversation" in lowered or "conversation_too_long" in lowered:
                raise ClaudeConversationLimitError(
                    "claude.ai reports that this conversation reached its length limit",
                    replay_safe=not self._native_saw_tool,
                )
            if "rate_limit" in lowered or "usage_limit" in lowered:
                raise ClaudeUsageLimitError(
                    "claude.ai reports that the current account "
                    "reached its usage limit",
                    replay_safe=not self._native_saw_tool,
                )
            raise RuntimeError(f"claude.ai stream error: {raw}")

        if event == "message_stop":
            self._native_terminal_seen = True
            return True
        return False
    def _take_native_text(self) -> str | None:
        text = "".join(
            value for _, value in sorted(self._native_text_blocks.items())
        )
        self._native_text_blocks.clear()
        return text or None
    def _take_native_thinking(self) -> str | None:
        thinking = "".join(
            value
            for _, value in sorted(self._native_thinking_blocks.items())
        )
        self._native_thinking_blocks.clear()
        return thinking or None
