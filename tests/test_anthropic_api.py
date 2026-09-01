"""Anthropic Messages API surface, the one Claude Code speaks."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_web_api import completions
from claude_web_api.api import anthropic as anthropic_api
from claude_web_api.app import app
from claude_web_api.protocol import anthropic as protocol
from claude_web_api.session.claude import NativeToolUse, NativeTurn


def native(
    content: str | None = "ok",
    tool_uses: list[NativeToolUse] | None = None,
    usage: dict[str, Any] | None = None,
) -> NativeTurn:
    return NativeTurn(
        content=content,
        tool_uses=tool_uses or [],
        usage=usage or {"input_tokens": 11, "output_tokens": 7},
        model="claude-sonnet-4-6",
    )


class MessagesEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.captured: list[completions.CompletionsIn] = []

    def run_with(self, turn: NativeTurn):
        async def fake_run(request_body, **kwargs):
            del kwargs
            self.captured.append(request_body)
            return turn

        return patch.object(
            completions, "run_native_with_limits", new=fake_run
        )

    def post(self, payload: dict[str, Any]):
        return self.client.post("/v1/messages", json=payload)

    def test_text_turn_returns_a_messages_response(self) -> None:
        with self.run_with(native("Привет")):
            response = self.post(
                {
                    "model": "claude-web",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "привет"}],
                }
            )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertTrue(body["id"].startswith("msg_"))
        self.assertEqual("message", body["type"])
        self.assertEqual("assistant", body["role"])
        self.assertEqual([{"type": "text", "text": "Привет"}], body["content"])
        self.assertEqual("end_turn", body["stop_reason"])
        self.assertIsNone(body["stop_sequence"])
        self.assertEqual(
            {"input_tokens": 11, "output_tokens": 7}, body["usage"]
        )

    def test_tool_call_is_returned_as_a_tool_use_block(self) -> None:
        turn = native(
            "смотрю файл",
            [NativeToolUse(id="toolu_1", name="Read", input={"path": "a.py"})],
        )
        with self.run_with(turn):
            body = self.post(
                {
                    "model": "claude-web",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "прочитай a.py"}],
                    "tools": [
                        {
                            "name": "Read",
                            "description": "Read a file",
                            "input_schema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"],
                            },
                        }
                    ],
                }
            ).json()
        self.assertEqual("tool_use", body["stop_reason"])
        self.assertEqual(
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "Read",
                "input": {"path": "a.py"},
            },
            body["content"][1],
        )
        sent_tools = self.captured[0].tools
        self.assertEqual("Read", sent_tools[0]["function"]["name"])
        self.assertIn("path", sent_tools[0]["function"]["parameters"]["properties"])

    def test_tool_result_continues_the_same_turn(self) -> None:
        """The Claude Code loop: a tool_result must reach the bridge bound to
        the tool_use it answers, or the turn restarts instead of continuing."""
        with self.run_with(native("готово")):
            response = self.post(
                {
                    "model": "claude-web",
                    "max_tokens": 1024,
                    "messages": [
                        {"role": "user", "content": "прочитай a.py"},
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "смотрю"},
                                {
                                    "type": "tool_use",
                                    "id": "toolu_1",
                                    "name": "Read",
                                    "input": {"path": "a.py"},
                                },
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "content": "print('hi')",
                                }
                            ],
                        },
                    ],
                }
            )
        self.assertEqual(200, response.status_code)
        history = self.captured[0].messages
        assistant = history[1]
        self.assertEqual("смотрю", assistant["content"])
        self.assertEqual("toolu_1", assistant["tool_calls"][0]["id"])
        self.assertEqual(
            {"path": "a.py"},
            json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
        )
        result = history[2]
        self.assertEqual("tool", result["role"])
        self.assertEqual("toolu_1", result["tool_call_id"])
        self.assertEqual("print('hi')", result["content"])

    def test_note_beside_a_tool_result_travels_with_it(self) -> None:
        """OpenClaude packs a result and its note into one user message.

        Emitting the note as its own turn reads as the user interrupting the
        continuation, and the completions core refuses that outright.
        """
        with self.run_with(native("ok")):
            self.post(
                {
                    "model": "claude-web",
                    "max_tokens": 64,
                    "messages": [
                        {"role": "user", "content": "read it"},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_5",
                                    "name": "Read",
                                    "input": {},
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_5",
                                    "content": "file body",
                                },
                                {"type": "text", "text": "note about it"},
                            ],
                        },
                    ],
                }
            )
        history = self.captured[0].messages
        self.assertEqual("tool", history[-1]["role"])
        self.assertIn("file body", history[-1]["content"])
        self.assertIn("note about it", history[-1]["content"])
        self.assertEqual(
            1, sum(1 for entry in history if entry["role"] == "tool")
        )

    def test_upstream_http_error_uses_the_anthropic_shape(self) -> None:
        """A rejection from deeper in the stack must stay parseable."""

        async def rejecting(request_body, **kwargs):
            del request_body, kwargs
            raise HTTPException(400, "Model 'x' is not available")

        with patch.object(completions, "run_native_with_limits", new=rejecting):
            response = self.post(
                {
                    "model": "x",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
        self.assertEqual(400, response.status_code)
        body = response.json()
        self.assertEqual("error", body["type"])
        self.assertIn("not available", body["error"]["message"])

    def test_error_tool_result_keeps_its_flag(self) -> None:
        with self.run_with(native("ясно")):
            self.post(
                {
                    "model": "claude-web",
                    "max_tokens": 1024,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_9",
                                    "content": "ENOENT",
                                    "is_error": True,
                                }
                            ],
                        }
                    ],
                }
            )
        self.assertTrue(self.captured[0].messages[0]["is_error"])

    def test_system_prompt_reaches_the_bridge_in_both_forms(self) -> None:
        for system in ("be terse", [{"type": "text", "text": "be terse"}]):
            self.captured.clear()
            with self.run_with(native("ok")):
                self.post(
                    {
                        "model": "claude-web",
                        "max_tokens": 16,
                        "system": system,
                        "messages": [{"role": "user", "content": "hi"}],
                    }
                )
            first = self.captured[0].messages[0]
            self.assertEqual("system", first["role"])
            self.assertEqual("be terse", first["content"])

    def test_mid_conversation_system_message_is_carried(self) -> None:
        """Claude Code sends operator instructions as their own system turn."""
        with self.run_with(native("ok")):
            response = self.post(
                {
                    "model": "claude-web",
                    "max_tokens": 64,
                    "messages": [
                        {"role": "user", "content": "hi"},
                        {"role": "system", "content": "be terse"},
                    ],
                }
            )
        self.assertEqual(200, response.status_code)
        history = self.captured[0].messages
        self.assertEqual(
            {"role": "system", "content": "be terse"},
            history[0],
        )
        self.assertNotIn("system", [entry["role"] for entry in history[1:]])

    def test_unsupported_block_is_refused_not_dropped(self) -> None:
        response = self.post(
            {
                "model": "claude-web",
                "max_tokens": 16,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "data": "x"},
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(400, response.status_code)
        body = response.json()
        self.assertEqual("invalid_request_error", body["error"]["type"])
        self.assertIn("image", body["error"]["message"])

    def test_upstream_failure_uses_the_anthropic_error_shape(self) -> None:
        async def failing(request_body, **kwargs):
            del request_body, kwargs
            raise ValueError("no browser profile is ready")

        with patch.object(completions, "run_native_with_limits", new=failing):
            response = self.post(
                {
                    "model": "claude-web",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
        self.assertEqual(400, response.status_code)
        body = response.json()
        self.assertEqual("error", body["type"])
        self.assertEqual("invalid_request_error", body["error"]["type"])
        self.assertIn("browser profile", body["error"]["message"])

    def test_missing_max_tokens_is_rejected_in_the_anthropic_shape(
        self,
    ) -> None:
        """A schema failure must still be parseable by an Anthropic client."""
        response = self.post(
            {
                "model": "claude-web",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        self.assertEqual(400, response.status_code)
        body = response.json()
        self.assertEqual("error", body["type"])
        self.assertEqual("invalid_request_error", body["error"]["type"])
        self.assertIn("max_tokens", body["error"]["message"])

    def test_other_endpoints_keep_the_framework_error_shape(self) -> None:
        response = self.client.post("/v1/chat/completions", json={})
        self.assertEqual(422, response.status_code)
        self.assertIn("detail", response.json())


class MessagesStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def stream_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        with self.client.stream("POST", "/v1/messages", json=payload) as raw:
            text = "".join(chunk for chunk in raw.iter_text())
        events = []
        for block in text.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

    def test_stream_follows_the_messages_event_sequence(self) -> None:
        async def fake_run(request_body, *, event_sink=None, **kwargs):
            del request_body, kwargs
            if event_sink is not None:
                event_sink({"type": "text_delta", "text": "При"})
                event_sink({"type": "text_delta", "text": "вет"})
            return native("Привет")

        with patch.object(completions, "run_native_with_limits", new=fake_run):
            events = self.stream_events(
                {
                    "model": "claude-web",
                    "max_tokens": 64,
                    "stream": True,
                    "messages": [{"role": "user", "content": "привет"}],
                }
            )
        kinds = [event["type"] for event in events]
        self.assertEqual("message_start", kinds[0])
        self.assertEqual("message_stop", kinds[-1])
        self.assertEqual("message_delta", kinds[-2])
        self.assertIn("content_block_start", kinds)
        self.assertIn("content_block_stop", kinds)
        deltas = [
            event["delta"]["text"]
            for event in events
            if event["type"] == "content_block_delta"
        ]
        self.assertEqual("Привет", "".join(deltas))
        final = events[-2]
        self.assertEqual("end_turn", final["delta"]["stop_reason"])
        self.assertEqual(7, final["usage"]["output_tokens"])

    def test_streamed_tool_call_sends_its_input_as_json_delta(self) -> None:
        turn = native(
            None,
            [NativeToolUse(id="toolu_7", name="Bash", input={"cmd": "ls"})],
        )

        async def fake_run(request_body, **kwargs):
            del request_body, kwargs
            return turn

        with patch.object(completions, "run_native_with_limits", new=fake_run):
            events = self.stream_events(
                {
                    "model": "claude-web",
                    "max_tokens": 64,
                    "stream": True,
                    "messages": [{"role": "user", "content": "ls"}],
                }
            )
        starts = [
            event
            for event in events
            if event["type"] == "content_block_start"
            and event["content_block"]["type"] == "tool_use"
        ]
        self.assertEqual("toolu_7", starts[0]["content_block"]["id"])
        partial = [
            event["delta"]["partial_json"]
            for event in events
            if event["type"] == "content_block_delta"
            and event["delta"]["type"] == "input_json_delta"
        ]
        self.assertEqual({"cmd": "ls"}, json.loads(partial[0]))
        self.assertEqual("tool_use", events[-2]["delta"]["stop_reason"])

    def test_stream_failure_is_reported_as_an_error_event(self) -> None:
        async def failing(request_body, **kwargs):
            del request_body, kwargs
            raise RuntimeError("claude.ai closed the stream")

        with patch.object(completions, "run_native_with_limits", new=failing):
            events = self.stream_events(
                {
                    "model": "claude-web",
                    "max_tokens": 64,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("api_error", events[-1]["error"]["type"])


class TranslationTests(unittest.TestCase):
    def test_tool_choice_maps_onto_the_bridge_vocabulary(self) -> None:
        self.assertEqual("auto", protocol.bridge_tool_choice({"type": "auto"}))
        self.assertEqual("required", protocol.bridge_tool_choice({"type": "any"}))
        self.assertEqual("none", protocol.bridge_tool_choice({"type": "none"}))
        self.assertEqual(
            {"type": "function", "function": {"name": "Read"}},
            protocol.bridge_tool_choice({"type": "tool", "name": "Read"}),
        )
        self.assertIsNone(protocol.bridge_tool_choice(None))

    def test_disable_parallel_tool_use_is_honoured(self) -> None:
        self.assertTrue(protocol.parallel_tool_calls({"type": "auto"}))
        self.assertFalse(
            protocol.parallel_tool_calls(
                {"type": "auto", "disable_parallel_tool_use": True}
            )
        )

    def test_server_side_tools_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "browser turn"):
            protocol.bridge_tools(
                [{"type": "web_search_20260209", "name": "web_search"}]
            )

    def test_usage_without_upstream_counts_is_zero_not_invented(self) -> None:
        self.assertEqual(
            {"input_tokens": 0, "output_tokens": 0},
            protocol.anthropic_usage({}),
        )

    def test_cache_counters_pass_through_when_present(self) -> None:
        usage = protocol.anthropic_usage(
            {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_input_tokens": 100,
            }
        )
        self.assertEqual(100, usage["cache_read_input_tokens"])

    def test_replayed_thinking_blocks_are_ignored(self) -> None:
        body = protocol.MessagesIn(
            max_tokens=16,
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hmm", "signature": "s"},
                        {"type": "text", "text": "answer"},
                    ],
                }
            ],
        )
        history = protocol.bridge_messages(body)
        self.assertEqual([{"role": "assistant", "content": "answer"}], history)


class CountTokensTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_estimate_grows_with_the_prompt(self) -> None:
        short = self.client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-web",
                "messages": [{"role": "user", "content": "hi"}],
            },
        ).json()
        long = self.client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-web",
                "messages": [{"role": "user", "content": "hi " * 500}],
            },
        ).json()
        self.assertGreaterEqual(short["input_tokens"], 1)
        self.assertGreater(long["input_tokens"], short["input_tokens"])

    def test_estimate_counts_tools_and_system(self) -> None:
        body = protocol.CountTokensIn(
            messages=[{"role": "user", "content": "hi"}],
            system="a system prompt that is clearly longer than the message",
            tools=[{"name": "Read", "input_schema": {"type": "object"}}],
        )
        bare = protocol.CountTokensIn(
            messages=[{"role": "user", "content": "hi"}]
        )
        self.assertGreater(
            protocol.estimated_input_tokens(body),
            protocol.estimated_input_tokens(bare),
        )


class ErrorShapeTests(unittest.TestCase):
    def test_status_codes_map_to_anthropic_error_types(self) -> None:
        self.assertEqual(
            "rate_limit_error",
            anthropic_api.error_payload(429, "slow down")["error"]["type"],
        )
        self.assertEqual(
            "overloaded_error",
            anthropic_api.error_payload(503, "browser down")["error"]["type"],
        )
        self.assertEqual(
            "api_error",
            anthropic_api.error_payload(500, "boom")["error"]["type"],
        )


if __name__ == "__main__":
    unittest.main()
