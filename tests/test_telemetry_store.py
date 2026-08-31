from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from claude_web_api.telemetry.store import TelemetryStore, stable_session_key


class TelemetryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="openclaude-store-tests-"
        )
        self.path = Path(self.temporary.name) / "telemetry.sqlite3"
        self.store = TelemetryStore(self.path)
        self.addCleanup(self.temporary.cleanup)

    def begin(
        self,
        request_id: str,
        *,
        capture_content: bool = True,
        started_at: float | None = None,
        provider_id: str = "claude_web",
    ) -> None:
        self.store.begin_request(
            request_id=request_id,
            session_key=stable_session_key("raw-client-session", request_id),
            profile_id="default",
            requested_model="claude-sonnet",
            started_at=started_at or time.time() - 2,
            streaming=True,
            privacy_mode="keep",
            user_text="секретный пользовательский текст",
            capture_content=capture_content,
            provider_id=provider_id,
        )

    def finish(
        self,
        request_id: str,
        *,
        capture_content: bool = True,
        usage: dict | None = None,
        status: str = "completed",
        final_provider_id: str | None = None,
    ) -> None:
        self.store.finish_request(
            request_id=request_id,
            status=status,
            finished_at=time.time(),
            first_token_at=time.time() - 1,
            resolved_model="claude-sonnet",
            final_profile_id="default",
            usage=usage,
            estimated_output_tokens=9,
            output_chars=36,
            thinking_chars=7,
            tool_call_count=1,
            assistant_text="финальный ответ Claude",
            capture_content=capture_content,
            error=None,
            final_provider_id=final_provider_id,
        )

    def test_exact_usage_is_separate_from_estimate_and_survives_reopen(
        self,
    ) -> None:
        self.begin("req-exact")
        self.finish(
            "req-exact",
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 999,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        )
        reopened = TelemetryStore(self.path)
        detail = reopened.request_detail("req-exact")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual("upstream", detail["usage_source"])
        self.assertEqual(19, detail["usage"]["total_tokens"])
        self.assertEqual(3, detail["usage"]["cached_tokens"])
        summary = reopened.summary(since=None)
        self.assertEqual(19, summary["total_tokens"])
        self.assertEqual(1, summary["exact_usage_requests"])
        self.assertEqual(0, summary["estimated_output_tokens"])

    def test_unknown_usage_remains_unknown_and_estimate_is_labelled(
        self,
    ) -> None:
        self.begin("req-estimate")
        self.finish("req-estimate", usage=None)
        detail = self.store.request_detail("req-estimate")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNone(detail["usage"])
        self.assertEqual("estimate", detail["usage_source"])
        summary = self.store.summary(since=None)
        self.assertEqual(0, summary["total_tokens"])
        self.assertEqual(9, summary["estimated_output_tokens"])

    def test_usage_coverage_is_bounded_across_request_statuses(self) -> None:
        exact_usage = {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
        }
        for request_id, status, usage in [
            ("req-completed", "completed", exact_usage),
            ("req-error", "error", exact_usage),
            ("req-cancelled", "cancelled", exact_usage),
            ("req-interrupted", "interrupted", None),
        ]:
            self.begin(request_id, capture_content=False)
            self.finish(
                request_id,
                capture_content=False,
                usage=usage,
                status=status,
            )

        summary = self.store.summary(since=None)
        self.assertEqual(4, summary["requests"])
        self.assertEqual(3, summary["exact_usage_requests"])
        self.assertEqual(0.75, summary["usage_coverage"])
        self.assertLessEqual(summary["usage_coverage"], 1.0)

    def test_metadata_only_never_persists_content_or_raw_session_id(
        self,
    ) -> None:
        self.begin("req-metadata", capture_content=False)
        self.finish("req-metadata", capture_content=False)
        raw_database = self.path.read_bytes()
        self.assertNotIn(
            "raw-client-session".encode(),
            raw_database,
        )
        self.assertNotIn(
            "секретный пользовательский текст".encode(),
            raw_database,
        )
        self.assertNotIn(
            "финальный ответ Claude".encode(),
            raw_database,
        )
        detail = self.store.request_detail("req-metadata")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertFalse(detail["content_saved"])
        self.assertEqual(32, detail["input_chars"])
        self.assertEqual(36, detail["output_chars"])

    def test_provider_fields_persist_filter_and_summarize(self) -> None:
        self.begin("req-claude")
        self.finish("req-claude")
        self.begin("req-grok", provider_id="grok_web")
        self.finish(
            "req-grok",
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 4,
                "total_tokens": 9,
            },
            final_provider_id="grok_web",
        )
        self.begin("req-rerouted", provider_id="claude_web")
        self.finish(
            "req-rerouted",
            final_provider_id="grok_web",
        )

        reopened = TelemetryStore(self.path)
        detail = reopened.request_detail("req-grok")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual("grok_web", detail["provider_id"])
        self.assertEqual("grok_web", detail["final_provider_id"])
        rerouted = reopened.request_detail("req-rerouted")
        self.assertIsNotNone(rerouted)
        assert rerouted is not None
        self.assertEqual("claude_web", rerouted["provider_id"])
        self.assertEqual("grok_web", rerouted["final_provider_id"])

        grok_rows, grok_total = reopened.list_requests(
            since=None,
            provider_id="grok_web",
        )
        self.assertEqual(2, grok_total)
        self.assertEqual(
            {"req-grok", "req-rerouted"},
            {row["request_id"] for row in grok_rows},
        )
        claude_rows, claude_total = reopened.list_requests(
            since=None,
            provider_id="claude_web",
        )
        self.assertEqual(1, claude_total)
        self.assertEqual(
            ["req-claude"],
            [row["request_id"] for row in claude_rows],
        )

        summary = reopened.summary(since=None)
        self.assertEqual(
            {"claude_web": 1, "grok_web": 2},
            {
                row["provider_id"]: row["requests"]
                for row in summary["providers"]
            },
        )
        grok_summary = reopened.summary(
            since=None,
            provider_id="grok_web",
        )
        self.assertEqual(2, grok_summary["requests"])
        self.assertEqual("grok_web", grok_summary["models"][0]["provider_id"])

    def test_v1_database_migrates_existing_rows_to_claude_web(self) -> None:
        legacy_path = Path(self.temporary.name) / "telemetry-v1.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as database:
            database.executescript(
                """
                CREATE TABLE requests (
                    request_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    final_profile_id TEXT,
                    requested_model TEXT NOT NULL,
                    resolved_model TEXT,
                    started_at REAL NOT NULL,
                    first_token_at REAL,
                    finished_at REAL,
                    duration_seconds REAL,
                    status TEXT NOT NULL,
                    streaming INTEGER NOT NULL DEFAULT 0,
                    privacy_mode TEXT NOT NULL DEFAULT 'keep',
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cached_tokens INTEGER,
                    estimated_output_tokens INTEGER,
                    input_chars INTEGER NOT NULL DEFAULT 0,
                    output_chars INTEGER NOT NULL DEFAULT 0,
                    thinking_chars INTEGER NOT NULL DEFAULT 0,
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    user_text TEXT,
                    assistant_text TEXT,
                    error TEXT
                );
                INSERT INTO requests (
                    request_id,
                    session_key,
                    profile_id,
                    final_profile_id,
                    requested_model,
                    resolved_model,
                    started_at,
                    finished_at,
                    duration_seconds,
                    status
                ) VALUES (
                    'legacy-request',
                    'legacy-session',
                    'default',
                    'default',
                    'claude-sonnet',
                    'claude-sonnet',
                    100.0,
                    102.0,
                    2.0,
                    'completed'
                );
                PRAGMA user_version = 1;
                """
            )

        migrated = TelemetryStore(legacy_path)
        detail = migrated.request_detail("legacy-request")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual("claude_web", detail["provider_id"])
        self.assertIsNone(detail["final_provider_id"])

        with closing(sqlite3.connect(legacy_path)) as database:
            columns = {
                row[1]
                for row in database.execute(
                    "PRAGMA table_info(requests)"
                ).fetchall()
            }
            schema_version = database.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
        self.assertIn("provider_id", columns)
        self.assertIn("final_provider_id", columns)
        self.assertEqual(2, schema_version)

        # The old call shape remains valid and defaults to claude_web.
        migrated.begin_request(
            request_id="legacy-caller",
            session_key="legacy-caller-session",
            profile_id="default",
            requested_model="claude-sonnet",
            started_at=103.0,
            streaming=False,
            privacy_mode="keep",
            user_text=None,
            capture_content=False,
        )
        migrated.finish_request(
            request_id="legacy-caller",
            status="completed",
            finished_at=104.0,
            first_token_at=None,
            resolved_model="claude-sonnet",
            final_profile_id="default",
            usage=None,
            estimated_output_tokens=None,
            output_chars=0,
            thinking_chars=0,
            tool_call_count=0,
            assistant_text=None,
            capture_content=False,
            error=None,
        )
        legacy_caller = migrated.request_detail("legacy-caller")
        self.assertIsNotNone(legacy_caller)
        assert legacy_caller is not None
        self.assertEqual("claude_web", legacy_caller["provider_id"])
        self.assertIsNone(legacy_caller["final_provider_id"])

        # A database migrated from v1 can persist a later provider handoff,
        # and effective-provider filtering must not count it as Claude.
        migrated.begin_request(
            request_id="migrated-reroute",
            session_key="migrated-reroute-session",
            profile_id="default",
            requested_model="auto",
            started_at=105.0,
            streaming=True,
            privacy_mode="keep",
            user_text=None,
            capture_content=False,
            provider_id="claude_web",
        )
        migrated.finish_request(
            request_id="migrated-reroute",
            status="completed",
            finished_at=106.0,
            first_token_at=105.5,
            resolved_model="grok-auto",
            final_profile_id="grok-default",
            usage=None,
            estimated_output_tokens=None,
            output_chars=0,
            thinking_chars=0,
            tool_call_count=0,
            assistant_text=None,
            capture_content=False,
            error=None,
            final_provider_id="grok_web",
        )
        migrated_grok, migrated_grok_total = migrated.list_requests(
            since=None,
            provider_id="grok_web",
        )
        self.assertEqual(1, migrated_grok_total)
        self.assertEqual(
            ["migrated-reroute"],
            [row["request_id"] for row in migrated_grok],
        )
        migrated_claude, migrated_claude_total = migrated.list_requests(
            since=None,
            provider_id="claude_web",
        )
        self.assertEqual(2, migrated_claude_total)
        self.assertNotIn(
            "migrated-reroute",
            {row["request_id"] for row in migrated_claude},
        )

    def test_constructor_leaves_running_until_explicit_recovery(
        self,
    ) -> None:
        self.begin(
            "req-running",
            capture_content=False,
            started_at=time.time() - 5,
        )
        reopened = TelemetryStore(self.path)
        before_recovery = reopened.request_detail("req-running")
        self.assertIsNotNone(before_recovery)
        assert before_recovery is not None
        self.assertEqual("running", before_recovery["status"])

        self.assertEqual(1, reopened.recover_interrupted())
        detail = reopened.request_detail("req-running")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual("interrupted", detail["status"])
        self.assertGreaterEqual(detail["duration_seconds"], 4)

    def test_scrub_and_clear_are_scoped(self) -> None:
        self.begin("req-clear")
        self.finish("req-clear")
        now = time.time()
        for index in range(3):
            self.store.record_event(
                event_time=now + index,
                level="INFO",
                component="Test",
                message=f"safe event {index}",
            )

        first_page, first_total = self.store.list_events(
            since=None,
            limit=2,
            offset=0,
        )
        second_page, second_total = self.store.list_events(
            since=None,
            limit=2,
            offset=2,
        )
        self.assertEqual(3, first_total)
        self.assertEqual(3, second_total)
        self.assertEqual(
            ["safe event 2", "safe event 1"],
            [item["message"] for item in first_page],
        )
        self.assertEqual(
            ["safe event 0"],
            [item["message"] for item in second_page],
        )

        self.store.scrub_content()
        detail = self.store.request_detail("req-clear")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNone(detail["user_text"])
        self.assertIsNone(detail["assistant_text"])
        events_after_scrub, total_after_scrub = self.store.list_events(
            since=None
        )
        self.assertEqual(3, len(events_after_scrub))
        self.assertEqual(3, total_after_scrub)

        self.store.clear_events()
        self.assertEqual(([], 0), self.store.list_events(since=None))
        self.assertIsNotNone(self.store.request_detail("req-clear"))
        self.store.clear_all()
        self.assertIsNone(self.store.request_detail("req-clear"))
        self.assertEqual(([], 0), self.store.list_events(since=None))


if __name__ == "__main__":
    unittest.main()
