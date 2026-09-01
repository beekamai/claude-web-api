"""The state and hooks the session mixins share with the driver.

`ClaudeSession` creates all of this in its constructor. Declaring it here gives
each mixin a contract to check against instead of relying on attributes that
only appear at runtime, which is what keeps the split honest under a type
checker.
"""

from __future__ import annotations

from typing import Any


class SessionState:
    """Attributes and hooks every session mixin may rely on."""

    page: Any
    ready: bool
    profile_specs: list[dict[str, Any]]
    profile_index: int

    _account_uuid: str | None
    _account_name: str | None
    _account_email: str | None
    _organization_uuid: str | None
    _profile_account_uuids: dict[str, str]
    _observed_models: set[str]
    _available_models: list[dict[str, Any]]
    _model_selector_state: dict[str, Any]
    _model_selector_diagnostics: dict[str, Any]
    _model_selector_cache_max_age_ms: int
    _model_selector_wait_ms: int
    _last_error: str | None
    _project_instructions: str
    _project_prompt_lease_file: Any
    _project_instructions_synced: bool
    _project_sync_error: str | None
    _project_privacy_verified: bool | None
    _project_lease_error: str | None

    def current_profile_spec(self) -> dict[str, Any]:
        raise NotImplementedError

    def current_profile_id(self) -> str:
        raise NotImplementedError

    def _set_phase(self, phase: str) -> None:
        raise NotImplementedError

    def _debug(self, message: str) -> None:
        raise NotImplementedError
