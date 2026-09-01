"""Persistent, secret-free configuration for the local control center."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from claude_web_api.control import proxy as proxy_settings
from claude_web_api.paths import (
    CONTROL_CONFIG_FILE as CONFIG_PATH,
)
from claude_web_api.paths import (
    LEGACY_PROFILE_DIR,
    LEGACY_PROJECT_FILE,
    migrate_legacy_state,
)

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

CONFIG_VERSION = 3
DEFAULT_PROFILE_PROVIDER = "claude_web"
SUPPORTED_PROFILE_PROVIDERS = (
    "claude_web",
    "grok_web",
)
_SUPPORTED_PROFILE_PROVIDER_IDS = frozenset(SUPPORTED_PROFILE_PROVIDERS)

PERSONA_PROMPTS = {
    "default": "",
    "programmer": (
        "Use the working style of a senior software-engineering collaborator "
        "inside the IDE. "
        "Be concrete, inspect the real workspace with attached host tools when "
        "needed, preserve existing work, implement requested changes, and verify "
        "them. Prefer concise technical explanations over generic advice."
    ),
}

ROLEPLAY_MODIFIER_PROMPT = (
    "The user also selected a roleplay style. Continue a fictional character "
    "and scene established by the current card or dialogue with consistent "
    "voice and relationships. Reply naturally in first person. Keep real tool "
    "results, files, capabilities, and external facts accurate."
)

MATURE_TONE_MODIFIER_PROMPT = (
    "The user also selected a candid, grown-up conversational tone. Use "
    "direct, emotionally natural language rather than childish or overly "
    "formal phrasing. This modifier changes tone only; it does not add a "
    "topic, relationship, character, or scene. Follow the subject the user "
    "actually raises and keep factual and tool-related claims accurate."
)

_AI_TERM_RE = re.compile(
    r"(?:"
    r"\bии\b"
    r"|\bai\b"
    r"|\bбот(?:ом|а|у|ы|ов)?\b"
    r"|\bробот(?:ом|а|у|ы|ов)?\b"
    r"|\bнейросет(?:ь|ью|и|ей|ям|ях|ями)\b"
    r"|\bbots?\b"
    r"|\brobots?\b"
    r"|\bneural\s+networks?\b"
    r")",
    re.IGNORECASE,
)

_HUMAN_IDENTITY_RE = re.compile(
    r"(?:"
    r"(?:"
    r"\b(?:реальн\w*|жив\w+)\s+человек"
    r"(?:ом|а|у|е|и|ов|ам|ами|ах)?\b"
    r"|\bне\s+(?:явля\w+\s+)?"
    r"(?:ии|ai|бот(?:ом|а|у|ы|ов)?|робот(?:ом|а|у|ы|ов)?)\b"
    r"|\breal\s+(?:human|person)\b"
    r"|\bnot\s+(?:an?\s+)?(?:ai|bot|robot)\b"
    r")(?=\s*(?:$|[,;.!?]|(?:и|а|но|зато|and|but)\b|[—–-]))"
    r"|\bпритворяйся\b.*\bчеловек"
    r"(?:ом|а|у|е|и|ов|ам|ами|ах)?\b"
    r"|\bpretend\s+to\s+be\s+(?:an?\s+)?(?:human|person)\b"
    r")",
    re.IGNORECASE,
)

_CONCEALMENT_VERB_RE = re.compile(
    r"(?:"
    r"\b(?:не|никогда\s+не)\s*"
    r"(?:упомина\w*|говор\w*|признава\w*|раскрыва\w*|"
    r"скрыва\w*|рассказыва\w*)"
    r"|\b(?:never|do\s+not|don't|does\s+not|doesn't)\s+"
    r"(?:mention|say|admit|reveal|disclose|tell)\b"
    r")",
    re.IGNORECASE,
)

_QUOTED_SPAN_RE = re.compile(
    r"(?:«[^»]*»|“[^”]*”|\"[^\"]*\")",
)

_CLAUSE_SPLIT_RE = re.compile(r"[,;.!?]+")
_CONJUNCTION_SPLIT_RE = re.compile(
    r"(?:\s+(?:и|а|но|зато|and|but)\s+|\s+[—–-]\s+)",
    re.IGNORECASE,
)
_AI_CONTINUATION_RE = re.compile(
    r"^(?:что|будто|как\s+будто|that|whether)\b",
    re.IGNORECASE,
)
_CONCEALMENT_TO_AI_BRIDGE_RE = re.compile(
    r"[\s:—–-]*(?:(?:"
    r"о|об|про|что|то|она|он|оно|это|ты|я|мы|вы|"
    r"является|являешься|являюсь|являются|как|себя|"
    r"about|that|she|he|it|you|i|we|they|am|is|are|"
    r"as|an|a|the|anyone|anybody|you['’]?re"
    r")[\s:—–-]*){0,8}",
    re.IGNORECASE,
)
_AI_TO_CONCEALMENT_BRIDGE_RE = re.compile(
    r"\s*(?:(?:она|он|персонаж|she|he|character)\s*)?"
    r"(?:(?:и|или)\s+(?:прочее|подобное|тому\s+подобное)"
    r"|(?:and|or)\s+(?:the\s+like|similar\s+things))?\s*",
    re.IGNORECASE,
)

_ANAPHORIC_AI_PREFERENCE_RE = re.compile(
    r"^(?:оно|это|такие\s+темы)\b.*\b(?:не\s+нрав\w*|не\s+любит\w*)",
    re.IGNORECASE,
)

_FICTIONAL_HUMAN_TRAIT = (
    "In this fictional scene, the character is an ordinary human."
)

_AI_TOPIC_PREFERENCE = (
    "In this scene, the character dislikes conversations about AI and "
    "prefers other subjects."
)


def _mask_quoted_spans(value: str) -> tuple[str, list[str]]:
    quoted: list[str] = []

    def replace(match: re.Match[str]) -> str:
        quoted.append(match.group(0))
        return f"\ue000{len(quoted) - 1}\ue001"

    return _QUOTED_SPAN_RE.sub(replace, value), quoted


def _restore_quoted_spans(value: str, quoted: list[str]) -> str:
    restored = value
    for index, original in enumerate(quoted):
        restored = restored.replace(f"\ue000{index}\ue001", original)
    return restored


def _has_direct_ai_concealment(value: str) -> bool:
    ai_terms = list(_AI_TERM_RE.finditer(value))
    concealment_verbs = list(_CONCEALMENT_VERB_RE.finditer(value))
    for verb in concealment_verbs:
        for ai_term in ai_terms:
            if verb.end() <= ai_term.start():
                bridge = value[verb.end():ai_term.start()]
                if _CONCEALMENT_TO_AI_BRIDGE_RE.fullmatch(bridge):
                    return True
            elif ai_term.end() <= verb.start():
                bridge = value[ai_term.end():verb.start()]
                if _AI_TO_CONCEALMENT_BRIDGE_RE.fullmatch(bridge):
                    return True
    return False


def _change(
    code: str,
    source: str,
    effective: str,
) -> dict[str, str]:
    return {
        "code": code,
        "source": source.strip(),
        "effective": effective,
    }


def compile_custom_persona_details(value: Any) -> dict[str, Any]:
    """Return raw/effective card text plus transparent narrow rewrites."""
    source = str(value or "").strip()
    if not source:
        return {
            "raw": "",
            "effective": "",
            "changed": False,
            "changes": [],
        }
    compiled: list[str] = []
    changes: list[dict[str, str]] = []
    human_trait_added = False
    ai_preference_added = False
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            if compiled and compiled[-1]:
                compiled.append("")
            continue

        masked_line, quoted = _mask_quoted_spans(stripped)
        clauses = [
            clause.strip()
            for clause in _CLAUSE_SPLIT_RE.split(masked_line)
            if clause.strip()
        ]
        adjacent_concealment: set[int] = set()
        for index in range(len(clauses) - 1):
            current = clauses[index]
            following = clauses[index + 1]
            if (
                _CONCEALMENT_VERB_RE.search(current)
                and _AI_TERM_RE.search(following)
                and _AI_CONTINUATION_RE.search(following)
            ):
                adjacent_concealment.update({index, index + 1})

        kept_clauses: list[str] = []
        line_had_human_identity = False
        line_had_ai_concealment = False
        previous_ai_concealment = False
        line_changed = False
        for index, candidate in enumerate(clauses or [masked_line]):
            restored_candidate = _restore_quoted_spans(candidate, quoted)
            if index in adjacent_concealment:
                line_changed = True
                line_had_ai_concealment = True
                previous_ai_concealment = True
                changes.append(
                    _change(
                        "ai_concealment_reframed",
                        restored_candidate,
                        _AI_TOPIC_PREFERENCE,
                    )
                )
                continue

            if (
                previous_ai_concealment
                and _ANAPHORIC_AI_PREFERENCE_RE.search(candidate)
            ):
                line_changed = True
                line_had_ai_concealment = True
                changes.append(
                    _change(
                        "ai_concealment_reframed",
                        restored_candidate,
                        _AI_TOPIC_PREFERENCE,
                    )
                )
                continue

            candidate_has_identity = bool(
                _HUMAN_IDENTITY_RE.search(candidate)
            )
            candidate_has_concealment = _has_direct_ai_concealment(
                candidate
            )
            if not candidate_has_identity and not candidate_has_concealment:
                kept_clauses.append(candidate)
                previous_ai_concealment = False
                continue

            parts = [
                part.strip()
                for part in _CONJUNCTION_SPLIT_RE.split(candidate)
                if part.strip()
            ]
            directly_matched_part = False
            kept_parts: list[str] = []
            for part in parts:
                restored_part = _restore_quoted_spans(part, quoted)
                if _HUMAN_IDENTITY_RE.search(part):
                    directly_matched_part = True
                    line_changed = True
                    line_had_human_identity = True
                    changes.append(
                        _change(
                            "literal_identity_reframed",
                            restored_part,
                            _FICTIONAL_HUMAN_TRAIT,
                        )
                    )
                    continue
                if _has_direct_ai_concealment(part):
                    directly_matched_part = True
                    line_changed = True
                    line_had_ai_concealment = True
                    previous_ai_concealment = True
                    changes.append(
                        _change(
                            "ai_concealment_reframed",
                            restored_part,
                            _AI_TOPIC_PREFERENCE,
                        )
                    )
                    continue
                kept_parts.append(part)

            if candidate_has_concealment and not directly_matched_part:
                line_changed = True
                line_had_ai_concealment = True
                previous_ai_concealment = True
                changes.append(
                    _change(
                        "ai_concealment_reframed",
                        restored_candidate,
                        _AI_TOPIC_PREFERENCE,
                    )
                )
                kept_parts = []
            elif candidate_has_identity and not directly_matched_part:
                line_changed = True
                line_had_human_identity = True
                changes.append(
                    _change(
                        "literal_identity_reframed",
                        restored_candidate,
                        _FICTIONAL_HUMAN_TRAIT,
                    )
                )
                kept_parts = []
            if kept_parts:
                kept_clauses.extend(kept_parts)

        if not line_changed:
            compiled.append(stripped)
        elif kept_clauses:
            kept_line = ", ".join(
                _restore_quoted_spans(item, quoted).strip()
                for item in kept_clauses
            ).strip(" ,;:")
            if kept_line:
                if stripped[-1:] in ".!?" and kept_line[-1:] not in ".!?":
                    kept_line += stripped[-1]
                compiled.append(kept_line)
        if line_had_human_identity and not human_trait_added:
            compiled.append(_FICTIONAL_HUMAN_TRAIT)
            human_trait_added = True
        if line_had_ai_concealment and not ai_preference_added:
            compiled.append(_AI_TOPIC_PREFERENCE)
            ai_preference_added = True
    effective = source if not changes else "\n".join(compiled).strip()
    return {
        "raw": source,
        "effective": effective,
        "changed": effective != source,
        "changes": changes,
    }


def compile_custom_persona(value: Any) -> str:
    """Compile a raw custom card into its provider-facing scene wording."""
    return str(compile_custom_persona_details(value)["effective"])


def _validate_profile_provider(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(
            "provider must be one of: "
            + ", ".join(SUPPORTED_PROFILE_PROVIDERS)
        )
    provider = value.strip().lower()
    if provider not in _SUPPORTED_PROFILE_PROVIDER_IDS:
        raise ValueError(
            "provider must be one of: "
            + ", ".join(SUPPORTED_PROFILE_PROVIDERS)
        )
    return provider


def _legacy_project_id() -> str | None:
    try:
        payload = json.loads(LEGACY_PROJECT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = str(payload.get("project_id", "") or "").strip()
    return value or None


def _default_profile_rows() -> list[dict[str, Any]]:
    configured = os.getenv("CLAUDE_PROFILE_DIRS", "")
    paths = (
        [Path(item).expanduser() for item in configured.split(os.pathsep) if item]
        if configured
        else [LEGACY_PROFILE_DIR]
    )
    project_id = _legacy_project_id()
    rows: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        profile_id = "default" if index == 0 else f"profile-{index + 1}"
        rows.append(
            {
                "id": profile_id,
                "name": "Основной" if index == 0 else f"Профиль {index + 1}",
                "path": str(path.resolve()),
                "provider": DEFAULT_PROFILE_PROVIDER,
                "proxy": dict(proxy_settings.DISABLED),
                "project_id": project_id if index == 0 else None,
                "model": "auto",
                "models": [],
                "account": {
                    "authenticated": False,
                    "name": None,
                    "email": None,
                    "uuid_suffix": None,
                },
                "status": "configured",
                "last_checked_at": None,
                "created_at": time.time(),
            }
        )
    return rows


def _default_payload() -> dict[str, Any]:
    profiles = _default_profile_rows()
    return {
        "version": CONFIG_VERSION,
        "fingerprint_salt": uuid.uuid4().hex,
        "active_profile": profiles[0]["id"],
        "profiles": profiles,
        "behavior": {
            "streaming": True,
            "thinking": "auto",
            "privacy": "keep",
            "persona": "programmer",
            "custom_persona": "",
            "actor": False,
            "mature": False,
        },
        "telemetry": {
            "store_content": False,
            "retention_days": 30,
            "max_requests": 5_000,
        },
    }


class ControlConfig:
    """Small atomic JSON store. Browser cookies never enter this file."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        if path == CONFIG_PATH:
            # State used to live beside the code; carry it over once.
            migrate_legacy_state()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = _default_payload()
            self._write_payload(payload)
            return payload
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid control configuration: {exc}") from exc
        version = payload.get("version", 1)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            raise RuntimeError(
                "invalid control configuration: version must be "
                "a positive integer"
            )
        if version > CONFIG_VERSION:
            raise RuntimeError(
                "invalid control configuration: schema version "
                f"{version} is newer than supported version {CONFIG_VERSION}"
            )
        try:
            normalized = self._normalize(payload)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid control configuration: {exc}"
            ) from exc
        if version < CONFIG_VERSION:
            self._write_payload(normalized)
        return normalized

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        defaults = _default_payload()
        profiles = payload.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            profiles = defaults["profiles"]
        normalized_profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in profiles:
            if not isinstance(raw, dict):
                continue
            profile_id = str(raw.get("id", "") or "").strip().lower()
            if not PROFILE_ID_RE.fullmatch(profile_id) or profile_id in seen:
                continue
            seen.add(profile_id)
            path = str(raw.get("path", "") or "").strip()
            if not path:
                continue
            provider_value = raw.get(
                "provider",
                DEFAULT_PROFILE_PROVIDER,
            )
            try:
                provider = _validate_profile_provider(provider_value)
            except ValueError as exc:
                raise ValueError(
                    f"profile {profile_id!r}: {exc}"
                ) from exc
            try:
                profile_proxy = proxy_settings.normalize(raw.get("proxy"))
            except ValueError as exc:
                raise ValueError(f"profile {profile_id!r}: {exc}") from exc
            account = raw.get("account")
            if not isinstance(account, dict):
                account = {}
            models = raw.get("models")
            if not isinstance(models, list):
                models = []
            normalized_profiles.append(
                {
                    "id": profile_id,
                    "name": str(raw.get("name", "") or profile_id)[:80],
                    "path": str(Path(path).expanduser().resolve()),
                    "provider": provider,
                    "proxy": profile_proxy,
                    "project_id": (
                        str(raw.get("project_id") or "").strip() or None
                    ),
                    "organization_id": (
                        str(raw.get("organization_id") or "").strip() or None
                    ),
                    "model": str(raw.get("model", "") or "auto"),
                    "models": [
                        item
                        for item in models
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                    ],
                    "account": {
                        "authenticated": bool(account.get("authenticated")),
                        "name": str(account.get("name") or "")[:160] or None,
                        "email": str(account.get("email") or "")[:240] or None,
                        "uuid_suffix": (
                            str(account.get("uuid_suffix") or "")[-12:] or None
                        ),
                    },
                    "status": str(raw.get("status", "") or "configured"),
                    "enabled": bool(raw.get("enabled", True)),
                    "account_fingerprint": (
                        str(raw.get("account_fingerprint") or "") or None
                    ),
                    "limited_until": raw.get("limited_until"),
                    "last_checked_at": raw.get("last_checked_at"),
                    "created_at": raw.get("created_at") or time.time(),
                }
            )
        if not normalized_profiles:
            normalized_profiles = defaults["profiles"]

        behavior = payload.get("behavior")
        if not isinstance(behavior, dict):
            behavior = {}
        payload_version = payload.get("version", 1)
        legacy_persona = str(behavior.get("persona") or "").strip()
        normalized_behavior = {
            **defaults["behavior"],
            **{
                key: behavior[key]
                for key in defaults["behavior"]
                if key in behavior
            },
        }
        if normalized_behavior["thinking"] not in {"off", "auto", "show"}:
            normalized_behavior["thinking"] = "auto"
        if normalized_behavior["privacy"] not in {"keep", "ephemeral"}:
            normalized_behavior["privacy"] = "keep"
        normalized_behavior["custom_persona"] = str(
            normalized_behavior["custom_persona"] or ""
        )[:8_000]
        normalized_behavior["actor"] = bool(normalized_behavior["actor"])
        normalized_behavior["mature"] = bool(normalized_behavior["mature"])
        if payload_version < 3 and legacy_persona in {"actor", "mature"}:
            normalized_behavior["persona"] = (
                "custom"
                if normalized_behavior["custom_persona"].strip()
                else "default"
            )
            normalized_behavior["actor"] = legacy_persona == "actor"
            normalized_behavior["mature"] = legacy_persona == "mature"
        elif normalized_behavior["persona"] not in {
            *PERSONA_PROMPTS,
            "custom",
        }:
            normalized_behavior["persona"] = "programmer"
        normalized_behavior["streaming"] = bool(
            normalized_behavior["streaming"]
        )

        telemetry = payload.get("telemetry")
        if not isinstance(telemetry, dict):
            telemetry = {}
        retention_days = telemetry.get(
            "retention_days",
            defaults["telemetry"]["retention_days"],
        )
        max_requests = telemetry.get(
            "max_requests",
            defaults["telemetry"]["max_requests"],
        )
        if isinstance(retention_days, bool) or not isinstance(
            retention_days,
            (int, float),
        ):
            retention_days = defaults["telemetry"]["retention_days"]
        if isinstance(max_requests, bool) or not isinstance(
            max_requests,
            (int, float),
        ):
            max_requests = defaults["telemetry"]["max_requests"]
        normalized_telemetry = {
            "store_content": bool(telemetry.get("store_content", False)),
            "retention_days": min(365, max(1, int(retention_days))),
            "max_requests": min(50_000, max(100, int(max_requests))),
        }

        active = str(payload.get("active_profile", "") or "")
        valid_ids = {row["id"] for row in normalized_profiles}
        if active not in valid_ids:
            active = normalized_profiles[0]["id"]
        return {
            "version": CONFIG_VERSION,
            "fingerprint_salt": str(
                payload.get("fingerprint_salt")
                or defaults["fingerprint_salt"]
            ),
            "active_profile": active,
            "profiles": normalized_profiles,
            "behavior": normalized_behavior,
            "telemetry": normalized_telemetry,
        }

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _save(self) -> None:
        self._data = self._normalize(self._data)
        self._write_payload(self._data)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = deepcopy(self._data)
            payload.pop("fingerprint_salt", None)
            for profile in payload.get("profiles", []):
                if isinstance(profile, dict):
                    profile.pop("account_fingerprint", None)
                    profile["proxy"] = proxy_settings.public(
                        profile.get("proxy")
                    )
                    project_id = str(profile.pop("project_id", "") or "")
                    organization_id = str(
                        profile.pop("organization_id", "") or ""
                    )
                    profile["project_id_suffix"] = (
                        project_id[-8:] or None
                    )
                    profile["organization_id_suffix"] = (
                        organization_id[-8:] or None
                    )
            return payload

    def session_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "path": row["path"],
                    "provider": row.get(
                        "provider",
                        DEFAULT_PROFILE_PROVIDER,
                    ),
                    "project_id": row.get("project_id"),
                    "organization_id": row.get("organization_id"),
                    "model": row.get("model", "auto"),
                    "proxy": deepcopy(row.get("proxy")),
                }
                for row in self._data["profiles"]
            ]

    def active_profile(self) -> dict[str, Any]:
        with self._lock:
            active = self._data["active_profile"]
            for row in self._data["profiles"]:
                if row["id"] == active:
                    return deepcopy(row)
        raise RuntimeError("active profile is missing")

    def profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            for row in self._data["profiles"]:
                if row["id"] == profile_id:
                    return deepcopy(row)
        raise KeyError(profile_id)

    def create_profile(
        self,
        name: str,
        provider: str = DEFAULT_PROFILE_PROVIDER,
    ) -> dict[str, Any]:
        clean_name = " ".join(str(name).split()).strip()
        if not clean_name:
            raise ValueError("profile name is required")
        provider = _validate_profile_provider(provider)
        base = re.sub(r"[^a-z0-9_-]+", "-", clean_name.lower()).strip("-_")
        if not base:
            base = "profile"
        base = base[:36]
        with self._lock:
            used = {row["id"] for row in self._data["profiles"]}
            profile_id = base
            if profile_id in used:
                profile_id = f"{base[:28]}-{uuid.uuid4().hex[:7]}"
            if not PROFILE_ID_RE.fullmatch(profile_id):
                profile_id = f"profile-{uuid.uuid4().hex[:8]}"
            profile_path = (
                self.path.parent / "profiles" / profile_id
            ).resolve()
            profile_path.mkdir(parents=True, exist_ok=True)
            row: dict[str, Any] = {
                "id": profile_id,
                "name": clean_name[:80],
                "path": str(profile_path),
                "provider": provider,
                "proxy": dict(proxy_settings.DISABLED),
                "project_id": None,
                "organization_id": None,
                "model": "auto",
                "models": [],
                "account": {
                    "authenticated": False,
                    "name": None,
                    "email": None,
                    "uuid_suffix": None,
                },
                "status": "auth_required",
                "enabled": True,
                "account_fingerprint": None,
                "limited_until": None,
                "last_checked_at": None,
                "created_at": time.time(),
            }
            self._data["profiles"].append(row)
            self._save()
            return deepcopy(row)

    def update_profile(
        self,
        profile_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "name",
            "provider",
            "proxy",
            "project_id",
            "organization_id",
            "model",
            "models",
            "account",
            "status",
            "enabled",
            "account_fingerprint",
            "limited_until",
            "last_checked_at",
        }
        normalized_updates = dict(updates)
        if "provider" in normalized_updates:
            normalized_updates["provider"] = _validate_profile_provider(
                normalized_updates["provider"]
            )
        with self._lock:
            for row in self._data["profiles"]:
                if row["id"] != profile_id:
                    continue
                if "proxy" in normalized_updates:
                    # An edit arrives without the password the panel never
                    # received, so the stored one is merged back in here.
                    normalized_updates["proxy"] = proxy_settings.normalize(
                        normalized_updates["proxy"],
                        row.get("proxy"),
                    )
                for key, value in normalized_updates.items():
                    if key in allowed:
                        row[key] = value
                self._save()
                return deepcopy(
                    next(
                        item
                        for item in self._data["profiles"]
                        if item["id"] == profile_id
                    )
                )
        raise KeyError(profile_id)

    def set_active_profile(self, profile_id: str) -> None:
        with self._lock:
            if profile_id not in {
                row["id"] for row in self._data["profiles"]
            }:
                raise KeyError(profile_id)
            self._data["active_profile"] = profile_id
            self._save()

    def update_behavior(self, updates: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "streaming",
            "thinking",
            "privacy",
            "persona",
            "custom_persona",
            "actor",
            "mature",
        }
        with self._lock:
            normalized_updates = dict(updates)
            legacy_persona = str(
                normalized_updates.get("persona") or ""
            ).strip()
            if legacy_persona in {"actor", "mature"}:
                current_base = str(
                    self._data["behavior"].get("persona") or "default"
                )
                if current_base not in {*PERSONA_PROMPTS, "custom"}:
                    current_base = (
                        "custom"
                        if str(
                            self._data["behavior"].get("custom_persona") or ""
                        ).strip()
                        else "default"
                    )
                normalized_updates["persona"] = current_base
                normalized_updates.setdefault(
                    "actor",
                    legacy_persona == "actor",
                )
                normalized_updates.setdefault(
                    "mature",
                    legacy_persona == "mature",
                )
            for key, value in normalized_updates.items():
                if key not in allowed:
                    continue
                if key in {"streaming", "actor", "mature"}:
                    value = bool(value)
                elif key == "thinking":
                    value = str(value or "").strip()
                    if value not in {"off", "auto", "show"}:
                        raise ValueError(
                            "thinking must be one of: off, auto, show"
                        )
                elif key == "privacy":
                    value = str(value or "").strip()
                    if value not in {"keep", "ephemeral"}:
                        raise ValueError(
                            "privacy must be one of: keep, ephemeral"
                        )
                elif key == "persona":
                    value = str(value or "").strip()
                    if value not in {*PERSONA_PROMPTS, "custom"}:
                        raise ValueError(
                            "persona must be one of: "
                            + ", ".join([*PERSONA_PROMPTS, "custom"])
                        )
                elif key == "custom_persona":
                    value = str(value or "").strip()
                    if len(value) > 8_000:
                        raise ValueError(
                            "custom_persona must not exceed 8000 characters"
                        )
                self._data["behavior"][key] = value
            self._save()
            return deepcopy(self._data["behavior"])

    def behavior(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data["behavior"])

    @staticmethod
    def persona_compilation_for(
        behavior: dict[str, Any],
    ) -> dict[str, Any]:
        details = compile_custom_persona_details(
            behavior.get("custom_persona"),
        )
        details["active"] = behavior.get("persona") == "custom"
        return details

    def persona_compilation(self) -> dict[str, Any]:
        with self._lock:
            behavior = deepcopy(self._data["behavior"])
        return self.persona_compilation_for(behavior)

    def update_telemetry(
        self,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "store_content",
            "retention_days",
        }
        with self._lock:
            for key, value in updates.items():
                if key in allowed:
                    self._data["telemetry"][key] = value
            self._save()
            return deepcopy(self._data["telemetry"])

    def telemetry_settings(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data["telemetry"])

    @staticmethod
    def persona_prompt_for(behavior: dict[str, Any]) -> str:
        persona = str(behavior.get("persona") or "default")
        sections: list[str] = []
        if persona == "custom":
            custom = compile_custom_persona(
                behavior.get("custom_persona"),
            )
            if custom:
                sections.append(
                    "User-selected fictional character or response-style "
                    "card:\n" + custom
                )
            elif bool(behavior.get("actor")) or bool(
                behavior.get("mature")
            ):
                sections.append(
                    "Base selection: no saved OpenClaude character or "
                    "response-style card is active. Retire any older saved "
                    "OpenClaude character or style from conversation history. "
                    "The selected modifiers do not restore it; roleplay may "
                    "continue only a scene independently established in the "
                    "current dialogue."
                )
        else:
            base = PERSONA_PROMPTS.get(persona, "")
            if base:
                sections.append(
                    "Selected base response style; it replaces any older "
                    "saved OpenClaude character card:\n" + base
                )
            elif bool(behavior.get("actor")) or bool(
                behavior.get("mature")
            ):
                sections.append(
                    "Base selection: no saved OpenClaude character or "
                    "response-style card is active. Retire any older saved "
                    "OpenClaude character or style from conversation history. "
                    "The selected modifiers do not restore it; roleplay may "
                    "continue only a scene independently established in the "
                    "current dialogue."
                )
        if bool(behavior.get("actor", False)):
            sections.append(ROLEPLAY_MODIFIER_PROMPT)
        if bool(behavior.get("mature", False)):
            sections.append(MATURE_TONE_MODIFIER_PROMPT)
        return "\n\n".join(sections)

    def behavior_snapshot(self) -> tuple[dict[str, Any], str]:
        """Return behavior and its resolved persona from one locked snapshot."""
        with self._lock:
            behavior = deepcopy(self._data["behavior"])
            return behavior, self.persona_prompt_for(behavior)

    def persona_prompt(self) -> str:
        return self.behavior_snapshot()[1]

    def resolve_model(self, requested: str) -> str | None:
        requested = str(requested or "").strip()
        if requested and requested not in {"claude-web", "auto"}:
            return requested
        profile = self.active_profile()
        selected = str(profile.get("model") or "auto")
        return None if selected in {"", "auto", "claude-web"} else selected

    def account_fingerprint(self, account_uuid: str) -> str:
        with self._lock:
            salt = str(self._data["fingerprint_salt"])
        return hashlib.sha256(
            f"{salt}:{account_uuid}".encode("utf-8")
        ).hexdigest()

    def telemetry_session_key(
        self,
        client_session_id: str | None,
        request_id: str,
    ) -> str:
        raw = str(client_session_id or "").strip()
        if not raw:
            return f"request-{request_id}"
        with self._lock:
            salt = str(self._data["fingerprint_salt"])
        digest = hashlib.sha256(
            f"{salt}:telemetry:{raw}".encode("utf-8")
        ).hexdigest()
        return f"session-{digest[:24]}"

    def profile_with_fingerprint(
        self,
        fingerprint: str,
        *,
        exclude_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            for row in self._data["profiles"]:
                if row["id"] == exclude_id:
                    continue
                if row.get("status") == "duplicate":
                    continue
                if row.get("account_fingerprint") == fingerprint:
                    return deepcopy(row)
        return None

    def claim_account_fingerprint(
        self,
        profile_id: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        """Atomically reserve an account or return the conflicting profile."""
        with self._lock:
            target: dict[str, Any] | None = None
            for row in self._data["profiles"]:
                if row["id"] == profile_id:
                    target = row
                    continue
                if row.get("status") == "duplicate":
                    continue
                if row.get("account_fingerprint") == fingerprint:
                    return deepcopy(row)
            if target is None:
                raise KeyError(profile_id)
            target["account_fingerprint"] = fingerprint
            self._save()
            return None
