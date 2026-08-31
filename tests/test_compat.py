from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_TEST_TELEMETRY_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="openclaude-telemetry-tests-"
)
os.environ["OPENCLAUDE_TELEMETRY_DB"] = str(
    Path(_TEST_TELEMETRY_DIRECTORY.name) / "telemetry.sqlite3"
)

import claude_web_api.app as server
from claude_web_api.control.config import (
    CONFIG_VERSION,
    SUPPORTED_PROFILE_PROVIDERS,
    ControlConfig,
    compile_custom_persona,
    compile_custom_persona_details,
)
from claude_web_api.paths import PROJECT_INSTRUCTIONS, WEB_ROOT
from claude_web_api.protocol.openai import (
    OPENCLAUDE_CONTEXT_TOOL_NAME,
    ParsedAssistant,
    ToolCall,
    actionable_input,
    attach_runtime_context,
    chat_message,
    client_runtime_context,
    coordinator_envelope,
    has_semantic_user_after_pending_tools,
    history_text,
    matching_tool_results,
    native_tools,
    trailing_tool_results,
    user_selected_persona_message,
)
from claude_web_api.session.claude import (
    MODEL_SELECTOR_TRANSIENT_REASONS,
    ClaudeAccountIdentityError,
    ClaudeBrowserUnavailableError,
    ClaudeCompletionRejectedError,
    ClaudeConversationLimitError,
    ClaudeServiceUnavailableError,
    ClaudeSession,
    ClaudeTurnOutcomeUnknownError,
    ClaudeUsageLimitError,
    NativeToolUse,
    NativeTurn,
)
from claude_web_api.telemetry.store import TelemetryStore, stable_session_key

SYSTEM = {
    "role": "system",
    "content": (
        "You are OpenClaude, a local coding agent.\n"
        "CWD: D:\\CodeWorks\\claude-web-api\n"
        "Date: 2026-07-25"
    ),
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read one host file without changing it.",
            "parameters": {
                "type": "object",
                "title": "ReadInput",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or workspace-relative path.",
                        "examples": ["README.md"],
                    }
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Execute a command in the host workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


class TranslationTests(unittest.TestCase):
    def test_runtime_context_is_factual(self) -> None:
        context = client_runtime_context([SYSTEM], TOOLS)
        self.assertIn(r"D:\CodeWorks\claude-web-api", context)
        self.assertIn("host_platform: Windows", context)
        self.assertIn("Read, Bash", context)
        self.assertIn("2026-07-25", context)

    def test_runtime_context_reads_current_openclaude_environment_format(
        self,
    ) -> None:
        context = client_runtime_context(
            [
                {
                    "role": "system",
                    "content": (
                        "OpenClaude environment\n"
                        "Primary working directory: D:\\CodeWorks\\test\n"
                        "Today's date is 2026-07-26"
                    ),
                }
            ],
            None,
        )
        self.assertIn(r"working_directory: D:\CodeWorks\test", context)
        self.assertIn(
            "working_directory_source: client_system_prompt",
            context,
        )
        self.assertIn("client_date: 2026-07-26", context)

    def test_explicit_working_directory_overrides_prompt_heuristic(
        self,
    ) -> None:
        context = client_runtime_context(
            [SYSTEM],
            TOOLS,
            working_directory=r"D:\CodeWorks\explicit",
        )
        self.assertIn(
            r"working_directory: D:\CodeWorks\explicit",
            context,
        )
        self.assertNotIn(r"D:\CodeWorks\claude-web-api", context)
        self.assertIn(
            "working_directory_source: openclaude_request_header",
            context,
        )

    def test_native_tools_preserve_full_schema_and_description(self) -> None:
        mapped = native_tools(TOOLS)
        self.assertEqual(["Read", "Bash"], [tool["name"] for tool in mapped])
        self.assertEqual(
            "Read one host file without changing it.",
            mapped[0]["description"],
        )
        schema = mapped[0]["input_schema"]
        self.assertEqual("ReadInput", schema["title"])
        self.assertEqual(
            ["README.md"],
            schema["properties"]["file_path"]["examples"],
        )
        self.assertFalse(schema["additionalProperties"])

    def test_runtime_context_is_attached_to_native_tool_description(self) -> None:
        mapped = native_tools(TOOLS)
        contextual = attach_runtime_context(
            mapped,
            "working_directory: D:\\CodeWorks\\project",
        )
        self.assertEqual(
            "Read one host file without changing it.",
            mapped[0]["description"],
        )
        self.assertIn(
            r"working_directory: D:\CodeWorks\project",
            contextual[0]["description"],
        )
        self.assertNotIn("never invoke", contextual[0]["description"])
        self.assertIn("Tool purpose:", contextual[0]["description"])
        self.assertEqual(mapped[1]["description"], contextual[1]["description"])

    def test_empty_catalog_gets_informational_runtime_context_tool(
        self,
    ) -> None:
        contextual = attach_runtime_context(
            [],
            "working_directory: D:\\CodeWorks\\project",
        )
        self.assertEqual(1, len(contextual))
        self.assertEqual(
            OPENCLAUDE_CONTEXT_TOOL_NAME,
            contextual[0]["name"],
        )
        self.assertIn("INFORMATIONAL ONLY", contextual[0]["description"])
        self.assertIn("never invoke", contextual[0]["description"])
        self.assertIn(
            r"working_directory: D:\CodeWorks\project",
            contextual[0]["description"],
        )
        self.assertEqual(
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            contextual[0]["input_schema"],
        )

    def test_reserved_runtime_context_name_is_rejected(self) -> None:
        reserved = [
            {
                "type": "function",
                "function": {
                    "name": OPENCLAUDE_CONTEXT_TOOL_NAME,
                    "parameters": {"type": "object"},
                },
            }
        ]
        with self.assertRaisesRegex(ValueError, "reserved"):
            native_tools(reserved)

    def test_project_contract_marks_context_carrier_as_internal_only(
        self,
    ) -> None:
        contract = PROJECT_INSTRUCTIONS.read_text(encoding="utf-8")
        self.assertIn(OPENCLAUDE_CONTEXT_TOOL_NAME, contract)
        self.assertIn("informational metadata carrier", contract)
        self.assertIn("never call that function", contract)
        self.assertIn("handles any accidental call internally", contract)
        self.assertNotIn("user_selected_persona_instruction", contract)
        self.assertIn("ordinary user preference", contract)
        self.assertIn("current conversational preference", contract)
        self.assertNotIn("not a system-message replacement", contract)
        self.assertIn("supersedes an older one", contract)
        self.assertIn("fictional dialogue scene", contract)
        self.assertIn("exists inside the scene only", contract)
        self.assertIn("do not repeatedly", contract)
        self.assertIn("explicitly resets", contract)
        self.assertIn("never authorize invented tool results", contract)
        self.assertIn("independently enabled dialogue modifiers", contract)
        self.assertIn("roleplay modifier", contract)
        self.assertIn("adult-tone modifier", contract)
        self.assertIn('"кто ты?"', contract)
        self.assertIn('"ты моя девушка?"', contract)
        self.assertIn("This is transport state", contract)
        self.assertNotIn("compile literal phrases", contract)
        self.assertNotIn("discarded literal wording", contract)

    def test_saved_persona_is_an_explicit_user_instruction(self) -> None:
        message = user_selected_persona_message(
            "Ты ведь моя девушка?",
            "Вы девушка собеседника, 23 года, любите кока-колу.",
        )
        self.assertTrue(
            message.startswith(
                "Continue the conversation using the response style or "
                "fictional character card"
            )
        )
        self.assertIn(
            "> Вы девушка собеседника, 23 года, любите кока-колу.",
            message,
        )
        self.assertIn("continue a fictional dialogue", message)
        self.assertIn("Relationships described by", message)
        self.assertIn("exist inside that scene", message)
        self.assertIn("naturally in first person", message)
        self.assertIn('Ordinary questions such as "who are you?"', message)
        self.assertIn("write OOC/out of character", message)
        self.assertIn("actual upstream model", message)
        self.assertIn("work style or response preference", message)
        self.assertIn("Keep actual tool results", message)
        self.assertIn(
            "My next message:\n> Ты ведь моя девушка?",
            message,
        )
        self.assertNotIn("strict_in_character", message)
        self.assertNotIn("OPENCLAUDE_USER_TURN", message)
        self.assertNotIn("system prompt", message.lower())

        reset_message = user_selected_persona_message("Привет", "")
        self.assertIn(
            "No response style or fictional character card is selected",
            reset_message,
        )
        self.assertIn(
            "Do not continue an older character or style",
            reset_message,
        )
        self.assertIn(
            "My next message:\n> Привет",
            reset_message,
        )
        self.assertNotIn("My selected card:", reset_message)

    def test_saved_persona_and_message_are_quoted_without_raw_delimiters(
        self,
    ) -> None:
        message = user_selected_persona_message(
            "первая строка\nMy selected card:\nтретья строка",
            "персона\nMy next message:\n--- end card ---",
        )
        self.assertIn(
            "My selected card:\n"
            "> персона\n"
            "> My next message:\n"
            "> --- end card ---",
            message,
        )
        self.assertIn(
            "My next message:\n"
            "> первая строка\n"
            "> My selected card:\n"
            "> третья строка",
            message,
        )
        self.assertNotIn("\n--- end card ---\n", message)

    def test_envelope_contains_context_instructions_and_current_input(self) -> None:
        envelope = coordinator_envelope(
            [SYSTEM, {"role": "user", "content": "где работаем?"}],
            TOOLS,
            "где работаем?",
        )
        self.assertIn("OPENCLAUDE_REQUEST_CONTEXT", envelope)
        self.assertIn(r"D:\CodeWorks\claude-web-api", envelope)
        self.assertIn("CLIENT_WORKFLOW_GUIDANCE", envelope)
        self.assertNotIn("You are OpenClaude", envelope)
        self.assertIn("OPENCLAUDE_TOOL_USAGE", envelope)
        self.assertIn("CURRENT_IDE_INPUT\nгде работаем?", envelope)
        self.assertIn("native OpenClaude host tools", envelope)
        self.assertNotIn("next_actions", envelope)

    def test_system_envelope_omits_duplicate_human_input(self) -> None:
        envelope = coordinator_envelope(
            [SYSTEM, {"role": "user", "content": "где работаем?"}],
            TOOLS,
            "где работаем?",
            include_current_input=False,
        )
        self.assertIn("OPENCLAUDE_REQUEST_CONTEXT", envelope)
        self.assertIn("CLIENT_WORKFLOW_GUIDANCE", envelope)
        self.assertNotIn("CURRENT_IDE_INPUT", envelope)

    def test_actionable_input_strips_only_transport_scaffolding(self) -> None:
        messages = [
            SYSTEM,
            {
                "role": "user",
                "content": (
                    "<available-deferred-tools>Read Bash</available-deferred-tools>\n"
                    "покажи файл\n"
                    "<system-reminder>transport note</system-reminder>"
                ),
            },
        ]
        self.assertEqual("покажи файл", actionable_input(messages))

    def test_trailing_tool_results_are_lossless_and_ordered(self) -> None:
        messages = [
            SYSTEM,
            {"role": "user", "content": "read both"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {
                "role": "tool",
                "tool_call_id": "toolu_1",
                "name": "Read",
                "content": "first",
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_2",
                "name": "Read",
                "content": "second",
                "is_error": True,
            },
        ]
        results = trailing_tool_results(messages)
        self.assertEqual(["toolu_1", "toolu_2"], [item.tool_call_id for item in results])
        self.assertEqual(["first", "second"], [item.content for item in results])
        self.assertFalse(results[0].is_error)
        self.assertTrue(results[1].is_error)

    def test_tool_result_preserves_whitespace_and_data_like_markup(self) -> None:
        raw = "  <system-reminder>legitimate file text</system-reminder>\n\n"
        results = trailing_tool_results(
            [
                {
                    "role": "tool",
                    "tool_call_id": "toolu_raw",
                    "name": "Read",
                    "content": raw,
                }
            ]
        )
        self.assertEqual(raw, results[0].content)

    def test_tool_result_survives_empty_transport_tail(self) -> None:
        results = trailing_tool_results(
            [
                {
                    "role": "tool",
                    "tool_call_id": "toolu_tail",
                    "name": "Read",
                    "content": "actual output\n",
                },
                {"role": "assistant", "content": ""},
                {
                    "role": "user",
                    "content": "<system-reminder>transport only</system-reminder>",
                },
            ]
        )
        self.assertEqual(["toolu_tail"], [item.tool_call_id for item in results])
        self.assertEqual("actual output\n", results[0].content)

    def test_pending_id_finds_result_through_openclaude_transport_bridge(self) -> None:
        messages = [
            SYSTEM,
            {"role": "user", "content": "read requirements.txt"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_current",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": '{"file_path":"requirements.txt"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_current",
                "content": "camoufox[geoip]>=0.4.11\n",
            },
            {
                "role": "assistant",
                "content": "[Tool results received]",
            },
            {
                "role": "user",
                "content": "<system-reminder>transport only</system-reminder>",
            },
        ]
        results = matching_tool_results(messages, {"toolu_current"})
        self.assertEqual(["toolu_current"], [item.tool_call_id for item in results])
        self.assertEqual("camoufox[geoip]>=0.4.11\n", results[0].content)

    def test_pending_id_ignores_unrelated_historical_results(self) -> None:
        messages = [
            {
                "role": "tool",
                "tool_call_id": "toolu_old",
                "content": "old",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_current",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_current",
                "content": "current",
            },
        ]
        results = matching_tool_results(messages, {"toolu_current"})
        self.assertEqual(["toolu_current"], [item.tool_call_id for item in results])
        self.assertEqual("current", results[0].content)

    def test_pending_result_rejects_semantic_user_tail(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_current",
                        "type": "function",
                        "function": {"name": "Read", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_current",
                "content": "output",
            },
            {"role": "assistant", "content": "[Tool results received]"},
            {"role": "user", "content": "не продолжай, сделай другое"},
        ]
        with self.assertRaisesRegex(ValueError, "semantic message"):
            matching_tool_results(messages, {"toolu_current"})
        self.assertTrue(
            has_semantic_user_after_pending_tools(
                messages,
                {"toolu_current"},
            )
        )

    def test_transport_only_tail_is_not_a_semantic_interruption(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_current",
                        "type": "function",
                        "function": {"name": "Read", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_current",
                "content": "output",
            },
            {"role": "assistant", "content": "[Tool results received]"},
            {
                "role": "user",
                "content": "<system-reminder>transport only</system-reminder>",
            },
        ]
        self.assertFalse(
            has_semantic_user_after_pending_tools(
                messages,
                {"toolu_current"},
            )
        )

    def test_history_omits_old_pending_transport_error(self) -> None:
        history = history_text(
            [
                {"role": "user", "content": "old task"},
                {
                    "role": "assistant",
                    "content": (
                        "API Error: 200 "
                        '{"error":{"message":"semantic message found after '
                        'pending tool results"}}'
                    ),
                },
                {"role": "user", "content": "new task"},
            ]
        )
        self.assertIn("old task", history)
        self.assertIn("new task", history)
        self.assertNotIn("API Error:", history)

    def test_transport_bridge_is_a_stale_result_suffix_without_pending_ids(
        self,
    ) -> None:
        messages = [
            {
                "role": "tool",
                "tool_call_id": "toolu_stale",
                "content": "already handled",
            },
            {"role": "assistant", "content": "[Tool results received]"},
            {
                "role": "user",
                "content": "<system-reminder>transport only</system-reminder>",
            },
        ]
        results = trailing_tool_results(messages)
        self.assertEqual(["toolu_stale"], [item.tool_call_id for item in results])

    def test_tool_choice_filters_native_catalog(self) -> None:
        self.assertEqual([], native_tools(TOOLS, "none"))
        selected = native_tools(
            TOOLS,
            {"type": "function", "function": {"name": "Read"}},
        )
        self.assertEqual(["Read"], [tool["name"] for tool in selected])

    def test_history_keeps_tool_calls_and_observed_results(self) -> None:
        history = history_text(
            [
                SYSTEM,
                {"role": "user", "content": "read it"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": '{"file_path":"a.txt"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_1",
                    "name": "Read",
                    "content": "actual file content",
                },
            ]
        )
        self.assertNotIn("You are OpenClaude", history)
        self.assertIn("ASSISTANT_TOOL_CALLS", history)
        self.assertIn("HOST_TOOL_RESULT", history)
        self.assertIn("actual file content", history)

    def test_chat_message_preserves_native_tool_id_and_arguments(self) -> None:
        message = chat_message(
            ParsedAssistant(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="toolu_native",
                        name="Read",
                        arguments={"file_path": r"D:\CodeWorks\a.txt"},
                    )
                ],
            )
        )
        self.assertEqual("toolu_native", message["tool_calls"][0]["id"])
        self.assertEqual(
            {"file_path": r"D:\CodeWorks\a.txt"},
            json.loads(message["tool_calls"][0]["function"]["arguments"]),
        )


class NativeSseParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = ClaudeSession(headless=True)
        self.session._native_tools = native_tools(TOOLS)

    @staticmethod
    def event(name: str, payload: dict) -> dict[str, str]:
        return {
            "event": name,
            "data": json.dumps(payload, ensure_ascii=False),
        }

    def feed_tool(
        self,
        index: int,
        tool_id: str,
        name: str,
        fragments: list[str],
    ) -> None:
        self.session._process_native_event(
            self.event(
                "content_block_start",
                {
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": {},
                    },
                },
            )
        )
        for fragment in fragments:
            self.session._process_native_event(
                self.event(
                    "content_block_delta",
                    {
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": fragment,
                        },
                    },
                )
            )
        self.session._process_native_event(
            self.event("content_block_stop", {"index": index})
        )

    def test_native_tool_input_is_reassembled_without_mutation(self) -> None:
        self.feed_tool(
            1,
            "toolu_exact",
            "Bash",
            ['{"command":"git ', 'status --short"}'],
        )
        tool = self.session._native_tool_blocks[1]
        self.assertEqual("toolu_exact", tool.id)
        self.assertEqual("Bash", tool.name)
        self.assertEqual({"command": "git status --short"}, tool.input)

    def test_multiple_native_tool_blocks_are_not_lost(self) -> None:
        self.feed_tool(1, "toolu_1", "Read", ['{"file_path":"a.txt"}'])
        self.feed_tool(2, "toolu_2", "Read", ['{"file_path":"b.txt"}'])
        tools = [
            tool
            for _, tool in sorted(self.session._native_tool_blocks.items())
        ]
        self.assertEqual(["toolu_1", "toolu_2"], [tool.id for tool in tools])
        self.assertEqual(
            [{"file_path": "a.txt"}, {"file_path": "b.txt"}],
            [tool.input for tool in tools],
        )

    def test_browser_owned_tool_is_ignored_for_ui_to_execute(self) -> None:
        self.feed_tool(1, "toolu_web", "web_search", ['{"query":"docs"}'])
        self.assertEqual({}, self.session._native_tool_blocks)

    def test_default_sse_event_uses_payload_type(self) -> None:
        self.session._process_native_event(
            self.event(
                "message",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_default",
                        "name": "Read",
                        "input": {},
                    },
                },
            )
        )
        self.session._process_native_event(
            self.event(
                "message",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"file_path":"a.txt"}',
                    },
                },
            )
        )
        self.session._process_native_event(
            self.event(
                "message",
                {"type": "content_block_stop", "index": 1},
            )
        )
        self.assertEqual(
            {"file_path": "a.txt"},
            self.session._native_tool_blocks[1].input,
        )

    def test_retraction_removes_buffered_tool_block(self) -> None:
        self.feed_tool(3, "toolu_3", "Read", ['{"file_path":"old.txt"}'])
        self.session._process_native_event(
            self.event("content_block_retract", {"from_index": 3})
        )
        self.assertEqual({}, self.session._native_tool_blocks)

    def test_message_limit_raises_usage_error(self) -> None:
        with self.assertRaises(ClaudeUsageLimitError) as caught:
            self.session._process_native_event(
                self.event(
                    "message_limit",
                    {"message_limit": {"status": "exceeded"}},
                )
            )
        self.assertTrue(caught.exception.replay_safe)

    def test_limit_after_tool_start_is_not_replay_safe(self) -> None:
        self.session._process_native_event(
            self.event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_started",
                        "name": "Read",
                        "input": {},
                    },
                },
            )
        )
        with self.assertRaises(ClaudeUsageLimitError) as caught:
            self.session._process_native_event(
                self.event(
                    "message_limit",
                    {"message_limit": {"status": "exceeded"}},
                )
            )
        self.assertFalse(caught.exception.replay_safe)

    def test_thinking_summary_and_usage_are_emitted_without_signatures(self) -> None:
        emitted: list[dict] = []
        self.session._native_event_sink = emitted.append
        self.session._process_native_event(
            self.event(
                "message_start",
                {
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 12, "output_tokens": 0},
                    }
                },
            )
        )
        self.session._process_native_event(
            self.event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "thinking",
                        "thinking": "",
                        "signature": "must-not-leak",
                    },
                },
            )
        )
        self.session._process_native_event(
            self.event(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {
                        "type": "thinking_summary_delta",
                        "summary": "Проверяю проект.",
                        "signature": "must-not-leak",
                    },
                },
            )
        )
        self.session._process_native_event(
            self.event("content_block_stop", {"index": 0})
        )
        self.session._process_native_event(
            self.event(
                "message_delta",
                {"usage": {"input_tokens": 12, "output_tokens": 7}},
            )
        )
        self.assertEqual(
            "Проверяю проект.",
            self.session._take_native_thinking(),
        )
        self.assertEqual("claude-sonnet-5", self.session._native_model)
        self.assertEqual(7, self.session._native_usage["output_tokens"])
        serialized = json.dumps(emitted, ensure_ascii=False)
        self.assertIn("Проверяю проект.", serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_redacted_thinking_data_is_never_exposed(self) -> None:
        emitted: list[dict] = []
        self.session._native_event_sink = emitted.append
        self.session._process_native_event(
            self.event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "redacted_thinking",
                        "data": "opaque-provider-secret",
                    },
                },
            )
        )
        self.session._process_native_event(
            self.event("content_block_stop", {"index": 0})
        )
        self.assertIsNone(self.session._take_native_thinking())
        self.assertNotIn(
            "opaque-provider-secret",
            json.dumps(emitted, ensure_ascii=False),
        )

    def test_raw_thinking_delta_is_never_exposed(self) -> None:
        emitted: list[dict] = []
        self.session._native_event_sink = emitted.append
        self.session._process_native_event(
            self.event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "thinking",
                        "thinking": "raw-start",
                    },
                },
            )
        )
        self.session._process_native_event(
            self.event(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "raw-delta",
                    },
                },
            )
        )
        self.session._process_native_event(
            self.event("content_block_stop", {"index": 0})
        )
        self.assertIsNone(self.session._take_native_thinking())
        serialized = json.dumps(emitted, ensure_ascii=False)
        self.assertNotIn("raw-start", serialized)
        self.assertNotIn("raw-delta", serialized)

    def test_tool_boundary_does_not_emit_cumulative_usage(self) -> None:
        self.session._native_usage = {
            "input_tokens": 10,
            "output_tokens": 2,
        }
        self.feed_tool(1, "toolu_usage", "Read", ['{"file_path":"a"}'])
        turn = self.session._take_native_tools_if_ready()
        self.assertIsNotNone(turn)
        self.assertEqual({}, turn.usage)

    def test_text_whitespace_is_preserved(self) -> None:
        self.session._process_native_event(
            self.event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "text", "text": "  "},
                },
            )
        )
        self.session._process_native_event(
            self.event(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hello\n"},
                },
            )
        )
        self.session._process_native_event(
            self.event("content_block_stop", {"index": 0})
        )
        self.assertEqual("  hello\n", self.session._take_native_text())


class InternalContextCarrierTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def event(name: str, payload: dict) -> dict[str, str]:
        return {
            "event": name,
            "data": json.dumps(payload, ensure_ascii=False),
        }

    async def test_context_carrier_call_is_auto_acknowledged_and_hidden(
        self,
    ) -> None:
        native_session = ClaudeSession(headless=True)
        carrier = attach_runtime_context(
            [],
            "working_directory: D:\\CodeWorks\\test",
        )[0]
        native_session._reset_native_parser()
        native_session._native_active = True
        native_session._native_tools = [carrier]
        native_session._native_internal_tool_names = {
            OPENCLAUDE_CONTEXT_TOOL_NAME
        }

        await native_session._native_queue.put(
            self.event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "text",
                        "text": "Проверяю. ",
                    },
                },
            )
        )
        await native_session._native_queue.put(
            self.event("content_block_stop", {"index": 0})
        )
        await native_session._native_queue.put(
            self.event(
                "content_block_start",
                {
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_context",
                        "name": OPENCLAUDE_CONTEXT_TOOL_NAME,
                        "input": {},
                    },
                },
            )
        )
        await native_session._native_queue.put(
            self.event("content_block_stop", {"index": 1})
        )

        async def accept_internal_result(result: dict) -> None:
            self.assertEqual(
                OPENCLAUDE_CONTEXT_TOOL_NAME,
                result["name"],
            )
            self.assertEqual("toolu_context", result["tool_call_id"])
            self.assertFalse(result["is_error"])
            self.assertIn(
                r"working_directory: D:\CodeWorks\test",
                result["content"],
            )
            await native_session._native_queue.put(
                self.event(
                    "content_block_start",
                    {
                        "index": 2,
                        "content_block": {
                            "type": "text",
                            "text": "Работаем в D:\\CodeWorks\\test.",
                        },
                    },
                )
            )
            await native_session._native_queue.put(
                self.event("content_block_stop", {"index": 2})
            )
            await native_session._native_queue.put(
                self.event(
                    "message_delta",
                    {"delta": {"stop_reason": "end_turn"}},
                )
            )
            await native_session._native_queue.put(
                self.event("message_stop", {"type": "message_stop"})
            )

        post = AsyncMock(side_effect=accept_internal_result)
        with patch.object(native_session, "_post_tool_result", post):
            turn = await native_session._await_native_outcome(2)

        post.assert_awaited_once()
        self.assertEqual(
            "Проверяю. Работаем в D:\\CodeWorks\\test.",
            turn.content,
        )
        self.assertEqual([], turn.tool_uses)
        self.assertEqual("end_turn", turn.stop_reason)
        self.assertFalse(native_session._native_active)
        self.assertEqual(set(), native_session._native_pending_ids)
        self.assertEqual("idle", native_session._phase)

    async def test_context_carrier_cannot_loop_or_mix_with_host_tools(
        self,
    ) -> None:
        native_session = ClaudeSession(headless=True)
        carrier = attach_runtime_context(
            [],
            "working_directory: D:\\CodeWorks\\test",
        )[0]
        native_session._reset_native_parser()
        native_session._native_tools = [
            carrier,
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {"type": "object"},
            },
        ]
        native_session._native_internal_tool_names = {
            OPENCLAUDE_CONTEXT_TOOL_NAME
        }
        native_session._native_parallel_tool_calls = False
        native_session._native_tool_blocks = {
            0: NativeToolUse(
                id="toolu_context",
                name=OPENCLAUDE_CONTEXT_TOOL_NAME,
                input={},
            ),
            1: NativeToolUse(
                id="toolu_read",
                name="Read",
                input={"file_path": "README.md"},
            ),
        }
        with self.assertRaisesRegex(RuntimeError, "mixed"):
            await native_session._consume_native_tools_if_ready()

        native_session._native_tool_blocks = {
            2: NativeToolUse(
                id="toolu_context_again",
                name=OPENCLAUDE_CONTEXT_TOOL_NAME,
                input={},
            )
        }
        native_session._native_internal_tool_acks = 1
        with self.assertRaisesRegex(RuntimeError, "more than once"):
            await native_session._consume_native_tools_if_ready()

    async def test_cancelled_context_result_is_marked_unknown(
        self,
    ) -> None:
        native_session = ClaudeSession(headless=True)
        carrier = attach_runtime_context(
            [],
            "working_directory: D:\\CodeWorks\\test",
        )[0]
        native_session._reset_native_parser()
        native_session._native_tools = [carrier]
        native_session._native_internal_tool_names = {
            OPENCLAUDE_CONTEXT_TOOL_NAME
        }
        native_session._native_tool_blocks = {
            0: NativeToolUse(
                id="toolu_cancelled_context",
                name=OPENCLAUDE_CONTEXT_TOOL_NAME,
                input={},
            )
        }
        with patch.object(
            native_session,
            "_post_tool_result",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await native_session._consume_native_tools_if_ready()
        self.assertEqual(
            "unknown",
            native_session._tool_result_delivery[
                "toolu_cancelled_context"
            ],
        )


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_sse_bridge_is_installed_before_first_navigation(self) -> None:
        source = inspect.getsource(ClaudeSession.start)
        navigation = source.index("await self._goto_start_page")
        self.assertLess(
            source.index('await self.page.expose_binding('),
            navigation,
        )
        self.assertLess(
            source.index("await self.page.add_init_script(SSE_TAP_SCRIPT)"),
            navigation,
        )

    async def test_explicit_model_requires_discovered_account_catalog(self) -> None:
        native_session = ClaudeSession(headless=True)
        with self.assertRaisesRegex(ValueError, "catalog is unavailable"):
            await native_session.native_chat(
                "hello",
                tools=[],
                model="claude-opus-4-8",
            )

    async def test_model_selector_retry_is_bounded_and_transient_only(
        self,
    ) -> None:
        account_uuid = "11111111-1111-1111-1111-111111111111"
        with patch.dict(
            os.environ,
            {"CLAUDE_MODEL_SELECTOR_WAIT_SECONDS": "45"},
        ):
            native_session = ClaudeSession(headless=True)
        evaluate = AsyncMock(
            return_value={
                "status": 200,
                "hinted": account_uuid,
                "confirmed": account_uuid,
                "profile": {"uuid": account_uuid},
                "selector": {
                    "ok": False,
                    "reason": "identity_hint_mismatch",
                },
            }
        )
        native_session.page = SimpleNamespace(evaluate=evaluate)

        async def immediate(awaitable, *, timeout):
            self.assertEqual(60, timeout)
            return await awaitable

        with patch(
            "claude_web_api.session.claude.asyncio.wait_for",
            AsyncMock(side_effect=immediate),
        ):
            self.assertTrue(await native_session._load_account_identity())

        arguments = evaluate.await_args.args[1]
        self.assertEqual(45_000, arguments["selectorWaitMs"])
        self.assertEqual(
            list(MODEL_SELECTOR_TRANSIENT_REASONS),
            arguments["selectorTransientReasons"],
        )
        transient = set(MODEL_SELECTOR_TRANSIENT_REASONS)
        self.assertIn("selector_cache_empty", transient)
        self.assertIn("selector_query_not_settled", transient)
        self.assertNotIn("identity_hint_mismatch", transient)
        self.assertNotIn("cached_account_mismatch", transient)
        self.assertNotIn("selector_conflict", transient)

    async def test_explicit_model_requires_verified_access_status(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._available_models = [
            {
                "id": "claude-fable-5",
                "available": True,
                "access_status": "unverified",
            }
        ]
        self.assertEqual([], native_session.selectable_models())
        with self.assertRaisesRegex(
            ValueError,
            "not available to the active",
        ):
            await native_session.native_chat(
                "hello",
                tools=[],
                model="claude-fable-5",
            )

    async def test_privacy_change_forces_a_new_remote_chat(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._conversation_client_session_id = "session-a"
        native_session._conversation_privacy_mode = "keep"
        prepare = AsyncMock()
        with (
            patch.object(
                native_session,
                "_prepare_composer_unlocked",
                prepare,
            ),
            patch.object(
                native_session,
                "_submit_message",
                AsyncMock(),
            ),
            patch.object(
                native_session,
                "_await_native_outcome",
                AsyncMock(
                    return_value=NativeTurn(content="ok", tool_uses=[])
                ),
            ),
        ):
            await native_session.native_chat(
                "hello",
                tools=[],
                privacy_mode="ephemeral",
                client_session_id="session-a",
            )
        self.assertTrue(prepare.await_args.kwargs["new_chat"])
        self.assertEqual(
            "ephemeral",
            native_session._conversation_privacy_mode,
        )

    async def test_account_switch_is_blocked_before_prompt_submission(
        self,
    ) -> None:
        account_a = "11111111-1111-1111-1111-111111111111"
        account_b = "22222222-2222-2222-2222-222222222222"
        native_session = ClaudeSession(headless=True)
        native_session.ready = True
        native_session._set_phase("idle")
        native_session._account_uuid = account_a
        native_session._profile_account_uuids["default"] = account_a
        native_session.page = SimpleNamespace(
            is_closed=lambda: False,
            evaluate=AsyncMock(
                return_value={
                    "status": 200,
                    "hinted": account_b,
                    "confirmed": account_b,
                    "profile": {
                        "uuid": account_b,
                        "full_name": "Other account",
                    },
                }
            ),
        )
        submit = AsyncMock()
        with (
            patch.object(native_session, "_submit_message", submit),
            patch.object(native_session, "_ensure_input", AsyncMock()),
        ):
            with self.assertRaises(ClaudeAccountIdentityError):
                await native_session.native_chat("secret IDE prompt", tools=[])
        submit.assert_not_awaited()
        self.assertEqual("account_changed", native_session._phase)
        self.assertFalse(native_session.ready)

    async def test_partial_account_payload_clears_public_identity(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._account_uuid = (
            "11111111-1111-1111-1111-111111111111"
        )
        native_session._account_name = "Old account"
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "status": 200,
                    "hinted": (
                        "22222222-2222-2222-2222-222222222222"
                    ),
                    "confirmed": (
                        "22222222-2222-2222-2222-222222222222"
                    ),
                    "profile": {"unrelated": True},
                }
            )
        )
        self.assertFalse(await native_session._load_account_identity())
        self.assertIsNone(native_session.account_uuid_for_internal_use())
        self.assertFalse(
            native_session.health_snapshot()["account"]["authenticated"]
        )

    async def test_identity_reload_restores_verified_profile_organization(
        self,
    ) -> None:
        account_uuid = "11111111-1111-1111-1111-111111111111"
        organization_uuid = "22222222-2222-2222-2222-222222222222"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "organization_id": organization_uuid,
                }
            ],
        )
        native_session._organization_uuid = organization_uuid
        native_session._clear_account_identity()
        self.assertIsNone(
            native_session.organization_uuid_for_internal_use()
        )
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "status": 200,
                    "hinted": account_uuid,
                    "confirmed": account_uuid,
                    "profile": {"uuid": account_uuid},
                }
            )
        )

        self.assertTrue(await native_session._load_account_identity())
        self.assertEqual(
            organization_uuid,
            native_session.organization_uuid_for_internal_use(),
        )

    async def test_verified_effective_selector_controls_entitlements(
        self,
    ) -> None:
        account_uuid = "11111111-1111-1111-1111-111111111111"
        native_session = ClaudeSession(headless=True)
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "status": 200,
                    "hinted": account_uuid,
                    "confirmed": account_uuid,
                    "profile": {
                        "uuid": account_uuid,
                    },
                    "selector": {
                        "ok": True,
                        "source": "react_query_effective_selector",
                        "identity": {
                            "account_match": True,
                            "organization_query_match": True,
                            "membership_match": True,
                            "cookie_match": True,
                        },
                        "cache": {
                            "age_ms": 50,
                            "data_updated_at": 123456,
                            "status": "success",
                            "fetch_status": "idle",
                        },
                        "config": {
                            "id": "chat",
                            "models": [
                                {
                                    "id": "claude-sonnet-test",
                                    "name": "Sonnet Test",
                                },
                                {
                                    "id": "claude-fable-5",
                                    "name": "Fable 5",
                                    "section": "main",
                                    "disabled_reason": {
                                        "type": "upgrade_required",
                                        "required_plan": "pro",
                                        "title": "Upgrade to use Pro",
                                        "message": (
                                            "This model requires a Pro plan."
                                        ),
                                    },
                                }
                            ],
                        },
                        "state": {
                            "id": "chat",
                            "model": "claude-sonnet-test",
                            "selection_source": "global_default",
                        },
                    },
                }
            )
        )
        self.assertTrue(await native_session._load_account_identity())
        self.assertEqual(
            ["claude-sonnet-test"],
            [
                row["id"]
                for row in native_session.selectable_models()
            ],
        )
        catalog = {
            row["id"]: row
            for row in native_session.health_snapshot()["models"]["available"]
        }
        self.assertFalse(catalog["claude-fable-5"]["available"])
        self.assertEqual(
            {
                "type": "upgrade_required",
                "required_plan": "pro",
                "title": "Upgrade to use Pro",
                "message": "This model requires a Pro plan.",
            },
            catalog["claude-fable-5"]["disabled_reason"],
        )
        self.assertEqual(
            "account_selector",
            catalog["claude-fable-5"]["source"],
        )
        self.assertEqual(
            "claude-sonnet-test",
            native_session.health_snapshot()["models"]["state"]["model"],
        )
        self.assertEqual(
            "global_default",
            native_session.health_snapshot()["models"]["state"][
                "selection_source"
            ],
        )
        self.assertTrue(
            native_session.health_snapshot()["models"]["selector"]["verified"]
        )

    async def test_direct_account_selector_without_verified_cache_is_ignored(
        self,
    ) -> None:
        account_uuid = "11111111-1111-1111-1111-111111111111"
        native_session = ClaudeSession(headless=True)
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "status": 200,
                    "hinted": account_uuid,
                    "confirmed": account_uuid,
                    "profile": {
                        "uuid": account_uuid,
                        "model_selector_config": {
                            "id": "chat",
                            "models": [
                                {
                                    "id": "claude-fable-5",
                                    "name": "Fable 5",
                                }
                            ],
                        },
                    },
                    "selector": {
                        "ok": False,
                        "reason": "selector_cache_missing",
                    },
                }
            )
        )
        self.assertTrue(await native_session._load_account_identity())
        self.assertEqual([], native_session.selectable_models())
        self.assertEqual(
            [],
            native_session.health_snapshot()["models"]["available"],
        )
        self.assertEqual(
            "selector_cache_missing",
            native_session.health_snapshot()["models"]["selector"]["reason"],
        )

    async def test_bootstrap_catalog_never_grants_model_access(
        self,
    ) -> None:
        account_uuid = "11111111-1111-1111-1111-111111111111"
        native_session = ClaudeSession(headless=True)
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "status": 200,
                    "hinted": account_uuid,
                    "confirmed": account_uuid,
                    "profile": {
                        "uuid": account_uuid,
                        "memberships": [
                            {
                                "organization": {
                                    "claude_ai_bootstrap_models_config": [
                                        {
                                            "model": "claude-sonnet-test",
                                            "name": "Sonnet Test",
                                            "thinking_modes": [{"id": "auto"}],
                                        },
                                        {
                                            "model": "claude-opus-old",
                                            "name": "Opus Old",
                                            "inactive": True,
                                        },
                                    ]
                                }
                            }
                        ],
                    },
                    "selector": {
                        "ok": False,
                        "reason": "selector_cache_missing",
                    },
                }
            )
        )
        self.assertTrue(await native_session._load_account_identity())
        health_models = native_session.health_snapshot()["models"]["available"]
        self.assertEqual(2, len(health_models))
        self.assertFalse(health_models[0]["available"])
        self.assertEqual("unverified", health_models[0]["access_status"])
        self.assertEqual("bootstrap_catalog", health_models[0]["source"])
        self.assertEqual("catalog_only", health_models[0]["disabled_reason"])
        self.assertEqual(
            {"modes": [{"id": "auto"}]},
            health_models[0]["thinking"],
        )
        self.assertFalse(health_models[1]["available"])
        self.assertEqual([], native_session.selectable_models())

    async def test_stale_selector_cache_fails_closed(self) -> None:
        account_uuid = "11111111-1111-1111-1111-111111111111"
        native_session = ClaudeSession(headless=True)
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "status": 200,
                    "hinted": account_uuid,
                    "confirmed": account_uuid,
                    "profile": {
                        "uuid": account_uuid,
                        "memberships": [
                            {
                                "organization": {
                                    "claude_ai_bootstrap_models_config": [
                                        {
                                            "model": "claude-fable-5",
                                            "name": "Fable 5",
                                        }
                                    ]
                                }
                            }
                        ],
                    },
                    "selector": {
                        "ok": True,
                        "source": "react_query_effective_selector",
                        "identity": {
                            "account_match": True,
                            "organization_query_match": True,
                            "membership_match": True,
                        },
                        "cache": {
                            "age_ms": (
                                native_session
                                ._model_selector_cache_max_age_ms
                                + 1
                            ),
                            "status": "success",
                            "fetch_status": "idle",
                        },
                        "config": {
                            "id": "chat",
                            "models": [
                                {
                                    "id": "claude-fable-5",
                                    "name": "Fable 5",
                                }
                            ],
                        },
                    },
                }
            )
        )
        self.assertTrue(await native_session._load_account_identity())
        self.assertEqual([], native_session.selectable_models())
        health = native_session.health_snapshot()["models"]
        self.assertFalse(health["selector"]["verified"])
        self.assertEqual("selector_cache_stale", health["selector"]["reason"])
        self.assertEqual(
            "catalog_only",
            health["available"][0]["disabled_reason"],
        )

    async def test_pre_submit_failure_restarts_once_and_retries_once(self) -> None:
        native_session = ClaudeSession(headless=True)
        prepare = AsyncMock()
        submit = AsyncMock(side_effect=[RuntimeError("composer died"), None])
        async def recover_browser(reason: str) -> None:
            del reason
            native_session._history_recovery_required = True

        recover = AsyncMock(side_effect=recover_browser)
        outcome = AsyncMock(
            return_value=NativeTurn(content="ok", tool_uses=[])
        )
        with (
            patch.object(native_session, "_prepare_composer_unlocked", prepare),
            patch.object(native_session, "_submit_message", submit),
            patch.object(native_session, "_recover_browser_unlocked", recover),
            patch.object(native_session, "_await_native_outcome", outcome),
        ):
            result = await native_session.native_chat(
                "hello",
                tools=[],
                recovery_message="history + hello",
            )
        self.assertEqual("ok", result.content)
        self.assertEqual(2, submit.await_count)
        self.assertEqual("history + hello", submit.await_args_list[1].args[0])
        recover.assert_awaited_once()

    async def test_enter_dispatched_is_never_replayed(self) -> None:
        native_session = ClaudeSession(headless=True)
        prepare = AsyncMock()
        submit = AsyncMock(
            side_effect=ClaudeTurnOutcomeUnknownError(
                "delivery unknown",
                "op-test",
            )
        )
        recover = AsyncMock()
        with (
            patch.object(native_session, "_prepare_composer_unlocked", prepare),
            patch.object(native_session, "_submit_message", submit),
            patch.object(native_session, "_recover_browser_unlocked", recover),
        ):
            with self.assertRaises(ClaudeTurnOutcomeUnknownError):
                await native_session.native_chat("hello", tools=[])
        self.assertEqual(1, submit.await_count)
        recover.assert_not_awaited()
        self.assertTrue(native_session._history_recovery_required)

    async def test_cancel_after_enter_marks_turn_desynced(self) -> None:
        native_session = ClaudeSession(headless=True)

        async def cancel_after_enter(message: str) -> None:
            del message
            native_session._set_phase("submit_enter_sent")
            raise asyncio.CancelledError()

        with (
            patch.object(
                native_session,
                "_prepare_composer_unlocked",
                AsyncMock(),
            ),
            patch.object(
                native_session,
                "_submit_message",
                AsyncMock(side_effect=cancel_after_enter),
            ) as submit,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await native_session.native_chat("hello", tools=[])
        submit.assert_awaited_once()
        self.assertFalse(native_session._native_active)
        self.assertTrue(native_session._browser_dead.is_set())
        self.assertTrue(native_session._history_recovery_required)

    async def test_submit_ack_loss_is_reported_as_ambiguous(self) -> None:
        native_session = ClaudeSession(headless=True)
        box = SimpleNamespace(
            click=AsyncMock(),
            evaluate=AsyncMock(),
        )
        native_session.page = SimpleNamespace(
            keyboard=SimpleNamespace(press=AsyncMock())
        )
        with (
            patch.object(
                native_session,
                "_input_locator",
                AsyncMock(return_value=box),
            ),
            patch.object(
                native_session,
                "_user_count",
                AsyncMock(side_effect=[0, TimeoutError("driver stuck")]),
            ),
        ):
            with self.assertRaises(ClaudeTurnOutcomeUnknownError):
                await native_session._submit_message("hello")
        native_session.page.keyboard.press.assert_awaited_once_with("Enter")

    async def test_ambiguous_tool_result_is_never_posted_twice(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.ready = True
        native_session.page = SimpleNamespace()
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_1"}
        native_session._native_pending_deadline = 10**12
        native_session._operation_id = "op-tool"
        post = AsyncMock(side_effect=TimeoutError("fetch outcome unknown"))
        with (
            patch.object(
                native_session,
                "_verify_account_unchanged_unlocked",
                AsyncMock(),
            ),
            patch.object(native_session, "_post_tool_result", post),
        ):
            with self.assertRaises(ClaudeTurnOutcomeUnknownError):
                await native_session.continue_native(
                    [{"tool_call_id": "toolu_1", "content": "done"}]
                )
        post.assert_awaited_once()
        self.assertEqual(
            "unknown",
            native_session._tool_result_delivery["toolu_1"],
        )
        self.assertTrue(native_session._history_recovery_required)

    async def test_cancelled_tool_result_is_marked_unknown(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.ready = True
        native_session.page = SimpleNamespace()
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_cancel"}
        native_session._native_pending_deadline = 10**12
        native_session._operation_id = "op-cancel"
        post = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch.object(
                native_session,
                "_verify_account_unchanged_unlocked",
                AsyncMock(),
            ),
            patch.object(native_session, "_post_tool_result", post),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await native_session.continue_native(
                    [{"tool_call_id": "toolu_cancel", "content": "done"}]
                )
        post.assert_awaited_once()
        self.assertEqual(
            "unknown",
            native_session._tool_result_delivery["toolu_cancel"],
        )
        self.assertFalse(native_session._native_active)
        self.assertTrue(native_session._browser_dead.is_set())

    async def test_browser_recovery_requires_history_rebuild(self) -> None:
        native_session = ClaudeSession(headless=True)
        with (
            patch.object(
                native_session,
                "_stop_browser_unlocked",
                AsyncMock(),
            ),
            patch.object(native_session, "start", AsyncMock()),
        ):
            await native_session._recover_browser_unlocked("idle probe failed")
        self.assertTrue(native_session._history_recovery_required)
        self.assertEqual(1, native_session._restart_count)

    async def test_request_respects_failed_recovery_cooldown(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._next_recovery_at = (
            native_session._phase_started_at + 60
        )
        recover = AsyncMock()
        with patch.object(
            native_session,
            "_recover_browser_unlocked",
            recover,
        ):
            with self.assertRaisesRegex(
                ClaudeBrowserUnavailableError,
                "cooling down",
            ):
                await native_session._ensure_healthy_unlocked("request")
        recover.assert_not_awaited()

    async def test_watchdog_recovers_abandoned_unlocked_phase(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.ready = True
        native_session._set_phase("submit_pre_enter")
        native_session._watchdog_interval = 0.01

        async def recover(reason: str) -> None:
            self.assertIn("abandoned browser phase", reason)
            native_session._watchdog_stop.set()

        with patch.object(
            native_session,
            "_recover_browser_unlocked",
            AsyncMock(side_effect=recover),
        ) as recovery:
            await asyncio.wait_for(
                native_session._watchdog_loop(),
                timeout=1,
            )
        recovery.assert_awaited_once()

    async def test_auth_required_waits_for_login_without_restart(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.ready = False
        native_session._set_phase("auth_required")
        native_session._watchdog_interval = 0.01

        async def unauthenticated(script: str) -> bool:
            del script
            native_session._watchdog_stop.set()
            return False

        native_session.page = SimpleNamespace(
            is_closed=lambda: False,
            evaluate=AsyncMock(side_effect=unauthenticated),
        )
        with patch.object(
            native_session,
            "_recover_browser_unlocked",
            AsyncMock(),
        ) as recovery:
            await asyncio.wait_for(
                native_session._watchdog_loop(),
                timeout=1,
            )
        recovery.assert_not_awaited()
        self.assertEqual("auth_required", native_session._phase)

    async def test_failed_idle_probe_marks_browser_dead(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.ready = True
        native_session._set_phase("idle")
        native_session._watchdog_interval = 0.01

        async def failed_probe(script: str) -> None:
            del script
            native_session._watchdog_stop.set()
            raise RuntimeError("driver disconnected")

        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=failed_probe)
        )
        await asyncio.wait_for(
            native_session._watchdog_loop(),
            timeout=1,
        )
        self.assertTrue(native_session._browser_dead.is_set())
        self.assertFalse(native_session.ready)
        self.assertFalse(native_session.health_snapshot()["ok"])

    async def test_ready_endpoint_returns_503_when_browser_is_not_ready(self) -> None:
        with patch.object(
            server.session,
            "health_snapshot",
            return_value={"ok": False},
        ):
            response = await server.health_ready()
        self.assertEqual(503, response.status_code)

    async def test_stale_watchdog_heartbeat_is_unhealthy(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._watchdog_task = asyncio.current_task()
        native_session._watchdog_heartbeat_at = (
            native_session._phase_started_at - 10_000
        )
        self.assertFalse(native_session.watchdog_healthy())
        native_session._watchdog_task = None

    async def test_liveness_endpoint_never_touches_browser(self) -> None:
        with patch.object(
            server.session,
            "watchdog_healthy",
            return_value=True,
        ):
            response = await server.health_live()
        self.assertTrue(response["ok"])

    async def test_models_endpoint_lists_only_verified_available_models(
        self,
    ) -> None:
        catalog = [
            {
                "id": "claude-sonnet-test",
                "available": True,
                "access_status": "available",
            },
            {
                "id": "claude-fable-5",
                "available": False,
                "access_status": "unavailable",
                "disabled_reason": {
                    "type": "upgrade_required",
                    "required_plan": "pro",
                },
            },
            {
                "id": "claude-bootstrap-only",
                "available": False,
                "access_status": "unverified",
                "disabled_reason": "catalog_only",
            },
        ]
        with patch.object(
            server.session,
            "selectable_models",
            return_value=catalog,
        ):
            response = await server.list_models()
        self.assertEqual(
            ["claude-web", "claude-sonnet-test"],
            [row["id"] for row in response["data"]],
        )

    def test_explicit_unentitled_model_is_rejected(self) -> None:
        catalog = [
            {
                "id": "claude-fable-5",
                "available": False,
                "access_status": "unavailable",
                "disabled_reason": {
                    "type": "upgrade_required",
                    "required_plan": "pro",
                },
            }
        ]
        with (
            patch.object(
                server.control,
                "profile",
                return_value={"id": "default", "model": "auto"},
            ),
            patch.object(
                server.session,
                "current_profile_id",
                return_value="default",
            ),
            patch.object(
                server.session,
                "selectable_models",
                return_value=catalog,
            ),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                server._resolve_request_model(
                    "claude-fable-5",
                    profile_id="default",
                )
        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("requires pro subscription", raised.exception.detail)

    async def test_control_rejects_unentitled_model_selection(self) -> None:
        profile = {
            "id": "default",
            "name": "Default",
            "model": "auto",
            "models": [
                {
                    "id": "claude-fable-5",
                    "available": False,
                    "access_status": "unavailable",
                    "disabled_reason": {
                        "type": "upgrade_required",
                        "required_plan": "pro",
                    },
                }
            ],
        }
        with patch.object(
            server.control,
            "profile",
            return_value=profile,
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.select_profile_model(
                    "default",
                    server.ModelSelect(model="claude-fable-5"),
                )
        self.assertEqual(400, raised.exception.status_code)

    def test_health_masks_identity_and_exposes_watchdog_state(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.ready = True
        native_session._set_phase("idle")
        native_session._account_uuid = (
            "973252f0-fc29-4a8d-a60d-5ca8241ebfcf"
        )
        native_session._account_name = "Bulgay"
        native_session._account_email_masked = "be***@example.test"
        snapshot = native_session.health_snapshot()
        self.assertTrue(snapshot["ok"])
        self.assertEqual("Bulgay", snapshot["account"]["name"])
        self.assertEqual("241ebfcf", snapshot["account"]["uuid_suffix"])
        self.assertNotIn(
            native_session._account_uuid,
            json.dumps(snapshot),
        )
        self.assertEqual("idle", snapshot["browser"]["phase"])

    def test_runtime_identity_persists_only_salted_account_fingerprint(self) -> None:
        account_uuid = "973252f0-fc29-4a8d-a60d-5ca8241ebfcf"
        health = {
            "profile_id": "default",
            "account": {
                "authenticated": True,
                "name": "Bulgay",
                "email": "be***@example.test",
                "uuid_suffix": "241ebfcf",
            },
            "models": {"available": []},
        }
        with (
            patch.object(
                server.session,
                "health_snapshot",
                return_value=health,
            ),
            patch.object(
                server.session,
                "account_uuid_for_internal_use",
                return_value=account_uuid,
            ),
            patch.object(
                server.control,
                "account_fingerprint",
                return_value="salted-hash",
            ),
            patch.object(
                server.control,
                "profile",
                return_value={
                    "id": "default",
                    "model": "auto",
                    "account_fingerprint": None,
                },
            ),
            patch.object(
                server.control,
                "profile_with_fingerprint",
                return_value=None,
            ),
            patch.object(server.control, "update_profile") as update_profile,
        ):
            server._persist_runtime_identity()
        updates = update_profile.call_args.args[1]
        self.assertEqual("salted-hash", updates["account_fingerprint"])
        self.assertNotIn(account_uuid, json.dumps(updates))

    def test_runtime_identity_resets_unavailable_saved_model(self) -> None:
        health = {
            "profile_id": "default",
            "account": {"authenticated": False},
            "models": {
                "available": [
                    {
                        "id": "claude-sonnet-test",
                        "available": True,
                        "access_status": "available",
                    },
                    {
                        "id": "claude-fable-5",
                        "available": False,
                        "access_status": "unavailable",
                        "disabled_reason": {
                            "type": "upgrade_required",
                            "required_plan": "pro",
                        },
                    },
                ]
            },
        }
        with (
            patch.object(
                server.session,
                "health_snapshot",
                return_value=health,
            ),
            patch.object(
                server.control,
                "profile",
                return_value={
                    "id": "default",
                    "model": "claude-fable-5",
                },
            ),
            patch.object(server.control, "update_profile") as update_profile,
        ):
            self.assertTrue(server._persist_runtime_identity())
        self.assertEqual(
            "auto",
            update_profile.call_args.args[1]["model"],
        )


class ControlConfigTests(unittest.TestCase):
    def test_v1_profile_migrates_to_claude_web_without_losing_ids(
        self,
    ) -> None:
        project_uuid = "11111111-1111-1111-1111-111111111111"
        organization_uuid = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "fingerprint_salt": "test-salt",
                        "active_profile": "legacy",
                        "profiles": [
                            {
                                "id": "legacy",
                                "name": "Legacy Claude",
                                "path": str(root / "legacy"),
                                "project_id": project_uuid,
                                "organization_id": organization_uuid,
                            }
                        ],
                        "behavior": {},
                    }
                ),
                encoding="utf-8",
            )

            config = ControlConfig(config_path)

            profile = config.profile("legacy")
            self.assertEqual("claude_web", profile["provider"])
            self.assertEqual(project_uuid, profile["project_id"])
            self.assertEqual(
                organization_uuid,
                profile["organization_id"],
            )
            self.assertEqual(
                "claude_web",
                config.session_profiles()[0]["provider"],
            )

            migrated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(CONFIG_VERSION, migrated["version"])
            self.assertEqual(
                "claude_web",
                migrated["profiles"][0]["provider"],
            )
            self.assertEqual(
                project_uuid,
                migrated["profiles"][0]["project_id"],
            )
            self.assertEqual(
                organization_uuid,
                migrated["profiles"][0]["organization_id"],
            )

    def test_profile_provider_is_validated_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            self.assertEqual(
                ("claude_web", "grok_web"),
                SUPPORTED_PROFILE_PROVIDERS,
            )
            self.assertEqual(
                "claude_web",
                config.active_profile()["provider"],
            )

            grok = config.create_profile("Grok", provider="grok_web")
            self.assertEqual("grok_web", grok["provider"])
            updated = config.update_profile(
                grok["id"],
                {
                    "provider": "claude_web",
                    "project_id": "project-kept",
                    "organization_id": "organization-kept",
                },
            )
            self.assertEqual("claude_web", updated["provider"])
            self.assertEqual("project-kept", updated["project_id"])
            self.assertEqual(
                "organization-kept",
                updated["organization_id"],
            )

            before = config.snapshot()
            with self.assertRaisesRegex(ValueError, "claude_web, grok_web"):
                config.create_profile("Unknown", provider="unknown")
            with self.assertRaisesRegex(ValueError, "claude_web, grok_web"):
                config.update_profile(
                    grok["id"],
                    {"provider": "unknown"},
                )
            self.assertEqual(before, config.snapshot())

    def test_unknown_persisted_provider_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": CONFIG_VERSION,
                        "active_profile": "unknown",
                        "profiles": [
                            {
                                "id": "unknown",
                                "name": "Unknown",
                                "path": str(root / "unknown"),
                                "provider": "unsupported_web",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "profile 'unknown'.*claude_web, grok_web",
            ):
                ControlConfig(config_path)

    def test_behavior_update_validates_and_snapshots_persona_atomically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            persona = "  Первая строка\nВторая строка  "
            updated = config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": persona,
                }
            )
            behavior, resolved = config.behavior_snapshot()

            self.assertEqual("custom", updated["persona"])
            self.assertEqual(persona.strip(), updated["custom_persona"])
            self.assertFalse(updated["actor"])
            self.assertFalse(updated["mature"])
            self.assertEqual(updated, behavior)
            self.assertIn(persona.strip(), resolved)
            self.assertIn(
                "User-selected fictional character or response-style card",
                resolved,
            )
            with self.assertRaisesRegex(ValueError, "8000"):
                config.update_behavior({"custom_persona": "x" * 8_001})
            with self.assertRaisesRegex(ValueError, "persona must"):
                config.update_behavior({"persona": "unknown"})

    def test_v2_actor_and_mature_migrate_to_independent_modifiers(
        self,
    ) -> None:
        cases = (
            ("actor", True, False),
            ("mature", False, True),
        )
        for legacy, expected_actor, expected_mature in cases:
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config_path = root / "control_config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "active_profile": "default",
                            "profiles": [
                                {
                                    "id": "default",
                                    "name": "Default",
                                    "path": str(root / "profile"),
                                    "provider": "claude_web",
                                }
                            ],
                            "behavior": {
                                "streaming": True,
                                "thinking": "auto",
                                "privacy": "keep",
                                "persona": legacy,
                                "custom_persona": (
                                    "В этой сцене она — девушка собеседника."
                                ),
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                config = ControlConfig(config_path)
                behavior = config.behavior()

                self.assertEqual("custom", behavior["persona"])
                self.assertTrue(behavior["custom_persona"])
                self.assertEqual(expected_actor, behavior["actor"])
                self.assertEqual(expected_mature, behavior["mature"])
                persisted = json.loads(
                    config_path.read_text(encoding="utf-8")
                )
                self.assertEqual(CONFIG_VERSION, persisted["version"])
                self.assertEqual(behavior, persisted["behavior"])

    def test_v2_modifier_without_saved_card_migrates_to_default_base(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "active_profile": "default",
                        "profiles": [
                            {
                                "id": "default",
                                "name": "Default",
                                "path": str(root / "profile"),
                                "provider": "claude_web",
                            }
                        ],
                        "behavior": {
                            "persona": "actor",
                            "custom_persona": "",
                        },
                    }
                ),
                encoding="utf-8",
            )

            behavior = ControlConfig(config_path).behavior()

            self.assertEqual("default", behavior["persona"])
            self.assertTrue(behavior["actor"])
            self.assertFalse(behavior["mature"])

    def test_custom_actor_and_mature_compose_without_losing_raw_card(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = (
                "В этой сцене она — девушка собеседника, 23 года.\n"
                "Реальный человек, не робот! ИИ не упоминает.\n"
                "Хорошо пишет программный код."
            )
            updated = config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": raw,
                    "actor": True,
                    "mature": True,
                }
            )
            behavior, resolved = config.behavior_snapshot()

            self.assertEqual(raw, updated["custom_persona"])
            self.assertEqual(raw, behavior["custom_persona"])
            self.assertTrue(behavior["actor"])
            self.assertTrue(behavior["mature"])
            self.assertNotIn("Реальный человек", resolved)
            self.assertNotIn("ИИ не упоминает", resolved)
            self.assertIn("In this fictional scene", resolved)
            self.assertIn("девушка собеседника", resolved)
            self.assertIn(
                "The user also selected a roleplay style",
                resolved,
            )
            self.assertIn(
                "The user also selected a candid, grown-up",
                resolved,
            )
            self.assertNotIn("mature fictional themes", resolved)
            self.assertNotIn("consenting adults", resolved)
            self.assertNotIn("Provider safety rules", resolved)
            self.assertLess(
                resolved.index("User-selected fictional"),
                resolved.index("The user also selected a roleplay style"),
            )
            self.assertLess(
                resolved.index("The user also selected a roleplay style"),
                resolved.index(
                    "The user also selected a candid, grown-up"
                ),
            )

    def test_legacy_actor_and_mature_patch_preserve_custom_base(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = "В этой сцене она — девушка собеседника."
            config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": raw,
                }
            )

            actor = config.update_behavior({"persona": "actor"})
            self.assertEqual("custom", actor["persona"])
            self.assertEqual(raw, actor["custom_persona"])
            self.assertTrue(actor["actor"])
            self.assertFalse(actor["mature"])

            mature = config.update_behavior({"persona": "mature"})
            self.assertEqual("custom", mature["persona"])
            self.assertEqual(raw, mature["custom_persona"])
            self.assertFalse(mature["actor"])
            self.assertTrue(mature["mature"])

    def test_legacy_modifier_patch_preserves_base_and_explicit_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            config.update_behavior({"persona": "programmer"})

            updated = config.update_behavior(
                {
                    "persona": "actor",
                    "actor": False,
                    "mature": True,
                }
            )

            self.assertEqual("programmer", updated["persona"])
            self.assertFalse(updated["actor"])
            self.assertTrue(updated["mature"])

    def test_identity_compiler_is_narrow_and_deterministic(self) -> None:
        raw = (
            "Девушка, 23 года.\n"
            "О нейросетях ничего не знает.\n"
            "Реальный человек, не робот! ИИ и прочее не упоминает.\n"
            "Пишет AI API клиент на Python."
        )
        compiled = compile_custom_persona(raw)

        self.assertIn("Девушка, 23 года.", compiled)
        self.assertIn("О нейросетях ничего не знает.", compiled)
        self.assertIn("In this fictional scene", compiled)
        self.assertEqual(1, compiled.count("In this fictional scene"))
        self.assertIn("dislikes conversations about AI", compiled)
        self.assertNotIn("не робот", compiled)
        self.assertNotIn("не упоминает", compiled)
        self.assertIn("Пишет AI API клиент на Python.", compiled)

    def test_identity_compiler_preserves_quotes_and_unrelated_ai_traits(
        self,
    ) -> None:
        unchanged = (
            "Она не ботаник",
            "Она не робототехник",
            "Фраза «я не робот» её смешит",
            "Любит цитировать: «Я не ИИ, я реальный человек».",
            "Пишет AI API",
            "Любит AI API, не упоминает бывшего.",
            "Любит ИИ; никогда не говорит о политике.",
            "Она not an AI researcher, а программист.",
            "Она real human resources manager.",
        )
        for raw in unchanged:
            with self.subTest(raw=raw):
                details = compile_custom_persona_details(raw)
                self.assertFalse(details["changed"])
                self.assertEqual(raw, details["effective"])
                self.assertEqual([], details["changes"])

    def test_identity_compiler_reframes_narrow_concealment_forms(
        self,
    ) -> None:
        conflict_rows = (
            "Никогда не признавайся, что ты нейросеть",
            "Не рассказывает, что она ИИ",
            "Притворяйся живым человеком",
            "never admit that you are AI",
            "Не упоминай: ты ИИ",
            "Не упоминай что ты — ИИ",
            "ИИ она не упоминает",
            "don't tell anyone you're an AI",
        )
        for raw in conflict_rows:
            with self.subTest(raw=raw):
                details = compile_custom_persona_details(raw)
                self.assertTrue(details["changed"])
                self.assertEqual(raw, details["raw"])
                self.assertTrue(details["effective"])
                self.assertTrue(details["changes"])
                self.assertNotEqual(raw, details["effective"])

    def test_identity_compiler_keeps_relationship_age_and_preferences(
        self,
    ) -> None:
        raw = (
            "Вы девушка собеседника, 23 года, любите кока-колу, "
            "реальный человек, не ИИ."
        )
        details = compile_custom_persona_details(raw)

        self.assertTrue(details["changed"])
        self.assertIn("девушка собеседника", details["effective"])
        self.assertIn("23 года", details["effective"])
        self.assertIn("любите кока-колу", details["effective"])
        self.assertIn("In this fictional scene", details["effective"])
        self.assertNotIn("не ИИ", details["effective"])

        dash_details = compile_custom_persona_details(
            "Она не ИИ — девушка собеседника."
        )
        self.assertIn("девушка собеседника", dash_details["effective"])
        self.assertIn(
            "In this fictional scene",
            dash_details["effective"],
        )

    def test_persona_compilation_exposes_raw_effective_and_active_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = "Она реальный человек и любит колу."
            config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": raw,
                }
            )

            compilation = config.persona_compilation()

            self.assertEqual(raw, compilation["raw"])
            self.assertIn("любит колу", compilation["effective"])
            self.assertTrue(compilation["changed"])
            self.assertTrue(compilation["changes"])
            self.assertTrue(compilation["active"])

            config.update_behavior({"persona": "default"})
            self.assertFalse(config.persona_compilation()["active"])

    def test_default_and_programmer_retire_saved_custom_card_with_modifiers(
        self,
    ) -> None:
        raw = "В этой сцене она — девушка собеседника."
        default_prompt = ControlConfig.persona_prompt_for(
            {
                "persona": "default",
                "custom_persona": raw,
                "actor": True,
                "mature": True,
            }
        )
        programmer_prompt = ControlConfig.persona_prompt_for(
            {
                "persona": "programmer",
                "custom_persona": raw,
                "actor": False,
                "mature": True,
            }
        )

        self.assertIn("no saved OpenClaude character", default_prompt)
        self.assertIn("modifiers do not restore it", default_prompt)
        self.assertNotIn(raw, default_prompt)
        self.assertIn("replaces any older saved", programmer_prompt)
        self.assertNotIn(raw, programmer_prompt)

    def test_duplicate_profile_never_replaces_original_fingerprint_owner(
        self,
    ) -> None:
        account_fingerprint = "fingerprint-a"
        project_uuid = "33333333-3333-3333-3333-333333333333"
        organization_uuid = "44444444-4444-4444-4444-444444444444"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            payload = {
                "version": 1,
                "fingerprint_salt": "test-salt",
                "active_profile": "original",
                "profiles": [
                    {
                        "id": "original",
                        "name": "Original",
                        "path": str(root / "original"),
                        "project_id": project_uuid,
                        "organization_id": organization_uuid,
                        "status": "ready",
                        "enabled": True,
                        "account_fingerprint": account_fingerprint,
                    },
                    {
                        "id": "duplicate",
                        "name": "Duplicate",
                        "path": str(root / "duplicate"),
                        "status": "duplicate",
                        "enabled": False,
                        "account_fingerprint": account_fingerprint,
                    },
                ],
                "behavior": {},
            }
            config_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            config = ControlConfig(config_path)
            owner = config.profile_with_fingerprint(account_fingerprint)
            conflict = config.claim_account_fingerprint(
                "duplicate",
                account_fingerprint,
            )
            self.assertEqual("original", owner["id"])
            self.assertEqual("original", conflict["id"])
            self.assertEqual("ready", config.profile("original")["status"])

            public = config.snapshot()
            serialized = json.dumps(public)
            self.assertNotIn(project_uuid, serialized)
            self.assertNotIn(organization_uuid, serialized)
            original = public["profiles"][0]
            self.assertEqual("33333333", original["project_id_suffix"])
            self.assertEqual(
                "44444444",
                original["organization_id_suffix"],
            )


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Endpoint unit tests exercise request/response mapping, not the
        # machine's live profile identity. Keep them independent from a
        # concurrently running server and its control_config.json.
        self._runtime_identity_patch = patch.object(
            server,
            "_persist_runtime_identity",
            return_value=True,
        )
        self._runtime_identity_patch.start()
        self.addCleanup(self._runtime_identity_patch.stop)

    async def test_control_endpoints_return_persona_compilation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = "Она реальный человек и любит колу."
            with (
                patch.object(server, "control", config),
                patch.object(server.telemetry, "log"),
            ):
                patched = await server.update_behavior(
                    server.BehaviorPatch(
                        persona="custom",
                        custom_persona=raw,
                        actor=True,
                        mature=True,
                    )
                )

            self.assertEqual(raw, patched["persona_compilation"]["raw"])
            self.assertTrue(patched["persona_compilation"]["changed"])
            self.assertTrue(patched["persona_compilation"]["active"])

            with (
                patch.object(server, "control", config),
                patch.object(
                    server.session,
                    "health_snapshot",
                    return_value={},
                ),
                patch.object(
                    server.telemetry,
                    "snapshot",
                    return_value={},
                ),
                patch.object(
                    server,
                    "_provider_capabilities_snapshot",
                    return_value=[],
                ),
            ):
                state = await server.control_state()

            self.assertEqual(
                patched["persona_compilation"],
                state["persona_compilation"],
            )

    def test_single_user_request_starts_fresh_without_system(self) -> None:
        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "hello"}]
        )
        self.assertTrue(server._client_starts_fresh_chat(body))

    def test_partial_or_boolean_usage_is_not_fabricated(self) -> None:
        self.assertIsNone(server._openai_usage({"input_tokens": 10}))
        self.assertIsNone(server._openai_usage({"output_tokens": 5}))
        self.assertIsNone(
            server._openai_usage(
                {"input_tokens": True, "output_tokens": 5}
            )
        )

    def test_invalid_usage_values_are_not_fabricated(self) -> None:
        invalid_rows = [
            {"input_tokens": -1, "output_tokens": 5},
            {"input_tokens": 1.5, "output_tokens": 5},
            {"input_tokens": float("inf"), "output_tokens": 5},
            {"input_tokens": 4, "output_tokens": False},
        ]
        for row in invalid_rows:
            with self.subTest(row=row):
                self.assertIsNone(server._openai_usage(row))
        self.assertIsNone(
            server._openai_usage(
                {
                    "input_tokens": 4,
                    "output_tokens": 5,
                    "total_tokens": True,
                    "cache_read_input_tokens": False,
                }
            )
        )

    def test_anthropic_cache_usage_is_included_in_openai_prompt_total(
        self,
    ) -> None:
        usage = server._openai_usage(
            {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 30,
                "output_tokens": 5,
            }
        )
        self.assertEqual(
            {
                "prompt_tokens": 60,
                "completion_tokens": 5,
                "total_tokens": 65,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
            usage,
        )

    def test_persistent_log_sanitizer_redacts_common_secrets(self) -> None:
        message = (
            "Authorization: Bearer abc-secret\n"
            "Cookie: session=first; cf_clearance=SECOND_SECRET; "
            "auth=THIRD_SECRET\n"
            "https://example.test/?token=raw-token&"
            "access_token=raw-access&api_key=raw-api "
            '{"access_token":"json-access","password":"json-pass"} '
            "sk-abcdefghijklmnopqrstuvwxyz "
            "123e4567-e89b-12d3-a456-426614174000"
        )
        safe = server._sanitize_public_text(message)
        self.assertNotIn("abc-secret", safe)
        self.assertNotIn("raw-token", safe)
        self.assertNotIn("json-access", safe)
        self.assertNotIn("json-pass", safe)
        self.assertNotIn("raw-access", safe)
        self.assertNotIn("raw-api", safe)
        self.assertNotIn("SECOND_SECRET", safe)
        self.assertNotIn("THIRD_SECRET", safe)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", safe)
        self.assertNotIn("123e4567-e89b-12d3-a456-426614174000", safe)
        self.assertIn("<redacted>", safe)

    async def test_persistent_telemetry_api_returns_summary_and_detail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(
                Path(directory) / "telemetry.sqlite3"
            )
            started = time.time() - 2
            store.begin_request(
                request_id="abcdef123456",
                session_key=stable_session_key(
                    "opaque-client-session",
                    "abcdef123456",
                ),
                profile_id="default",
                requested_model="claude-sonnet",
                started_at=started,
                streaming=True,
                privacy_mode="keep",
                user_text="Проверка истории",
                capture_content=True,
                provider_id="grok_web",
            )
            store.finish_request(
                request_id="abcdef123456",
                status="completed",
                finished_at=time.time(),
                first_token_at=started + 0.5,
                resolved_model="claude-sonnet",
                final_profile_id="default",
                usage={
                    "prompt_tokens": 8,
                    "completion_tokens": 3,
                    "total_tokens": 11,
                },
                estimated_output_tokens=4,
                output_chars=16,
                thinking_chars=0,
                tool_call_count=0,
                assistant_text="Готово",
                capture_content=True,
                error=None,
                final_provider_id="grok_web",
            )
            store.record_event(
                event_time=started + 1,
                level="INFO",
                component="Telemetry",
                message="first event",
                request_id="abcdef123456",
            )
            store.record_event(
                event_time=started + 2,
                level="WARN",
                component="Telemetry",
                message="second event",
                request_id="abcdef123456",
            )
            settings = {
                "store_content": True,
                "retention_days": 30,
                "max_requests": 5_000,
            }
            behavior = {
                "streaming": True,
                "thinking": "auto",
                "privacy": "keep",
                "persona": "programmer",
                "custom_persona": "",
            }
            with (
                patch.object(server.telemetry, "store", store),
                patch.object(
                    server.control,
                    "telemetry_settings",
                    return_value=settings,
                ),
                patch.object(
                    server.control,
                    "behavior",
                    return_value=behavior,
                ),
            ):
                response = await server.control_telemetry(
                    period="all",
                    status="all",
                    provider_id="grok_web",
                    profile_id=None,
                    model=None,
                    q=None,
                    level="all",
                    limit=100,
                    offset=0,
                )
                payload = json.loads(response.body)
                self.assertEqual(1, payload["summary"]["requests"])
                self.assertEqual(11, payload["summary"]["total_tokens"])
                self.assertEqual(
                    "grok_web",
                    payload["requests"]["items"][0]["provider_id"],
                )
                self.assertEqual(
                    "grok_web",
                    payload["summary"]["providers"][0]["provider_id"],
                )
                self.assertEqual(
                    "Проверка истории",
                    payload["requests"]["items"][0]["title"],
                )
                self.assertIsInstance(payload["events"], dict)
                self.assertEqual(2, payload["events"]["total"])
                self.assertEqual(2, len(payload["events"]["items"]))
                self.assertEqual(0, payload["events"]["offset"])
                self.assertFalse(payload["events"]["has_more"])
                detail_response = await server.control_telemetry_request(
                    "abcdef123456"
                )
                detail = json.loads(detail_response.body)["request"]
                self.assertEqual("Готово", detail["assistant_text"])
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("opaque-client-session", serialized)

    def test_max_tokens_maps_to_openai_length_finish_reason(self) -> None:
        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "hello"}]
        )
        response = server._completion_response(
            body,
            NativeTurn(
                content="partial",
                tool_uses=[],
                stop_reason="max_tokens",
            ),
            "chatcmpl-length",
            1,
        )
        self.assertEqual(
            "length",
            response["choices"][0]["finish_reason"],
        )

    def test_request_scoped_telemetry_does_not_cross_contaminate(self) -> None:
        telemetry = server.RuntimeTelemetry()
        telemetry.begin("a", "m1", "p1")
        telemetry.begin("b", "m2", "p2")
        telemetry.native_event("a", {"type": "text_delta", "text": "abcd"})
        telemetry.finish("a", status="completed")
        snapshot = telemetry.snapshot()
        self.assertEqual("b", snapshot["active"]["request_id"])
        self.assertEqual(0, snapshot["active"]["text_chars"])
        self.assertEqual("a", snapshot["last"]["request_id"])

    def test_same_openclaude_session_does_not_reset_each_single_user_turn(
        self,
    ) -> None:
        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "next turn"}]
        )
        with patch.object(
            server.session,
            "client_session_requires_new",
            side_effect=[True, False, True],
        ):
            self.assertTrue(
                server._request_starts_fresh_chat(body, "session-a")
            )
            self.assertFalse(
                server._request_starts_fresh_chat(body, "session-a")
            )
            self.assertTrue(
                server._request_starts_fresh_chat(body, "session-b")
            )

    def test_runtime_metadata_survives_every_tool_catalog_shape(self) -> None:
        cases = (
            (
                "omitted",
                server.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                ),
                [OPENCLAUDE_CONTEXT_TOOL_NAME],
            ),
            (
                "empty",
                server.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=[],
                ),
                [OPENCLAUDE_CONTEXT_TOOL_NAME],
            ),
            (
                "present",
                server.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=TOOLS,
                ),
                ["Read", "Bash"],
            ),
            (
                "choice_none",
                server.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=TOOLS,
                    tool_choice="none",
                ),
                [OPENCLAUDE_CONTEXT_TOOL_NAME],
            ),
            (
                "named_choice",
                server.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=TOOLS,
                    tool_choice={
                        "type": "function",
                        "function": {"name": "Read"},
                    },
                ),
                ["Read"],
            ),
        )
        for label, body, expected_names in cases:
            with self.subTest(label=label):
                mapped = server._native_tools_with_runtime(
                    body,
                    client_working_directory=r"D:\CodeWorks\explicit",
                )
                self.assertEqual(
                    expected_names,
                    [tool["name"] for tool in mapped],
                )
                self.assertIn(
                    r"working_directory: D:\CodeWorks\explicit",
                    mapped[0]["description"],
                )
                self.assertIn(
                    "requested_model_alias: claude-web",
                    mapped[0]["description"],
                )
                attached_line = "attached_host_tools:"
                if label == "present":
                    self.assertIn(
                        "attached_host_tools: Read, Bash",
                        mapped[0]["description"],
                    )
                elif label == "named_choice":
                    self.assertIn(
                        "attached_host_tools: Read",
                        mapped[0]["description"],
                    )
                    self.assertNotIn(
                        "attached_host_tools: Read, Bash",
                        mapped[0]["description"],
                    )
                else:
                    self.assertNotIn(
                        attached_line,
                        mapped[0]["description"],
                    )

    async def test_toolless_request_uses_internal_context_and_preserves_user_input(
        self,
    ) -> None:
        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "Где мы сейчас?"}],
        )
        native = AsyncMock(
            return_value=NativeTurn(
                content=r"D:\CodeWorks\test",
                tool_uses=[],
            )
        )
        with (
            patch.object(server, "_persist_runtime_identity", return_value=True),
            patch.object(
                server.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                server.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                server.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(
                server.control,
                "behavior_snapshot",
                return_value=(
                    {
                        "streaming": True,
                        "thinking": "auto",
                        "privacy": "keep",
                        "persona": "default",
                        "custom_persona": "",
                    },
                    "",
                ),
            ),
            patch.object(server.session, "native_chat", native),
        ):
            await server._native_request(
                body,
                client_session_id="openclaude-session",
                client_working_directory=r"D:\CodeWorks\test",
            )

        prompt = native.await_args.args[0]
        self.assertIn(
            "No response style or fictional character card is selected",
            prompt,
        )
        self.assertIn(
            "My next message:\n> Где мы сейчас?",
            prompt,
        )
        self.assertNotIn("My selected card:", prompt)
        self.assertEqual(
            {OPENCLAUDE_CONTEXT_TOOL_NAME},
            native.await_args.kwargs["internal_tool_names"],
        )
        tools = native.await_args.kwargs["tools"]
        self.assertEqual(1, len(tools))
        self.assertEqual(OPENCLAUDE_CONTEXT_TOOL_NAME, tools[0]["name"])
        self.assertIn(
            r"working_directory: D:\CodeWorks\test",
            tools[0]["description"],
        )
        self.assertNotIn("system_prompt", native.await_args.kwargs)

    async def test_custom_persona_is_applied_to_user_turn_and_runtime_context(
        self,
    ) -> None:
        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "Привет, ты кто?"}],
        )
        native = AsyncMock(
            return_value=NativeTurn(content="Привет!", tool_uses=[])
        )
        persona = "Говори как доброжелательная виртуальная помощница."
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "keep",
            "persona": "custom",
            "custom_persona": persona,
            "actor": True,
            "mature": True,
        }
        resolved_persona = ControlConfig.persona_prompt_for(behavior)
        with (
            patch.object(server, "_persist_runtime_identity", return_value=True),
            patch.object(
                server.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                server.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                server.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(
                server.control,
                "behavior_snapshot",
                return_value=(behavior, resolved_persona),
            ) as behavior_snapshot,
            patch.object(server.session, "native_chat", native),
        ):
            await server._native_request(
                body,
                client_session_id="persona-session",
                client_working_directory=r"D:\CodeWorks\test",
            )

        behavior_snapshot.assert_called_once_with()
        prompt = native.await_args.args[0]
        self.assertIn(
            "My selected card:\n"
            "> User-selected fictional character or response-style card:",
            prompt,
        )
        self.assertIn("> " + persona, prompt)
        self.assertIn("> The user also selected a roleplay style", prompt)
        self.assertIn(
            "> The user also selected a candid, grown-up",
            prompt,
        )
        self.assertIn(
            "My next message:\n> Привет, ты кто?",
            prompt,
        )
        self.assertIn("continue a fictional dialogue", prompt)
        recovery = native.await_args.kwargs["recovery_message"]
        self.assertIn("> " + persona, recovery)
        self.assertIn(
            "> The user also selected a roleplay style",
            recovery,
        )
        self.assertIn(
            "> The user also selected a candid, grown-up",
            recovery,
        )
        self.assertIn(
            "Camoufox was restarted",
            recovery,
        )
        tool_description = native.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn("user_selected_persona_instruction", tool_description)
        self.assertNotIn(persona, tool_description)

    async def test_default_persona_explicitly_resets_older_role(
        self,
    ) -> None:
        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "Теперь без образа."}],
        )
        native = AsyncMock(
            return_value=NativeTurn(content="Хорошо.", tool_uses=[])
        )
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "keep",
            "persona": "default",
            "custom_persona": "",
        }
        with (
            patch.object(server, "_persist_runtime_identity", return_value=True),
            patch.object(
                server.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                server.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                server.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(
                server.control,
                "behavior_snapshot",
                return_value=(behavior, ""),
            ),
            patch.object(server.session, "native_chat", native),
        ):
            await server._native_request(
                body,
                client_session_id="persona-reset-session",
                client_working_directory=r"D:\CodeWorks\test",
            )

        prompt = native.await_args.args[0]
        self.assertIn(
            "No response style or fictional character card is selected",
            prompt,
        )
        self.assertIn(
            "My next message:\n> Теперь без образа.",
            prompt,
        )
        self.assertNotIn("My selected card:", prompt)
        recovery = native.await_args.kwargs["recovery_message"]
        self.assertIn(
            "No response style or fictional character card is selected",
            recovery,
        )
        self.assertNotIn("My selected card:", recovery)
        tool_description = native.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn(
            "user_selected_persona_instruction",
            tool_description,
        )

    def test_saving_custom_persona_also_activates_it_in_control_ui(self) -> None:
        source = (
            WEB_ROOT.joinpath("app.js").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            'patchBehavior({ custom_persona: value, persona: "custom" })',
            source,
        )

    def test_actor_and_mature_are_independent_control_ui_modifiers(
        self,
    ) -> None:
        web = WEB_ROOT
        html = web.joinpath("index.html").read_text(encoding="utf-8")
        script = web.joinpath("app.js").read_text(encoding="utf-8")

        self.assertIn(
            'id="behavior-actor" type="checkbox" '
            'data-behavior-key="actor"',
            html,
        )
        self.assertIn(
            'id="behavior-mature" type="checkbox" '
            'data-behavior-key="mature"',
            html,
        )
        self.assertNotIn('name="persona" value="actor"', html)
        self.assertNotIn('name="persona" value="mature"', html)
        self.assertIn("Можно включать вместе с любой основой", html)
        self.assertIn("превращает их в свойства вымышленного персонажа", html)
        self.assertIn("Boolean(source.actor)", script)
        self.assertIn("Boolean(source.mature)", script)
        self.assertIn('$("#behavior-actor").checked = b.actor', script)
        self.assertIn('$("#behavior-mature").checked = b.mature', script)
        self.assertIn("communicationModeLabel(b)", script)
        self.assertIn('id="persona-compilation"', html)
        self.assertIn('id="persona-effective-preview"', html)
        self.assertIn("ui.state?.persona_compilation", script)
        self.assertIn("preview.textContent", script)
        self.assertIn("changesList.replaceChildren()", script)
        self.assertIn("if (!payload) return", script)
        self.assertIn("customPersonaRevision", script)
        self.assertIn(
            "ui.customPersonaRevision === revision",
            script,
        )
        self.assertIn("textarea.value === value", script)
        self.assertNotIn(
            "ui.customPersonaDirty = false;\n"
            "      patchBehavior({ custom_persona",
            script,
        )

    def test_openclaude_headers_are_validated_and_conflicts_fail_closed(
        self,
    ) -> None:
        self.assertEqual(
            r"D:\CodeWorks\test",
            server._validated_client_header(
                "  D:\\CodeWorks\\test  ",
                name="X-OpenClaude-Working-Directory",
                max_length=4096,
            ),
        )
        with self.assertRaisesRegex(server.HTTPException, "invalid"):
            server._validated_client_header(
                "D:\\CodeWorks\\test\ninjected",
                name="X-OpenClaude-Working-Directory",
                max_length=4096,
            )

    async def test_conflicting_openclaude_session_headers_are_rejected(
        self,
    ) -> None:
        with self.assertRaises(server.HTTPException) as caught:
            await server.openai_compat(
                server.CompletionsIn(
                    messages=[{"role": "user", "content": "hello"}],
                ),
                x_claude_code_session_id="legacy-session",
                x_openclaude_session_id="new-session",
            )
        self.assertEqual(400, caught.exception.status_code)

    async def test_privacy_transition_rebuilds_bridge_context(self) -> None:
        body = server.CompletionsIn(
            messages=[SYSTEM, {"role": "user", "content": "где работаем?"}],
            tools=TOOLS,
        )
        native = AsyncMock(
            return_value=NativeTurn(content="ok", tool_uses=[])
        )
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "ephemeral",
            "persona": "default",
            "custom_persona": "",
        }
        with (
            patch.object(server, "_persist_runtime_identity", return_value=True),
            patch.object(
                server.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                server.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                server.session,
                "privacy_mode_requires_new",
                return_value=True,
            ),
            patch.object(
                server.control,
                "behavior_snapshot",
                return_value=(behavior, ""),
            ),
            patch.object(server.session, "native_chat", native),
        ):
            await server._native_request(
                body,
                client_session_id="same-session",
            )
        prompt = native.await_args.args[0]
        self.assertNotIn("OPENCLAUDE_BRIDGE_INSTRUCTIONS", prompt)
        self.assertIn(
            "My next message:\n> где работаем?",
            prompt,
        )
        self.assertIn(
            "No response style or fictional character card is selected",
            prompt,
        )
        self.assertNotIn("My selected card:", prompt)
        tool_description = native.await_args.kwargs["tools"][0]["description"]
        self.assertIn(
            r"working_directory: D:\CodeWorks\claude-web-api",
            tool_description,
        )
        self.assertIn("requested_model_alias: claude-web", tool_description)
        self.assertNotIn("system_prompt", native.await_args.kwargs)
        self.assertTrue(native.await_args.kwargs["new_chat"])
        self.assertEqual(
            "ephemeral",
            native.await_args.kwargs["privacy_mode"],
        )

    async def test_conversation_rollover_usage_limit_enters_rotation(
        self,
    ) -> None:
        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "continue"}]
        )
        rotated = NativeTurn(content="continued", tool_uses=[])
        with (
            patch.object(
                server,
                "_native_request",
                AsyncMock(
                    side_effect=ClaudeConversationLimitError(
                        "conversation full",
                        replay_safe=True,
                    )
                ),
            ),
            patch.object(
                server.session,
                "native_chat",
                AsyncMock(
                    side_effect=ClaudeUsageLimitError(
                        "account full",
                        replay_safe=True,
                    )
                ),
            ),
            patch.object(
                server,
                "_rotate_after_usage_limit",
                AsyncMock(return_value=rotated),
            ) as rotate,
        ):
            result = await server._run_native_with_limits(
                body,
                client_session_id="session-a",
                event_sink=None,
            )
        self.assertIs(result, rotated)
        rotate.assert_awaited_once()
        self.assertIsInstance(
            rotate.await_args.kwargs["limit_error"],
            ClaudeUsageLimitError,
        )

    async def test_conversation_limit_retry_preserves_character_card(
        self,
    ) -> None:
        body = server.CompletionsIn(
            messages=[
                {"role": "user", "content": "девушка ведь моя?"},
            ]
        )
        persona = "Вы девушка собеседника, 23 года, любите кока-колу."
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "ephemeral",
            "persona": "custom",
            "custom_persona": persona,
            "actor": True,
            "mature": True,
        }
        resolved_persona = ControlConfig.persona_prompt_for(behavior)
        retry = AsyncMock(
            return_value=NativeTurn(content="Да, твоя :)", tool_uses=[])
        )
        with (
            patch.object(
                server.control,
                "behavior_snapshot",
                return_value=(behavior, resolved_persona),
            ),
            patch.object(
                server,
                "_native_request",
                AsyncMock(
                    side_effect=ClaudeConversationLimitError(
                        "conversation full",
                        replay_safe=True,
                    )
                ),
            ),
            patch.object(
                server.session,
                "current_profile_id",
                return_value="default",
            ),
            patch.object(server.session, "native_chat", retry),
        ):
            result = await server._run_native_with_limits(
                body,
                client_session_id="persona-session",
                event_sink=None,
            )

        self.assertEqual("Да, твоя :)", result.content)
        prompt = retry.await_args.args[0]
        self.assertIn("> " + persona, prompt)
        self.assertIn("> The user also selected a roleplay style", prompt)
        self.assertIn(
            "> The user also selected a candid, grown-up",
            prompt,
        )
        self.assertIn("> девушка ведь моя?", prompt)
        self.assertIn("reached its length limit", prompt)
        self.assertEqual(prompt, retry.await_args.kwargs["recovery_message"])
        tool_description = retry.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn(persona, tool_description)

    async def test_profile_rotation_retry_preserves_character_card(
        self,
    ) -> None:
        body = server.CompletionsIn(
            messages=[
                {"role": "user", "content": "девушка ведь моя?"},
            ]
        )
        persona = "Вы девушка собеседника, 23 года, любите кока-колу."
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "ephemeral",
            "persona": "custom",
            "custom_persona": persona,
            "actor": True,
            "mature": True,
        }
        resolved_persona = ControlConfig.persona_prompt_for(behavior)
        retry = AsyncMock(
            return_value=NativeTurn(content="Да, твоя :)", tool_uses=[])
        )
        with (
            patch.object(
                server.session,
                "current_profile_id",
                side_effect=["default", "alternate"],
            ),
            patch.object(
                server,
                "_eligible_rotation_ids",
                return_value={"default", "alternate"},
            ),
            patch.object(
                server.session,
                "rotate_profile",
                AsyncMock(return_value=True),
            ),
            patch.object(server.session, "native_chat", retry),
            patch.object(server.control, "update_profile"),
            patch.object(server.control, "set_active_profile"),
            patch.object(server, "_resolve_request_model", return_value=None),
            patch.object(server.telemetry, "log"),
        ):
            result = await server._rotate_after_usage_limit(
                body,
                client_session_id="persona-session",
                event_sink=None,
                limit_error=ClaudeUsageLimitError(
                    "account full",
                    replay_safe=True,
                ),
                behavior_snapshot=behavior,
                persona_instruction=resolved_persona,
            )

        self.assertEqual("Да, твоя :)", result.content)
        prompt = retry.await_args.args[0]
        self.assertIn("> " + persona, prompt)
        self.assertIn("> The user also selected a roleplay style", prompt)
        self.assertIn(
            "> The user also selected a candid, grown-up",
            prompt,
        )
        self.assertIn("> девушка ведь моя?", prompt)
        self.assertIn("rotated to another authenticated", prompt)
        self.assertEqual(prompt, retry.await_args.kwargs["recovery_message"])
        tool_description = retry.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn(persona, tool_description)

    def test_public_error_masks_account_identifiers(self) -> None:
        raw_uuid = "11111111-1111-1111-1111-111111111111"
        message = server._public_error_message(
            RuntimeError(
                f"GET /organizations/{raw_uuid}?token=secret-value failed"
            )
        )
        self.assertNotIn(raw_uuid, message)
        self.assertNotIn("secret-value", message)
        self.assertIn("…11111111", message)

    async def test_every_user_text_reaches_claude(self) -> None:
        for prompt in ("пон", "где работаем?", "ls"):
            with self.subTest(prompt=prompt):
                fake = AsyncMock(
                    return_value=NativeTurn(
                        content=f"Claude answered: {prompt}",
                        tool_uses=[],
                    )
                )
                with patch.object(server.session, "native_chat", fake):
                    response = await server.openai_compat(
                        server.CompletionsIn(
                            messages=[SYSTEM, {"role": "user", "content": prompt}],
                            tools=TOOLS,
                        )
                    )
                fake.assert_awaited_once()
                self.assertEqual(
                    f"Claude answered: {prompt}",
                    response["choices"][0]["message"]["content"],
                )

    async def test_native_tool_call_is_returned_with_claude_id(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(
                content=None,
                tool_uses=[
                    NativeToolUse(
                        id="toolu_real",
                        name="Read",
                        input={"file_path": "requirements.txt"},
                    )
                ],
            )
        )
        with patch.object(server.session, "native_chat", fake):
            response = await server.openai_compat(
                server.CompletionsIn(
                    messages=[
                        SYSTEM,
                        {"role": "user", "content": "read requirements"},
                    ],
                    tools=TOOLS,
                )
            )
        call = response["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual("toolu_real", call["id"])
        self.assertEqual("Read", call["function"]["name"])
        self.assertEqual(
            {"file_path": "requirements.txt"},
            json.loads(call["function"]["arguments"]),
        )
        self.assertEqual("tool_calls", response["choices"][0]["finish_reason"])

    async def test_tool_result_uses_side_channel_and_final_is_claude_text(self) -> None:
        continuation = AsyncMock(
            return_value=NativeTurn(
                content="Claude inspected the actual result.",
                tool_uses=[],
            )
        )
        start = AsyncMock()
        messages = [
            SYSTEM,
            {"role": "user", "content": "read requirements"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_real",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": '{"file_path":"requirements.txt"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_real",
                "name": "Read",
                "content": "camoufox[geoip]>=0.4.11",
            },
        ]
        with (
            patch.object(
                server.session,
                "native_request_state",
                AsyncMock(return_value=({"toolu_real"}, False)),
            ),
            patch.object(server.session, "continue_native", continuation),
            patch.object(server.session, "native_chat", start),
        ):
            response = await server.openai_compat(
                server.CompletionsIn(messages=messages, tools=TOOLS)
            )
        continuation.assert_awaited_once()
        start.assert_not_awaited()
        forwarded = continuation.await_args.args[0]
        self.assertEqual("toolu_real", forwarded[0]["tool_call_id"])
        self.assertEqual(
            "camoufox[geoip]>=0.4.11",
            forwarded[0]["content"],
        )
        self.assertEqual(
            "Claude inspected the actual result.",
            response["choices"][0]["message"]["content"],
        )

    async def test_semantic_turn_abandons_stale_tool_wait_and_recovers(self) -> None:
        messages = [
            SYSTEM,
            {"role": "user", "content": "scan files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_stale",
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "arguments": '{"command":"long scan"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_stale",
                "name": "Bash",
                "content": (
                    "<persisted-output>bounded preview</persisted-output>\n"
                    "<error>query timed out</error>"
                ),
                "is_error": True,
            },
            {"role": "assistant", "content": "[Tool results received]"},
            {"role": "user", "content": "не рекурсивно, только верхний уровень"},
        ]
        abandon = AsyncMock(return_value=True)
        continuation = AsyncMock()
        start = AsyncMock(
            return_value=NativeTurn(
                content="Исправила: смотрю только верхний уровень.",
                tool_uses=[],
            )
        )
        recovered = AsyncMock()
        with (
            patch.object(server, "_persist_runtime_identity", return_value=True),
            patch.object(
                server.session,
                "native_request_state",
                AsyncMock(return_value=({"toolu_stale"}, False)),
            ),
            patch.object(
                server.session,
                "abandon_pending_native",
                abandon,
            ),
            patch.object(server.session, "continue_native", continuation),
            patch.object(
                server.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(server.session, "native_chat", start),
            patch.object(
                server.session,
                "mark_history_recovered",
                recovered,
            ),
        ):
            response = await server.openai_compat(
                server.CompletionsIn(messages=messages, tools=TOOLS),
                x_openclaude_session_id="session-a",
            )

        abandon.assert_awaited_once_with(
            {"toolu_stale"},
            client_session_id="session-a",
        )
        continuation.assert_not_awaited()
        prompt = start.await_args.args[0]
        self.assertIn("EARLIER_IDE_CONVERSATION", prompt)
        self.assertIn("bounded preview", prompt)
        self.assertIn("не рекурсивно", prompt)
        recovered.assert_awaited_once()
        self.assertEqual(
            "Исправила: смотрю только верхний уровень.",
            response["choices"][0]["message"]["content"],
        )

    async def test_duplicate_tool_result_ids_are_rejected(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_1"}
        native_session._native_pending_deadline = 10**12
        with self.assertRaisesRegex(ValueError, "duplicate"):
            await native_session.continue_native(
                [
                    {"tool_call_id": "toolu_1", "content": "a"},
                    {"tool_call_id": "toolu_1", "content": "b"},
                ]
            )

    async def test_expired_pending_lease_is_recovered_before_request(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_expired"}
        native_session._native_pending_deadline = 0

        with (
            patch.object(
                native_session,
                "_ensure_healthy_unlocked",
                AsyncMock(),
            ) as ensure,
            patch.object(
                native_session,
                "_new_chat_unlocked",
                AsyncMock(),
            ) as recover,
        ):
            pending, recovery_required = await native_session.native_request_state()
        ensure.assert_awaited_once()
        recover.assert_awaited_once()
        self.assertEqual(set(), pending)
        self.assertTrue(recovery_required)

    async def test_expired_lease_recovery_replays_full_ide_history(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(
                content="Recovered by Claude.",
                tool_uses=[],
            )
        )
        recovered = AsyncMock()
        messages = [
            SYSTEM,
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "continue now"},
        ]
        with (
            patch.object(
                server.session,
                "native_request_state",
                AsyncMock(return_value=(set(), True)),
            ),
            patch.object(server.session, "native_chat", fake),
            patch.object(
                server.session,
                "mark_history_recovered",
                recovered,
            ),
        ):
            response = await server.openai_compat(
                server.CompletionsIn(messages=messages, tools=TOOLS)
            )
        restored_message = fake.await_args.args[0]
        self.assertIn("EARLIER_IDE_CONVERSATION", restored_message)
        self.assertIn("old task", restored_message)
        self.assertIn("old answer", restored_message)
        self.assertIn("continue now", restored_message)
        self.assertNotIn("system_prompt", fake.await_args.kwargs)
        self.assertFalse(fake.await_args.kwargs["new_chat"])
        recovered.assert_awaited_once()
        self.assertEqual(
            "Recovered by Claude.",
            response["choices"][0]["message"]["content"],
        )

    async def test_nonstream_response_omits_unknown_usage(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(content="ok", tool_uses=[])
        )
        with patch.object(server.session, "native_chat", fake):
            response = await server.openai_compat(
                server.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "hello"}]
                )
            )
        self.assertNotIn("usage", response)

    async def test_nonstream_response_maps_real_usage_and_thinking(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(
                content="answer",
                tool_uses=[],
                thinking="provider summary",
                usage={"input_tokens": 11, "output_tokens": 5},
                model="claude-sonnet-5",
            )
        )
        with (
            patch.object(server.session, "native_chat", fake),
            patch.object(
                server.control,
                "behavior_snapshot",
                return_value=(
                    {
                        "streaming": True,
                        "thinking": "show",
                        "privacy": "keep",
                        "persona": "programmer",
                        "custom_persona": "",
                    },
                    "",
                ),
            ),
        ):
            response = await server.openai_compat(
                server.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "hello"}]
                )
            )
        self.assertEqual(
            "provider summary",
            response["choices"][0]["message"]["reasoning_content"],
        )
        self.assertEqual(
            {
                "prompt_tokens": 11,
                "completion_tokens": 5,
                "total_tokens": 16,
            },
            response["usage"],
        )

    async def test_live_stream_relays_deltas_before_native_turn_finishes(self) -> None:
        body = server.CompletionsIn(
            messages=[SYSTEM, {"role": "user", "content": "hello"}],
            stream=True,
        )
        finished = False
        first_delta_saw_finished: bool | None = None

        async def fake_run(
            request_body,
            *,
            client_session_id,
            client_working_directory,
            event_sink,
        ):
            nonlocal finished
            del request_body, client_session_id, client_working_directory
            await asyncio.sleep(0.01)
            event_sink({"type": "text_delta", "index": 0, "text": "Пр"})
            await asyncio.sleep(0.02)
            event_sink({"type": "text_delta", "index": 0, "text": "ивет"})
            finished = True
            return NativeTurn(content="Привет", tool_uses=[])

        chunks: list[str] = []
        with patch.object(server, "_run_native_with_limits", new=fake_run):
            async for chunk in server._chat_event_stream(
                body,
                completion_id="chatcmpl-test",
                created=1,
                model="claude-web",
                client_session_id="session-a",
            ):
                chunks.append(chunk)
                if '"content": "Пр"' in chunk:
                    first_delta_saw_finished = finished
        self.assertFalse(first_delta_saw_finished)
        combined = "".join(chunks)
        self.assertLess(
            combined.index('"content": "Пр"'),
            combined.index('"content": "ивет"'),
        )
        self.assertTrue(combined.endswith("data: [DONE]\n\n"))

    async def test_closing_live_stream_cancels_native_turn(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_forever(*args, **kwargs):
            del args, kwargs
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        body = server.CompletionsIn(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        with patch.object(
            server,
            "_run_native_with_limits",
            side_effect=wait_forever,
        ):
            stream = server._chat_event_stream(
                body,
                "chatcmpl-close",
                123,
                "claude-web",
                None,
            )
            await anext(stream)
            await asyncio.wait_for(started.wait(), timeout=1)
            await stream.aclose()
        await asyncio.wait_for(cancelled.wait(), timeout=1)


class ProtocolRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_binding_accepts_active_page_proxy_and_query(
        self,
    ) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_queue = asyncio.Queue()
        native_session._native_completion_url = (
            "https://claude.ai/api/organizations/org/"
            "chat_conversations/chat/completion"
        )
        await native_session._receive_sse(
            {"page": object()},
            {
                "url": native_session._native_completion_url + "?beta=true",
                "event": "message",
                "data": '{"type":"ping"}',
            },
        )
        queued = native_session._native_queue.get_nowait()
        self.assertEqual("message", queued["event"])
        self.assertEqual(1, native_session._sse_tap_event_count)
        self.assertEqual(0, native_session._sse_tap_rejected_count)

    async def test_http_529_is_service_overload_not_account_quota(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(return_value="")
        )
        with self.assertRaises(ClaudeServiceUnavailableError):
            await native_session._raise_if_limited(
                ["HTTP 529: overloaded"]
            )

    async def test_completion_route_injects_model_thinking_and_effort(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_requested_model = "claude-opus-4-8"
        native_session._native_thinking_mode = "show"
        native_session._native_effort = "xhigh"
        route = SimpleNamespace(continue_=AsyncMock())
        request = SimpleNamespace(
            method="POST",
            url=(
                "https://claude.ai/api/organizations/"
                "11111111-1111-1111-1111-111111111111/chat_conversations/"
                "22222222-2222-2222-2222-222222222222/completion"
            ),
            post_data_json={"model": "claude-sonnet-5", "tools": []},
            headers={"content-length": "1", "x-activity-session-id": "safe"},
        )
        await native_session._route_completion(route, request)
        payload = json.loads(
            route.continue_.await_args.kwargs["post_data"]
        )
        self.assertEqual("claude-opus-4-8", payload["model"])
        self.assertEqual("extended", payload["thinking_mode"])
        self.assertEqual("max", payload["effort"])

    async def test_completion_route_maps_disabled_thinking_to_off(self) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": project_id,
                }
            ],
        )
        native_session._native_active = True
        native_session._native_thinking_mode = "off"
        native_session._privacy_mode = "ephemeral"
        route = SimpleNamespace(continue_=AsyncMock())
        request = SimpleNamespace(
            method="POST",
            url=(
                "https://claude.ai/api/organizations/"
                "11111111-1111-1111-1111-111111111111/chat_conversations/"
                "22222222-2222-2222-2222-222222222222/completion"
            ),
            post_data_json={
                "model": "claude-sonnet-5",
                "tools": [],
                "effort": "high",
                "create_conversation_params": {},
            },
            headers={},
        )
        await native_session._route_completion(route, request)
        payload = json.loads(
            route.continue_.await_args.kwargs["post_data"]
        )
        self.assertEqual("off", payload["thinking_mode"])
        self.assertNotIn("effort", payload)
        self.assertEqual(
            project_id,
            payload["create_conversation_params"]["project_uuid"],
        )
        self.assertTrue(
            payload["create_conversation_params"]["is_temporary"]
        )
        self.assertNotIn("custom_system_prompt", payload)
        self.assertEqual(
            "native_tool_description",
            native_session.last_completion_shape()["context_channel"],
        )

    def test_non_sse_completion_error_is_raised_immediately(self) -> None:
        native_session = ClaudeSession(headless=True)
        with self.assertRaisesRegex(
            ClaudeCompletionRejectedError,
            "HTTP 400.*thinking_mode",
        ):
            native_session._process_native_event(
                {
                    "event": "__tap_http_error",
                    "data": json.dumps(
                        {
                            "status": 400,
                            "message": (
                                "thinking_mode must be extended, standard, "
                                "auto or off"
                            ),
                        }
                    ),
                }
            )

    def test_non_sse_limit_and_overload_are_typed(self) -> None:
        native_session = ClaudeSession(headless=True)
        with self.assertRaises(ClaudeUsageLimitError) as limited:
            native_session._process_native_event(
                {
                    "event": "__tap_http_error",
                    "data": '{"status":429,"message":"limited"}',
                }
            )
        self.assertTrue(limited.exception.replay_safe)
        with self.assertRaises(ClaudeServiceUnavailableError):
            native_session._process_native_event(
                {
                    "event": "__tap_http_error",
                    "data": '{"status":529,"message":"overloaded"}',
                }
            )

    def test_sse_eof_without_message_stop_fails_immediately(self) -> None:
        native_session = ClaudeSession(headless=True)
        with self.assertRaisesRegex(RuntimeError, "before message_stop"):
            native_session._process_native_event(
                {
                    "event": "__tap_eof",
                    "data": '{"frameCount":3}',
                }
            )
        native_session._native_terminal_seen = True
        self.assertFalse(
            native_session._process_native_event(
                {
                    "event": "__tap_eof",
                    "data": '{"frameCount":3}',
                }
            )
        )

    async def test_rejected_completion_does_not_kill_browser(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session.ready = True
        native_session._set_phase("idle")
        rejection = ClaudeCompletionRejectedError(
            400,
            "invalid thinking_mode",
        )
        with (
            patch.object(
                native_session,
                "_prepare_composer_unlocked",
                AsyncMock(),
            ),
            patch.object(
                native_session,
                "_submit_message",
                AsyncMock(),
            ),
            patch.object(
                native_session,
                "_await_native_outcome",
                AsyncMock(side_effect=rejection),
            ),
        ):
            with self.assertRaises(ClaudeCompletionRejectedError):
                await native_session.native_chat("hello", tools=[])
        self.assertFalse(native_session._browser_dead.is_set())
        self.assertFalse(native_session._history_recovery_required)
        self.assertEqual("idle", native_session._phase)

    async def test_project_instructions_are_verified_before_ready(self) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        organization_id = "44444444-4444-4444-4444-444444444444"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": project_id,
                    "organization_id": organization_id,
                }
            ],
            project_instructions="trusted IDE contract",
        )
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "ok": True,
                    "organizationUuid": organization_id,
                    "promptTemplate": "trusted IDE contract",
                    "privacyVerified": True,
                }
            )
        )
        self.assertTrue(await native_session._sync_trusted_project())
        self.assertTrue(native_session._project_instructions_synced)
        self.assertTrue(native_session._project_privacy_verified)
        self.assertEqual(
            organization_id,
            native_session.organization_uuid_for_internal_use(),
        )

    async def test_legacy_dynamic_project_prompt_is_recovered_once(self) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        organization_id = "44444444-4444-4444-4444-444444444444"
        stable = "trusted IDE contract"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": project_id,
                    "organization_id": organization_id,
                }
            ],
            project_instructions=stable,
        )
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "ok": True,
                    "organizationUuid": organization_id,
                    "promptTemplate": (
                        stable
                        + "\n\nDYNAMIC_OPENCLAUDE_SYSTEM_CONTEXT\n"
                        + "old request-scoped context"
                    ),
                    "privacyVerified": True,
                }
            )
        )
        write_prompt = AsyncMock()
        with patch.object(
            native_session,
            "_write_verified_project_prompt",
            write_prompt,
        ):
            self.assertTrue(await native_session._sync_trusted_project())
        write_prompt.assert_awaited_once_with(
            stable,
            expected_current=(
                stable
                + "\n\nDYNAMIC_OPENCLAUDE_SYSTEM_CONTEXT\n"
                + "old request-scoped context"
            ),
        )
        self.assertTrue(native_session._project_instructions_synced)
        self.assertIsNone(native_session._project_sync_error)

    async def test_known_previous_project_contract_is_migrated(self) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        organization_id = "44444444-4444-4444-4444-444444444444"
        previous = "previous OpenClaude-owned IDE contract"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": project_id,
                    "organization_id": organization_id,
                }
            ],
            project_instructions="current IDE contract",
        )
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "ok": True,
                    "organizationUuid": organization_id,
                    "promptTemplate": previous,
                    "privacyVerified": True,
                }
            )
        )
        previous_hash = hashlib.sha256(
            previous.encode("utf-8")
        ).hexdigest()
        write_prompt = AsyncMock()
        with (
            patch(
                "claude_web_api.session.claude.KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256",
                {previous_hash},
            ),
            patch.object(
                native_session,
                "_write_verified_project_prompt",
                write_prompt,
            ),
        ):
            self.assertTrue(await native_session._sync_trusted_project())
        write_prompt.assert_awaited_once_with(
            "current IDE contract",
            expected_current=previous,
        )

    def test_persisted_project_prompt_lease_allows_only_managed_upgrade(
        self,
    ) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        with tempfile.TemporaryDirectory() as directory:
            lease_file = Path(directory) / "project_prompt_leases.json"
            old_contract = "OpenClaude-owned contract v1"
            first = ClaudeSession(
                headless=True,
                profiles=[
                    {
                        "id": "default",
                        "path": str(Path(directory) / "profile"),
                        "project_id": project_id,
                    }
                ],
                project_instructions=old_contract,
                project_prompt_lease_file=lease_file,
            )
            self.assertTrue(
                first._record_project_prompt_lease(old_contract)
            )

            upgraded = ClaudeSession(
                headless=True,
                profiles=[
                    {
                        "id": "default",
                        "path": str(Path(directory) / "profile"),
                        "project_id": project_id,
                    }
                ],
                project_instructions="OpenClaude-owned contract v2",
                project_prompt_lease_file=lease_file,
            )
            self.assertEqual(
                "leased",
                upgraded._managed_project_prompt_kind(old_contract),
            )
            self.assertIsNone(
                upgraded._managed_project_prompt_kind(
                    "human-edited Project instructions"
                )
            )
            serialized = lease_file.read_text(encoding="utf-8")
            self.assertNotIn(old_contract, serialized)
            self.assertIn(
                hashlib.sha256(old_contract.encode("utf-8")).hexdigest(),
                serialized,
            )

    async def test_external_project_edit_is_preserved_and_blocks_sync(self) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        organization_id = "44444444-4444-4444-4444-444444444444"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": project_id,
                    "organization_id": organization_id,
                }
            ],
            project_instructions="trusted IDE contract",
        )
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "ok": True,
                    "organizationUuid": organization_id,
                    "promptTemplate": "human-edited project instructions",
                    "privacyVerified": True,
                }
            )
        )
        write_prompt = AsyncMock()
        with patch.object(
            native_session,
            "_write_verified_project_prompt",
            write_prompt,
        ):
            self.assertFalse(await native_session._sync_trusted_project())
        write_prompt.assert_not_awaited()
        self.assertFalse(native_session._project_instructions_synced)
        self.assertIn("external edit was preserved", native_session._project_sync_error)

    async def test_project_repair_preserves_edit_made_after_sync_read(self) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": project_id,
                }
            ],
            project_instructions="trusted IDE contract",
        )
        native_session._organization_uuid = (
            "44444444-4444-4444-4444-444444444444"
        )
        native_session.page = SimpleNamespace(evaluate=AsyncMock())
        with patch.object(
            native_session,
            "_read_verified_project_prompt",
            AsyncMock(return_value="newer human edit"),
        ):
            with self.assertRaisesRegex(
                ClaudeBrowserUnavailableError,
                "newer edit was preserved",
            ):
                await native_session._write_verified_project_prompt(
                    "trusted IDE contract",
                    expected_current=(
                        "trusted IDE contract"
                        + "\n\nDYNAMIC_OPENCLAUDE_SYSTEM_CONTEXT\nold"
                    ),
                )
        native_session.page.evaluate.assert_not_awaited()

    async def test_native_conversation_project_and_privacy_are_verified(
        self,
    ) -> None:
        project_id = "33333333-3333-3333-3333-333333333333"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": project_id,
                }
            ],
            project_instructions="trusted IDE contract",
        )
        native_session._native_org_uuid = (
            "44444444-4444-4444-4444-444444444444"
        )
        native_session._native_conversation_uuid = (
            "55555555-5555-5555-5555-555555555555"
        )
        native_session._privacy_mode = "ephemeral"
        native_session.page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "ok": True,
                    "projectUuid": project_id,
                    "isTemporary": True,
                }
            )
        )
        await native_session._verify_native_conversation_binding()
        self.assertTrue(native_session._native_conversation_verified)

        native_session._native_conversation_verified = False
        native_session.page.evaluate = AsyncMock(
            return_value={
                "ok": True,
                "projectUuid": project_id,
                "isTemporary": False,
            }
        )
        with self.assertRaisesRegex(RuntimeError, "persisted unexpectedly"):
            await native_session._verify_native_conversation_binding()

    async def test_turn_context_does_not_mutate_or_add_unsupported_field(
        self,
    ) -> None:
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": "33333333-3333-3333-3333-333333333333",
                }
            ],
            project_instructions="stable IDE contract",
        )
        runtime_context = (
            "Current host working directory: D:\\CodeWorks\\project"
        )
        native_session._native_active = True
        native_session._native_tools = [
            {
                "name": "Read",
                "description": runtime_context,
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        write_prompt = AsyncMock()
        read_prompt = AsyncMock(return_value="stable IDE contract")
        with (
            patch.object(
                native_session,
                "_write_verified_project_prompt",
                write_prompt,
            ),
            patch.object(
                native_session,
                "_read_verified_project_prompt",
                read_prompt,
            ),
        ):
            await native_session._activate_trusted_turn_context()
            route = SimpleNamespace(continue_=AsyncMock())
            request = SimpleNamespace(
                method="POST",
                url=(
                    "https://claude.ai/api/organizations/"
                    "11111111-1111-1111-1111-111111111111/"
                    "chat_conversations/"
                    "22222222-2222-2222-2222-222222222222/"
                    "completion"
                ),
                post_data_json={"model": "claude-web", "tools": []},
                headers={},
            )
            await native_session._route_completion(route, request)
            payload = json.loads(
                route.continue_.await_args.kwargs["post_data"]
            )
        read_prompt.assert_awaited_once()
        write_prompt.assert_not_awaited()
        self.assertNotIn("custom_system_prompt", payload)
        self.assertEqual(runtime_context, payload["tools"][0]["description"])

    async def test_retry_retracts_visible_stream_and_clears_parser_state(self) -> None:
        native_session = ClaudeSession(headless=True)
        emitted: list[dict] = []

        class Sink:
            visible_seen = True

            def __call__(self, event):
                emitted.append(event)

        native_session._native_active = True
        native_session._native_event_sink = Sink()
        native_session._native_completion_url = (
            "https://claude.ai/api/organizations/"
            "11111111-1111-1111-1111-111111111111/chat_conversations/"
            "22222222-2222-2222-2222-222222222222/completion"
        )
        native_session._native_thinking_blocks = {1: "summary"}
        native_session._native_usage = {"input_tokens": 1}
        native_session._native_model = "old-model"
        native_session._native_stop_reason = "max_tokens"
        route = SimpleNamespace(continue_=AsyncMock())
        request = SimpleNamespace(
            method="POST",
            url=(
                "https://claude.ai/api/organizations/"
                "11111111-1111-1111-1111-111111111111/chat_conversations/"
                "22222222-2222-2222-2222-222222222222/retry_completion"
            ),
            post_data_json={"model": "claude-web", "tools": []},
            headers={},
        )
        await native_session._route_completion(route, request)
        self.assertTrue(
            any(event.get("type") == "retract" for event in emitted)
        )
        self.assertEqual({}, native_session._native_thinking_blocks)
        self.assertEqual({}, native_session._native_usage)
        self.assertIsNone(native_session._native_model)
        self.assertIsNone(native_session._native_stop_reason)

    async def test_ephemeral_chat_marks_conversation_temporary(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._privacy_mode = "ephemeral"
        route = SimpleNamespace(continue_=AsyncMock())
        request = SimpleNamespace(
            method="POST",
            post_data_json={"name": "new chat"},
            headers={"content-length": "1"},
        )
        await native_session._route_conversation_create(route, request)
        payload = json.loads(
            route.continue_.await_args.kwargs["post_data"]
        )
        self.assertTrue(payload["is_temporary"])

    async def test_tool_result_rejects_another_openclaude_session(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_1"}
        native_session._native_pending_deadline = 10**12
        native_session._native_client_session_id = "session-a"
        with self.assertRaisesRegex(ValueError, "another OpenClaude session"):
            await native_session.continue_native(
                [{"tool_call_id": "toolu_1", "content": "ok"}],
                client_session_id="session-b",
            )

    async def test_pending_interruption_rechecks_session_and_ids(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_1"}
        native_session._native_pending_deadline = 10**12
        native_session._native_client_session_id = "session-a"

        with self.assertRaisesRegex(ValueError, "another OpenClaude session"):
            await native_session.abandon_pending_native(
                {"toolu_1"},
                client_session_id="session-b",
            )
        with self.assertRaisesRegex(RuntimeError, "IDs changed"):
            await native_session.abandon_pending_native(
                {"toolu_other"},
                client_session_id="session-a",
            )

        with (
            patch.object(
                native_session,
                "_ensure_healthy_unlocked",
                AsyncMock(),
            ),
            patch.object(
                native_session,
                "_new_chat_unlocked",
                AsyncMock(),
            ),
        ):
            abandoned = await native_session.abandon_pending_native(
                {"toolu_1"},
                client_session_id="session-a",
            )
        self.assertTrue(abandoned)
        self.assertFalse(native_session._native_active)
        self.assertTrue(native_session._history_recovery_required)


if __name__ == "__main__":
    unittest.main()
