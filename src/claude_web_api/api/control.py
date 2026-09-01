"""Control-panel API.

Everything the local panel needs to inspect and steer the bridge: health of
the browser profiles, the request journal, behaviour switches and the
enrollment flow for adding an account. Browser cookies, raw account UUIDs and
tool payloads never cross this boundary.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from claude_web_api import __version__, clients, runtime
from claude_web_api.control import proxy as proxy_settings
from claude_web_api.control.config import ControlConfig
from claude_web_api.paths import DATA_ROOT, PROJECT_INSTRUCTIONS, PROJECT_ROOT
from claude_web_api.providers.claude_web import CLAUDE_WEB_PROVIDER_ID
from claude_web_api.sanitize import (
    public_error_message,
    sanitize_public_text,
)
from claude_web_api.telemetry.store import TelemetryStore

router = APIRouter()


class BehaviorPatch(BaseModel):
    streaming: bool | None = None
    thinking: str | None = None
    privacy: str | None = None
    persona: str | None = None
    custom_persona: str | None = None
    actor: bool | None = None
    mature: bool | None = None


class TelemetryPatch(BaseModel):
    store_content: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=365)


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(default=CLAUDE_WEB_PROVIDER_ID, max_length=32)


class ModelSelect(BaseModel):
    model: str = Field(min_length=1, max_length=160)


class ProxyPatch(BaseModel):
    """A profile's outbound proxy.

    ``password`` is omitted, not blanked, when the operator leaves the field
    untouched: the panel never receives the stored one back, so an absent
    value means "keep it" and an empty string means "clear it".
    """

    enabled: bool = False
    server: str = Field(default="", max_length=300)
    username: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, max_length=400)

    def updates(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled": self.enabled,
            "server": self.server,
            "username": self.username,
        }
        if self.password is not None:
            payload["password"] = self.password
        return payload


@router.get("/api/control/state")
async def control_state():
    snapshot = runtime.control.snapshot()
    persona_compilation = ControlConfig.persona_compilation_for(
        snapshot.get("behavior", {}),
    )
    health = runtime.session.health_snapshot()
    for profile in snapshot["profiles"]:
        if profile["id"] == health.get("profile_id"):
            profile["runtime"] = {
                "active": True,
                "account": health.get("account"),
                "models": health.get("models"),
                "browser": health.get("browser"),
            }
    return {
        "config": snapshot,
        "persona_compilation": persona_compilation,
        "health": health,
        "providers": runtime.provider_capabilities_snapshot(),
        "activity": runtime.telemetry.snapshot(),
        "server": {
            "version": __version__,
            "port": int(os.getenv("PORT", "8765")),
            "project_root": str(PROJECT_ROOT),
            "data_root": str(DATA_ROOT),
            "working_directory": os.getcwd(),
            "streaming_is_live": bool(
                runtime.control.behavior().get("streaming")
            ),
            "thinking_note": (
                "Claude Web exposes provider summaries when available; "
                "hidden chain-of-thought is never fabricated or leaked."
            ),
        },
        "protocol": runtime.session.last_completion_shape(),
    }


TELEMETRY_PERIOD_SECONDS = {
    "1h": 3_600,
    "24h": 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
    "all": None,
}
TELEMETRY_STATUSES = {
    "running",
    "completed",
    "error",
    "cancelled",
    "interrupted",
}


def _telemetry_since(period: str) -> float | None:
    if period not in TELEMETRY_PERIOD_SECONDS:
        raise HTTPException(
            400,
            "period must be one of 1h, 24h, 7d, 30d or all",
        )
    seconds = TELEMETRY_PERIOD_SECONDS[period]
    return time.time() - seconds if seconds is not None else None


def _persistent_telemetry_store() -> TelemetryStore:
    if runtime.telemetry.store is None:
        raise HTTPException(
            503,
            runtime.telemetry.storage_error
            or "persistent telemetry storage is unavailable",
        )
    return runtime.telemetry.store


@router.get("/api/control/telemetry")
async def control_telemetry(
    period: str = Query(default="7d"),
    status: str = Query(default="all"),
    provider_id: str | None = Query(default=None, max_length=32),
    profile_id: str | None = Query(default=None, max_length=80),
    model: str | None = Query(default=None, max_length=160),
    q: str | None = Query(default=None, max_length=200),
    level: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    since = _telemetry_since(period)
    normalized_status = status.lower()
    if normalized_status != "all" and normalized_status not in TELEMETRY_STATUSES:
        raise HTTPException(400, "unsupported telemetry status")
    normalized_level = level.lower()
    if normalized_level not in {"all", "info", "warn", "error"}:
        raise HTTPException(400, "unsupported event level")
    _persistent_telemetry_store()
    try:
        requests, total = await runtime.telemetry.store_call_async(
            "list_requests",
            since=since,
            status=(
                None if normalized_status == "all" else normalized_status
            ),
            provider_id=provider_id or None,
            profile_id=profile_id or None,
            model=model or None,
            search=q or None,
            limit=limit,
            offset=offset,
        )
        summary = await runtime.telemetry.store_call_async(
            "summary",
            since=since,
            provider_id=provider_id or None,
            profile_id=profile_id or None,
            model=model or None,
            search=q or None,
        )
        events, event_total = await runtime.telemetry.store_call_async(
            "list_events",
            since=since,
            level=(
                None if normalized_level == "all" else normalized_level
            ),
            search=q or None,
            limit=limit,
            offset=offset,
        )
        runtime.telemetry.storage_error = None
    except Exception as exc:
        runtime.telemetry.storage_error = public_error_message(exc)
        raise HTTPException(
            503,
            "persistent telemetry query failed",
        ) from exc
    settings = runtime.control.telemetry_settings()
    payload = {
        "period": period,
        "summary": summary,
        "requests": {
            "items": requests,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(requests) < total,
        },
        "events": {
            "items": events,
            "total": event_total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(events) < event_total,
        },
        "settings": {
            **settings,
            "content_effective": runtime.telemetry_content_enabled(),
            "ephemeral_suppresses_content": (
                str(runtime.control.behavior().get("privacy") or "keep")
                == "ephemeral"
            ),
        },
    }
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@router.patch("/api/control/telemetry/settings")
async def update_telemetry_settings(body: TelemetryPatch):
    updates = {
        key: value
        for key, value in body.model_dump().items()
        if value is not None
    }
    _persistent_telemetry_store()
    try:
        settings = runtime.control.update_telemetry(updates)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        if updates.get("store_content") is False:
            await runtime.telemetry.store_call_async("scrub_content")
        await runtime.telemetry.store_call_async(
            "prune",
            retention_days=int(settings["retention_days"]),
            max_requests=int(settings["max_requests"]),
        )
        runtime.telemetry.storage_error = None
    except Exception as exc:
        runtime.telemetry.storage_error = public_error_message(exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Настройка сохранена, но локальная очистка не завершена; "
                    "сервер повторит её автоматически"
                ),
                "settings": {
                    **settings,
                    "content_effective": runtime.telemetry_content_enabled(),
                },
                "cleanup_pending": True,
            },
        )
    runtime.telemetry.log(
        "INFO",
        "Telemetry",
        "Настройки локальной истории обновлены",
    )
    return {
        "ok": True,
        "settings": {
            **settings,
            "content_effective": runtime.telemetry_content_enabled(),
        },
    }


@router.delete("/api/control/telemetry")
async def clear_control_telemetry():
    _persistent_telemetry_store()
    runtime.telemetry.events.clear()
    runtime.telemetry.last = None
    try:
        await runtime.telemetry.store_call_async("clear_all")
    except Exception:
        raise HTTPException(503, "could not clear persistent telemetry") from None
    if runtime.telemetry.storage_error:
        raise HTTPException(503, "could not clear persistent telemetry")
    return {"ok": True}


@router.get("/api/control/telemetry/{request_id}")
async def control_telemetry_request(request_id: str):
    if not re.fullmatch(r"(?:chatcmpl-)?[a-zA-Z0-9_-]{6,80}", request_id):
        raise HTTPException(400, "invalid request id")
    _persistent_telemetry_store()
    try:
        item = await runtime.telemetry.store_call_async(
            "request_detail",
            request_id.removeprefix("chatcmpl-"),
        )
    except Exception as exc:
        runtime.telemetry.storage_error = public_error_message(exc)
        raise HTTPException(
            503,
            "persistent telemetry query failed",
        ) from exc
    if item is None:
        raise HTTPException(404, "telemetry request not found")
    return JSONResponse(
        content={"request": item},
        headers={"Cache-Control": "no-store"},
    )


@router.patch("/api/control/behavior")
async def update_behavior(body: BehaviorPatch):
    updates = {
        key: value
        for key, value in body.model_dump().items()
        if value is not None
    }
    try:
        behavior = runtime.control.update_behavior(updates)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    runtime.telemetry.log("INFO", "Settings", "Настройки поведения обновлены")
    return {
        "ok": True,
        "behavior": behavior,
        "persona_compilation": ControlConfig.persona_compilation_for(
            behavior,
        ),
    }


@router.post("/api/control/profiles")
async def create_profile(body: ProfileCreate):
    try:
        profile = runtime.control.create_profile(body.name, body.provider)
        runtime.bind_claude_profile_route(profile)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    runtime.telemetry.log(
        "INFO",
        "Profiles",
        f"Создан профиль «{profile['name']}»; требуется вход",
    )
    return {"ok": True, "profile": profile}


@router.post("/api/control/profiles/{profile_id}/login")
async def launch_profile_login(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    if runtime.is_active_claude_profile(profile):
        health = runtime.session.health_snapshot()
        browser = health.get("browser", {})
        if (
            isinstance(browser, dict)
            and browser.get("phase")
            in {"auth_required", "account_unknown", "account_changed"}
        ):
            if runtime.HEADLESS and runtime.session.headless:
                runtime.session.headless = False
                await runtime.session.sync_profiles(
                    runtime.runtime_profiles(),
                    profile_id,
                    restart=True,
                )
                await runtime.session.start_watchdog()
            else:
                await runtime.session.bring_to_front()
            runtime.control.update_profile(profile_id, {"status": "auth_pending"})
            runtime.telemetry.log(
                "INFO",
                "Profiles",
                f"Открыт Camoufox для повторного входа в профиль «{profile['name']}»",
            )
            return {
                "ok": True,
                "login": {
                    "profile_id": profile_id,
                    "status": "waiting_for_login",
                    "authenticated": False,
                    "browser_open": True,
                    "active_browser": True,
                    "ready": False,
                },
            }
        raise HTTPException(
            409,
            "the active Camoufox profile is already open and authenticated",
        )
    try:
        state = await runtime.enrollment.launch(
            profile_id,
            profile["path"],
            str(profile.get("provider") or CLAUDE_WEB_PROVIDER_ID),
            profile.get("proxy"),
        )
        runtime.control.update_profile(profile_id, {"status": "auth_pending"})
        if profile.get("provider") == runtime.GROK_WEB_PROVIDER_ID:
            log_message = (
                "Открыто диагностическое окно Playwright Chrome для проверки "
                f"доступа профиля «{profile['name']}»"
            )
        else:
            log_message = (
                f"Открыт Camoufox для входа в профиль «{profile['name']}»"
            )
        runtime.telemetry.log(
            "INFO",
            "Profiles",
            log_message,
        )
        return {"ok": True, "login": state}
    except Exception as exc:
        runtime.control.update_profile(profile_id, {"status": "error"})
        raise HTTPException(500, str(exc)) from exc


@router.get("/api/control/profiles/{profile_id}/login")
async def inspect_profile_login(profile_id: str):
    task = runtime.profile_login_tasks.get(profile_id)
    if task is None or task.done():
        task = asyncio.create_task(
            _inspect_profile_login_once(profile_id),
            name=f"profile-login-{profile_id}",
        )
        runtime.profile_login_tasks[profile_id] = task
    try:
        return await asyncio.shield(task)
    finally:
        if runtime.profile_login_tasks.get(profile_id) is task and task.done():
            runtime.profile_login_tasks.pop(profile_id, None)


async def _inspect_profile_login_once(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    provider_id = str(
        profile.get("provider") or CLAUDE_WEB_PROVIDER_ID
    )
    active_claude_profile = runtime.is_active_claude_profile(profile)
    login_running = await runtime.enrollment.is_running(profile_id)
    if (
        provider_id != CLAUDE_WEB_PROVIDER_ID
        and not login_running
        and profile.get("status") in {"ready", "protocol_unverified"}
    ):
        # A browser login is not sufficient to make an experimental provider
        # executable. Normalize stale/legacy "ready" rows to the fail-closed
        # state and never expose them as an activatable completion profile.
        runtime.control.update_profile(
            profile_id,
            {
                "status": "protocol_unverified",
                "enabled": False,
                "last_checked_at": time.time(),
            },
        )
        profile = runtime.control.profile(profile_id)
        public_profile = next(
            row
            for row in runtime.control.snapshot()["profiles"]
            if row["id"] == profile_id
        )
        return {
            "ok": True,
            "login": {
                "profile_id": profile_id,
                "provider": provider_id,
                "status": "protocol_unverified",
                "authenticated": bool(
                    profile.get("account", {}).get("authenticated")
                ),
                "browser_open": False,
                "ready": False,
                "account": profile.get("account", {}),
                "models": profile.get("models", []),
                "profile": public_profile,
                "protocol_error": (
                    "Browser login alone does not verify this provider's "
                    "completion stream or tool continuation."
                ),
            },
        }
    if (
        not active_claude_profile
        and not login_running
        and provider_id == CLAUDE_WEB_PROVIDER_ID
        and profile.get("status") == "ready"
        and profile.get("project_id")
    ):
        public_profile = next(
            row
            for row in runtime.control.snapshot()["profiles"]
            if row["id"] == profile_id
        )
        return {
            "ok": True,
            "login": {
                "profile_id": profile_id,
                "status": "ready",
                "authenticated": bool(
                    profile.get("account", {}).get("authenticated")
                ),
                "browser_open": False,
                "ready": True,
                "account": profile.get("account", {}),
                "models": profile.get("models", []),
                "profile": public_profile,
            },
        }
    if (
        not active_claude_profile
        and not login_running
        and profile.get("status") == "duplicate"
    ):
        return {
            "ok": True,
            "login": {
                "profile_id": profile_id,
                "status": "duplicate",
                "authenticated": True,
                "browser_open": False,
                "ready": False,
                "account": profile.get("account", {}),
            },
        }
    if (
        active_claude_profile
        and not login_running
    ):
        health = runtime.session.health_snapshot()
        account = health.get("account")
        models = health.get("models")
        if not isinstance(account, dict):
            account = {}
        if not isinstance(models, dict):
            models = {}
        authenticated = bool(account.get("authenticated"))
        result = {
            "profile_id": profile_id,
            "status": "ready" if authenticated else "waiting_for_login",
            "authenticated": authenticated,
            "browser_open": health.get("browser", {}).get("phase")
            not in {"stopped", "browser_dead"},
            "active_browser": True,
            "ready": authenticated,
            "account": account,
            "models": models.get("available", []),
        }
        if authenticated:
            identity_ok = runtime.persist_runtime_identity()
            if not identity_ok:
                result["status"] = "account_changed"
                result["ready"] = False
                result["account_error"] = (
                    "This browser is logged into another or duplicate "
                    "account. Add it as a separate profile."
                )
            elif runtime.HEADLESS and not runtime.session.headless:
                runtime.session.headless = True
                await runtime.session.sync_profiles(
                    runtime.runtime_profiles(),
                    profile_id,
                    restart=True,
                )
                await runtime.session.start_watchdog()
                result["browser_open"] = False
                result["active_browser"] = True
        else:
            runtime.control.update_profile(
                profile_id,
                {
                    "status": "auth_pending",
                    "last_checked_at": time.time(),
                },
            )
        return {"ok": True, "login": result}
    result = await runtime.enrollment.inspect(profile_id)
    if not result.get("authenticated"):
        observed_status = str(result.get("status") or "checking")
        if observed_status in {"provider_blocked", "access_denied"}:
            # These are terminal provider/WAF outcomes, not an unfinished
            # login. Preserve the exact status so callers can stop polling and
            # present the provider's diagnostic instead of waiting forever.
            persisted_status = observed_status
        elif observed_status == "error":
            persisted_status = "error"
        elif observed_status in {"browser_closed", "not_running"}:
            persisted_status = "auth_required"
        else:
            persisted_status = "auth_pending"
        runtime.control.update_profile(
            profile_id,
            {
                "status": persisted_status,
                "last_checked_at": time.time(),
            },
        )
        result["ready"] = False
        return {"ok": True, "login": result}
    identity = await runtime.enrollment.internal_identity(profile_id)
    account_uuid = str(identity.get("account_uuid") or "")
    if not account_uuid:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "auth_pending",
                "account": result.get("account"),
                "models": result.get("models", []),
                "last_checked_at": time.time(),
            },
        )
        result["status"] = "identity_unverified"
        result["ready"] = False
        provider_name = (
            "Claude"
            if provider_id == CLAUDE_WEB_PROVIDER_ID
            else "Grok"
        )
        result["account_error"] = (
            f"The web account is visible, but {provider_name} has not "
            "exposed a stable account identity in the verified browser "
            "state yet."
        )
        return {"ok": True, "login": result}
    fingerprint_source = (
        account_uuid
        if provider_id == CLAUDE_WEB_PROVIDER_ID
        else f"{provider_id}:{account_uuid}"
    )
    fingerprint = runtime.control.account_fingerprint(fingerprint_source)
    duplicate = runtime.control.claim_account_fingerprint(
        profile_id,
        fingerprint,
    )
    if duplicate is not None:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "duplicate",
                "enabled": False,
                "account": result.get("account"),
                "last_checked_at": time.time(),
            },
        )
        await runtime.enrollment.finish(profile_id)
        result["status"] = "duplicate"
        result["ready"] = False
        result["duplicate"] = {
            "profile_id": duplicate["id"],
            "name": duplicate["name"],
        }
        runtime.telemetry.log(
            "WARN",
            "Profiles",
            f"Профиль «{profile['name']}» использует уже добавленный аккаунт",
        )
        return {"ok": True, "login": result}
    if provider_id != CLAUDE_WEB_PROVIDER_ID:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "protocol_unverified",
                "enabled": False,
                "account_fingerprint": fingerprint,
                "account": result.get("account"),
                "models": result.get("models", []),
                "last_checked_at": time.time(),
            },
        )
        await runtime.enrollment.finish(profile_id)
        result["status"] = "protocol_unverified"
        result["ready"] = False
        result["protocol_error"] = (
            "The browser login was observed, but Grok completion streaming "
            "and tool continuation have not been verified. The profile stays "
            "disabled."
        )
        result["profile"] = next(
            row
            for row in runtime.control.snapshot()["profiles"]
            if row["id"] == profile_id
        )
        runtime.telemetry.log(
            "WARN",
            "Profiles",
            (
                f"Профиль «{profile['name']}» виден в браузере, но "
                "completion-транспорт Grok не проверен и остаётся выключен"
            ),
        )
        return {"ok": True, "login": result}
    project: dict[str, Any]
    try:
        instructions = (
            PROJECT_INSTRUCTIONS.read_text(encoding="utf-8")
            if provider_id == CLAUDE_WEB_PROVIDER_ID
            else ""
        )
        project = await runtime.enrollment.ensure_project(profile_id, instructions)
    except Exception as exc:
        runtime.control.update_profile(
            profile_id,
            {
                "status": "project_setup_error",
                "enabled": False,
                "account_fingerprint": fingerprint,
                "account": result.get("account"),
                "models": result.get("models", []),
                "last_checked_at": time.time(),
            },
        )
        result["status"] = "project_setup_error"
        result["ready"] = False
        result["project_error"] = public_error_message(exc)
        return {"ok": True, "login": result}
    identity = await runtime.enrollment.internal_identity(profile_id)
    runtime.control.update_profile(
        profile_id,
        {
            "status": "ready",
            "enabled": True,
            "account_fingerprint": fingerprint,
            "account": result.get("account"),
            "models": result.get("models", []),
            "organization_id": identity.get("organization_uuid"),
            "project_id": project["project_id"],
            "last_checked_at": time.time(),
        },
    )
    await runtime.enrollment.finish(profile_id)
    if provider_id == CLAUDE_WEB_PROVIDER_ID:
        await runtime.session.sync_profiles(
            runtime.runtime_profiles(),
            runtime.provider_profile_id(CLAUDE_WEB_PROVIDER_ID),
        )
    public_profile = next(
        row
        for row in runtime.control.snapshot()["profiles"]
        if row["id"] == profile_id
    )
    result["status"] = "ready"
    result["ready"] = True
    if project.get("required", True):
        result["project"] = {
            "name": project["name"],
            "project_id_suffix": project["project_id"][-8:],
            "organization_id_suffix": project.get(
                "organization_uuid_suffix"
            ),
        }
    else:
        result["project"] = {
            "required": False,
            "status": "not_required",
        }
    result["profile"] = public_profile
    runtime.telemetry.log(
        "INFO",
        "Profiles",
        f"Профиль «{profile['name']}» авторизован и готов",
    )
    return {"ok": True, "login": result}


@router.delete("/api/control/profiles/{profile_id}/login")
async def cancel_profile_login(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    finalize_task = runtime.profile_login_tasks.pop(profile_id, None)
    if finalize_task is not None and not finalize_task.done():
        finalize_task.cancel()
        await asyncio.gather(finalize_task, return_exceptions=True)
    if not await runtime.enrollment.is_running(profile_id):
        if (
            runtime.is_active_claude_profile(profile)
            and runtime.HEADLESS
            and not runtime.session.headless
        ):
            runtime.session.headless = True
            await runtime.session.sync_profiles(
                runtime.runtime_profiles(),
                profile_id,
                restart=True,
            )
            await runtime.session.start_watchdog()
            runtime.control.update_profile(profile_id, {"status": "auth_required"})
            return {"ok": True, "cancelled": True}
        return {"ok": True, "cancelled": False}
    await runtime.enrollment.finish(profile_id)
    runtime.control.update_profile(profile_id, {"status": "auth_required"})
    return {"ok": True, "cancelled": True}


@router.post("/api/control/profiles/{profile_id}/activate")
async def activate_profile(profile_id: str):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    if (
        not profile.get("enabled", True)
        or profile.get("status") not in {"ready", "limited"}
    ):
        raise HTTPException(409, "profile is not ready for activation")
    provider_id = str(
        profile.get("provider") or CLAUDE_WEB_PROVIDER_ID
    )
    if provider_id != CLAUDE_WEB_PROVIDER_ID:
        raise HTTPException(
            409,
            "Grok Web cannot be activated until its authenticated Chrome "
            "stream protocol has been verified.",
        )
    runtime.bind_claude_profile_route(profile)
    limited_until = profile.get("limited_until")
    if isinstance(limited_until, (int, float)) and limited_until > time.time():
        raise HTTPException(
            409,
            "profile is temporarily limited and cannot be activated yet",
        )
    native_state = runtime.session.health_snapshot().get("native", {})
    if isinstance(native_state, dict) and native_state.get("active"):
        raise HTTPException(
            409,
            "cannot switch profile while Claude is waiting for tool_result",
        )
    old_profile_id = runtime.control.snapshot()["active_profile"]
    runtime_profiles = runtime.runtime_profiles()
    try:
        await runtime.session.sync_profiles(
            runtime_profiles,
            profile_id,
            restart=True,
        )
        if not runtime.session.health_snapshot().get("ok"):
            raise RuntimeError(
                "target profile requires authentication or account verification"
            )
        runtime.control.set_active_profile(profile_id)
        if not runtime.persist_runtime_identity():
            raise RuntimeError(
                "target profile is logged into a different or duplicate account"
            )
    except Exception as exc:
        if old_profile_id != profile_id:
            try:
                await runtime.session.sync_profiles(
                    runtime_profiles,
                    old_profile_id,
                    restart=True,
                )
                runtime.control.set_active_profile(old_profile_id)
            except Exception as rollback_exc:
                runtime.telemetry.log(
                    "ERROR",
                    "Profiles",
                    f"Не удалось вернуть предыдущий профиль: {rollback_exc}",
                )
        raise HTTPException(503, str(exc)) from exc
    runtime.telemetry.log(
        "INFO",
        "Profiles",
        f"Активирован профиль «{profile['name']}»",
    )
    return {"ok": True, "health": runtime.session.health_snapshot()}


@router.post("/api/control/profiles/{profile_id}/model")
async def select_profile_model(profile_id: str, body: ModelSelect):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    available = {
        str(item.get("id"))
        for item in profile.get("models", [])
        if (
            isinstance(item, dict)
            and item.get("available") is True
            and item.get("access_status") == "available"
        )
    }
    provider_alias = (
        "grok-web"
        if profile.get("provider") == runtime.GROK_WEB_PROVIDER_ID
        else "claude-web"
    )
    if body.model not in {"auto", provider_alias}:
        if not profile.get("models"):
            raise HTTPException(
                409,
                "account model catalog has not been discovered yet",
            )
        if body.model not in available:
            raise HTTPException(
                400,
                "model is not available to this authenticated account",
            )
    updated = runtime.control.update_profile(profile_id, {"model": body.model})
    if profile_id == runtime.session.current_profile_id():
        await runtime.session.sync_profiles(
            runtime.runtime_profiles(),
            profile_id,
            restart=False,
        )
    runtime.telemetry.log(
        "INFO",
        "Models",
        f"Для профиля «{profile['name']}» выбрана модель {body.model}",
    )
    return {"ok": True, "model": updated["model"]}


@router.put("/api/control/profiles/{profile_id}/proxy")
async def update_profile_proxy(profile_id: str, body: ProxyPatch):
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    try:
        updated = runtime.control.update_profile(
            profile_id,
            {"proxy": body.updates()},
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    restarted = False
    if profile_id == runtime.session.current_profile_id():
        # The running browser holds the old exit address, so an enabled proxy
        # that is not applied now would keep sending this machine's own IP.
        native_state = runtime.session.health_snapshot().get("native", {})
        if isinstance(native_state, dict) and native_state.get("active"):
            raise HTTPException(
                409,
                "cannot restart the profile while Claude is waiting for "
                "tool_result; the proxy was saved but is not applied yet",
            )
        try:
            await runtime.session.sync_profiles(
                runtime.runtime_profiles(),
                profile_id,
                restart=True,
            )
            restarted = True
        except Exception as exc:
            raise HTTPException(503, public_error_message(exc)) from exc

    proxy_view = proxy_settings.public(updated.get("proxy"))
    runtime.telemetry.log(
        "INFO",
        "Profiles",
        f"Прокси профиля «{profile['name']}»: " + (
            f"включён ({proxy_view['server']})"
            if proxy_view["enabled"]
            else "выключен"
        ),
    )
    return {"ok": True, "proxy": proxy_view, "restarted": restarted}


@router.post("/api/control/profiles/{profile_id}/proxy/test")
async def test_profile_proxy(profile_id: str, body: ProxyPatch | None = None):
    """Connect through the proxy and report the exit address it hands out."""
    try:
        profile = runtime.control.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, "profile not found") from exc
    stored = profile.get("proxy") or {}
    candidate = dict(stored)
    if body is not None and body.server.strip():
        candidate = {**stored, **body.updates()}
    try:
        candidate = proxy_settings.normalize(candidate, stored)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    result = await proxy_settings.check(candidate)
    if result.get("error"):
        result["error"] = sanitize_public_text(str(result["error"]))
    return {"ok": True, "result": result}


@router.delete("/api/control/events")
async def clear_control_events():
    _persistent_telemetry_store()
    runtime.telemetry.events.clear()
    try:
        await runtime.telemetry.store_call_async("clear_events")
    except Exception:
        raise HTTPException(503, "could not clear persistent event log") from None
    if runtime.telemetry.storage_error:
        raise HTTPException(503, "could not clear persistent event log")
    return {"ok": True}


def _bridge_port() -> int:
    return int(os.getenv("PORT", "8765"))


@router.get("/api/control/clients")
async def control_clients():
    """What the coding clients on this machine are pointed at."""
    return clients.snapshot(_bridge_port())


@router.post("/api/control/clients/{client_id}/configure")
async def configure_client(client_id: str):
    definition = clients.find_definition(client_id)
    if definition is None:
        raise HTTPException(404, "unknown client")
    try:
        result = clients.configure(definition, _bridge_port())
    except OSError as exc:
        raise HTTPException(
            500,
            f"не удалось записать настройки клиента: {exc.strerror or exc}",
        ) from exc
    runtime.telemetry.log(
        "INFO",
        "Clients",
        f"{definition.name} направлен на мост",
    )
    return result


@router.post("/api/control/clients/{client_id}/install")
async def install_client(client_id: str):
    definition = clients.find_definition(client_id)
    if definition is None:
        raise HTTPException(404, "unknown client")
    command, reason = clients.install_plan(definition)
    if command is None:
        raise HTTPException(400, reason or "installation is not supported")
    state = runtime.client_installs.get(client_id)
    if state and state.get("status") == "running":
        return state
    state = {
        "client": client_id,
        "status": "running",
        "command": " ".join(command),
        "started_at": time.time(),
        "output": "",
    }
    runtime.client_installs[client_id] = state
    # The state dict is serialised into responses, so the task itself is kept
    # beside it rather than inside it.
    runtime.client_install_tasks[client_id] = asyncio.create_task(
        _run_install(command, state),
        name=f"install-{client_id}",
    )
    runtime.telemetry.log(
        "INFO",
        "Clients",
        f"Установка {definition.name}: {state['command']}",
    )
    return state


@router.get("/api/control/clients/{client_id}/install")
async def install_client_status(client_id: str):
    state = runtime.client_installs.get(client_id)
    if state is None:
        raise HTTPException(404, "no installation has been started")
    return state


async def _run_install(
    command: tuple[str, ...],
    state: dict[str, Any],
) -> None:
    """Run the client's installer, keeping only a bounded tail of its output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, FileNotFoundError) as exc:
        state.update(
            status="error",
            finished_at=time.time(),
            output=sanitize_public_text(
                f"не удалось запустить установщик: {exc}"
            ),
        )
        return
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=900,
        )
    except asyncio.TimeoutError:
        process.kill()
        state.update(
            status="error",
            finished_at=time.time(),
            output="установка не завершилась за 15 минут",
        )
        return
    text = (stdout or b"").decode("utf-8", "replace")
    state.update(
        status="completed" if process.returncode == 0 else "error",
        finished_at=time.time(),
        return_code=process.returncode,
        output=sanitize_public_text(text[-4000:], limit=4000),
    )
