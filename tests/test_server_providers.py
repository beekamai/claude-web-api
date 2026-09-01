import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import claude_web_api.app as server
from claude_web_api import runtime
from claude_web_api.control.config import ControlConfig


class ServerProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        config_path = Path(self.temporary.name) / "control_config.json"
        self.control = ControlConfig(config_path)
        self.control.update_profile(
            "default",
            {
                "status": "ready",
                "enabled": True,
            },
        )
        self.grok = self.control.create_profile("Grok main", "grok_web")
        self.control.update_profile(
            self.grok["id"],
            {
                "status": "ready",
                "enabled": True,
                "account": {
                    "authenticated": True,
                    "name": "Grok Account",
                    "email": None,
                    "uuid_suffix": "12345678",
                },
            },
        )
        self.control_patch = patch.object(runtime, "control",
            self.control,
        )
        self.control_patch.start()
        self.addCleanup(self.control_patch.stop)
        self.registry = runtime.ProviderRegistry()
        self.registry.register(
            server.CLAUDE_WEB_PROVIDER_ID,
            runtime.claude_provider,
            profile_ids=("default",),
        )
        self.registry_patch = patch.object(runtime, "provider_registry",
            self.registry,
        )
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)

    def test_grok_profile_never_enters_claude_session_rotation(self) -> None:
        self.control.set_active_profile(self.grok["id"])

        profiles = runtime.runtime_profiles()

        self.assertEqual(["default"], [row["id"] for row in profiles])
        self.assertTrue(
            all(row["provider"] == "claude_web" for row in profiles)
        )

    async def test_profile_create_persists_requested_browser_provider(
        self,
    ) -> None:
        response = await server.create_profile(
            server.ProfileCreate(name="Grok spare", provider="grok_web")
        )

        self.assertTrue(response["ok"])
        self.assertEqual("grok_web", response["profile"]["provider"])
        self.assertTrue(
            Path(response["profile"]["path"]).is_relative_to(
                Path(self.temporary.name)
            )
        )
        with self.assertRaises(KeyError):
            self.registry.provider_id_for_profile(response["profile"]["id"])

    async def test_new_claude_profile_is_bound_to_claude_provider(self) -> None:
        response = await server.create_profile(
            server.ProfileCreate(name="Claude spare", provider="claude_web")
        )

        profile_id = response["profile"]["id"]
        self.assertEqual(
            "claude_web",
            self.registry.provider_id_for_profile(profile_id),
        )

    async def test_activation_repairs_missing_claude_registry_binding(
        self,
    ) -> None:
        self.registry.unbind_profile("default")

        with (
            patch.object(
                runtime.session,
                "health_snapshot",
                return_value={"native": {}, "ok": True},
            ),
            patch.object(
                runtime.session,
                "sync_profiles",
                AsyncMock(),
            ),
            patch.object(runtime, "persist_runtime_identity",
                return_value=True,
            ),
        ):
            await server.activate_profile("default")

        self.assertEqual(
            "claude_web",
            self.registry.provider_id_for_profile("default"),
        )

    def test_default_model_resolution_uses_actual_claude_runtime(self) -> None:
        self.control.update_profile(
            "default",
            {
                "model": "claude-sonnet",
                "models": [
                    {
                        "id": "claude-sonnet",
                        "available": True,
                        "access_status": "available",
                    }
                ],
            },
        )
        self.control.update_profile(
            self.grok["id"],
            {
                "model": "grok-4",
                "models": [
                    {
                        "id": "grok-4",
                        "available": True,
                        "access_status": "available",
                    }
                ],
            },
        )
        self.control.set_active_profile(self.grok["id"])

        with (
            patch.object(
                runtime.session,
                "current_profile_id",
                return_value="default",
            ),
            patch.object(
                runtime.session,
                "selectable_models",
                return_value=[
                    {
                        "id": "claude-sonnet",
                        "available": True,
                        "access_status": "available",
                    }
                ],
            ),
        ):
            resolved = runtime.resolve_request_model("auto")

        self.assertEqual("claude-sonnet", resolved)

    async def test_provider_access_failures_remain_terminal_statuses(
        self,
    ) -> None:
        for status in ("provider_blocked", "access_denied"):
            with self.subTest(status=status):
                self.control.update_profile(
                    self.grok["id"],
                    {"status": "auth_pending"},
                )
                with (
                    patch.object(
                        runtime.enrollment,
                        "is_running",
                        AsyncMock(return_value=True),
                    ),
                    patch.object(
                        runtime.enrollment,
                        "inspect",
                        AsyncMock(
                            return_value={
                                "profile_id": self.grok["id"],
                                "provider": "grok_web",
                                "status": status,
                                "authenticated": False,
                                "browser_open": True,
                                "ready": False,
                                "last_error": "Provider denied browser access",
                            }
                        ),
                    ),
                ):
                    response = await server._inspect_profile_login_once(
                        self.grok["id"]
                    )

                self.assertEqual(status, response["login"]["status"])
                self.assertEqual(
                    status,
                    self.control.profile(self.grok["id"])["status"],
                )

    async def test_cached_grok_ready_state_is_normalized_fail_closed(
        self,
    ) -> None:
        with patch.object(
            runtime.enrollment,
            "is_running",
            AsyncMock(return_value=False),
        ):
            response = await server._inspect_profile_login_once(
                self.grok["id"]
            )

        self.assertEqual(
            "protocol_unverified",
            response["login"]["status"],
        )
        self.assertFalse(response["login"]["ready"])
        profile = self.control.profile(self.grok["id"])
        self.assertEqual("protocol_unverified", profile["status"])
        self.assertFalse(profile["enabled"])

    async def test_grok_browser_identity_never_becomes_completion_ready(
        self,
    ) -> None:
        self.control.update_profile(
            self.grok["id"],
            {
                "status": "auth_pending",
                "enabled": True,
                "account_fingerprint": None,
            },
        )
        ensure_project = AsyncMock()
        finish = AsyncMock()
        with (
            patch.object(
                runtime.enrollment,
                "is_running",
                AsyncMock(return_value=True),
            ),
            patch.object(
                runtime.enrollment,
                "inspect",
                AsyncMock(
                    return_value={
                        "profile_id": self.grok["id"],
                        "provider": "grok_web",
                        "status": "authenticated",
                        "authenticated": True,
                        "browser_open": True,
                        "ready": False,
                        "account": {
                            "authenticated": True,
                            "name": "Grok Account",
                        },
                        "models": [],
                    }
                ),
            ),
            patch.object(
                runtime.enrollment,
                "internal_identity",
                AsyncMock(return_value={"account_uuid": "grok-account-1"}),
            ),
            patch.object(runtime.enrollment, "finish", finish),
            patch.object(
                runtime.enrollment,
                "ensure_project",
                ensure_project,
            ),
        ):
            response = await server._inspect_profile_login_once(
                self.grok["id"]
            )

        self.assertEqual(
            "protocol_unverified",
            response["login"]["status"],
        )
        self.assertFalse(response["login"]["ready"])
        self.assertIn("protocol_error", response["login"])
        profile = self.control.profile(self.grok["id"])
        self.assertEqual("protocol_unverified", profile["status"])
        self.assertFalse(profile["enabled"])
        finish.assert_awaited_once_with(self.grok["id"])
        ensure_project.assert_not_awaited()

    async def test_unverified_grok_runtime_cannot_be_activated(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await server.activate_profile(self.grok["id"])

        self.assertEqual(409, raised.exception.status_code)
        self.assertIn("protocol", str(raised.exception.detail).lower())

    def test_capability_snapshot_advertises_no_unverified_features(
        self,
    ) -> None:
        grok = runtime.provider_capabilities_snapshot()["grok_web"]

        self.assertFalse(grok["streaming"])
        self.assertFalse(grok["thinking"])
        self.assertEqual("unsupported", grok["tool_continuation"])
        self.assertIn("manual Chrome works", grok["detail"])


if __name__ == "__main__":
    unittest.main()
