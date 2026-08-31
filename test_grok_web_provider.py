from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from grok_web_provider import (
    GROK_PROTOCOL_SCHEMA,
    GROK_PROBE_SCHEMA,
    GROK_WEB_ORIGIN,
    GROK_WEB_PROVIDER_ID,
    GrokBrowserTransport,
    GrokCapabilityUnavailableError,
    GrokModelCatalogEntry,
    GrokModelEntitlement,
    GrokProtocolVerifier,
    GrokProtocolUnverifiedError,
    GrokProtocolViolationError,
    GrokProviderNotReadyError,
    GrokWebProtocolDescriptor,
    GrokWebProvider,
)
from provider_contracts import (
    CompletionProvider,
    ProviderEvent,
    ProviderEventKind,
    ProviderHealth,
    ProviderProfileIdentity,
    ProviderToolResult,
    ProviderToolUse,
    ProviderTurn,
    ProviderTurnRequest,
)


DIGEST = "a" * 64
SECOND_DIGEST = "b" * 64
TRACE_CAPTURE_ID = "1" * 32
TRACE_JSONL = "".join(
    json.dumps(record, separators=(",", ":"))
    + "\n"
    for record in [
        {
            "schema": GROK_PROBE_SCHEMA,
            "time": "2026-07-26T18:00:00+00:00",
            "capture_id": TRACE_CAPTURE_ID,
            "event": event,
            **payload,
        }
        for event, payload in (
            ("probe_started", {}),
            ("browser_ready", {}),
            ("login_status", {"logged_in": True}),
            ("capture_armed", {}),
            (
                "request",
                {
                    "url": {
                        "origin": GROK_WEB_ORIGIN,
                        "path_template": (
                            "/verified/{conversation_id}/completion"
                        ),
                    },
                    "method": "POST",
                },
            ),
            (
                "response",
                {
                    "url": {
                        "origin": GROK_WEB_ORIGIN,
                        "path_template": (
                            "/verified/{conversation_id}/completion"
                        ),
                    },
                    "status": 200,
                },
            ),
            (
                "stream_open",
                {
                    "protocol": "sse",
                    "url": {
                        "origin": GROK_WEB_ORIGIN,
                        "path_template": (
                            "/verified/{conversation_id}/completion"
                        ),
                    },
                },
            ),
            (
                "stream_frame",
                {
                    "protocol": "sse",
                    "data_shape": {"type": "object"},
                    "url": {
                        "origin": GROK_WEB_ORIGIN,
                        "path_template": (
                            "/verified/{conversation_id}/completion"
                        ),
                    },
                },
            ),
            (
                "stream_complete",
                {
                    "url": {
                        "origin": GROK_WEB_ORIGIN,
                        "path_template": (
                            "/verified/{conversation_id}/completion"
                        ),
                    },
                },
            ),
            (
                "request",
                {
                    "url": {
                        "origin": GROK_WEB_ORIGIN,
                        "path_template": (
                            "/verified/{conversation_id}/completion"
                        ),
                    },
                    "method": "POST",
                },
            ),
            (
                "response",
                {
                    "url": {
                        "origin": GROK_WEB_ORIGIN,
                        "path_template": (
                            "/verified/{conversation_id}/completion"
                        ),
                    },
                    "status": 200,
                },
            ),
            ("capture_disarmed", {}),
            ("probe_finished", {}),
        )
    ]
)
TRACE_DIGEST = hashlib.sha256(TRACE_JSONL.encode("utf-8")).hexdigest()


def descriptor_mapping(**overrides):
    value = {
        "schema": GROK_PROTOCOL_SCHEMA,
        "verified": True,
        "verified_at": "2026-07-26T18:00:00+00:00",
        "evidence_sha256": TRACE_DIGEST,
        "origin": GROK_WEB_ORIGIN,
        "endpoint_path": "/verified/{conversation_id}/completion",
        "method": "POST",
        "stream_protocol": "none",
        "streaming_verified": False,
        "stream_evidence_sha256": None,
        "thinking_verified": False,
        "thinking_evidence_sha256": None,
        "tool_continuation": "unsupported",
        "custom_tools_accepted": False,
        "tool_call_observed": False,
        "tool_result_continuation_observed": False,
        "tool_evidence_sha256": None,
    }
    value.update(overrides)
    return value


def verified_descriptor(**overrides):
    return GrokProtocolVerifier.verify_sanitized_trace(
        descriptor_mapping(**overrides),
        trace_jsonl=TRACE_JSONL,
    )


class FakeGrokTransport:
    def __init__(self) -> None:
        self.calls = []
        self.started = False
        self.authenticated = True
        self.browser_ready = True
        self.identity_provider = GROK_WEB_PROVIDER_ID
        self.identity_profile_id = "grok-a"
        self.catalog = []
        self.turn = ProviderTurn(
            content="browser answer",
            model="grok-observed",
            stop_reason="end_turn",
        )
        self.events = []

    async def start_browser(
        self,
        *,
        profile_path,
        profile_id,
        origin,
    ):
        self.calls.append(
            ("start_browser", profile_path, profile_id, origin)
        )
        self.started = True

    async def stop_browser(self):
        self.calls.append(("stop_browser",))
        self.started = False

    async def new_browser_conversation(self):
        self.calls.append(("new_browser_conversation",))

    def browser_health(self):
        return ProviderHealth(
            live=self.started,
            ready=self.started and self.browser_ready,
            phase="idle" if self.started else "stopped",
        )

    def authenticated_profile_identity(self):
        if not self.started or not self.authenticated:
            return None
        return ProviderProfileIdentity(
            provider=self.identity_provider,
            profile_id=self.identity_profile_id,
            display_name="Grok account",
            account_email_masked="g***@example.test",
        )

    def observed_model_catalog(self):
        return self.catalog

    async def complete_same_origin(
        self,
        request,
        *,
        descriptor,
        event_sink,
    ):
        self.calls.append(
            ("complete_same_origin", request, descriptor, event_sink)
        )
        if event_sink is not None:
            for event in self.events:
                event_sink(event)
        return self.turn

    async def continue_same_origin(
        self,
        results,
        *,
        descriptor,
        timeout_seconds,
        client_session_id,
        event_sink,
    ):
        self.calls.append(
            (
                "continue_same_origin",
                results,
                descriptor,
                timeout_seconds,
                client_session_id,
                event_sink,
            )
        )
        return self.turn


class GrokProtocolDescriptorTests(unittest.TestCase):
    def test_strict_sanitized_descriptor_is_accepted(self) -> None:
        descriptor = GrokWebProtocolDescriptor.from_sanitized_mapping(
            descriptor_mapping()
        )

        self.assertTrue(descriptor.verified)
        self.assertEqual("POST", descriptor.method)
        self.assertEqual(
            "/verified/{conversation_id}/completion",
            descriptor.endpoint_path,
        )

    def test_verifier_binds_descriptor_to_complete_probe_trace(self) -> None:
        descriptor = verified_descriptor()

        self.assertEqual(TRACE_DIGEST, descriptor.evidence_sha256)

    def test_verifier_rejects_mismatched_or_incomplete_trace(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            GrokProtocolVerifier.verify_sanitized_trace(
                descriptor_mapping(evidence_sha256=DIGEST),
                trace_jsonl=TRACE_JSONL,
            )
        incomplete = TRACE_JSONL.rsplit("\n", 2)[0] + "\n"
        incomplete_digest = hashlib.sha256(
            incomplete.encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(
            GrokProtocolUnverifiedError,
            "incomplete",
        ):
            GrokProtocolVerifier.verify_sanitized_trace(
                descriptor_mapping(evidence_sha256=incomplete_digest),
                trace_jsonl=incomplete,
            )

    def test_verifier_rejects_a_trace_with_probe_error(self) -> None:
        records = [
            json.loads(line)
            for line in TRACE_JSONL.splitlines()
        ]
        records.insert(
            -1,
            {
                "schema": GROK_PROBE_SCHEMA,
                "time": "2026-07-26T18:00:01+00:00",
                "capture_id": TRACE_CAPTURE_ID,
                "event": "probe_error",
                "error_type": "RuntimeError",
            },
        )
        failed_trace = "".join(
            json.dumps(record, separators=(",", ":")) + "\n"
            for record in records
        )
        failed_digest = hashlib.sha256(
            failed_trace.encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(
            GrokProtocolUnverifiedError,
            "contains a probe or browser error",
        ):
            GrokProtocolVerifier.verify_sanitized_trace(
                descriptor_mapping(evidence_sha256=failed_digest),
                trace_jsonl=failed_trace,
            )

    def test_full_urls_queries_and_sensitive_fields_are_rejected(self) -> None:
        cases = [
            descriptor_mapping(
                endpoint_path="https://grok.com/private/completion"
            ),
            descriptor_mapping(endpoint_path="/completion?token=private"),
            {
                **descriptor_mapping(),
                "cookie": "must never enter a descriptor",
            },
        ]

        for value in cases:
            with self.subTest(value=value["endpoint_path"]):
                with self.assertRaises(ValueError):
                    GrokWebProtocolDescriptor.from_sanitized_mapping(value)

    def test_streaming_requires_observed_protocol_and_evidence(self) -> None:
        with self.assertRaises(ValueError):
            GrokWebProtocolDescriptor.from_sanitized_mapping(
                descriptor_mapping(
                    streaming_verified=True,
                    stream_protocol="none",
                )
            )
        descriptor = GrokWebProtocolDescriptor.from_sanitized_mapping(
            descriptor_mapping(
                streaming_verified=True,
                stream_protocol="sse",
                stream_evidence_sha256=SECOND_DIGEST,
            )
        )
        self.assertTrue(descriptor.streaming_verified)

    def test_tool_continuation_requires_complete_evidence(self) -> None:
        with self.assertRaises(ValueError):
            GrokWebProtocolDescriptor.from_sanitized_mapping(
                descriptor_mapping(
                    tool_continuation="next_request",
                    custom_tools_accepted=True,
                    tool_call_observed=True,
                    tool_result_continuation_observed=False,
                    tool_evidence_sha256=SECOND_DIGEST,
                )
            )
        descriptor = GrokWebProtocolDescriptor.from_sanitized_mapping(
            descriptor_mapping(
                tool_continuation="next_request",
                custom_tools_accepted=True,
                tool_call_observed=True,
                tool_result_continuation_observed=True,
                tool_evidence_sha256=SECOND_DIGEST,
            )
        )
        self.assertEqual(
            "next_request",
            descriptor.tool_continuation.value,
        )


class GrokWebProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile_path = Path(self.temporary.name) / "grok-profile"
        self.transport = FakeGrokTransport()
        self.provider = GrokWebProvider(
            self.transport,
            profile_id="grok-a",
            profile_path=self.profile_path,
        )

    def test_provider_and_transport_implement_neutral_contracts(self) -> None:
        self.assertIsInstance(self.transport, GrokBrowserTransport)
        self.assertIsInstance(self.provider, CompletionProvider)
        capabilities = self.provider.capabilities
        self.assertFalse(capabilities.streaming)
        self.assertFalse(capabilities.thinking)
        self.assertEqual("unsupported", capabilities.tool_continuation.value)
        self.assertTrue(capabilities.profiles)

    async def test_lifecycle_uses_exact_dedicated_browser_profile(self) -> None:
        await self.provider.start()
        await self.provider.new_conversation()
        await self.provider.stop()

        start = self.transport.calls[0]
        self.assertEqual("start_browser", start[0])
        self.assertEqual(self.profile_path.resolve(), start[1])
        self.assertEqual("grok-a", start[2])
        self.assertEqual(GROK_WEB_ORIGIN, start[3])
        self.assertEqual(
            ("new_browser_conversation",),
            self.transport.calls[1],
        )
        self.assertEqual(("stop_browser",), self.transport.calls[2])

    async def test_completion_before_descriptor_fails_without_fallback(self):
        await self.provider.start()

        with self.assertRaisesRegex(
            GrokProtocolUnverifiedError,
            "completion is disabled",
        ):
            await self.provider.complete(
                ProviderTurnRequest(message="do not prompt-emulate tools")
            )

        self.assertFalse(
            any(call[0] == "complete_same_origin" for call in self.transport.calls)
        )
        health = self.provider.health()
        self.assertFalse(health.ready)
        self.assertEqual("protocol_unverified", health.phase)

    async def test_protocol_install_is_atomic_on_validation_failure(self):
        await self.provider.start()
        self.assertEqual(
            1,
            self.provider.load_verified_protocol(verified_descriptor()),
        )
        installed = self.provider.protocol_descriptor

        with self.assertRaises(ValueError):
            self.provider.load_verified_protocol(
                descriptor_mapping(
                    endpoint_path="https://attacker.test/not-same-origin"
                )
            )

        self.assertIs(installed, self.provider.protocol_descriptor)
        self.assertEqual(1, self.provider.protocol_revision)
        turn = await self.provider.complete(
            ProviderTurnRequest(message="hello")
        )
        self.assertEqual("browser answer", turn.content)

    async def test_caller_supplied_descriptor_cannot_enable_protocol(self):
        await self.provider.start()

        with self.assertRaisesRegex(
            GrokProtocolUnverifiedError,
            "GrokProtocolVerifier",
        ):
            self.provider.load_verified_protocol(descriptor_mapping())

        self.assertIsNone(self.provider.protocol_descriptor)
        self.assertEqual(0, self.provider.protocol_revision)

    async def test_health_and_identity_fail_closed(self) -> None:
        self.assertIsNone(self.provider.profile_identity)
        self.assertEqual("stopped", self.provider.health().phase)
        await self.provider.start()
        self.provider.load_verified_protocol(verified_descriptor())

        self.transport.identity_provider = "claude_web"
        self.assertIsNone(self.provider.profile_identity)
        self.assertEqual(
            "authentication_required",
            self.provider.health().phase,
        )
        with self.assertRaises(GrokProviderNotReadyError):
            await self.provider.complete(
                ProviderTurnRequest(message="hello")
            )

    async def test_model_catalog_only_exposes_verified_entitlements(self):
        await self.provider.start()
        self.transport.catalog = [
            GrokModelCatalogEntry(
                model_id="grok-observed",
                display_name="Observed",
                entitlement=GrokModelEntitlement.AVAILABLE,
                evidence_sha256=DIGEST,
            ),
            GrokModelCatalogEntry(
                model_id="grok-paid",
                display_name="Paid",
                entitlement=GrokModelEntitlement.UNAVAILABLE,
                evidence_sha256=SECOND_DIGEST,
            ),
            GrokModelCatalogEntry(
                model_id="grok-guessed",
                display_name="Guessed",
                entitlement=GrokModelEntitlement.AVAILABLE,
                evidence_sha256=DIGEST,
                entitlement_verified=False,
            ),
            object(),
        ]

        self.assertEqual(
            ["grok-observed", "grok-paid"],
            [entry.model_id for entry in self.provider.model_catalog],
        )
        self.assertEqual(
            ("grok-observed",),
            self.provider.selectable_models(),
        )

    async def test_verified_capabilities_control_stream_and_thinking(self):
        await self.provider.start()
        self.provider.load_verified_protocol(
            verified_descriptor(
                stream_protocol="sse",
                streaming_verified=True,
                stream_evidence_sha256=TRACE_DIGEST,
                thinking_verified=True,
                thinking_evidence_sha256=TRACE_DIGEST,
            )
        )
        self.transport.events = [
            ProviderEvent(
                kind=ProviderEventKind.THINKING_DELTA,
                text="verified thought",
            ),
            ProviderEvent(
                kind=ProviderEventKind.TEXT_DELTA,
                text="answer",
            ),
        ]
        self.transport.turn = ProviderTurn(
            content="answer",
            thinking="verified thought",
        )
        events = []

        turn = await self.provider.complete(
            ProviderTurnRequest(
                message="hello",
                reasoning_mode="extended",
            ),
            event_sink=events.append,
        )

        self.assertTrue(self.provider.capabilities.streaming)
        self.assertTrue(self.provider.capabilities.thinking)
        self.assertEqual("verified thought", turn.thinking)
        self.assertEqual(2, len(events))

    async def test_unverified_tools_are_rejected_not_prompt_emulated(self):
        await self.provider.start()
        self.provider.load_verified_protocol(verified_descriptor())

        with self.assertRaises(GrokCapabilityUnavailableError):
            await self.provider.complete(
                ProviderTurnRequest(
                    message="read a file",
                    tools=({"name": "Read"},),
                )
            )
        with self.assertRaises(GrokCapabilityUnavailableError):
            await self.provider.continue_with_tool_results(
                [ProviderToolResult(tool_use_id="t1", content="result")]
            )
        self.assertFalse(
            any(
                call[0] in {"complete_same_origin", "continue_same_origin"}
                for call in self.transport.calls
            )
        )

    async def test_verified_tool_continuation_delegates_same_origin(self):
        await self.provider.start()
        self.provider.load_verified_protocol(
            verified_descriptor(
                tool_continuation="next_request",
                custom_tools_accepted=True,
                tool_call_observed=True,
                tool_result_continuation_observed=True,
                tool_evidence_sha256=TRACE_DIGEST,
            )
        )
        self.transport.turn = ProviderTurn(
            content=None,
            tool_uses=(
                ProviderToolUse(
                    id="tool-1",
                    name="Read",
                    input={"path": "README.md"},
                ),
            ),
            stop_reason="tool_use",
        )

        first = await self.provider.complete(
            ProviderTurnRequest(
                message="read",
                tools=({"name": "Read"},),
            )
        )
        self.assertEqual("Read", first.tool_uses[0].name)
        await self.provider.continue_with_tool_results(
            [ProviderToolResult(tool_use_id="tool-1", content="contents")],
            client_session_id="client-a",
        )

        self.assertEqual(
            ["complete_same_origin", "continue_same_origin"],
            [
                call[0]
                for call in self.transport.calls
                if call[0].endswith("same_origin")
            ],
        )

    async def test_transport_cannot_expose_unverified_tool_or_thinking(self):
        await self.provider.start()
        self.provider.load_verified_protocol(verified_descriptor())

        self.transport.turn = ProviderTurn(
            content=None,
            tool_uses=(ProviderToolUse(id="t", name="Read"),),
        )
        with self.assertRaises(GrokProtocolViolationError):
            await self.provider.complete(
                ProviderTurnRequest(message="hello")
            )

        self.transport.turn = ProviderTurn(
            content="answer",
            thinking="unverified",
        )
        with self.assertRaises(GrokProtocolViolationError):
            await self.provider.complete(
                ProviderTurnRequest(message="hello")
            )


if __name__ == "__main__":
    unittest.main()
