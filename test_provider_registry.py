import unittest
from types import SimpleNamespace

from claude_web_provider import (
    CLAUDE_WEB_PROVIDER_ID,
    ClaudeWebProviderAdapter,
)
from provider_contracts import (
    CompletionProvider,
    ProviderEventKind,
    ProviderToolResult,
    ProviderTurnRequest,
)
from provider_registry import (
    ProfileRouteError,
    ProviderRegistry,
    ProviderRegistryError,
)


class FakeClaudeSession:
    def __init__(self) -> None:
        self.calls = []
        self.native_events = []

    async def start(self) -> None:
        self.calls.append(("start",))

    async def stop(self) -> None:
        self.calls.append(("stop",))

    async def new_chat(self) -> None:
        self.calls.append(("new_chat",))

    async def native_chat(self, message, **kwargs):
        self.calls.append(("native_chat", message, kwargs))
        sink = kwargs.get("event_sink")
        if sink is not None:
            for event in self.native_events:
                sink(event)
        return SimpleNamespace(
            content="answer",
            tool_uses=[
                SimpleNamespace(
                    id="tool-1",
                    name="Read",
                    input={"path": "README.md"},
                )
            ],
            thinking="summary",
            usage={"output_tokens": 7},
            model="claude-test",
            stop_reason="tool_use",
        )

    async def continue_native(self, results, **kwargs):
        self.calls.append(("continue_native", results, kwargs))
        return SimpleNamespace(
            content="continued",
            tool_uses=[],
            thinking=None,
            usage={"output_tokens": 3},
            model="claude-test",
            stop_reason="end_turn",
        )

    def current_profile_spec(self):
        return {"id": "claude-a", "name": "Claude A"}

    def current_profile_id(self):
        return "claude-a"

    def account_uuid_for_internal_use(self):
        return "account-uuid"

    def organization_uuid_for_internal_use(self):
        return "organization-uuid"

    def health_snapshot(self):
        return {
            "ok": True,
            "account": {"name": "Alice", "email": "a***@example.test"},
            "browser": {"phase": "idle", "last_error": None},
        }


class ClaudeWebProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = FakeClaudeSession()
        self.provider = ClaudeWebProviderAdapter(
            self.session,
            internal_tool_names={"OpenClaudeContext"},
        )

    def test_adapter_implements_contract_and_reports_identity_health(self) -> None:
        self.assertIsInstance(self.provider, CompletionProvider)
        self.assertEqual("side_channel", self.provider.capabilities.tool_continuation)
        self.assertTrue(self.provider.capabilities.streaming)
        self.assertTrue(self.provider.capabilities.thinking)

        identity = self.provider.profile_identity
        self.assertIsNotNone(identity)
        self.assertEqual(CLAUDE_WEB_PROVIDER_ID, identity.provider)
        self.assertEqual("claude-a", identity.profile_id)
        self.assertEqual("a***@example.test", identity.account_email_masked)
        self.assertEqual("organization-uuid", identity.organization_id)

        health = self.provider.health()
        self.assertTrue(health.live)
        self.assertTrue(health.ready)
        self.assertEqual("idle", health.phase)

    async def test_lifecycle_delegates_to_existing_session(self) -> None:
        await self.provider.start()
        await self.provider.new_conversation()
        await self.provider.stop()
        self.assertEqual(
            [("start",), ("new_chat",), ("stop",)],
            self.session.calls,
        )

    async def test_complete_preserves_native_options_and_normalizes_result(self):
        self.session.native_events = [
            {"type": "model", "model": "claude-test"},
            {"type": "thinking_delta", "index": 0, "thinking": "why"},
            {"type": "usage", "usage": {"output_tokens": 2}},
            {"type": "text_delta", "index": 1, "text": "hello"},
            {"type": "retract", "from_index": 1},
        ]
        events = []
        request = ProviderTurnRequest(
            message="question",
            tools=({"name": "Read"},),
            timeout_seconds=12.5,
            new_conversation=True,
            parallel_tool_calls=False,
            model="claude-test",
            reasoning_mode="extended",
            reasoning_effort="high",
            privacy_mode="ephemeral",
            client_session_id="client-a",
        )

        turn = await self.provider.complete_native(
            request,
            recovery_message="full recovered context",
            event_sink=events.append,
        )

        call = self.session.calls[-1]
        self.assertEqual(("native_chat", "question"), call[:2])
        options = call[2]
        self.assertEqual([{"name": "Read"}], options["tools"])
        self.assertEqual(
            {"OpenClaudeContext"},
            options["internal_tool_names"],
        )
        self.assertEqual("full recovered context", options["recovery_message"])
        self.assertEqual(12.5, options["timeout"])
        self.assertTrue(options["new_chat"])
        self.assertFalse(options["parallel_tool_calls"])
        self.assertEqual("extended", options["thinking_mode"])
        self.assertEqual("high", options["effort"])
        self.assertEqual("ephemeral", options["privacy_mode"])
        self.assertEqual("client-a", options["client_session_id"])

        self.assertEqual("answer", turn.content)
        self.assertEqual("Read", turn.tool_uses[0].name)
        self.assertEqual({"path": "README.md"}, turn.tool_uses[0].input)
        self.assertEqual({"output_tokens": 7}, turn.usage)
        self.assertEqual(
            [
                ProviderEventKind.MODEL,
                ProviderEventKind.THINKING_DELTA,
                ProviderEventKind.USAGE,
                ProviderEventKind.TEXT_DELTA,
                ProviderEventKind.RETRACT,
            ],
            [event.kind for event in events],
        )
        self.assertEqual("why", events[1].text)
        self.assertEqual(1, events[-1].metadata["from_index"])

    async def test_tool_result_continuation_keeps_side_channel_shape(self):
        turn = await self.provider.continue_with_tool_results(
            [
                ProviderToolResult(
                    tool_use_id="tool-1",
                    name="Read",
                    content="contents",
                    is_error=False,
                )
            ],
            timeout_seconds=8,
            client_session_id="client-a",
        )
        call = self.session.calls[-1]
        self.assertEqual("continue_native", call[0])
        self.assertEqual(
            [
                {
                    "tool_call_id": "tool-1",
                    "name": "Read",
                    "content": "contents",
                    "is_error": False,
                }
            ],
            call[1],
        )
        self.assertEqual(8, call[2]["timeout"])
        self.assertEqual("client-a", call[2]["client_session_id"])
        self.assertEqual("continued", turn.content)


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.claude_session = FakeClaudeSession()
        self.claude = ClaudeWebProviderAdapter(self.claude_session)
        self.other = ClaudeWebProviderAdapter(FakeClaudeSession())
        self.registry = ProviderRegistry()

    def test_register_route_and_capability_snapshot(self) -> None:
        self.registry.register(
            "claude_web",
            self.claude,
            profile_ids=("claude-a",),
        )

        self.assertIs(self.claude, self.registry.get("claude_web"))
        self.assertIs(
            self.claude,
            self.registry.resolve(profile_id="claude-a"),
        )
        self.assertEqual(("claude-a",), self.registry.profiles_for_provider("claude_web"))
        self.assertEqual(
            {
                "claude_web": {
                    "tool_continuation": "side_channel",
                    "streaming": True,
                    "thinking": True,
                    "profiles": True,
                }
            },
            self.registry.capabilities_snapshot(),
        )

    def test_profile_conflict_and_provider_mismatch_are_rejected(self) -> None:
        self.registry.register(
            "claude_web",
            self.claude,
            profile_ids=("shared",),
        )
        self.registry.register("grok_web", self.other)

        with self.assertRaises(ProfileRouteError):
            self.registry.bind_profile("shared", "grok_web")
        with self.assertRaises(ProfileRouteError):
            self.registry.resolve(
                provider_id="grok_web",
                profile_id="shared",
            )

    def test_ambiguous_resolution_needs_route_or_provider(self) -> None:
        self.registry.register("claude_web", self.claude)
        self.registry.register("grok_web", self.other)

        with self.assertRaises(ProviderRegistryError):
            self.registry.resolve()
        self.assertIs(
            self.other,
            self.registry.resolve(provider_id="grok_web"),
        )

    def test_unregister_removes_profile_routes(self) -> None:
        self.registry.register(
            "claude_web",
            self.claude,
            profile_ids=("claude-a",),
        )
        self.assertIs(self.claude, self.registry.unregister("claude_web"))
        with self.assertRaises(ProfileRouteError):
            self.registry.provider_id_for_profile("claude-a")


if __name__ == "__main__":
    unittest.main()
