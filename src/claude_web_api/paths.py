"""Filesystem anchors shared by the bridge.

Every path is derived from the repository root, so moving the package does not
require rewriting per-module constants.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]

WEB_ROOT = PROJECT_ROOT / "web"
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
RESOURCES_DIR = PACKAGE_ROOT / "resources"

PROJECT_INSTRUCTIONS = RESOURCES_DIR / "project_instructions.txt"
CONTROL_CONFIG_FILE = PROJECT_ROOT / "control_config.json"
LEGACY_PROJECT_FILE = PROJECT_ROOT / "claude_project.json"
LEGACY_PROFILE_DIR = PROJECT_ROOT / "profile"
PROJECT_PROMPT_LEASE_FILE = PROJECT_ROOT / "project_prompt_leases.json"
TELEMETRY_DB_FILE = RUNTIME_DIR / "telemetry.sqlite3"
