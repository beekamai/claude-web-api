"""Anthropic Messages API translation.

Claude Code speaks this protocol natively, and so does claude.ai: a turn is
text plus ``tool_use`` blocks, and results come back as ``tool_result`` blocks
in the next user message. Messages requests are therefore mapped onto the same
completions core the OpenAI surface uses, and responses are built directly
from the native turn.

Two deliberate gaps, both visible rather than faked: ``thinking`` summaries are
not returned as content blocks, because the bridge cannot produce the signature
that would let a client replay them; and ``max_tokens`` is validated but cannot
be enforced on a browser session that delivers a finished answer.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

from claude_web_api.protocol.openai_usage import usage_integer

TEXT_BLOCK_TYPES = ("text",)


class MessagesIn(BaseModel):
    """Request body of POST /v1/messages."""

    model: str = "claude-web"
    messages: list[dict[str, Any]]
    max_tokens: int = Field(ge=1)
    system: Any = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None
    stop_sequences: list[str] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    thinking: dict[str, Any] | None = None
    new_chat: bool = False


class CountTokensIn(BaseModel):
    """Request body of POST /v1/messages/count_tokens."""

    model: str = "claude-web"
    messages: list[dict[str, Any]]
    system: Any = None
    tools: list[dict[str, Any]] | None = None


def message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def system_text(system: Any) -> str:
    """Accept both the string and the block-list form of ``system``."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, list):
        parts = [
            str(block.get("text", "") or "")
            for block in system
            if isinstance(block, dict) and block.get("type") in TEXT_BLOCK_TYPES
        ]
        return "\n\n".join(part for part in parts if part).strip()
    return ""


def _block_text(block: dict[str, Any]) -> str:
    if block.get("type") in TEXT_BLOCK_TYPES:
        return str(block.get("text", "") or "")
    return ""


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            text
            for text in (
                _block_text(item) for item in content if isinstance(item, dict)
            )
            if text
        )
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def bridge_messages(body: MessagesIn) -> list[dict[str, Any]]:
    """Translate Messages content blocks into the bridge's internal history.

    The internal form is the one the completions core already consumes: a role
    per entry, ``tool_calls`` on an assistant turn, and a ``tool`` role carrying
    one result. Anything a browser turn cannot carry, such as images or
    documents, raises instead of being dropped silently.
    """
    history: list[dict[str, Any]] = []
    instructions = system_text(body.system)
    if instructions:
        history.append({"role": "system", "content": instructions})

    for message in body.messages:
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant"):
            raise ValueError(f"unsupported message role {role!r}")
        if isinstance(content, str):
            history.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("message content must be a string or a block list")

        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("content blocks must be objects")
            kind = block.get("type")
            if kind in TEXT_BLOCK_TYPES:
                text = _block_text(block)
                if text:
                    texts.append(text)
            elif kind == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(
                                block.get("input") or {},
                                ensure_ascii=False,
                            ),
                        },
                    }
                )
            elif kind == "tool_result":
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": _tool_result_text(block),
                        "is_error": bool(block.get("is_error")),
                    }
                )
            elif kind == "thinking":
                # Replayed thinking carries nothing the browser turn can use,
                # and the bridge never signed it in the first place.
                continue
            else:
                raise ValueError(
                    f"content block type {kind!r} is not supported "
                    "by the browser bridge"
                )

        if texts or tool_calls:
            entry: dict[str, Any] = {
                "role": role,
                "content": "\n\n".join(texts),
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            history.append(entry)
        history.extend(tool_results)

    return history


def bridge_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Map Messages tool definitions onto the internal function shape."""
    if not tools:
        return None
    mapped: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        declared = tool.get("type")
        if declared and not str(declared).startswith("custom"):
            raise ValueError(
                f"server-side tool {name!r} has no equivalent in a browser turn"
            )
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        mapped.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description", "") or ""),
                    "parameters": schema,
                },
            }
        )
    return mapped or None


def bridge_tool_choice(tool_choice: dict[str, Any] | None) -> Any:
    """Map Messages tool_choice onto the internal value."""
    if not isinstance(tool_choice, dict):
        return None
    kind = tool_choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = tool_choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
    return None


def parallel_tool_calls(tool_choice: dict[str, Any] | None) -> bool:
    if not isinstance(tool_choice, dict):
        return True
    return not bool(tool_choice.get("disable_parallel_tool_use"))


def anthropic_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Report upstream token counts, falling back to zeros when absent.

    A Messages response always carries a usage object, so a missing upstream
    count becomes an explicit zero rather than an invented estimate.
    """
    usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    if not isinstance(raw, dict) or not raw:
        return usage
    input_tokens = usage_integer(
        raw.get("input_tokens", raw.get("prompt_tokens"))
    )
    output_tokens = usage_integer(
        raw.get("output_tokens", raw.get("completion_tokens"))
    )
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage_integer(raw.get(key))
        if value is not None:
            usage[key] = value
    return usage


def content_blocks(
    text: str | None,
    tool_uses: list[Any],
) -> list[dict[str, Any]]:
    """Build response content: the visible answer, then each tool call."""
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in tool_uses:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.input,
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def stop_reason(tool_uses: list[Any]) -> str:
    return "tool_use" if tool_uses else "end_turn"


def message_response(
    *,
    response_id: str,
    model: str,
    text: str | None,
    tool_uses: list[Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks(text, tool_uses),
        "stop_reason": stop_reason(tool_uses),
        "stop_sequence": None,
        "usage": anthropic_usage(usage),
    }


def estimated_input_tokens(body: CountTokensIn) -> int:
    """Approximate a prompt's token count.

    The bridge has no access to Claude's tokenizer, and claude.ai reports usage
    only after a turn has run. Clients call this endpoint to size a context
    window before sending, so an approximation from character length is more
    useful than an error; it is documented as an estimate and never presented
    as an upstream measurement.
    """
    characters = len(system_text(body.system))
    for message in body.messages:
        content = message.get("content")
        if isinstance(content, str):
            characters += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind in TEXT_BLOCK_TYPES:
                    characters += len(_block_text(block))
                elif kind == "tool_use":
                    characters += len(
                        json.dumps(block.get("input") or {}, ensure_ascii=False)
                    )
                elif kind == "tool_result":
                    characters += len(_tool_result_text(block))
    for tool in body.tools or []:
        if isinstance(tool, dict):
            characters += len(json.dumps(tool, ensure_ascii=False))
    return max(1, round(characters / 4))
