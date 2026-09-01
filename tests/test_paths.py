"""State survives replacing the code folder.

People update by unpacking a fresh archive over the old directory, and a
profile that lived beside the code disappeared with it. State therefore
lives in a data directory outside the repository, and what older versions
kept beside the code is carried over once.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_web_api import paths
from claude_web_api.control.config import ControlConfig


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.old = Path(self.directory.name) / "repo"
        self.new = Path(self.directory.name) / "data"
        self.old.mkdir()

    def legacy_layout(self) -> None:
        (self.old / "profile").mkdir()
        (self.old / "profile" / "cookies.sqlite").write_text("c", encoding="utf-8")
        (self.old / "profiles" / "second").mkdir(parents=True)
        (self.old / ".runtime").mkdir()
        (self.old / ".runtime" / "telemetry.sqlite3").write_text("t", encoding="utf-8")
        (self.old / "control_config.json").write_text(
            json.dumps(
                {
                    "version": 3,
                    "active_profile": "default",
                    "profiles": [
                        {"id": "default", "path": str(self.old / "profile")},
                        {"id": "second", "path": str(self.old / "profiles" / "second")},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_state_beside_the_code_moves_into_the_data_root(self) -> None:
        """The scenario itself: the user replaces the folder afterwards and
        the logged-in profile must still be there."""
        self.legacy_layout()
        moved = paths.migrate_legacy_state(self.old, self.new)
        self.assertIn("profile", moved)
        self.assertTrue((self.new / "profile" / "cookies.sqlite").exists())
        self.assertTrue((self.new / "profiles" / "second").is_dir())
        self.assertTrue((self.new / ".runtime" / "telemetry.sqlite3").exists())
        config = json.loads((self.new / "control_config.json").read_text(encoding="utf-8"))
        rows = {row["id"]: row["path"] for row in config["profiles"]}
        self.assertEqual(str(self.new / "profile"), rows["default"])
        self.assertEqual(str(self.new / "profiles" / "second"), rows["second"])

    def test_migration_runs_only_once(self) -> None:
        self.legacy_layout()
        paths.migrate_legacy_state(self.old, self.new)
        (self.old / "control_config.json").write_text("{}", encoding="utf-8")
        self.assertEqual([], paths.migrate_legacy_state(self.old, self.new))

    def test_nothing_to_migrate_is_a_no_op(self) -> None:
        self.assertEqual([], paths.migrate_legacy_state(self.old, self.new))
        self.assertFalse(self.new.exists())

    def test_a_profile_that_cannot_move_keeps_its_old_path(self) -> None:
        """A browser holding the directory open must not break the bridge:
        the config keeps pointing where the profile still is."""
        self.legacy_layout()
        original_move = paths.shutil.move

        def refuse(src: str, dst: str) -> str:
            if src.endswith("profile"):
                raise OSError("in use")
            return original_move(src, dst)

        paths.shutil.move = refuse  # type: ignore[assignment]
        paths.migrate_legacy_state(self.old, self.new)
        config = json.loads((self.new / "control_config.json").read_text(encoding="utf-8"))
        rows = {row["id"]: row["path"] for row in config["profiles"]}
        self.assertEqual(str(self.old / "profile"), rows["default"])
        self.assertEqual(str(self.new / "profiles" / "second"), rows["second"])
        self.assertFalse((self.new / "profile").exists(), "no half copy left")

        # The browser is closed by the next start: the move is retried.
        paths.shutil.move = original_move
        self.assertIn("profile", paths.migrate_legacy_state(self.old, self.new))
        config = json.loads((self.new / "control_config.json").read_text(encoding="utf-8"))
        rows = {row["id"]: row["path"] for row in config["profiles"]}
        self.assertEqual(str(self.new / "profile"), rows["default"])

    def test_new_profiles_are_created_under_the_data_root(self) -> None:
        config = ControlConfig(self.new / "control_config.json")
        row = config.create_profile("Второй")
        self.assertTrue(Path(row["path"]).is_relative_to(self.new))


if __name__ == "__main__":
    unittest.main()
