"""Moving to another account when one hits its limit.

Rotation only helps if it lands on a profile that is actually usable and, now
that profiles carry their own exits, starts that profile behind its own proxy.
Both are pinned here, together with the promise that a failed rotation leaves
the operator on the profile they started from.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_web_api import completions, runtime
from claude_web_api.control.config import ControlConfig
from claude_web_api.session.claude import ClaudeSession
from claude_web_api.session.errors import ClaudeUsageLimitError


def spec(profile_id: str, server: str | None = None) -> dict:
    return {
        "id": profile_id,
        "name": profile_id,
        "path": str(Path(tempfile.gettempdir()) / profile_id),
        "project_id": None,
        "organization_id": None,
        "model": "auto",
        "proxy": (
            {
                "enabled": True,
                "server": server,
                "username": "mara",
                "password": "secret",
            }
            if server
            else None
        ),
    }


class RecordingSession(ClaudeSession):
    """A session whose browser start is recorded instead of performed."""

    def __init__(self, specs: list[dict]) -> None:
        super().__init__(headless=True, profiles=specs)
        self.started: list[dict] = []

    async def _stop_browser_unlocked(self) -> None:
        return None

    async def start(self) -> None:
        self.started.append(self.launch_options(self.profile_dirs[self.profile_index]))
        self.ready = True


class RotationTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_skips_a_profile_that_is_not_eligible(self) -> None:
        session = RecordingSession([spec("default"), spec("limited"), spec("spare")])
        self.assertTrue(await session.rotate_profile({"spare"}))
        self.assertEqual("spare", session.current_profile_id())

    async def test_rotation_reports_failure_when_nothing_is_eligible(self) -> None:
        session = RecordingSession([spec("default"), spec("limited")])
        self.assertFalse(await session.rotate_profile(set()))
        self.assertEqual("default", session.current_profile_id())
        self.assertEqual([], session.started)

    async def test_a_single_profile_never_rotates_onto_itself(self) -> None:
        session = RecordingSession([spec("default")])
        self.assertFalse(await session.rotate_profile(None))

    async def test_the_rotated_profile_starts_behind_its_own_proxy(self) -> None:
        """Rotation must not send the next account out through the first
        account's exit address."""
        session = RecordingSession(
            [
                spec("default", "socks5://one.io:1080"),
                spec("spare", "socks5://two.io:1080"),
            ]
        )
        await session.rotate_profile({"spare"})
        self.assertEqual("socks5://two.io:1080", session.started[-1]["proxy"]["server"])

    async def test_rotation_waits_for_the_session_lock(self) -> None:
        """Two turns must not switch profiles underneath each other."""
        session = RecordingSession([spec("default"), spec("spare")])
        async with session._lock:
            held = asyncio.create_task(session.rotate_profile({"spare"}))
            await asyncio.sleep(0)
            self.assertFalse(held.done())
        self.assertTrue(await held)


class EligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = ControlConfig(Path(self.directory.name) / "control.json")
        patcher = patch.object(runtime, "control", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ready(self, name: str) -> str:
        row = self.config.create_profile(name)
        self.config.update_profile(row["id"], {"status": "ready"})
        return str(row["id"])

    def test_a_profile_still_inside_its_limit_window_is_skipped(self) -> None:
        fresh = self.ready("Свежий")
        cooling = self.ready("Отдыхает")
        self.config.update_profile(
            cooling,
            {"status": "limited", "limited_until": time.time() + 600},
        )
        eligible = runtime.eligible_rotation_ids()
        self.assertIn(fresh, eligible)
        self.assertNotIn(cooling, eligible)

    def test_an_expired_limit_makes_the_profile_eligible_again(self) -> None:
        recovered = self.ready("Отошёл")
        self.config.update_profile(
            recovered,
            {"status": "limited", "limited_until": time.time() - 1},
        )
        self.assertIn(recovered, runtime.eligible_rotation_ids())

    def test_a_disabled_profile_is_never_rotated_onto(self) -> None:
        parked = self.ready("Выключен")
        self.config.update_profile(parked, {"enabled": False})
        self.assertNotIn(parked, runtime.eligible_rotation_ids())

    def test_a_profile_awaiting_login_is_not_offered(self) -> None:
        pending = self.config.create_profile("Без входа")["id"]
        self.assertNotIn(pending, runtime.eligible_rotation_ids())


class ExhaustionTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_operator_is_left_on_the_profile_they_started_from(
        self,
    ) -> None:
        """Every account is full: the failure must not strand the bridge on a
        half-switched profile."""
        restored = AsyncMock()
        with (
            patch.object(
                runtime.session, "current_profile_id", return_value="default"
            ),
            patch.object(
                runtime, "eligible_rotation_ids", return_value={"spare"}
            ),
            patch.object(
                runtime.session, "rotate_profile", AsyncMock(return_value=False)
            ),
            patch.object(runtime.session, "sync_profiles", restored),
            patch.object(runtime, "runtime_profiles", return_value=[]),
            patch.object(runtime.control, "update_profile"),
            patch.object(runtime.control, "set_active_profile") as reactivated,
            patch.object(runtime.telemetry, "log"),
        ):
            with self.assertRaises(Exception) as caught:
                await completions._rotate_after_usage_limit(
                    completions.CompletionsIn(
                        messages=[{"role": "user", "content": "привет"}]
                    ),
                    client_session_id=None,
                    event_sink=None,
                    limit_error=ClaudeUsageLimitError(
                        "account full", replay_safe=True
                    ),
                    behavior_snapshot={"streaming": True},
                    persona_instruction="",
                )
        self.assertEqual(429, getattr(caught.exception, "status_code", None))
        restored.assert_awaited_once()
        self.assertEqual("default", restored.await_args.args[1])
        reactivated.assert_called_once_with("default")

    async def test_a_turn_that_already_produced_output_is_not_replayed(
        self,
    ) -> None:
        """Replaying after visible output would show the user the answer twice."""
        sink = AsyncMock()
        sink.visible_seen = True
        with self.assertRaises(Exception) as caught:
            await completions._rotate_after_usage_limit(
                completions.CompletionsIn(
                    messages=[{"role": "user", "content": "привет"}]
                ),
                client_session_id=None,
                event_sink=sink,
                limit_error=ClaudeUsageLimitError(
                    "account full", replay_safe=True
                ),
                behavior_snapshot={"streaming": True},
                persona_instruction="",
            )
        self.assertEqual(409, getattr(caught.exception, "status_code", None))


if __name__ == "__main__":
    unittest.main()
