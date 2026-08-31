"""Bounded local telemetry storage for the OpenClaude control center."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from claude_web_api.paths import TELEMETRY_DB_FILE as DEFAULT_DB_PATH

MAX_USER_TEXT = 32_000
MAX_ASSISTANT_TEXT = 96_000
MAX_ERROR_TEXT = 1_000
MAX_EVENT_TEXT = 1_000
DEFAULT_PROVIDER_ID = "claude_web"
SCHEMA_VERSION = 2


def configured_database_path() -> Path:
    value = str(os.getenv("OPENCLAUDE_TELEMETRY_DB", "") or "").strip()
    return Path(value).expanduser().resolve() if value else DEFAULT_DB_PATH


def stable_session_key(
    client_session_id: str | None,
    request_id: str,
) -> str:
    """Group requests without retaining the caller's opaque session ID."""
    raw = str(client_session_id or "").strip()
    if not raw:
        return f"request-{request_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"session-{digest[:24]}"


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text[:limit]


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value >= 0
        and value.is_integer()
    ):
        return int(value)
    return None


class TelemetryStore:
    """Small SQLite store with bounded retention and no raw account IDs."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or configured_database_path()).resolve()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as database:
            current_schema_version = int(
                database.execute("PRAGMA user_version").fetchone()[0]
            )
            database.execute("PRAGMA journal_mode = WAL")
            database.execute("PRAGMA synchronous = NORMAL")
            database.execute("PRAGMA secure_delete = ON")
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT 'claude_web',
                    profile_id TEXT NOT NULL,
                    final_provider_id TEXT,
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

                CREATE INDEX IF NOT EXISTS requests_started_at_idx
                    ON requests(started_at DESC);
                CREATE INDEX IF NOT EXISTS requests_session_idx
                    ON requests(session_key, started_at DESC);
                CREATE INDEX IF NOT EXISTS requests_status_idx
                    ON requests(status, started_at DESC);
                CREATE INDEX IF NOT EXISTS requests_profile_idx
                    ON requests(profile_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time REAL NOT NULL,
                    level TEXT NOT NULL,
                    component TEXT NOT NULL,
                    message TEXT NOT NULL,
                    request_id TEXT
                );

                CREATE INDEX IF NOT EXISTS events_time_idx
                    ON events(time DESC, id DESC);
                CREATE INDEX IF NOT EXISTS events_level_idx
                    ON events(level, time DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in database.execute(
                    "PRAGMA table_info(requests)"
                ).fetchall()
            }
            if "provider_id" not in columns:
                database.execute(
                    """
                    ALTER TABLE requests
                    ADD COLUMN provider_id TEXT NOT NULL
                        DEFAULT 'claude_web'
                    """
                )
            if "final_provider_id" not in columns:
                database.execute(
                    """
                    ALTER TABLE requests
                    ADD COLUMN final_provider_id TEXT
                    """
                )
            database.execute(
                """
                UPDATE requests
                SET provider_id = ?
                WHERE provider_id IS NULL OR TRIM(provider_id) = ''
                """,
                (DEFAULT_PROVIDER_ID,),
            )
            database.execute(
                """
                CREATE INDEX IF NOT EXISTS requests_provider_idx
                ON requests(provider_id, started_at DESC)
                """
            )
            database.execute(
                """
                CREATE INDEX IF NOT EXISTS requests_final_provider_idx
                ON requests(final_provider_id, started_at DESC)
                """
            )
            database.execute(
                "PRAGMA user_version = "
                f"{max(current_schema_version, SCHEMA_VERSION)}"
            )

    @staticmethod
    def _truncate_wal(database: sqlite3.Connection) -> None:
        result: sqlite3.Row | tuple[Any, ...] | None = None
        for attempt in range(4):
            result = database.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if result is None or int(result[0]) == 0:
                return
            if attempt < 3:
                time.sleep(0.05 * (attempt + 1))
        raise RuntimeError(
            "telemetry WAL is busy; close external database readers and retry"
        )

    def recover_interrupted(self) -> int:
        now = time.time()
        with self._lock, closing(self._connect()) as database:
            cursor = database.execute(
                """
                UPDATE requests
                SET status = 'interrupted',
                    finished_at = ?,
                    duration_seconds = MAX(0, ? - started_at),
                    error = COALESCE(
                        error,
                        'Server restarted before the request completed'
                    )
                WHERE status = 'running'
                """,
                (now, now),
            )
            return int(cursor.rowcount or 0)

    def begin_request(
        self,
        *,
        request_id: str,
        session_key: str,
        profile_id: str,
        requested_model: str,
        started_at: float,
        streaming: bool,
        privacy_mode: str,
        user_text: str | None,
        capture_content: bool,
        provider_id: str = DEFAULT_PROVIDER_ID,
    ) -> None:
        bounded_user = (
            _bounded_text(user_text, MAX_USER_TEXT)
            if capture_content
            else None
        )
        input_chars = len(str(user_text or ""))
        normalized_provider_id = (
            str(provider_id or "").strip() or DEFAULT_PROVIDER_ID
        )
        with self._lock, closing(self._connect()) as database:
            database.execute(
                """
                INSERT INTO requests (
                    request_id,
                    session_key,
                    provider_id,
                    profile_id,
                    requested_model,
                    started_at,
                    status,
                    streaming,
                    privacy_mode,
                    input_chars,
                    user_text
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    provider_id = excluded.provider_id,
                    profile_id = excluded.profile_id,
                    requested_model = excluded.requested_model,
                    started_at = excluded.started_at,
                    status = 'running',
                    streaming = excluded.streaming,
                    privacy_mode = excluded.privacy_mode,
                    input_chars = excluded.input_chars,
                    user_text = excluded.user_text,
                    final_provider_id = NULL,
                    finished_at = NULL,
                    duration_seconds = NULL,
                    error = NULL
                """,
                (
                    request_id,
                    session_key,
                    normalized_provider_id,
                    profile_id,
                    requested_model,
                    started_at,
                    int(streaming),
                    privacy_mode,
                    input_chars,
                    bounded_user,
                ),
            )

    def finish_request(
        self,
        *,
        request_id: str,
        status: str,
        finished_at: float,
        first_token_at: float | None,
        resolved_model: str | None,
        final_profile_id: str | None,
        usage: dict[str, Any] | None,
        estimated_output_tokens: int | None,
        output_chars: int,
        thinking_chars: int,
        tool_call_count: int,
        assistant_text: str | None,
        capture_content: bool,
        error: str | None,
        final_provider_id: str | None = None,
    ) -> None:
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None
        cached_tokens = None
        if isinstance(usage, dict):
            prompt_tokens = _safe_int(
                usage.get("prompt_tokens", usage.get("input_tokens"))
            )
            completion_tokens = _safe_int(
                usage.get("completion_tokens", usage.get("output_tokens"))
            )
            if prompt_tokens is not None and completion_tokens is not None:
                total_tokens = _safe_int(usage.get("total_tokens"))
                expected_total = prompt_tokens + completion_tokens
                if total_tokens != expected_total:
                    total_tokens = expected_total
                prompt_details = usage.get("prompt_tokens_details")
                if isinstance(prompt_details, dict):
                    cached_tokens = _safe_int(
                        prompt_details.get("cached_tokens")
                    )
        bounded_assistant = (
            _bounded_text(assistant_text, MAX_ASSISTANT_TEXT)
            if capture_content
            else None
        )
        bounded_error = _bounded_text(error, MAX_ERROR_TEXT)
        safe_estimate = _safe_int(estimated_output_tokens)
        normalized_final_provider_id = (
            str(final_provider_id).strip()
            if final_provider_id is not None
            and str(final_provider_id).strip()
            else None
        )
        with self._lock, closing(self._connect()) as database:
            database.execute(
                """
                UPDATE requests
                SET final_provider_id = ?,
                    final_profile_id = ?,
                    resolved_model = ?,
                    first_token_at = ?,
                    finished_at = ?,
                    duration_seconds = MAX(0, ? - started_at),
                    status = ?,
                    prompt_tokens = ?,
                    completion_tokens = ?,
                    total_tokens = ?,
                    cached_tokens = ?,
                    estimated_output_tokens = ?,
                    output_chars = ?,
                    thinking_chars = ?,
                    tool_call_count = ?,
                    user_text = CASE WHEN ? THEN user_text ELSE NULL END,
                    assistant_text = ?,
                    error = ?
                WHERE request_id = ?
                """,
                (
                    normalized_final_provider_id,
                    final_profile_id,
                    resolved_model,
                    first_token_at,
                    finished_at,
                    finished_at,
                    status,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cached_tokens,
                    safe_estimate,
                    max(0, int(output_chars)),
                    max(0, int(thinking_chars)),
                    max(0, int(tool_call_count)),
                    int(capture_content),
                    bounded_assistant,
                    bounded_error,
                    request_id,
                ),
            )

    def record_event(
        self,
        *,
        event_time: float,
        level: str,
        component: str,
        message: str,
        request_id: str | None = None,
    ) -> None:
        bounded = _bounded_text(message, MAX_EVENT_TEXT)
        if not bounded:
            return
        with self._lock, closing(self._connect()) as database:
            database.execute(
                """
                INSERT INTO events (
                    time,
                    level,
                    component,
                    message,
                    request_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_time,
                    str(level or "INFO").upper()[:16],
                    str(component or "Service")[:80],
                    bounded,
                    request_id,
                ),
            )

    def prune(
        self,
        *,
        retention_days: int,
        max_requests: int,
        max_events: int = 5_000,
    ) -> None:
        cutoff = time.time() - max(1, retention_days) * 86_400
        with self._lock, closing(self._connect()) as database:
            database.execute(
                "DELETE FROM requests WHERE started_at < ?",
                (cutoff,),
            )
            database.execute(
                "DELETE FROM events WHERE time < ?",
                (cutoff,),
            )
            database.execute(
                """
                DELETE FROM requests
                WHERE request_id IN (
                    SELECT request_id
                    FROM requests
                    ORDER BY started_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (max(100, int(max_requests)),),
            )
            database.execute(
                """
                DELETE FROM events
                WHERE id IN (
                    SELECT id
                    FROM events
                    ORDER BY time DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (max(200, int(max_events)),),
            )

    def scrub_content(self) -> None:
        with self._lock, closing(self._connect()) as database:
            database.execute(
                "UPDATE requests SET user_text = NULL, assistant_text = NULL"
            )
            self._truncate_wal(database)

    def clear_events(self) -> None:
        with self._lock, closing(self._connect()) as database:
            database.execute("DELETE FROM events")
            self._truncate_wal(database)

    def clear_all(self) -> None:
        with self._lock, closing(self._connect()) as database:
            database.execute("DELETE FROM requests")
            database.execute("DELETE FROM events")
            self._truncate_wal(database)

    @staticmethod
    def _where_clause(
        *,
        since: float | None,
        status: str | None,
        provider_id: str | None,
        profile_id: str | None,
        model: str | None,
        search: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if since is not None:
            clauses.append("started_at >= ?")
            values.append(since)
        if status:
            clauses.append("status = ?")
            values.append(status)
        if provider_id:
            clauses.append(
                """
                COALESCE(
                    NULLIF(final_provider_id, ''),
                    provider_id
                ) = ?
                """
            )
            values.append(provider_id)
        if profile_id:
            clauses.append(
                "(profile_id = ? OR final_profile_id = ?)"
            )
            values.extend([profile_id, profile_id])
        if model:
            clauses.append(
                "(requested_model = ? OR resolved_model = ?)"
            )
            values.extend([model, model])
        if search:
            clauses.append(
                "("
                "provider_id LIKE ? OR final_provider_id LIKE ? OR "
                "requested_model LIKE ? OR resolved_model LIKE ? OR "
                "user_text LIKE ? OR assistant_text LIKE ? OR error LIKE ?"
                ")"
            )
            pattern = f"%{search[:200]}%"
            values.extend([pattern] * 7)
        return (
            (" WHERE " + " AND ".join(clauses)) if clauses else "",
            values,
        )

    @staticmethod
    def _public_request(row: sqlite3.Row) -> dict[str, Any]:
        prompt_tokens = _safe_int(row["prompt_tokens"])
        completion_tokens = _safe_int(row["completion_tokens"])
        usage_exact = (
            prompt_tokens is not None and completion_tokens is not None
        )
        duration = row["duration_seconds"]
        first_token_at = row["first_token_at"]
        finished_at = row["finished_at"]
        generation_seconds = (
            float(finished_at) - float(first_token_at)
            if isinstance(first_token_at, (int, float))
            and isinstance(finished_at, (int, float))
            and finished_at > first_token_at
            else None
        )
        tokens_per_second = None
        if (
            usage_exact
            and generation_seconds is not None
            and generation_seconds > 0
        ):
            tokens_per_second = completion_tokens / generation_seconds
        user_preview = str(row["user_preview"] or "").strip()
        assistant_preview = str(row["assistant_preview"] or "").strip()
        title = user_preview.splitlines()[0].strip() if user_preview else ""
        return {
            "request_id": row["request_id"],
            "session_suffix": str(row["session_key"])[-8:],
            "provider_id": row["provider_id"],
            "final_provider_id": row["final_provider_id"],
            "profile_id": row["profile_id"],
            "final_profile_id": row["final_profile_id"],
            "requested_model": row["requested_model"],
            "resolved_model": row["resolved_model"],
            "started_at": row["started_at"],
            "first_token_at": row["first_token_at"],
            "finished_at": row["finished_at"],
            "duration_seconds": duration,
            "status": row["status"],
            "streaming": bool(row["streaming"]),
            "privacy_mode": row["privacy_mode"],
            "usage": (
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": _safe_int(row["total_tokens"]),
                    "cached_tokens": _safe_int(row["cached_tokens"]),
                }
                if usage_exact
                else None
            ),
            "usage_source": (
                "upstream"
                if usage_exact
                else (
                    "estimate"
                    if _safe_int(row["estimated_output_tokens"]) is not None
                    else "unknown"
                )
            ),
            "estimated_output_tokens": _safe_int(
                row["estimated_output_tokens"]
            ),
            "tokens_per_second": tokens_per_second,
            "input_chars": int(row["input_chars"] or 0),
            "output_chars": int(row["output_chars"] or 0),
            "thinking_chars": int(row["thinking_chars"] or 0),
            "tool_call_count": int(row["tool_call_count"] or 0),
            "title": title or (
                "Продолжение после инструмента"
                if int(row["tool_call_count"] or 0)
                else "Запрос без сохранённого текста"
            ),
            "user_preview": user_preview,
            "assistant_preview": assistant_preview,
            "content_saved": bool(user_preview or assistant_preview),
            "error": row["error"],
        }

    def list_requests(
        self,
        *,
        since: float | None,
        status: str | None = None,
        provider_id: str | None = None,
        profile_id: str | None = None,
        model: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where, values = self._where_clause(
            since=since,
            status=status,
            provider_id=provider_id,
            profile_id=profile_id,
            model=model,
            search=search,
        )
        safe_limit = min(200, max(1, int(limit)))
        safe_offset = max(0, int(offset))
        with self._lock, closing(self._connect()) as database:
            total = int(
                database.execute(
                    "SELECT COUNT(*) FROM requests" + where,
                    values,
                ).fetchone()[0]
            )
            rows = database.execute(
                """
                SELECT
                    request_id,
                    session_key,
                    provider_id,
                    final_provider_id,
                    profile_id,
                    final_profile_id,
                    requested_model,
                    resolved_model,
                    started_at,
                    first_token_at,
                    finished_at,
                    duration_seconds,
                    status,
                    streaming,
                    privacy_mode,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cached_tokens,
                    estimated_output_tokens,
                    input_chars,
                    output_chars,
                    thinking_chars,
                    tool_call_count,
                    SUBSTR(user_text, 1, 240) AS user_preview,
                    SUBSTR(assistant_text, 1, 320) AS assistant_preview,
                    error
                FROM requests
                """
                + where
                + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                [*values, safe_limit, safe_offset],
            ).fetchall()
        return [self._public_request(row) for row in rows], total

    def request_detail(self, request_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as database:
            row = database.execute(
                """
                SELECT
                    *,
                    user_text AS user_preview,
                    assistant_text AS assistant_preview
                FROM requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        payload = self._public_request(row)
        payload["user_text"] = row["user_text"]
        payload["assistant_text"] = row["assistant_text"]
        payload.pop("user_preview", None)
        payload.pop("assistant_preview", None)
        return payload

    def summary(
        self,
        *,
        since: float | None,
        provider_id: str | None = None,
        profile_id: str | None = None,
        model: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        where, values = self._where_clause(
            since=since,
            status=None,
            provider_id=provider_id,
            profile_id=profile_id,
            model=model,
            search=search,
        )
        with self._lock, closing(self._connect()) as database:
            row = database.execute(
                """
                SELECT
                    COUNT(*) AS requests,
                    COUNT(DISTINCT session_key) AS conversations,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                        AS completed,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)
                        AS errors,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END)
                        AS cancelled,
                    SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END)
                        AS interrupted,
                    SUM(
                        CASE
                            WHEN prompt_tokens IS NOT NULL
                             AND completion_tokens IS NOT NULL
                            THEN 1 ELSE 0
                        END
                    ) AS exact_usage_requests,
                    SUM(
                        CASE
                            WHEN prompt_tokens IS NOT NULL
                             AND completion_tokens IS NOT NULL
                            THEN prompt_tokens ELSE 0
                        END
                    ) AS prompt_tokens,
                    SUM(
                        CASE
                            WHEN prompt_tokens IS NOT NULL
                             AND completion_tokens IS NOT NULL
                            THEN completion_tokens ELSE 0
                        END
                    ) AS completion_tokens,
                    SUM(
                        CASE
                            WHEN prompt_tokens IS NOT NULL
                             AND completion_tokens IS NOT NULL
                            THEN total_tokens ELSE 0
                        END
                    ) AS total_tokens,
                    SUM(
                        CASE
                            WHEN prompt_tokens IS NULL
                              OR completion_tokens IS NULL
                            THEN COALESCE(estimated_output_tokens, 0)
                            ELSE 0
                        END
                    ) AS estimated_output_tokens,
                    AVG(duration_seconds) AS average_duration_seconds,
                    SUM(tool_call_count) AS tool_calls
                FROM requests
                """
                + where,
                values,
            ).fetchone()
            duration_rows = database.execute(
                """
                SELECT duration_seconds
                FROM requests
                """
                + where
                + (
                    " AND duration_seconds IS NOT NULL"
                    if where
                    else " WHERE duration_seconds IS NOT NULL"
                )
                + " ORDER BY duration_seconds",
                values,
            ).fetchall()
            model_rows = database.execute(
                """
                SELECT
                    COALESCE(
                        NULLIF(final_provider_id, ''),
                        provider_id
                    ) AS provider_id,
                    COALESCE(resolved_model, requested_model) AS model,
                    COUNT(*) AS requests,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                        AS completed,
                    SUM(
                        CASE
                            WHEN prompt_tokens IS NOT NULL
                             AND completion_tokens IS NOT NULL
                            THEN total_tokens ELSE 0
                        END
                    ) AS total_tokens
                FROM requests
                """
                + where
                + (
                    " GROUP BY"
                    " COALESCE(NULLIF(final_provider_id, ''), provider_id),"
                    " COALESCE(resolved_model, requested_model)"
                    " ORDER BY requests DESC, provider_id, model LIMIT 8"
                ),
                values,
            ).fetchall()
            provider_rows = database.execute(
                """
                SELECT
                    COALESCE(
                        NULLIF(final_provider_id, ''),
                        provider_id
                    ) AS provider_id,
                    COUNT(*) AS requests,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                        AS completed,
                    SUM(
                        CASE
                            WHEN prompt_tokens IS NOT NULL
                             AND completion_tokens IS NOT NULL
                            THEN total_tokens ELSE 0
                        END
                    ) AS total_tokens
                FROM requests
                """
                + where
                + (
                    " GROUP BY"
                    " COALESCE(NULLIF(final_provider_id, ''), provider_id)"
                    " ORDER BY requests DESC, provider_id"
                ),
                values,
            ).fetchall()
            series_rows = database.execute(
                """
                SELECT
                    started_at,
                    status,
                    prompt_tokens,
                    completion_tokens,
                    estimated_output_tokens
                FROM requests
                """
                + where
                + " ORDER BY started_at",
                values,
            ).fetchall()

        requests = int(row["requests"] or 0)
        completed = int(row["completed"] or 0)
        errors = int(row["errors"] or 0)
        cancelled = int(row["cancelled"] or 0)
        interrupted = int(row["interrupted"] or 0)
        terminal_requests = completed + errors + cancelled + interrupted
        exact_requests = int(row["exact_usage_requests"] or 0)
        durations = [float(item[0]) for item in duration_rows]
        p95_duration = None
        if durations:
            index = max(0, math.ceil(len(durations) * 0.95) - 1)
            p95_duration = durations[index]
        bucket_seconds = 3_600 if since and time.time() - since <= 172_800 else 86_400
        series: dict[int, dict[str, Any]] = {}
        for item in series_rows:
            bucket = int(float(item["started_at"]) // bucket_seconds) * bucket_seconds
            point = series.setdefault(
                bucket,
                {
                    "time": bucket,
                    "requests": 0,
                    "errors": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "estimated_output_tokens": 0,
                },
            )
            point["requests"] += 1
            if item["status"] in {"error", "interrupted"}:
                point["errors"] += 1
            prompt = _safe_int(item["prompt_tokens"])
            completion = _safe_int(item["completion_tokens"])
            if prompt is not None and completion is not None:
                point["prompt_tokens"] += prompt
                point["completion_tokens"] += completion
            else:
                point["estimated_output_tokens"] += (
                    _safe_int(item["estimated_output_tokens"]) or 0
                )
        return {
            "requests": requests,
            "conversations": int(row["conversations"] or 0),
            "completed": completed,
            "errors": errors,
            "cancelled": cancelled,
            "interrupted": interrupted,
            "success_rate": (
                completed / terminal_requests
                if terminal_requests
                else None
            ),
            "exact_usage_requests": exact_requests,
            "usage_coverage": (
                exact_requests / requests if requests else None
            ),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "estimated_output_tokens": int(
                row["estimated_output_tokens"] or 0
            ),
            "average_duration_seconds": (
                float(row["average_duration_seconds"])
                if row["average_duration_seconds"] is not None
                else None
            ),
            "p95_duration_seconds": p95_duration,
            "tool_calls": int(row["tool_calls"] or 0),
            "models": [
                {
                    "provider_id": item["provider_id"],
                    "model": item["model"],
                    "requests": int(item["requests"] or 0),
                    "completed": int(item["completed"] or 0),
                    "total_tokens": int(item["total_tokens"] or 0),
                }
                for item in model_rows
            ],
            "providers": [
                {
                    "provider_id": item["provider_id"],
                    "requests": int(item["requests"] or 0),
                    "completed": int(item["completed"] or 0),
                    "total_tokens": int(item["total_tokens"] or 0),
                }
                for item in provider_rows
            ],
            "series": list(series.values()),
        }

    def list_events(
        self,
        *,
        since: float | None,
        level: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        values: list[Any] = []
        if since is not None:
            clauses.append("time >= ?")
            values.append(since)
        if level:
            clauses.append("level = ?")
            values.append(level.upper())
        if search:
            clauses.append("(component LIKE ? OR message LIKE ?)")
            pattern = f"%{search[:200]}%"
            values.extend([pattern, pattern])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_limit = min(200, max(1, int(limit)))
        safe_offset = max(0, int(offset))
        with self._lock, closing(self._connect()) as database:
            total = int(
                database.execute(
                    "SELECT COUNT(*) FROM events" + where,
                    values,
                ).fetchone()[0]
            )
            rows = database.execute(
                """
                SELECT id, time, level, component, message, request_id
                FROM events
                """
                + where
                + " ORDER BY time DESC, id DESC LIMIT ? OFFSET ?",
                [*values, safe_limit, safe_offset],
            ).fetchall()
        items = [
            {
                "id": int(row["id"]),
                "time": row["time"],
                "level": str(row["level"]).lower(),
                "component": row["component"],
                "message": row["message"],
                "request_id": row["request_id"],
            }
            for row in rows
        ]
        return items, total
