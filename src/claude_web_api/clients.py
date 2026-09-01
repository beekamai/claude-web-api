"""Detecting and configuring the coding clients that talk to the bridge.

Claude Code and OpenClaude both read an Anthropic base URL from their own
configuration. The panel needs to answer three questions about each: is it
installed, is it pointed at this bridge, and — if not — can that be fixed from
here. Nothing in this module reads a client's secrets back out: only whether a
credential is present, never its value.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BRIDGE_TOKEN = "local-claude-web"
BRIDGE_MODEL = "claude-web"
VERSION_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class ClientDefinition:
    """A client the panel knows how to inspect and point at the bridge."""

    id: str
    name: str
    executables: tuple[str, ...]
    home: Path
    settings_file: str
    profile_file: str | None = None
    install_command: tuple[str, ...] | None = None
    docs: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def definitions() -> list[ClientDefinition]:
    home = _home()
    return [
        ClientDefinition(
            id="claude-code",
            name="Claude Code",
            executables=("claude",),
            home=home / ".claude",
            settings_file="settings.json",
            install_command=(
                "npm",
                "install",
                "-g",
                "@anthropic-ai/claude-code",
            ),
            docs="docs/claude-code-setup.md",
            notes=(
                "Модель передаётся флагом --model claude-web.",
            ),
        ),
        ClientDefinition(
            id="openclaude",
            name="OpenClaude",
            executables=("openclaude",),
            home=home / ".openclaude",
            settings_file="settings.json",
            profile_file=".openclaude-profile.json",
            install_command=("npm", "install", "-g", "@gitlawb/openclaude"),
            docs="docs/claude-code-setup.md",
            notes=(
                "Активный профиль перекрывает settings.json.",
                "Модель передаётся флагом --model claude-web.",
            ),
        ),
    ]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _candidates(definition: ClientDefinition) -> list[str]:
    """Every launcher on PATH for this client, best-supported form first.

    npm installs a POSIX shim next to the Windows one, and a user may shadow
    both with a wrapper of their own. Any of them can be the broken one, so the
    caller tries them in turn rather than trusting the first hit.
    """
    suffixes = [""]
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        suffixes = [
            *(ext.lower() for ext in pathext.split(os.pathsep) if ext),
            "",
        ]
    found: list[str] = []
    for name in definition.executables:
        for suffix in suffixes:
            for directory in os.environ.get("PATH", "").split(os.pathsep):
                if not directory:
                    continue
                candidate = Path(directory) / f"{name}{suffix}"
                if candidate.is_file() and str(candidate) not in found:
                    found.append(str(candidate))
    return found


def _version(executable: str) -> tuple[str | None, str | None]:
    """Return the reported version, or why it could not be read."""
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError as exc:
        return None, f"не удалось запустить: {exc.strerror or exc}"
    except subprocess.TimeoutExpired:
        return None, "клиент не ответил на --version"
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        first = output.splitlines()[0] if output else "ненулевой код возврата"
        return None, first[:200]
    return (output.splitlines()[0].strip() if output else None), None


def _effective_env(definition: ClientDefinition) -> tuple[dict[str, str], str]:
    """Merge the client's settings with the profile that overrides them."""
    settings = _read_json(definition.home / definition.settings_file)
    env: dict[str, str] = {
        str(key): str(value)
        for key, value in (settings.get("env") or {}).items()
    }
    source = str(definition.home / definition.settings_file)
    if definition.profile_file:
        profile_path = definition.home / definition.profile_file
        profile = _read_json(profile_path)
        profile_env = profile.get("env")
        if isinstance(profile_env, dict) and profile_env:
            env.update(
                {str(key): str(value) for key, value in profile_env.items()}
            )
            source = str(profile_path)
    return env, source


def bridge_base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def client_snapshot(definition: ClientDefinition, port: int) -> dict[str, Any]:
    candidates = _candidates(definition)
    executable = candidates[0] if candidates else None
    version: str | None = None
    version_error: str | None = None
    for candidate in candidates:
        version, version_error = _version(candidate)
        if version:
            executable = candidate
            break

    env, source = _effective_env(definition)
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    expected = bridge_base_url(port)
    uses_openai_path = env.get("CLAUDE_CODE_USE_OPENAI") in ("1", "true", "yes")
    openai_base = env.get("OPENAI_BASE_URL", "")

    if base_url.rstrip("/") == expected and not uses_openai_path:
        connection = "bridge"
    elif uses_openai_path and expected in openai_base:
        connection = "bridge_openai"
    elif base_url or uses_openai_path:
        connection = "elsewhere"
    else:
        connection = "unset"

    return {
        "id": definition.id,
        "name": definition.name,
        "installed": bool(executable),
        "executable": executable,
        "version": version,
        "version_error": version_error,
        "connection": connection,
        "base_url": base_url or (openai_base if uses_openai_path else ""),
        "model": env.get("ANTHROPIC_MODEL") or env.get("OPENAI_MODEL") or "",
        "has_credential": bool(
            env.get("ANTHROPIC_AUTH_TOKEN")
            or env.get("ANTHROPIC_API_KEY")
            or env.get("OPENAI_API_KEY")
        ),
        "config_path": source,
        "config_exists": Path(source).exists(),
        "can_install": bool(definition.install_command),
        "docs": definition.docs,
        "notes": list(definition.notes),
    }


def snapshot(port: int) -> dict[str, Any]:
    return {
        "bridge_url": bridge_base_url(port),
        "model": BRIDGE_MODEL,
        "clients": [
            client_snapshot(definition, port) for definition in definitions()
        ],
    }


def find_definition(client_id: str) -> ClientDefinition | None:
    for definition in definitions():
        if definition.id == client_id:
            return definition
    return None


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-bridge-{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def configure(definition: ClientDefinition, port: int) -> dict[str, Any]:
    """Point a client at this bridge, keeping what it had before.

    Settings that belonged to another provider are parked under an inactive
    key rather than deleted: switching back should not need a backup file,
    though one is written anyway.
    """
    base_url = bridge_base_url(port)
    bridge_env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": BRIDGE_TOKEN,
        "ANTHROPIC_MODEL": BRIDGE_MODEL,
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
    }
    parked_keys = (
        "CLAUDE_CODE_USE_OPENAI",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
    )
    touched: list[str] = []

    settings_path = definition.home / definition.settings_file
    settings = _read_json(settings_path)
    backup = _backup(settings_path)
    env = {
        str(key): value for key, value in (settings.get("env") or {}).items()
    }
    parked = {key: env.pop(key) for key in parked_keys if key in env}
    if parked:
        settings["_inactive_provider_env"] = parked
    env.update(bridge_env)
    settings["env"] = env
    previous_model = settings.get("model")
    if previous_model and previous_model != BRIDGE_MODEL:
        settings["_inactive_previous_model"] = previous_model
    settings["model"] = BRIDGE_MODEL
    _write_json(settings_path, settings)
    touched.append(str(settings_path))

    profile_backup = None
    if definition.profile_file:
        # The active profile overrides settings.json, so leaving an old one in
        # place would silently keep the client on its previous provider.
        profile_path = definition.home / definition.profile_file
        profile = _read_json(profile_path)
        if profile:
            profile_backup = _backup(profile_path)
            _write_json(
                profile_path,
                {
                    "profile": "claude-web",
                    "env": dict(bridge_env),
                    "createdAt": profile.get("createdAt"),
                    "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "_previous": profile,
                },
            )
            touched.append(str(profile_path))

    return {
        "ok": True,
        "client": definition.id,
        "base_url": base_url,
        "model": BRIDGE_MODEL,
        "written": touched,
        "backups": [str(path) for path in (backup, profile_backup) if path],
        "parked": sorted(parked),
    }
