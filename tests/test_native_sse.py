"""NativeSseParserTests and friends, split out of the original suite."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from claude_web_api.protocol.openai import (
    OPENCLAUDE_CONTEXT_TOOL_NAME,
    attach_runtime_context,
    native_tools,
)
from claude_web_api.session.claude import (
    ClaudeSession,
    ClaudeUsageLimitError,
    NativeToolUse,
)
from tests.support import TOOLS


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


if __name__ == "__main__":
    unittest.main()
