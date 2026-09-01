"""Filesystem anchors shared by the bridge.

Code lives in the repository; state lives in a data directory outside it.
People update by unpacking a fresh archive over — or instead of — the old
folder, and a profile that lived beside the code vanished with it. The data
directory defaults to the per-user application-data folder and can be
pointed elsewhere with ``CLAUDE_WEB_API_DATA``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]

WEB_ROOT = PROJECT_ROOT / "web"
RESOURCES_DIR = PACKAGE_ROOT / "resources"
PROJECT_INSTRUCTIONS = RESOURCES_DIR / "project_instructions.txt"


def _default_data_root() -> Path:
    configured = os.getenv("CLAUDE_WEB_API_DATA", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "claude-web-api"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-web-api"
    base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "claude-web-api"


DATA_ROOT = _default_data_root()
RUNTIME_DIR = DATA_ROOT / ".runtime"
CONTROL_CONFIG_FILE = DATA_ROOT / "control_config.json"
LEGACY_PROJECT_FILE = DATA_ROOT / "claude_project.json"
LEGACY_PROFILE_DIR = DATA_ROOT / "profile"
PROJECT_PROMPT_LEASE_FILE = DATA_ROOT / "project_prompt_leases.json"
TELEMETRY_DB_FILE = RUNTIME_DIR / "telemetry.sqlite3"

_STATE_FILES = (
    "control_config.json",
    "claude_project.json",
    "project_prompt_leases.json",
)
_STATE_DIRS = ("profile", "profiles", ".runtime")


def migrate_legacy_state(
    old_root: Path = PROJECT_ROOT,
    new_root: Path = DATA_ROOT,
) -> list[str]:
    """Move state that older versions kept beside the code into the data root.

    Runs once: nothing happens when the new root already holds a config. A
    profile directory that cannot be moved — a browser still has it open — is
    left where it is, and the config keeps pointing at it, so the bridge keeps
    working and the move is retried on a later start.
    """
    if old_root == new_root:
        return []
    config = new_root / "control_config.json"
    moved: list[str] = []
    if not config.exists():
        if not (old_root / "control_config.json").exists():
            return []
        new_root.mkdir(parents=True, exist_ok=True)
        for name in _STATE_FILES:
            source = old_root / name
            if not source.is_file():
                continue
            shutil.copy2(source, new_root / name)
            moved.append(name)
            if name != "control_config.json":
                try:
                    source.unlink()
                except OSError:
                    pass
        if _move_dir(old_root / ".runtime", new_root / ".runtime"):
            moved.append(".runtime")
    # Profile directories are retried on every start until they have moved:
    # a browser that still has one open makes the first attempt fail.
    for name in ("profile", "profiles"):
        source = old_root / name
        if source.is_dir() and _move_dir(source, new_root / name):
            moved.append(name)
    if moved:
        _rewrite_profile_paths(config, old_root, new_root, moved)
    return moved


def _move_dir(source: Path, target: Path) -> bool:
    """Move a state directory, leaving no half-copied target behind.

    Across drives ``shutil.move`` copies and then deletes; when the delete
    fails because a browser holds the files, the copy is removed again so the
    original stays the only one.
    """
    if target.exists():
        return False
    try:
        shutil.move(str(source), str(target))
        return True
    except OSError:
        if source.exists() and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        return False


def _rewrite_profile_paths(
    config: Path,
    old_root: Path,
    new_root: Path,
    moved: list[str],
) -> None:
    """Point profile rows at the directories that actually moved."""
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    changed = False
    for row in payload.get("profiles", []):
        if not isinstance(row, dict):
            continue
        path = Path(str(row.get("path") or ""))
        for name in ("profile", "profiles"):
            if name not in moved:
                continue
            old_dir = old_root / name
            try:
                relative = path.resolve().relative_to(old_dir.resolve())
            except (OSError, ValueError):
                # resolve() fails for a directory that no longer exists;
                # compare the textual prefix in that case.
                if not str(path).startswith(str(old_dir)):
                    continue
                relative = Path(str(path)[len(str(old_dir)) :].lstrip("\\/"))
            row["path"] = str(new_root / name / relative)
            changed = True
    if changed:
        config.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
