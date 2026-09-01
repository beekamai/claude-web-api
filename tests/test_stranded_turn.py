"""Recovering from a turn the client walked away from.

Claude Code interrupts turns: Ctrl+C during a tool call leaves the browser
session waiting for a result nobody will send. Until the lease expires that
pending call must not fail every later request.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from claude_web_api import completions, runtime
from claude_web_api.session.models import NativeTurn

PENDING = {"toolu_stranded"}


def answer(text: str = "ok") -> NativeTurn:
    return NativeTurn(content=text, tool_uses=[], model="claude-sonnet-5")


class StrandedTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.abandon = AsyncMock(return_value=True)
        self.state = AsyncMock(return_value=(set(PENDING), False))
        self.continued = AsyncMock()

    def bridge(self):
        """Patch the session so a stranded tool call is pending."""
        return (
            patch.object(runtime, "persist_runtime_identity", return_value=True),
            patch.object(runtime.session, "native_request_state", self.state),
            patch.object(runtime.session, "abandon_pending_native", self.abandon),
            patch.object(
                runtime.claude_provider,
                "continue_with_tool_results",
                self.continued,
            ),
        )

    async def run_request(self, body: completions.CompletionsIn) -> Any:
        native = AsyncMock(return_value=answer())
        patches = (*self.bridge(), patch.object(runtime.session, "native_chat", native))
        for entry in patches:
            entry.start()
            self.addCleanup(entry.stop)
        return await completions.native_request(body)

    async def test_plain_question_releases_the_stranded_call(self) -> None:
        """The interrupted client asks something else; the turn must proceed."""
        turn = await self.run_request(
            completions.CompletionsIn(
                messages=[{"role": "user", "content": "what time is it?"}],
            )
        )
        self.abandon.assert_awaited_once()
        self.assertEqual(PENDING, self.abandon.await_args.args[0])
        self.continued.assert_not_awaited()
        self.assertEqual("ok", turn.content)

    async def test_starting_over_does_not_replay_a_transcript(self) -> None:
        """Recovery exists to rebuild a reset chat. A client that is itself
        starting over has nothing to rebuild, and the recovery preamble talks
        Claude out of using its tools."""
        recovered = AsyncMock()
        native = AsyncMock(return_value=answer())
        patches = (
            *self.bridge(),
            patch.object(runtime.session, "native_chat", native),
            patch.object(runtime.session, "mark_history_recovered", recovered),
        )
        for entry in patches:
            entry.start()
            self.addCleanup(entry.stop)

        await completions.native_request(
            completions.CompletionsIn(
                messages=[{"role": "user", "content": "something else"}],
            )
        )
        recovered.assert_awaited_once()
        sent = native.await_args.kwargs.get("message") or native.await_args.args[0]
        self.assertNotIn("Recover the IDE task", str(sent))

    async def test_explicit_new_chat_releases_the_stranded_call(self) -> None:
        await self.run_request(
            completions.CompletionsIn(
                new_chat=True,
                messages=[
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "second"},
                    {"role": "user", "content": "start over"},
                ],
            )
        )
        self.abandon.assert_awaited_once()

    async def test_matching_tool_result_still_continues_the_turn(self) -> None:
        """The guard must not swallow a legitimate continuation."""
        self.continued.return_value = AsyncMock()
        native = AsyncMock(return_value=answer())
        patches = (*self.bridge(), patch.object(runtime.session, "native_chat", native))
        for entry in patches:
            entry.start()
            self.addCleanup(entry.stop)

        body = completions.CompletionsIn(
            messages=[
                {"role": "user", "content": "read a.py"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "toolu_stranded",
                            "type": "function",
                            "function": {"name": "Read", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_stranded",
                    "content": "print('hi')",
                },
            ]
        )
        with patch.object(completions, "_provider_turn_as_native") as as_native:
            as_native.return_value = answer("continued")
            turn = await completions.native_request(body)

        self.continued.assert_awaited_once()
        self.abandon.assert_not_awaited()
        self.assertEqual("continued", turn.content)

    async def test_unrelated_tool_result_is_still_refused(self) -> None:
        """A result for a call the browser is not waiting on stays an error."""
        for entry in self.bridge():
            entry.start()
            self.addCleanup(entry.stop)

        body = completions.CompletionsIn(
            messages=[
                {"role": "user", "content": "read a.py"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "toolu_other",
                            "type": "function",
                            "function": {"name": "Read", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_other",
                    "content": "print('hi')",
                },
            ]
        )
        with self.assertRaisesRegex(ValueError, "pending"):
            await completions.native_request(body)
        self.abandon.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
