"""ProtocolRoutingTests and friends, split out of the original suite."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from claude_web_api.session.claude import (
    ClaudeBrowserUnavailableError,
    ClaudeCompletionRejectedError,
    ClaudeServiceUnavailableError,
    ClaudeSession,
    ClaudeUsageLimitError,
)


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
                "claude_web_api.session.project."
                "KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256",
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

    async def test_a_profile_without_a_project_creates_one_instead_of_stalling(
        self,
    ) -> None:
        """A lost config or an enrollment that failed at the Project step used
        to park the session in project_unavailable until someone re-enrolled."""
        organization_id = "44444444-4444-4444-4444-444444444444"
        created_id = "55555555-5555-5555-5555-555555555555"
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": None,
                    "organization_id": organization_id,
                }
            ],
            project_instructions="trusted IDE contract",
        )
        native_session._organization_uuid = organization_id

        async def evaluate(script, arguments=None):
            if "OpenClaude IDE" in script and "projectId" not in (arguments or {}):
                return {"projectId": created_id}
            return {
                "ok": True,
                "organizationUuid": organization_id,
                "promptTemplate": "trusted IDE contract",
                "privacyVerified": True,
            }

        native_session.page = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate))
        self.assertTrue(await native_session._sync_trusted_project())
        self.assertEqual(created_id, native_session.current_profile_spec()["project_id"])
        self.assertTrue(native_session._project_instructions_synced)

    async def test_a_profile_without_a_project_or_organization_reports_why(
        self,
    ) -> None:
        native_session = ClaudeSession(
            headless=True,
            profiles=[
                {
                    "id": "default",
                    "path": str(Path.cwd() / "profile"),
                    "project_id": None,
                }
            ],
            project_instructions="trusted IDE contract",
        )
        native_session.page = SimpleNamespace(evaluate=AsyncMock())
        self.assertFalse(await native_session._sync_trusted_project())
        self.assertIn("organization is not known", native_session._project_sync_error)
        native_session.page.evaluate.assert_not_awaited()

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
