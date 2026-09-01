"""Detecting coding clients and pointing them at the bridge.

These paths write into a user's home directory and read files that hold API
keys, so the tests pin two things above all: a secret never leaves the module,
and switching a client over keeps what it had before.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web_api import clients

SECRET = "sk-or-v1-must-never-appear-in-a-response"


def definition(home: Path, *, profile: bool = True) -> clients.ClientDefinition:
    return clients.ClientDefinition(
        id="openclaude",
        name="OpenClaude",
        executables=("openclaude",),
        home=home,
        settings_file="settings.json",
        profile_file=".openclaude-profile.json" if profile else None,
        install_command=("npm", "install", "-g", "@gitlawb/openclaude"),
    )


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def write(self, name: str, payload: dict) -> None:
        (self.home / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def snapshot(self, **kwargs) -> dict:
        with patch.object(clients, "_candidates", return_value=[]):
            return clients.client_snapshot(definition(self.home, **kwargs), 8765)

    def test_credentials_are_reported_but_never_returned(self) -> None:
        self.write(
            "settings.json",
            {"env": {"OPENAI_API_KEY": SECRET, "CLAUDE_CODE_USE_OPENAI": "1"}},
        )
        snapshot = self.snapshot()
        self.assertTrue(snapshot["has_credential"])
        self.assertNotIn(SECRET, json.dumps(snapshot, ensure_ascii=False))

    def test_active_profile_overrides_settings(self) -> None:
        """The profile file wins, and the panel must say which file it read."""
        self.write(
            "settings.json",
            {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8765"}},
        )
        self.write(
            ".openclaude-profile.json",
            {"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}},
        )
        snapshot = self.snapshot()
        self.assertEqual("elsewhere", snapshot["connection"])
        self.assertTrue(snapshot["config_path"].endswith("profile.json"))

    def test_bridge_on_the_openai_path_is_recognised(self) -> None:
        self.write(
            "settings.json",
            {
                "env": {
                    "CLAUDE_CODE_USE_OPENAI": "1",
                    "OPENAI_BASE_URL": "http://127.0.0.1:8765/v1",
                }
            },
        )
        self.assertEqual("bridge_openai", self.snapshot()["connection"])

    def test_a_client_with_no_configuration_is_unset(self) -> None:
        self.assertEqual("unset", self.snapshot()["connection"])

    def test_another_port_is_not_this_bridge(self) -> None:
        self.write(
            "settings.json",
            {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9999"}},
        )
        self.assertEqual("elsewhere", self.snapshot()["connection"])


class ConfigureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.definition = definition(self.home)

    def read(self, name: str) -> dict:
        return json.loads((self.home / name).read_text(encoding="utf-8"))

    def test_previous_provider_is_parked_not_destroyed(self) -> None:
        """Switching over must not cost the user their other provider setup."""
        (self.home / "settings.json").write_text(
            json.dumps(
                {
                    "env": {
                        "CLAUDE_CODE_USE_OPENAI": "1",
                        "OPENAI_API_KEY": SECRET,
                        "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
                    },
                    "model": "deepseek/deepseek-v4-flash-0731",
                }
            ),
            encoding="utf-8",
        )
        result = clients.configure(self.definition, 8765)

        settings = self.read("settings.json")
        self.assertEqual(
            "http://127.0.0.1:8765", settings["env"]["ANTHROPIC_BASE_URL"]
        )
        self.assertNotIn("CLAUDE_CODE_USE_OPENAI", settings["env"])
        self.assertEqual(SECRET, settings["_inactive_provider_env"]["OPENAI_API_KEY"])
        self.assertEqual(
            "deepseek/deepseek-v4-flash-0731",
            settings["_inactive_previous_model"],
        )
        self.assertEqual("claude-web", settings["model"])
        self.assertTrue(result["backups"])

    def test_stale_profile_is_rewritten_or_it_keeps_winning(self) -> None:
        """An old active profile overrides settings.json, so it must move too."""
        (self.home / "settings.json").write_text("{}", encoding="utf-8")
        (self.home / ".openclaude-profile.json").write_text(
            json.dumps(
                {
                    "profile": "openai",
                    "env": {"OPENAI_BASE_URL": "https://openrouter.ai/api/v1"},
                }
            ),
            encoding="utf-8",
        )
        clients.configure(self.definition, 8765)

        profile = self.read(".openclaude-profile.json")
        self.assertEqual("claude-web", profile["profile"])
        self.assertEqual(
            "http://127.0.0.1:8765", profile["env"]["ANTHROPIC_BASE_URL"]
        )
        self.assertNotIn("OPENAI_BASE_URL", profile["env"])
        self.assertEqual("openai", profile["_previous"]["profile"])

    def test_a_backup_is_written_before_touching_anything(self) -> None:
        (self.home / "settings.json").write_text(
            json.dumps({"env": {"KEEP": "me"}}), encoding="utf-8"
        )
        result = clients.configure(self.definition, 8765)
        backups = [Path(path) for path in result["backups"]]
        self.assertTrue(backups)
        restored = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual({"KEEP": "me"}, restored["env"])

    def test_configuring_a_fresh_machine_creates_the_file(self) -> None:
        result = clients.configure(self.definition, 8765)
        self.assertTrue((self.home / "settings.json").exists())
        self.assertEqual([], result["backups"])


if __name__ == "__main__":
    unittest.main()
