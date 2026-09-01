"""TranslationTests and friends, split out of the original suite."""

from __future__ import annotations

import json
import unittest

from claude_web_api.paths import PROJECT_INSTRUCTIONS
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
from tests.support import SYSTEM, TOOLS


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


if __name__ == "__main__":
    unittest.main()
