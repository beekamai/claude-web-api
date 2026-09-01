"""Process-wide runtime the HTTP layer talks to.

The browser session, control configuration, provider registry, enrollment
manager and request journal are created once per process and shared by every
route. Routes reach them through this module rather than importing the values
directly, so a test can replace one in place.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from claude_web_api.control.config import ControlConfig
from claude_web_api.enrollment.manager import ProfileEnrollmentManager
from claude_web_api.paths import PROJECT_INSTRUCTIONS, PROJECT_PROMPT_LEASE_FILE
from claude_web_api.providers.claude_web import (
    CLAUDE_WEB_PROVIDER_ID,
    ClaudeWebProviderAdapter,
)
from claude_web_api.providers.registry import ProviderRegistry
from claude_web_api.sanitize import public_error_message
from claude_web_api.session.claude import ClaudeSession
from claude_web_api.telemetry.runtime import RuntimeTelemetry
from claude_web_api.telemetry.store import TelemetryStore

BRIDGE_INSTRUCTIONS = PROJECT_INSTRUCTIONS.read_text(encoding="utf-8")
HEADLESS = os.getenv("CLAUDE_HEADLESS", "0").lower() in ("1", "true", "yes")
DEBUG_REQUESTS = os.getenv("CLAUDE_DEBUG_REQUESTS", "0").lower() in (
    "1",
    "true",
    "yes",
)

control = ControlConfig()
GROK_WEB_PROVIDER_ID = "grok_web"


def profile_provider_id(profile_id: str | None) -> str:
    if not profile_id:
        return CLAUDE_WEB_PROVIDER_ID
    try:
        profile = control.profile(profile_id)
    except KeyError:
        return CLAUDE_WEB_PROVIDER_ID
    return str(
        profile.get("provider") or CLAUDE_WEB_PROVIDER_ID
    ).strip() or CLAUDE_WEB_PROVIDER_ID


def provider_profile_id(provider_id: str) -> str:
    rows = [
        row
        for row in control.session_profiles()
        if row.get("provider", CLAUDE_WEB_PROVIDER_ID) == provider_id
    ]
    if not rows:
        raise RuntimeError(
            f"no configured browser profile for provider {provider_id!r}"
        )
    active_id = control.snapshot()["active_profile"]
    active = next((row for row in rows if row["id"] == active_id), None)
    if active is not None:
        return str(active["id"])
    for status in ("ready", "limited", "configured", "auth_required"):
        candidate = next(
            (
                row
                for row in rows
                if control.profile(row["id"]).get("status") == status
            ),
            None,
        )
        if candidate is not None:
            return str(candidate["id"])
    return str(rows[0]["id"])


def runtime_profiles(
    provider_id: str = CLAUDE_WEB_PROVIDER_ID,
) -> list[dict[str, Any]]:
    snapshot = control.snapshot()
    del snapshot
    active = provider_profile_id(provider_id)
    allowed = []
    for row in control.session_profiles():
        if row.get("provider", CLAUDE_WEB_PROVIDER_ID) != provider_id:
            continue
        raw = control.profile(row["id"])
        if not raw.get("enabled", True):
            continue
        if (
            raw["id"] != active
            and raw.get("status") not in {"ready", "limited"}
        ):
            continue
        allowed.append(row)
    if not any(row["id"] == active for row in allowed):
        allowed.insert(
            0,
            next(
                row
                for row in control.session_profiles()
                if row["id"] == active
            ),
        )
    return allowed


def eligible_rotation_ids() -> set[str]:
    now = time.time()
    eligible: set[str] = set()
    for row in control.session_profiles():
        if (
            row.get("provider", CLAUDE_WEB_PROVIDER_ID)
            != CLAUDE_WEB_PROVIDER_ID
        ):
            continue
        profile = control.profile(row["id"])
        if not profile.get("enabled", True):
            continue
        if profile.get("status") not in {"ready", "limited"}:
            continue
        limited_until = profile.get("limited_until")
        if isinstance(limited_until, (int, float)) and limited_until > now:
            continue
        eligible.add(row["id"])
    return eligible


def resolve_request_model(
    requested: str,
    profile_id: str | None = None,
) -> str | None:
    requested = str(requested or "").strip()
    # Completion requests currently execute on the Claude Camoufox runtime.
    # The persisted control-plane active profile can legitimately name a
    # fail-closed provider (for example after restoring a newer config), so it
    # must never decide which model is sent to the actual browser runtime.
    target_id = profile_id or session.current_profile_id()
    try:
        profile = control.profile(target_id)
    except KeyError:
        return None
    if target_id == session.current_profile_id():
        catalog = session.selectable_models()
    else:
        catalog = profile.get("models", [])
    available = {
        str(item.get("id"))
        for item in catalog
        if (
            isinstance(item, dict)
            and item.get("available") is True
            and item.get("access_status") == "available"
        )
    }
    if requested and requested not in {"claude-web", "auto"}:
        if requested not in available:
            reason = next(
                (
                    item.get("disabled_reason")
                    for item in catalog
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == requested
                ),
                None,
            )
            suffix = ""
            if isinstance(reason, dict) and reason.get("required_plan"):
                suffix = (
                    f" (requires {reason['required_plan']} subscription)"
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model {requested!r} is not available to the active "
                    f"Claude account{suffix}."
                ),
            )
        return requested
    selected = str(profile.get("model") or "auto")
    if selected in {"", "auto", "claude-web"}:
        return None
    # A stale saved selection must not make the alias unusable. Browser auto
    # remains the safe fallback until account-scoped entitlement is verified.
    return selected if selected in available else None


session = ClaudeSession(
    headless=HEADLESS,
    profiles=runtime_profiles(),
    active_profile_id=provider_profile_id(CLAUDE_WEB_PROVIDER_ID),
    project_instructions=BRIDGE_INSTRUCTIONS,
    project_prompt_lease_file=PROJECT_PROMPT_LEASE_FILE,
)
claude_provider = ClaudeWebProviderAdapter(session)
provider_registry = ProviderRegistry()
provider_registry.register(
    CLAUDE_WEB_PROVIDER_ID,
    claude_provider,
    profile_ids=(
        row["id"]
        for row in control.session_profiles()
        if row.get("provider", CLAUDE_WEB_PROVIDER_ID)
        == CLAUDE_WEB_PROVIDER_ID
    ),
)
enrollment = ProfileEnrollmentManager()
profile_login_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}


def bind_claude_profile_route(profile: dict[str, Any]) -> None:
    """Keep the registry aligned with mutable control-plane profile rows."""
    if (
        profile.get("provider", CLAUDE_WEB_PROVIDER_ID)
        != CLAUDE_WEB_PROVIDER_ID
    ):
        return
    provider_registry.bind_profile(
        str(profile["id"]),
        CLAUDE_WEB_PROVIDER_ID,
        replace=True,
    )


def is_active_claude_profile(profile: dict[str, Any]) -> bool:
    if (
        profile.get("provider", CLAUDE_WEB_PROVIDER_ID)
        != CLAUDE_WEB_PROVIDER_ID
    ):
        return False
    try:
        active_path = Path(session.current_profile_spec()["path"]).resolve()
        target_path = Path(profile["path"]).resolve()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False
    return active_path == target_path


def provider_capabilities_snapshot() -> dict[str, dict[str, object]]:
    snapshot = provider_registry.capabilities_snapshot()
    snapshot.setdefault(
        GROK_WEB_PROVIDER_ID,
        {
            "tool_continuation": "unsupported",
            "streaming": False,
            "thinking": False,
            "profiles": True,
            "ready": False,
            "detail": (
                "xAI currently blocks automated Camoufox and Playwright "
                "Chrome sessions, while manual Chrome works on the same "
                "machine and IP. Grok Web stays disabled until browser access "
                "and its stream/tool behavior can be verified."
            ),
        },
    )
    return snapshot





try:
    telemetry = RuntimeTelemetry(TelemetryStore())
except Exception as telemetry_error:
    telemetry = RuntimeTelemetry()
    telemetry.storage_error = public_error_message(telemetry_error)


def persist_runtime_identity() -> bool:
    health = session.health_snapshot()
    profile_id = str(health.get("profile_id") or "")
    if not profile_id:
        return True
    account = health.get("account")
    models = health.get("models")
    if not isinstance(account, dict):
        account = {}
    if not isinstance(models, dict):
        models = {}
    updates: dict[str, Any] = {
        "account": account,
        "last_checked_at": time.time(),
        "models": models.get("available", []),
    }
    try:
        existing = control.profile(profile_id)
    except KeyError:
        return False
    verified_model_ids = {
        str(item.get("id") or "")
        for item in updates["models"]
        if (
            isinstance(item, dict)
            and item.get("available") is True
            and item.get("access_status") == "available"
        )
    }
    selected_model = str(existing.get("model") or "auto")
    if (
        selected_model not in {"", "auto", "claude-web"}
        and selected_model not in verified_model_ids
    ):
        updates["model"] = "auto"
    organization_uuid = session.organization_uuid_for_internal_use()
    if organization_uuid:
        updates["organization_id"] = organization_uuid
    if account.get("authenticated"):
        project = health.get("project")
        updates["status"] = (
            "ready"
            if isinstance(project, dict)
            and project.get("instructions_synced")
            else "project_error"
        )
        account_uuid = session.account_uuid_for_internal_use()
        if account_uuid:
            fingerprint = control.account_fingerprint(
                account_uuid
            )
            duplicate = control.profile_with_fingerprint(
                fingerprint,
                exclude_id=profile_id,
            )
            old_fingerprint = existing.get("account_fingerprint")
            if duplicate is not None or (
                old_fingerprint
                and old_fingerprint != fingerprint
            ):
                updates["status"] = (
                    "duplicate" if duplicate is not None else "account_changed"
                )
                try:
                    control.update_profile(profile_id, updates)
                except (KeyError, OSError, RuntimeError):
                    pass
                return False
            updates["account_fingerprint"] = fingerprint
    try:
        control.update_profile(profile_id, updates)
    except (KeyError, OSError, RuntimeError):
        return False
    return True


def telemetry_content_enabled() -> bool:
    """Content is journalled only when both the setting and privacy mode allow it."""
    settings = control.telemetry_settings()
    privacy_mode = str(control.behavior().get("privacy") or "keep")
    return bool(settings.get("store_content")) and privacy_mode != "ephemeral"


async def telemetry_maintenance() -> None:
    while True:
        await asyncio.sleep(3_600)
        settings = control.telemetry_settings()
        if telemetry.store is None:
            continue
        try:
            if not bool(settings.get("store_content")):
                await telemetry.store_call_async("scrub_content")
            await telemetry.store_call_async(
                "prune",
                retention_days=int(
                    settings.get("retention_days") or 30
                ),
                max_requests=int(
                    settings.get("max_requests") or 5_000
                ),
            )
            telemetry.storage_error = None
        except Exception as exc:
            telemetry.storage_error = public_error_message(exc)
