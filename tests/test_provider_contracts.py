import unittest

from claude_web_api.providers.contracts import (
    CompletionProvider,
    ProviderCapabilities,
    ProviderEvent,
    ProviderEventKind,
    ProviderHealth,
    ProviderProfileIdentity,
    ProviderToolResult,
    ProviderToolUse,
    ProviderTurn,
    ProviderTurnRequest,
    ToolContinuation,
)


class FakeProvider:
    capabilities = ProviderCapabilities(
        tool_continuation=ToolContinuation.NEXT_REQUEST,
        thinking=True,
    )
    profile_identity = ProviderProfileIdentity(
        provider="fake",
        profile_id="profile-1",
        display_name="Test profile",
    )

    def health(self) -> ProviderHealth:
        return ProviderHealth(live=True, ready=True, phase="idle")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def new_conversation(self) -> None:
        return None

    async def complete(
        self,
        request: ProviderTurnRequest,
        *,
        event_sink=None,
    ) -> ProviderTurn:
        if event_sink is not None:
            event_sink(
                ProviderEvent(
                    kind=ProviderEventKind.TEXT_DELTA,
                    text=request.message,
                )
            )
        return ProviderTurn(content=request.message)

    async def continue_with_tool_results(
        self,
        results,
        *,
        timeout_seconds=300.0,
        client_session_id=None,
        event_sink=None,
    ) -> ProviderTurn:
        del timeout_seconds, client_session_id, event_sink
        return ProviderTurn(content=results[0].content)


class ProviderContractsTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_continuation_values_are_transport_stable(self) -> None:
        self.assertEqual("side_channel", ToolContinuation.SIDE_CHANNEL.value)
        self.assertEqual("next_request", ToolContinuation.NEXT_REQUEST.value)
        self.assertEqual("unsupported", ToolContinuation.UNSUPPORTED.value)
        self.assertEqual("usage", ProviderEventKind.USAGE.value)

    def test_normalized_turn_defaults_are_not_shared(self) -> None:
        first = ProviderTurn(content=None)
        second = ProviderTurn(content=None)

        self.assertEqual((), first.tool_uses)
        self.assertEqual({}, first.usage)
        self.assertIsNot(first.usage, second.usage)

    def test_turn_preserves_normalized_tool_use(self) -> None:
        tool = ProviderToolUse(
            id="tool-1",
            name="Read",
            input={"path": "README.md"},
        )
        turn = ProviderTurn(
            content=None,
            tool_uses=(tool,),
            model="test-model",
            stop_reason="tool_use",
        )

        self.assertEqual("tool-1", turn.tool_uses[0].id)
        self.assertEqual("README.md", turn.tool_uses[0].input["path"])
        self.assertEqual("tool_use", turn.stop_reason)

    async def test_protocol_is_structural_and_carries_events(self) -> None:
        provider = FakeProvider()
        events: list[ProviderEvent] = []

        self.assertIsInstance(provider, CompletionProvider)
        turn = await provider.complete(
            ProviderTurnRequest(message="hello"),
            event_sink=events.append,
        )

        self.assertEqual("hello", turn.content)
        self.assertEqual(ProviderEventKind.TEXT_DELTA, events[0].kind)
        self.assertEqual("hello", events[0].text)

    async def test_tool_result_continuation_is_provider_neutral(self) -> None:
        provider = FakeProvider()
        turn = await provider.continue_with_tool_results(
            [
                ProviderToolResult(
                    tool_use_id="tool-1",
                    content="file contents",
                )
            ],
            client_session_id="session-1",
        )

        self.assertEqual("file contents", turn.content)


if __name__ == "__main__":
    unittest.main()
