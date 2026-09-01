"""Account identity and the model catalogue the account may select."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from claude_web_api.session.errors import (
    ClaudeAccountIdentityError,
    ClaudeBrowserUnavailableError,
)
from claude_web_api.session.patterns import (
    MODEL_SELECTOR_TRANSIENT_REASONS,
    UUID_TEXT_RE,
)
from claude_web_api.session.scripts import ACCOUNT_AND_SELECTOR_SCRIPT
from claude_web_api.session.state import SessionState


def _string_list(value: Any) -> list[Any]:
    """Read a field that upstream may omit or send as a non-list."""
    return value if isinstance(value, list) else []


class AccountIdentityMixin(SessionState):
    """Account identity and the model catalogue the account may select."""

    def observed_models(self) -> list[str]:
        return sorted(self._observed_models)
    def account_uuid_for_internal_use(self) -> str | None:
        """Return the UUID only for local salted duplicate detection."""
        return self._account_uuid
    def organization_uuid_for_internal_use(self) -> str | None:
        """Return the verified Project organization for local profile config."""
        return self._organization_uuid
    def selectable_models(self) -> list[dict[str, Any]]:
        return [
            dict(model)
            for model in self._available_models
            if (
                model.get("available") is True
                and model.get("access_status") == "available"
            )
        ]
    def selected_model_for_runtime(self) -> str | None:
        model = str(self._model_selector_state.get("model") or "").strip()
        return model or None
    @staticmethod
    def _mask_email(value: str | None) -> str | None:
        if not value or "@" not in value:
            return None
        local, domain = value.rsplit("@", 1)
        visible = local[:2] if len(local) > 1 else local[:1]
        return f"{visible}***@{domain}"
    @staticmethod
    def _mask_identifiers(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return UUID_TEXT_RE.sub(
            lambda match: f"…{match.group(0)[-8:]}",
            value,
        )
    @staticmethod
    def _find_identity_record(
        value: Any,
        account_uuid: str | None,
    ) -> dict[str, Any] | None:
        if isinstance(value, dict):
            candidate_uuid = str(
                value.get("uuid")
                or value.get("account_uuid")
                or value.get("id")
                or ""
            )
            if (
                (account_uuid and candidate_uuid == account_uuid)
                or (
                    not account_uuid
                    and any(
                        key in value
                        for key in (
                            "full_name",
                            "display_name",
                            "email",
                            "email_address",
                        )
                    )
                )
            ):
                return value
            for child in value.values():
                found = AccountIdentityMixin._find_identity_record(child, account_uuid)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = AccountIdentityMixin._find_identity_record(child, account_uuid)
                if found is not None:
                    return found
        return None
    @staticmethod
    def _find_named_value(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = AccountIdentityMixin._find_named_value(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = AccountIdentityMixin._find_named_value(child, key)
                if found is not None:
                    return found
        return None
    @staticmethod
    def _normalize_disabled_reason(value: Any) -> str | dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key in ("type", "required_plan", "message", "title"):
                child = value.get(key)
                if child is None or isinstance(child, (str, bool, int, float)):
                    if child is not None:
                        safe[key] = child
            return safe or "account_unavailable"
        return str(value)
    async def _load_account_identity(self) -> bool:
        """Read identity from the authenticated page without exposing cookies."""
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(
                    ACCOUNT_AND_SELECTOR_SCRIPT,
                    {
                        "organizationUuid": self.current_profile_spec().get(
                            "organization_id"
                        ),
                        "selectorMaxAgeMs": (
                            self._model_selector_cache_max_age_ms
                        ),
                        "selectorWaitMs": self._model_selector_wait_ms,
                        "selectorTransientReasons": list(
                            MODEL_SELECTOR_TRANSIENT_REASONS
                        ),
                    },
                ),
                timeout=max(
                    10,
                    self._model_selector_wait_ms / 1_000 + 15,
                ),
            )
        except Exception:
            self._clear_account_identity()
            return False
        if (
            not isinstance(result, dict)
            or result.get("status") != 200
            or not isinstance(result.get("profile"), (dict, list))
        ):
            self._clear_account_identity()
            return False
        hinted = str(result.get("hinted") or "")
        confirmed = str(result.get("confirmed") or "")
        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$",
            re.I,
        )
        if hinted and confirmed and hinted != confirmed:
            self._last_error = "Claude account identity hints disagree"
            self._clear_account_identity()
            return False
        account_uuid = confirmed or hinted
        if not uuid_re.fullmatch(account_uuid):
            account_uuid = ""
        record = self._find_identity_record(
            result.get("profile"),
            account_uuid or None,
        )
        if record is None:
            self._clear_account_identity()
            return False
        if not account_uuid:
            candidate_uuid = str(
                record.get("uuid")
                or record.get("account_uuid")
                or record.get("id")
                or ""
            )
            if uuid_re.fullmatch(candidate_uuid):
                account_uuid = candidate_uuid
        if not account_uuid:
            self._clear_account_identity()
            return False
        account_name = str(
            record.get("full_name")
            or record.get("display_name")
            or record.get("name")
            or ""
        ) or None
        account_email_masked = self._mask_email(
            str(
                record.get("email_address")
                or record.get("email")
                or ""
            )
            or None
        )
        available_models: list[dict[str, Any]] = []
        model_selector_state: dict[str, Any] = {}
        account_payload = result.get("profile")
        selector_payload = result.get("selector")
        selector_config: Any = None
        selector_state: Any = None
        selector_diagnostics: dict[str, Any] = {
            "verified": False,
            "source": None,
            "reason": "selector_result_missing",
            "cache": {},
        }
        if isinstance(selector_payload, dict):
            selector_diagnostics["source"] = selector_payload.get("source")
            selector_diagnostics["reason"] = selector_payload.get("reason")
            cache = selector_payload.get("cache")
            identity = selector_payload.get("identity")
            cache_age = (
                cache.get("age_ms")
                if isinstance(cache, dict)
                else None
            )
            identity_verified = bool(
                isinstance(identity, dict)
                and identity.get("account_match") is True
                and identity.get("organization_query_match") is True
                and identity.get("membership_match") is True
            )
            cache_fresh = bool(
                isinstance(cache_age, (int, float))
                and 0 <= cache_age
                <= self._model_selector_cache_max_age_ms
            )
            selector_verified = bool(
                selector_payload.get("ok") is True
                and selector_payload.get("source")
                == "react_query_effective_selector"
                and identity_verified
                and cache_fresh
                and isinstance(selector_payload.get("config"), dict)
            )
            selector_diagnostics = {
                "verified": selector_verified,
                "source": selector_payload.get("source"),
                "reason": (
                    None
                    if selector_verified
                    else str(
                        selector_payload.get("reason")
                        or (
                            "selector_identity_unverified"
                            if not identity_verified
                            else (
                                "selector_cache_stale"
                                if not cache_fresh
                                else "selector_config_invalid"
                            )
                        )
                    )
                ),
                "cache": (
                    {
                        key: cache.get(key)
                        for key in (
                            "persisted_at",
                            "data_updated_at",
                            "age_ms",
                            "data_update_count",
                            "status",
                            "fetch_status",
                            "value_type",
                            "string_length",
                            "exact_key_present",
                            "key_count",
                            "shape_candidates",
                        )
                        if key in cache
                    }
                    if isinstance(cache, dict)
                    else {}
                ),
            }
            if selector_verified:
                selector_config = selector_payload.get("config")
                selector_state = selector_payload.get("state")
        chat_config: dict[str, Any] | None = None
        if isinstance(selector_config, dict):
            nested_chat = selector_config.get("chat")
            if isinstance(nested_chat, dict):
                chat_config = nested_chat
            elif (
                selector_config.get("id") == "chat"
                or isinstance(selector_config.get("models"), list)
            ):
                chat_config = selector_config
        elif isinstance(selector_config, list):
            chat_config = next(
                (
                    item
                    for item in selector_config
                    if isinstance(item, dict) and item.get("id") == "chat"
                ),
                None,
            )
        if isinstance(chat_config, dict):
            rows = chat_config.get("models")
            if isinstance(rows, list):
                for raw_model in rows:
                    if not isinstance(raw_model, dict):
                        continue
                    model_id = str(raw_model.get("id") or "").strip()
                    if not model_id:
                        continue
                    section = str(raw_model.get("section") or "")
                    catalog_available = bool(
                        raw_model.get("available", True) is not False
                        and not raw_model.get("inactive")
                        and section
                        not in {"deprecated", "inactive", "legacy"}
                    )
                    disabled_reason = self._normalize_disabled_reason(
                        raw_model.get("disabled_reason")
                    )
                    if not catalog_available and disabled_reason is None:
                        disabled_reason = (
                            "account_unavailable"
                            if raw_model.get("available") is False
                            else "inactive"
                        )
                    available = bool(
                        catalog_available and disabled_reason is None
                    )
                    model = {
                        "id": model_id,
                        "name": str(
                            raw_model.get("name")
                            or raw_model.get("label")
                            or model_id
                        ),
                        "section": section or None,
                        "available": available,
                        "catalog_available": catalog_available,
                        "access_status": (
                            "unavailable"
                            if not available
                            else "available"
                        ),
                        "source": "account_selector",
                        "disabled_reason": (
                            disabled_reason
                        ),
                        "capabilities": (
                            raw_model.get("capabilities")
                            if isinstance(
                                raw_model.get("capabilities"),
                                (dict, list),
                            )
                            else None
                        ),
                        "thinking": (
                            raw_model.get("thinking")
                            if isinstance(raw_model.get("thinking"), dict)
                            else None
                        ),
                        "supports_fast_mode": bool(
                            raw_model.get("supports_fast_mode")
                        ),
                    }
                    available_models.append(model)
        if not available_models:
            bootstrap_rows = self._find_named_value(
                account_payload,
                "claude_ai_bootstrap_models_config",
            )
            if isinstance(bootstrap_rows, list):
                for raw_model in bootstrap_rows:
                    if not isinstance(raw_model, dict):
                        continue
                    model_id = str(
                        raw_model.get("model")
                        or raw_model.get("id")
                        or ""
                    ).strip()
                    if not model_id:
                        continue
                    inactive = bool(raw_model.get("inactive"))
                    disabled_reason = self._normalize_disabled_reason(
                        raw_model.get("disabled_reason")
                    )
                    # The bootstrap list is a product catalog, not proof that
                    # the active account is entitled to invoke a model.
                    catalog_available = False
                    if inactive and disabled_reason is None:
                        disabled_reason = "inactive"
                    elif disabled_reason is None:
                        disabled_reason = (
                            "account_unavailable"
                            if raw_model.get("available") is False
                            else "catalog_only"
                        )
                    available = False
                    thinking_modes = raw_model.get("thinking_modes")
                    model = {
                        "id": model_id,
                        "name": str(
                            raw_model.get("name")
                            or raw_model.get("label")
                            or model_id
                        ),
                        "section": "inactive" if inactive else None,
                        "available": available,
                        "catalog_available": catalog_available,
                        "access_status": (
                            "unavailable"
                            if inactive
                            or raw_model.get("available") is False
                            or raw_model.get("disabled_reason") is not None
                            else "unverified"
                        ),
                        "source": "bootstrap_catalog",
                        "disabled_reason": (
                            disabled_reason
                        ),
                        "capabilities": (
                            raw_model.get("capabilities")
                            if isinstance(
                                raw_model.get("capabilities"),
                                (dict, list),
                            )
                            else None
                        ),
                        "thinking": (
                            {"modes": thinking_modes}
                            if isinstance(thinking_modes, list)
                            else None
                        ),
                        "supports_fast_mode": bool(
                            raw_model.get("supports_fast_mode")
                            or "instant" in _string_list(
                                raw_model.get("paprika_modes")
                            )
                        ),
                    }
                    available_models.append(model)
        state: dict[str, Any] | None = None
        if isinstance(selector_state, dict):
            nested_chat = selector_state.get("chat")
            if isinstance(nested_chat, dict):
                state = nested_chat
            elif selector_state.get("id") == "chat":
                state = selector_state
        elif isinstance(selector_state, list):
            state = next(
                (
                    item
                    for item in selector_state
                    if isinstance(item, dict) and item.get("id") == "chat"
                ),
                None,
            )
        if isinstance(state, dict):
            model_selector_state = {
                key: state.get(key)
                for key in (
                    "model",
                    "thinking",
                    "thinking_by_model",
                    "preset_key",
                    "org_enforced_default_model",
                    "selection_source",
                )
                if key in state
            }
        # Commit atomically only after the account record has been fully
        # verified. Partial `/api/account` data must never look authenticated.
        organization_hint = str(
            self.current_profile_spec().get("organization_id") or ""
        ).strip()
        if not uuid_re.fullmatch(organization_hint):
            organization_hint = ""
        self._account_uuid = account_uuid
        self._account_name = account_name
        self._account_email_masked = account_email_masked
        self._organization_uuid = organization_hint or None
        self._available_models = available_models
        self._model_selector_state = model_selector_state
        self._model_selector_diagnostics = selector_diagnostics
        self._observed_models.update(
            str(model["id"]) for model in available_models
        )
        return True
    def _clear_account_identity(self) -> None:
        self._account_uuid = None
        self._account_name = None
        self._account_email_masked = None
        self._organization_uuid = None
        self._available_models = []
        self._model_selector_state = {}
        self._model_selector_diagnostics = {}
    async def _verify_account_unchanged_unlocked(self) -> None:
        profile_id = self.current_profile_id()
        expected_uuid = (
            self._profile_account_uuids.get(profile_id)
            or self._account_uuid
        )
        if not expected_uuid:
            self.ready = False
            self._set_phase("account_unknown")
            raise ClaudeBrowserUnavailableError(
                "Claude account identity is not verified"
            )
        identity_ready = await self._load_account_identity()
        if not identity_ready:
            self.ready = False
            self._last_error = (
                "Claude /api/account identity could not be revalidated"
            )
            self._set_phase("account_unknown")
            raise ClaudeBrowserUnavailableError(self._last_error)
        if self._account_uuid != expected_uuid:
            self.ready = False
            self._last_error = (
                "The active Camoufox profile changed Claude accounts; "
                "the IDE request was blocked before submission"
            )
            self._set_phase("account_changed")
            raise ClaudeAccountIdentityError(self._last_error)
        self._profile_account_uuids.setdefault(profile_id, expected_uuid)
