"""RecoveryTests and friends, split out of the original suite."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import claude_web_api.app as server
from claude_web_api import runtime
from claude_web_api.api import control as control_api
from claude_web_api.api import openai as openai_api
from claude_web_api.session.claude import (
    MODEL_SELECTOR_TRANSIENT_REASONS,
    ClaudeAccountIdentityError,
    ClaudeBrowserUnavailableError,
    ClaudeSession,
    ClaudeTurnOutcomeUnknownError,
    NativeTurn,
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
            runtime.session,
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
            runtime.session,
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
            runtime.session,
            "selectable_models",
            return_value=catalog,
        ):
            response = await openai_api.list_models()
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
                runtime.control,
                "profile",
                return_value={"id": "default", "model": "auto"},
            ),
            patch.object(
                runtime.session,
                "current_profile_id",
                return_value="default",
            ),
            patch.object(
                runtime.session,
                "selectable_models",
                return_value=catalog,
            ),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                runtime.resolve_request_model(
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
            runtime.control,
            "profile",
            return_value=profile,
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await control_api.select_profile_model(
                    "default",
                    control_api.ModelSelect(model="claude-fable-5"),
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
                runtime.session,
                "health_snapshot",
                return_value=health,
            ),
            patch.object(
                runtime.session,
                "account_uuid_for_internal_use",
                return_value=account_uuid,
            ),
            patch.object(
                runtime.control,
                "account_fingerprint",
                return_value="salted-hash",
            ),
            patch.object(
                runtime.control,
                "profile",
                return_value={
                    "id": "default",
                    "model": "auto",
                    "account_fingerprint": None,
                },
            ),
            patch.object(
                runtime.control,
                "profile_with_fingerprint",
                return_value=None,
            ),
            patch.object(runtime.control, "update_profile") as update_profile,
        ):
            runtime.persist_runtime_identity()
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
                runtime.session,
                "health_snapshot",
                return_value=health,
            ),
            patch.object(
                runtime.control,
                "profile",
                return_value={
                    "id": "default",
                    "model": "claude-fable-5",
                },
            ),
            patch.object(runtime.control, "update_profile") as update_profile,
        ):
            self.assertTrue(runtime.persist_runtime_identity())
        self.assertEqual(
            "auto",
            update_profile.call_args.args[1]["model"],
        )


if __name__ == "__main__":
    unittest.main()
