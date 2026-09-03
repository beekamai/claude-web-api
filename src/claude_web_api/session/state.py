"""The state and hooks the session mixins share with the driver.

`ClaudeSession` creates all of this in its constructor and implements the
hooks below. Declaring both here gives each mixin a contract to check
against instead of relying on attributes that only appear at runtime,
which is what keeps the split honest under a type checker.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from claude_web_api.session.models import NativeToolUse


class SessionState:
    """Attributes and hooks every session mixin may rely on."""

    _account_email_masked: str | None
    _account_name: str | None
    _account_uuid: str | None
    _available_models: list[dict[str, Any]]
    _browser_close_timeout: Any
    _browser_dead: Any
    _browser_start_timeout: Any
    _camoufox: Any
    _context: Any
    _conversation_client_session_id: str | None
    _conversation_privacy_mode: str | None
    _driver_pid: int | None
    _identity_failure_transient: bool
    _waiting_reload_at: float
    _waiting_reload_interval: float
    _proxy_relay: Any
    _history_recovery_required: Any
    _fresh_chat_required: bool
    _humanize_seconds: Any
    _last_completion_shape: dict[str, Any]
    _last_error: str | None
    _last_probe_at: float | None
    _last_probe_ok: bool | None
    _last_progress_at: Any
    _last_recovery_at: float | None
    _last_recovery_reason: str | None
    _lock: Any
    _model_selector_cache_max_age_ms: Any
    _model_selector_diagnostics: dict[str, Any]
    _model_selector_state: dict[str, Any]
    _model_selector_wait_ms: Any
    _native_active: Any
    _native_blocks: dict[int, dict[str, Any]]
    _native_client_session_id: str | None
    _native_completion_url: str | None
    _native_conversation_uuid: str | None
    _native_conversation_verified: Any
    _native_effort: str | None
    _native_event_sink: Callable[[dict[str, Any]], None] | None
    _native_headers: dict[str, str]
    _native_internal_text_prefix: list[str]
    _native_internal_thinking_prefix: list[str]
    _native_internal_tool_acks: Any
    _native_internal_tool_names: set[str]
    _native_model: str | None
    _native_org_uuid: str | None
    _native_parallel_tool_calls: Any
    _native_pending_deadline: float | None
    _native_pending_ids: set[str]
    _native_queue: asyncio.Queue[dict[str, str]] | None
    _native_requested_model: str | None
    _native_saw_content: Any
    _native_saw_tool: Any
    _native_stop_reason: str | None
    _native_terminal_seen: Any
    _native_text_blocks: dict[int, str]
    _native_thinking_blocks: dict[int, str]
    _native_thinking_mode: Any
    _native_tool_blocks: dict[int, NativeToolUse]
    _native_tools: list[dict[str, Any]]
    _native_usage: dict[str, Any]
    _next_recovery_at: Any
    _observed_models: set[str]
    _operation_id: str | None
    _organization_uuid: str | None
    _phase: Any
    _phase_started_at: Any
    _privacy_mode: Any
    _profile_account_uuids: dict[str, str]
    _project_instructions: Any
    _project_instructions_synced: Any
    _project_lease_error: str | None
    _project_privacy_verified: bool | None
    _project_prompt_lease_file: Any
    _project_sync_error: str | None
    _recovery_exhausted: Any
    _recovery_failures: Any
    _restart_count: Any
    _restart_limit: Any
    _restart_times: list[float]
    _restart_window: Any
    _session_epoch: Any
    _sse_tap_event_count: Any
    _sse_tap_last_at: float | None
    _sse_tap_last_data: str | None
    _sse_tap_last_event: str | None
    _sse_tap_last_url: str | None
    _sse_tap_rejected_count: Any
    _stopping: Any
    _tool_result_delivery: dict[str, str]
    _tool_result_lease_seconds: Any
    _tool_result_post_timeout: Any
    _watchdog_heartbeat_at: Any
    _watchdog_interval: Any
    _watchdog_probe_timeout: Any
    _watchdog_stall_timeout: Any
    _watchdog_stop: Any
    _watchdog_task: asyncio.Task[Any] | None
    headless: Any
    page: Any
    profile_dirs: Any
    profile_index: Any
    profile_specs: Any
    ready: Any

    def current_profile_spec(self) -> dict[str, Any]:
        raise NotImplementedError

    def current_profile_id(self) -> str:
        raise NotImplementedError

    def _current_start_url(self) -> str:
        raise NotImplementedError

    def _set_phase(self, phase: str) -> None:
        raise NotImplementedError

    def _debug(self, message: str) -> None:
        raise NotImplementedError

    def _input_locator(self) -> Any:
        raise NotImplementedError

    def _native_wait_expired(self) -> bool:
        raise NotImplementedError

    async def _receive_sse(self, source: Any, payload: Any) -> None:
        raise NotImplementedError

    def _clear_native_state(self) -> None:
        raise NotImplementedError

    async def _expire_native_lease_unlocked(self) -> None:
        raise NotImplementedError

    async def _load_account_identity(self) -> bool:
        raise NotImplementedError

    async def _sync_trusted_project(self) -> bool:
        raise NotImplementedError

    async def _route_completion(
        self,
        route: Any,
        request: Any,
        epoch: Any = None,
    ) -> None:
        raise NotImplementedError

    async def _route_conversation_create(
        self,
        route: Any,
        request: Any,
        epoch: Any = None,
    ) -> None:
        raise NotImplementedError

    async def _ensure_healthy_unlocked(self, reason: str) -> None:
        raise NotImplementedError

    async def _goto_start_page(self, timeout_ms: int=60000) -> None:
        raise NotImplementedError

    async def _install_sse_tap(self) -> None:
        raise NotImplementedError

    def _mark_browser_dead(self, reason: str) -> None:
        raise NotImplementedError

    async def _recover_browser_unlocked(self, reason: str) -> None:
        raise NotImplementedError

    async def _verify_account_unchanged_unlocked(self) -> None:
        raise NotImplementedError

    async def _wait_ready(self, timeout: float=180.0) -> None:
        raise NotImplementedError

    async def _activate_trusted_turn_context(self) -> None:
        raise NotImplementedError

    async def _new_chat_unlocked(self) -> None:
        raise NotImplementedError

    async def _prepare_composer_unlocked(self, *, new_chat: bool, native: bool) -> None:
        raise NotImplementedError

    def _process_native_event(self, envelope: dict[str, str]) -> bool:
        raise NotImplementedError

    async def _raise_if_limited(self, response_errors: list[str]) -> None:
        raise NotImplementedError

    def _reset_native_parser(self) -> None:
        raise NotImplementedError

    async def _submit_message(self, message: str) -> None:
        raise NotImplementedError

    def _take_native_text(self) -> str | None:
        raise NotImplementedError

    def _take_native_thinking(self) -> str | None:
        raise NotImplementedError
