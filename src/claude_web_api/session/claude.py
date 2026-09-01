"""Authenticated Camoufox session for claude.ai web chat.

OpenClaude tools use claude.ai's native side-channel:

``completion.tools -> SSE tool_use -> POST tool_result -> same SSE continues``.

The browser remains the owner of authentication cookies. The gateway never
fabricates a tool decision or a final answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from claude_web_api.paths import (
    LEGACY_PROFILE_DIR as PROFILE_DIR,
)
from claude_web_api.paths import (
    LEGACY_PROJECT_FILE as PROJECT_CONFIG_FILE,
)
from claude_web_api.session.browser import BrowserLifecycleMixin
from claude_web_api.session.composer import ChatComposerMixin
from claude_web_api.session.errors import (
    ClaudeAccountIdentityError,
    ClaudeBrowserUnavailableError,
    ClaudeCompletionRejectedError,
    ClaudeConversationLimitError,
    ClaudeLimitError,
    ClaudeServiceUnavailableError,
    ClaudeTurnOutcomeUnknownError,
    ClaudeUsageLimitError,
)
from claude_web_api.session.identity import AccountIdentityMixin
from claude_web_api.session.models import NativeToolUse, NativeTurn
from claude_web_api.session.patterns import (
    KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256,
    MODEL_SELECTOR_TRANSIENT_REASONS,
)
from claude_web_api.session.project import TrustedProjectMixin
from claude_web_api.session.scripts import (
    SSE_TAP_SCRIPT,
)
from claude_web_api.session.stream import NativeStreamMixin
from claude_web_api.session.turn import NativeTurnMixin


def _legacy_project_id() -> str | None:
    if PROJECT_CONFIG_FILE.exists():
        try:
            config = json.loads(PROJECT_CONFIG_FILE.read_text(encoding="utf-8"))
            project_id = str(config.get("project_id", "") or "").strip()
            return project_id or None
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _claude_start_url(project_id: str | None = None) -> str:
    explicit_url = os.getenv("CLAUDE_START_URL")
    if explicit_url:
        return explicit_url
    project_id = (
        project_id
        or os.getenv("CLAUDE_PROJECT_ID")
        or _legacy_project_id()
    )
    if project_id:
        return f"https://claude.ai/project/{project_id}"
    return "https://claude.ai/new"





# claude.py stays the session facade: callers import the driver, the failures
# it raises and the shapes it returns from one place.
__all__ = [
    "KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256",
    "MODEL_SELECTOR_TRANSIENT_REASONS",
    "SSE_TAP_SCRIPT",
    "ClaudeAccountIdentityError",
    "ClaudeBrowserUnavailableError",
    "ClaudeCompletionRejectedError",
    "ClaudeConversationLimitError",
    "ClaudeLimitError",
    "ClaudeServiceUnavailableError",
    "ClaudeSession",
    "ClaudeTurnOutcomeUnknownError",
    "ClaudeUsageLimitError",
    "NativeToolUse",
    "NativeTurn",
]

class ClaudeSession(
    AccountIdentityMixin,
    BrowserLifecycleMixin,
    ChatComposerMixin,
    NativeStreamMixin,
    NativeTurnMixin,
    TrustedProjectMixin,
):
    def __init__(
        self,
        headless: bool = False,
        profiles: list[dict[str, Any]] | None = None,
        active_profile_id: str | None = None,
        project_instructions: str | None = None,
        project_prompt_lease_file: str | Path | None = None,
    ) -> None:
        self.headless = headless
        configured_profiles = os.getenv("CLAUDE_PROFILE_DIRS", "")
        if profiles:
            self.profile_specs = [
                {
                    "id": str(row.get("id") or f"profile-{index + 1}"),
                    "name": str(row.get("name") or f"Profile {index + 1}"),
                    "path": str(Path(str(row["path"])).expanduser().resolve()),
                    "project_id": (
                        str(row.get("project_id") or "").strip() or None
                    ),
                    "organization_id": (
                        str(row.get("organization_id") or "").strip() or None
                    ),
                    "model": str(row.get("model") or "auto"),
                    "proxy": row.get("proxy"),
                }
                for index, row in enumerate(profiles)
                if isinstance(row, dict) and row.get("path")
            ]
        else:
            profile_paths = (
                [
                    Path(item).expanduser()
                    for item in configured_profiles.split(os.pathsep)
                    if item
                ]
                if configured_profiles
                else [PROFILE_DIR]
            )
            self.profile_specs = [
                {
                    "id": "default" if index == 0 else f"profile-{index + 1}",
                    "name": (
                        "Основной" if index == 0 else f"Profile {index + 1}"
                    ),
                    "path": str(path.resolve()),
                    "project_id": _legacy_project_id()
                    if index == 0
                    else None,
                    "organization_id": None,
                    "model": "auto",
                    "proxy": None,
                }
                for index, path in enumerate(profile_paths)
            ]
        if not self.profile_specs:
            raise ValueError("at least one browser profile is required")
        self.profile_dirs = [
            Path(str(row["path"])) for row in self.profile_specs
        ]
        self.profile_index = next(
            (
                index
                for index, row in enumerate(self.profile_specs)
                if row["id"] == active_profile_id
            ),
            0,
        )
        self._camoufox: Any = None
        self._context: Any = None
        self.page: Any = None
        self._lock = asyncio.Lock()
        self.ready = False
        self._stopping = False
        self._browser_dead = asyncio.Event()
        self._watchdog_stop = asyncio.Event()
        self._watchdog_task: asyncio.Task[Any] | None = None
        self._watchdog_heartbeat_at = time.monotonic()
        self._phase = "stopped"
        self._phase_started_at = time.monotonic()
        self._last_progress_at = self._phase_started_at
        self._operation_id: str | None = None
        self._session_epoch = 0
        self._restart_count = 0
        self._restart_times: list[float] = []
        self._recovery_exhausted = False
        self._recovery_failures = 0
        self._next_recovery_at = 0.0
        self._last_recovery_reason: str | None = None
        self._last_recovery_at: float | None = None
        self._last_error: str | None = None
        self._last_probe_at: float | None = None
        self._last_probe_ok: bool | None = None
        self._account_uuid: str | None = None
        self._account_name: str | None = None
        self._account_email_masked: str | None = None
        self._organization_uuid: str | None = None
        self._project_instructions = str(project_instructions or "")
        self._project_prompt_lease_file = (
            Path(project_prompt_lease_file).expanduser().resolve()
            if project_prompt_lease_file is not None
            else None
        )
        self._project_lease_error: str | None = None
        self._project_instructions_synced = False
        self._project_sync_error: str | None = None
        self._project_privacy_verified: bool | None = None
        self._profile_account_uuids: dict[str, str] = {}
        self._available_models: list[dict[str, Any]] = []
        self._model_selector_state: dict[str, Any] = {}
        self._model_selector_diagnostics: dict[str, Any] = {}
        self._model_selector_cache_max_age_ms = max(
            30_000,
            int(
                float(
                    os.getenv(
                        "CLAUDE_MODEL_SELECTOR_MAX_AGE_SECONDS",
                        "300",
                    )
                )
                * 1_000
            ),
        )
        self._model_selector_wait_ms = max(
            0,
            int(
                float(
                    os.getenv(
                        "CLAUDE_MODEL_SELECTOR_WAIT_SECONDS",
                        "45",
                    )
                )
                * 1_000
            ),
        )
        self._driver_pid: int | None = None
        self._tool_result_delivery: dict[str, str] = {}
        self._watchdog_interval = max(
            2.0, float(os.getenv("CLAUDE_WATCHDOG_INTERVAL", "10"))
        )
        self._watchdog_probe_timeout = max(
            1.0, float(os.getenv("CLAUDE_WATCHDOG_PROBE_TIMEOUT", "4"))
        )
        self._watchdog_stall_timeout = max(
            30.0, float(os.getenv("CLAUDE_WATCHDOG_STALL_TIMEOUT", "90"))
        )
        self._browser_close_timeout = max(
            2.0, float(os.getenv("CLAUDE_BROWSER_CLOSE_TIMEOUT", "10"))
        )
        self._tool_result_post_timeout = max(
            3.0, float(os.getenv("CLAUDE_TOOL_RESULT_POST_TIMEOUT", "20"))
        )
        self._browser_start_timeout = max(
            60.0, float(os.getenv("CLAUDE_BROWSER_START_TIMEOUT", "330"))
        )
        self._restart_window = max(
            60.0, float(os.getenv("CLAUDE_RESTART_WINDOW", "600"))
        )
        self._restart_limit = max(
            2, int(os.getenv("CLAUDE_RESTART_LIMIT", "5"))
        )
        try:
            self._humanize_seconds = max(
                0.0,
                float(os.getenv("CLAUDE_HUMANIZE_SECONDS", "0.25")),
            )
        except ValueError:
            self._humanize_seconds = 0.25

        self._native_active = False
        self._native_queue: asyncio.Queue[dict[str, str]] | None = None
        self._native_tools: list[dict[str, Any]] = []
        self._native_internal_tool_names: set[str] = set()
        self._native_internal_tool_acks = 0
        self._native_internal_text_prefix: list[str] = []
        self._native_internal_thinking_prefix: list[str] = []
        self._native_completion_url: str | None = None
        self._native_org_uuid: str | None = None
        self._native_conversation_uuid: str | None = None
        self._native_headers: dict[str, str] = {}
        self._native_pending_ids: set[str] = set()
        self._native_pending_deadline: float | None = None
        self._native_parallel_tool_calls = True
        self._history_recovery_required = False
        self._fresh_chat_required = False
        self._tool_result_lease_seconds = float(
            os.getenv("CLAUDE_TOOL_RESULT_TIMEOUT", "600")
        )
        self._native_blocks: dict[int, dict[str, Any]] = {}
        self._native_text_blocks: dict[int, str] = {}
        self._native_tool_blocks: dict[int, NativeToolUse] = {}
        self._native_thinking_blocks: dict[int, str] = {}
        self._native_usage: dict[str, Any] = {}
        self._native_model: str | None = None
        self._native_stop_reason: str | None = None
        self._native_requested_model: str | None = None
        self._native_thinking_mode = "auto"
        self._native_effort: str | None = None
        self._native_conversation_verified = False
        self._native_event_sink: (
            Callable[[dict[str, Any]], None] | None
        ) = None
        self._privacy_mode = "keep"
        self._conversation_privacy_mode: str | None = None
        self._conversation_client_session_id: str | None = None
        self._native_client_session_id: str | None = None
        self._last_completion_shape: dict[str, Any] = {}
        self._observed_models: set[str] = set()
        self._native_saw_content = False
        self._native_saw_tool = False
        self._native_terminal_seen = False
        self._sse_tap_event_count = 0
        self._sse_tap_rejected_count = 0
        self._sse_tap_last_at: float | None = None
        self._sse_tap_last_event: str | None = None
        self._sse_tap_last_url: str | None = None
        self._sse_tap_last_data: str | None = None

    def current_profile_spec(self) -> dict[str, Any]:
        return dict(self.profile_specs[self.profile_index])

    def current_profile_id(self) -> str:
        return str(self.profile_specs[self.profile_index]["id"])

    def _current_start_url(self) -> str:
        project_id = self.profile_specs[self.profile_index].get("project_id")
        if self._privacy_mode == "ephemeral" and not project_id:
            return "https://claude.ai/new?incognito=true"
        return _claude_start_url(project_id)

    async def sync_profiles(
        self,
        profiles: list[dict[str, Any]],
        active_profile_id: str | None = None,
        *,
        restart: bool = False,
    ) -> None:
        """Refresh the runtime profile catalog without exposing browser data."""
        normalized = [
            {
                "id": str(row.get("id") or f"profile-{index + 1}"),
                "name": str(row.get("name") or f"Profile {index + 1}"),
                "path": str(Path(str(row["path"])).expanduser().resolve()),
                "project_id": (
                    str(row.get("project_id") or "").strip() or None
                ),
                "organization_id": (
                    str(row.get("organization_id") or "").strip() or None
                ),
                "model": str(row.get("model") or "auto"),
                "proxy": row.get("proxy"),
            }
            for index, row in enumerate(profiles)
            if isinstance(row, dict) and row.get("path")
        ]
        if not normalized:
            raise ValueError("at least one browser profile is required")
        async with self._lock:
            current_id = self.current_profile_id()
            target_id = active_profile_id or current_id
            target_index = next(
                (
                    index
                    for index, row in enumerate(normalized)
                    if row["id"] == target_id
                ),
                0,
            )
            current_path = self.current_profile_spec()["path"]
            target_path = normalized[target_index]["path"]
            needs_restart = restart or current_path != target_path
            if needs_restart and self._native_active:
                raise RuntimeError(
                    "cannot switch profile while Claude is waiting for tool_result"
                )
            if needs_restart:
                await self._stop_browser_unlocked()
            self.profile_specs = normalized
            self.profile_dirs = [
                Path(str(row["path"])) for row in normalized
            ]
            self.profile_index = target_index
            if needs_restart:
                await self.start()

    async def activate_profile(self, profile_id: str) -> None:
        target_index = next(
            (
                index
                for index, row in enumerate(self.profile_specs)
                if row["id"] == profile_id
            ),
            None,
        )
        if target_index is None:
            raise KeyError(profile_id)
        async with self._lock:
            if target_index == self.profile_index and self.ready:
                return
            if self._native_active:
                raise RuntimeError(
                    "cannot switch profile while Claude is waiting for tool_result"
                )
            await self._stop_browser_unlocked()
            self.profile_index = target_index
            await self.start()














    @staticmethod
    def _debug(message: str) -> None:
        if os.getenv("CLAUDE_DEBUG_BROWSER", "0").lower() in ("1", "true", "yes"):
            print(f"CLAUDE_BROWSER {message}", flush=True)

    def _set_phase(self, phase: str, *, progress: bool = True) -> None:
        now = time.monotonic()
        if phase != self._phase:
            self._phase = phase
            self._phase_started_at = now
        if progress:
            self._last_progress_at = now



















    def health_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "ok": bool(
                self.ready
                and not self._browser_dead.is_set()
                and self._last_probe_ok is not False
                and self._phase
                not in {
                    "stopped",
                    "starting_browser",
                    "recovering_browser",
                    "browser_dead",
                    "auth_required",
                    "account_unknown",
                    "account_changed",
                    "project_unavailable",
                }
            ),
            "url": self._mask_identifiers(
                getattr(self.page, "url", None)
            ),
            "profile": self.profile_index + 1,
            "profile_count": len(self.profile_dirs),
            "profile_id": self.current_profile_id(),
            "profile_name": self.current_profile_spec().get("name"),
            "account": {
                "authenticated": bool(self._account_uuid),
                "uuid_suffix": (
                    self._account_uuid[-8:] if self._account_uuid else None
                ),
                "name": self._account_name,
                "email": self._account_email_masked,
            },
            "project": {
                "configured": bool(
                    self.current_profile_spec().get("project_id")
                ),
                "instructions_synced": self._project_instructions_synced,
                "privacy_verified": self._project_privacy_verified,
                "lease_error": self._project_lease_error,
                "turn_context_active": False,
                "dynamic_context_channel": "native_tool_description",
                "error": self._project_sync_error,
            },
            "browser": {
                "phase": self._phase,
                "session_epoch": self._session_epoch,
                "driver_pid": self._driver_pid,
                "operation_id": self._operation_id,
                "phase_age_seconds": round(now - self._phase_started_at, 1),
                "progress_age_seconds": round(now - self._last_progress_at, 1),
                "restart_count": self._restart_count,
                "last_probe_ok": self._last_probe_ok,
                "last_probe_at": self._last_probe_at,
                "last_recovery_reason": self._last_recovery_reason,
                "last_recovery_at": self._last_recovery_at,
                "last_error": self._last_error,
                "watchdog_running": self.watchdog_running(),
                "watchdog_healthy": self.watchdog_healthy(),
                "recovery_exhausted": self._recovery_exhausted,
            },
            "native": {
                "active": self._native_active,
                "pending_tool_ids": sorted(self._native_pending_ids),
                "tool_result_delivery": dict(self._tool_result_delivery),
                "model": self._native_model,
                "usage": dict(self._native_usage),
                "tap": {
                    "event_count": self._sse_tap_event_count,
                    "rejected_count": self._sse_tap_rejected_count,
                    "last_at": self._sse_tap_last_at,
                    "last_event": self._sse_tap_last_event,
                    "last_url": self._mask_identifiers(
                        self._sse_tap_last_url
                    ),
                    "last_data": self._sse_tap_last_data,
                },
            },
            "models": {
                "available": [
                    dict(model) for model in self._available_models
                ],
                "observed": self.observed_models(),
                "state": dict(self._model_selector_state),
                "selector": dict(self._model_selector_diagnostics),
            },
        }











    async def rotate_profile(
        self,
        eligible_profile_ids: set[str] | None = None,
    ) -> bool:
        """Move to the next pre-authenticated browser profile, if configured."""
        async with self._lock:
            if len(self.profile_dirs) < 2:
                return False
            target_index: int | None = None
            for offset in range(1, len(self.profile_dirs) + 1):
                candidate = (self.profile_index + offset) % len(
                    self.profile_dirs
                )
                candidate_id = str(self.profile_specs[candidate]["id"])
                if (
                    eligible_profile_ids is None
                    or candidate_id in eligible_profile_ids
                ):
                    target_index = candidate
                    break
            if target_index is None or target_index == self.profile_index:
                return False
            await self._stop_browser_unlocked()
            self.profile_index = target_index
            try:
                await self.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ClaudeBrowserUnavailableError(
                    f"rotated Camoufox profile failed to start: {exc}"
                ) from exc
            if not self.ready:
                raise ClaudeBrowserUnavailableError(
                    "rotated Camoufox profile requires authentication or "
                    "account verification"
                )
            return True







































