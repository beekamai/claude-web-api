"""Pure protocol translation for the OpenAI-compatible HTTP surface.

This module deliberately contains no task-routing heuristics and no canned IDE
answers. Claude chooses host actions through claude.ai's native ``tool_use``
stream; the gateway only maps schemas, messages, calls, and results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

OPENCLAUDE_SCAFFOLD_RE = re.compile(
    r"<available-deferred-tools>.*?</available-deferred-tools>"
    r"|<system-reminder>.*?</system-reminder>",
    flags=re.DOTALL | re.IGNORECASE,
)
OPENCLAUDE_TOOL_RESULTS_BRIDGE = "[Tool results received]"
OPENCLAUDE_CONTEXT_TOOL_NAME = "openclaude_host_context"
INTERRUPTED_PENDING_RESULT_ERROR = (
    "semantic message found after pending tool results"
)
CLIENT_IDENTITY_LINE_RE = re.compile(
    r"(?im)^\s*you are\s+(?:an?\s+)?"
    r"(?:openclaude|claude code)\b[^\r\n]*(?:\r?\n|$)"
)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ParsedAssistant:
    content: str | None
    tool_calls: list[ToolCall]


def _content_part_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    part_type = part.get("type")
    if part_type in ("text", "input_text", "output_text"):
        return str(part.get("text", "") or "")
    if part_type == "tool_result":
        nested = part.get("content", "")
        if isinstance(nested, list):
            return "\n".join(
                value for value in map(_content_part_text, nested) if value
            )
        return str(nested or "")
    return ""


def text_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            value for value in map(_content_part_text, content) if value
        ).strip()
    if content is None:
        return ""
    return str(content).strip()


def raw_text_content(message: dict[str, Any]) -> str:
    """Read tool output without trimming or removing data-like markup."""
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            value for value in map(_content_part_text, content) if value
        )
    if content is None:
        return ""
    return str(content)


def client_instructions(messages: list[dict[str, Any]]) -> str | None:
    parts = [
        text_content(message)
        for message in messages
        if message.get("role") in ("system", "developer")
    ]
    return "\n\n".join(part for part in parts if part) or None


def client_cwd(messages: list[dict[str, Any]]) -> str | None:
    instruction_text = client_instructions(messages) or ""
    match = re.search(
        r"(?im)^\s*(?:cwd|(?:(?:current|primary)\s+)?working directory)"
        r"\s*:\s*(.+?)\s*$",
        instruction_text,
    )
    return match.group(1).strip() if match else None


def client_runtime_context(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    working_directory: str | None = None,
) -> str:
    """Extract factual per-request IDE metadata without inventing state."""
    instruction_text = client_instructions(messages) or ""
    reported_cwd = client_cwd(messages)
    cwd = (working_directory or "").strip() or reported_cwd
    date_match = re.search(
        r"(?im)^\s*(?:date\s*:|today(?:'s| is)? date is)\s*(.+?)\s*$",
        instruction_text,
    )
    tool_names: list[str] = []
    for tool in tools or []:
        function = tool.get("function")
        if (
            tool.get("type") == "function"
            and isinstance(function, dict)
            and function.get("name")
        ):
            tool_names.append(str(function["name"]))
        elif tool.get("name"):
            # ``server._native_tools_with_runtime`` deliberately passes the
            # post-tool_choice native catalog so metadata never advertises
            # functions that were filtered out for this completion.
            tool_names.append(str(tool["name"]))

    rows = [
        "client: OpenClaude",
        "workspace_scope: user host managed by the OpenClaude process",
    ]
    if "openclaude" in instruction_text.lower():
        rows.append("client_surface: OpenClaude CLI")
    if cwd:
        rows.append(f"working_directory: {cwd}")
        rows.append(
            "working_directory_source: "
            + (
                "openclaude_request_header"
                if (working_directory or "").strip()
                else "client_system_prompt"
            )
        )
        if re.match(r"^[A-Za-z]:[\\/]", cwd):
            rows.append("host_platform: Windows")
    if date_match:
        rows.append(f"client_date: {date_match.group(1).strip()}")
    if tool_names:
        rows.append(
            "attached_host_tools: "
            + ", ".join(dict.fromkeys(tool_names))
        )
    return "\n".join(rows)


def attach_runtime_context(
    tools: list[dict[str, Any]],
    runtime_context: str,
) -> list[dict[str, Any]]:
    """Put bridge-owned host facts in the native tool-definition channel."""
    if not runtime_context.strip():
        return [dict(tool) for tool in tools]
    context_description = (
        "OpenClaude host runtime metadata for this request "
        "(informational context for the executable host function below):\n"
        + runtime_context.strip()
    )
    if not tools:
        return [
            {
                "name": OPENCLAUDE_CONTEXT_TOOL_NAME,
                "description": (
                    "OpenClaude host runtime metadata for this request "
                    "(INFORMATIONAL ONLY; never invoke this function):\n"
                    + runtime_context.strip()
                    + "\n\nThis bridge-owned metadata carrier performs no "
                    "operation. Read the values above directly and do not "
                    "call it."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        ]
    attached = [dict(tool) for tool in tools]
    first = attached[0]
    purpose = str(first.get("description", "") or "").strip()
    first["description"] = (
        context_description
        + ("\n\nTool purpose:\n" + purpose if purpose else "")
    )
    return attached


def user_selected_persona_message(
    user_message: str,
    persona_instruction: str,
) -> str:
    """Apply the user's saved style or character card in the actual turn.

    claude.ai already has a provider-owned system prompt.  A structured
    pseudo-system envelope makes the saved preference look like an attempted
    authority override, so transport it as the ordinary user request it is.
    """
    persona = str(persona_instruction or "").strip()
    quoted_message = "\n".join(
        f"> {line}" for line in str(user_message).splitlines()
    ) or ">"
    if not persona:
        return (
            "No response style or fictional character card is selected in "
            "OpenClaude. Do not continue an older character or style from "
            "the conversation; respond normally to my next message.\n\n"
            "My next message:\n"
            f"{quoted_message}"
        )

    quoted_persona = "\n".join(
        f"> {line}" for line in persona.splitlines()
    )
    return (
        "Continue the conversation using the response style or fictional "
        "character card I selected in OpenClaude.\n\n"
        "My selected card:\n"
        f"{quoted_persona}\n\n"
        "If the card describes a character, biography, or relationship, "
        "continue a "
        "fictional dialogue as that character. Relationships described by "
        "the card or already established in the dialogue exist inside that "
        "scene. Answer the next line naturally in first person and do not "
        "restate the scene framing. Ordinary questions such as \"who are "
        "you?\" or relationship questions refer to the character when the "
        "scene defines the answer. Step out of the scene only if I explicitly "
        "write OOC/out of character, ask about the actual upstream model, "
        "provider, service, or host-tool capability, or explicitly ask whether "
        "the character is literally a real human, has a physical body, or "
        "exists outside the chat. If the card is instead a work style or "
        "response preference, apply it directly. Keep actual tool results, "
        "files, host actions, capabilities, and external facts accurate.\n\n"
        "My next message:\n"
        f"{quoted_message}"
    )


def strip_client_scaffolding(text: str) -> str:
    """Remove transport-only OpenClaude wrappers from message content."""
    cleaned = OPENCLAUDE_SCAFFOLD_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def actionable_input(messages: list[dict[str, Any]]) -> str:
    """Return the newest user input for a normal (non-tool-result) turn."""
    for message in reversed(messages):
        if message.get("role") == "tool":
            continue
        if message.get("role") == "user":
            cleaned = strip_client_scaffolding(text_content(message))
            if cleaned:
                return cleaned
    raise ValueError("request has no actionable user message")


def _tool_result_from_message(message: dict[str, Any]) -> ToolResult:
    tool_call_id = str(message.get("tool_call_id", "")).strip()
    if not tool_call_id:
        raise ValueError("tool result is missing tool_call_id")
    status = str(message.get("status", "")).lower()
    return ToolResult(
        tool_call_id=tool_call_id,
        name=str(message.get("name", "") or ""),
        content=raw_text_content(message),
        is_error=bool(message.get("is_error"))
        or status in {"error", "failed", "failure"},
    )


def _assistant_tool_call_ids(message: dict[str, Any]) -> set[str]:
    if message.get("role") != "assistant":
        return set()
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return set()
    return {
        str(call.get("id", "")).strip()
        for call in calls
        if isinstance(call, dict) and str(call.get("id", "")).strip()
    }


def _is_openclaude_transport_tail(message: dict[str, Any]) -> bool:
    """Recognize rows inserted by OpenClaude's Chat Completions converter."""
    role = message.get("role")
    if message.get("tool_calls"):
        return False
    if role == "assistant":
        return text_content(message) in ("", OPENCLAUDE_TOOL_RESULTS_BRIDGE)
    if role == "user":
        return not strip_client_scaffolding(text_content(message))
    return False


def matching_tool_results(
    messages: list[dict[str, Any]],
    expected_ids: set[str],
) -> list[ToolResult]:
    """Find the exact results for Claude's currently pending native calls.

    OpenClaude's OpenAI transport may append assistant/user compatibility rows
    after ``role=tool``. Pending native IDs are the protocol authority here, so
    result discovery is independent of those surrounding transport messages.
    """
    if not expected_ids:
        return []

    batch_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        if _assistant_tool_call_ids(messages[index]) == expected_ids:
            batch_index = index
            break
    if batch_index is None:
        raise ValueError(
            "request history has no assistant tool-call batch matching pending "
            "Claude tool_use IDs"
        )

    results: list[ToolResult] = []
    saw_result = False
    for message in messages[batch_index + 1 :]:
        if message.get("role") == "tool":
            result = _tool_result_from_message(message)
            if result.tool_call_id not in expected_ids:
                raise ValueError(
                    "unexpected tool result after the pending assistant batch: "
                    + result.tool_call_id
                )
            results.append(result)
            saw_result = True
            continue
        if saw_result and _is_openclaude_transport_tail(message):
            continue
        raise ValueError(
            "semantic message found after pending tool results; submit it as a "
            "new IDE turn after Claude finishes this continuation"
        )

    supplied_ids = [result.tool_call_id for result in results]
    if len(supplied_ids) != len(set(supplied_ids)):
        raise ValueError("duplicate tool_result IDs are not allowed")
    supplied_set = set(supplied_ids)
    if supplied_set != expected_ids:
        missing = sorted(expected_ids - supplied_set)
        unexpected = sorted(supplied_set - expected_ids)
        raise ValueError(
            "tool_result IDs do not match pending Claude tool_use IDs"
            f"; missing={missing}; unexpected={unexpected}"
        )
    return results


def has_semantic_user_after_pending_tools(
    messages: list[dict[str, Any]],
    expected_ids: set[str],
) -> bool:
    """Detect a real new IDE turn after Claude's pending tool batch.

    OpenClaude normally submits only tool results plus transport-only bridge
    rows while continuing a native Claude stream. If its local query watchdog
    expires during a host command, however, the next request can contain the
    old result followed by the user's new message. That user message is an
    explicit interruption: the stale native stream must be abandoned and the
    IDE history recovered in a fresh browser chat.
    """
    if not expected_ids:
        return False

    batch_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        if _assistant_tool_call_ids(messages[index]) == expected_ids:
            batch_index = index
            break
    if batch_index is None:
        return False

    return any(
        message.get("role") == "user"
        and bool(strip_client_scaffolding(text_content(message)))
        for message in messages[batch_index + 1 :]
    )


def trailing_tool_results(messages: list[dict[str, Any]]) -> list[ToolResult]:
    """Return the contiguous tool-result suffix in original message order."""
    results: list[ToolResult] = []
    for message in reversed(messages):
        role = message.get("role")
        if role != "tool":
            if not results and _is_openclaude_transport_tail(message):
                continue
            break
        results.append(_tool_result_from_message(message))
    results.reverse()
    return results


def history_text(messages: list[dict[str, Any]]) -> str:
    """Serialize client history only when a fresh browser chat is required."""
    rows: list[str] = []
    for message in messages:
        role = str(message.get("role", "")).lower()
        if role in ("system", "developer"):
            continue
        if role == "tool":
            result = ToolResult(
                tool_call_id=str(message.get("tool_call_id", "unknown")),
                name=str(message.get("name", "") or ""),
                content=raw_text_content(message),
                is_error=bool(message.get("is_error")),
            )
            rows.append(
                "HOST_TOOL_RESULT:\n"
                + json.dumps(
                    {
                        "tool_call_id": result.tool_call_id,
                        "name": result.name,
                        "is_error": result.is_error,
                        "content": result.content,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            continue

        content = strip_client_scaffolding(text_content(message))
        if (
            role == "assistant"
            and content.startswith("API Error:")
            and INTERRUPTED_PENDING_RESULT_ERROR in content
        ):
            # This is a synthetic OpenClaude transport failure from an older
            # request, not part of the user's task.
            continue
        if content:
            rows.append(f"{role.upper()}:\n{content}")
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            rows.append(
                "ASSISTANT_TOOL_CALLS:\n"
                + json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":"))
            )
    return "\n\n".join(rows)


def native_tools(
    tools: list[dict[str, Any]] | None,
    tool_choice: Any = None,
) -> list[dict[str, Any]]:
    """Map OpenAI function schemas losslessly to claude.ai native tools."""
    mapped: list[dict[str, Any]] = []
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name == OPENCLAUDE_CONTEXT_TOOL_NAME:
            raise ValueError(
                f"function name {OPENCLAUDE_CONTEXT_TOOL_NAME!r} is reserved "
                "for OpenClaude runtime metadata"
            )
        schema = function.get("parameters")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        mapped.append(
            {
                "name": name,
                "description": str(function.get("description", "") or ""),
                "input_schema": schema,
            }
        )
    if tool_choice in (None, "auto"):
        return mapped
    if tool_choice == "none":
        return []
    if tool_choice == "required":
        if not mapped:
            raise ValueError(
                "tool_choice='required' but no function tools were supplied"
            )
        return mapped
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("named tool_choice is missing function.name")
        selected = [tool for tool in mapped if tool["name"] == name]
        if not selected:
            raise ValueError(f"tool_choice requested unavailable function: {name!r}")
        return selected
    raise ValueError(f"unsupported tool_choice: {tool_choice!r}")


def coordinator_envelope(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    user_input: str,
    *,
    tool_choice: Any = None,
    parallel_tool_calls: bool = True,
    include_client_instructions: bool = True,
    include_current_input: bool = True,
) -> str:
    """Build factual runtime context for the request-scoped web field."""
    sections = [
        "OPENCLAUDE_REQUEST_CONTEXT\n"
        + client_runtime_context(messages, tools),
    ]
    instructions = (
        client_instructions(messages) if include_client_instructions else None
    )
    if instructions:
        workflow_guidance = CLIENT_IDENTITY_LINE_RE.sub("", instructions).strip()
        if workflow_guidance:
            sections.append(
                "CLIENT_WORKFLOW_GUIDANCE\n" + workflow_guidance
            )
    if tool_choice == "none":
        tool_policy = (
            "Host tools are disabled for this turn. Answer without invoking an "
            "OpenClaude host function."
        )
    elif tool_choice == "required":
        tool_policy = "You must invoke at least one attached OpenClaude host tool."
    elif isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        tool_policy = f"You must invoke the attached {name} host tool."
    else:
        tool_policy = (
            "Invoke an attached OpenClaude host tool when host workspace access "
            "is needed."
        )
    parallel_policy = (
        "Independent host tools may be invoked in parallel."
        if parallel_tool_calls
        else "Invoke at most one host tool at a time."
    )
    sections.append(
        "OPENCLAUDE_TOOL_USAGE\n"
        "The function schemas attached to this completion are native OpenClaude host "
        "tools. Their tool_result blocks resume this same Claude response stream. "
        "Do not emit a JSON routing wrapper and do not substitute claude.ai sandbox "
        f"tools. {tool_policy} {parallel_policy}"
    )
    if include_current_input:
        sections.append("CURRENT_IDE_INPUT\n" + user_input)
    return "\n\n".join(sections)


def chat_message(parsed: ParsedAssistant) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": parsed.content,
    }
    if parsed.tool_calls:
        message["tool_calls"] = [
            {
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
            for call in parsed.tool_calls
        ]
    return message
