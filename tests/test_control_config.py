"""ControlConfigTests and friends, split out of the original suite."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claude_web_api.control.config import (
    CONFIG_VERSION,
    SUPPORTED_PROFILE_PROVIDERS,
    ControlConfig,
    compile_custom_persona,
    compile_custom_persona_details,
)


class ControlConfigTests(unittest.TestCase):
    def test_v1_profile_migrates_to_claude_web_without_losing_ids(
        self,
    ) -> None:
        project_uuid = "11111111-1111-1111-1111-111111111111"
        organization_uuid = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "fingerprint_salt": "test-salt",
                        "active_profile": "legacy",
                        "profiles": [
                            {
                                "id": "legacy",
                                "name": "Legacy Claude",
                                "path": str(root / "legacy"),
                                "project_id": project_uuid,
                                "organization_id": organization_uuid,
                            }
                        ],
                        "behavior": {},
                    }
                ),
                encoding="utf-8",
            )

            config = ControlConfig(config_path)

            profile = config.profile("legacy")
            self.assertEqual("claude_web", profile["provider"])
            self.assertEqual(project_uuid, profile["project_id"])
            self.assertEqual(
                organization_uuid,
                profile["organization_id"],
            )
            self.assertEqual(
                "claude_web",
                config.session_profiles()[0]["provider"],
            )

            migrated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(CONFIG_VERSION, migrated["version"])
            self.assertEqual(
                "claude_web",
                migrated["profiles"][0]["provider"],
            )
            self.assertEqual(
                project_uuid,
                migrated["profiles"][0]["project_id"],
            )
            self.assertEqual(
                organization_uuid,
                migrated["profiles"][0]["organization_id"],
            )

    def test_profile_provider_is_validated_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            self.assertEqual(
                ("claude_web", "grok_web"),
                SUPPORTED_PROFILE_PROVIDERS,
            )
            self.assertEqual(
                "claude_web",
                config.active_profile()["provider"],
            )

            grok = config.create_profile("Grok", provider="grok_web")
            self.assertEqual("grok_web", grok["provider"])
            updated = config.update_profile(
                grok["id"],
                {
                    "provider": "claude_web",
                    "project_id": "project-kept",
                    "organization_id": "organization-kept",
                },
            )
            self.assertEqual("claude_web", updated["provider"])
            self.assertEqual("project-kept", updated["project_id"])
            self.assertEqual(
                "organization-kept",
                updated["organization_id"],
            )

            before = config.snapshot()
            with self.assertRaisesRegex(ValueError, "claude_web, grok_web"):
                config.create_profile("Unknown", provider="unknown")
            with self.assertRaisesRegex(ValueError, "claude_web, grok_web"):
                config.update_profile(
                    grok["id"],
                    {"provider": "unknown"},
                )
            self.assertEqual(before, config.snapshot())

    def test_unknown_persisted_provider_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": CONFIG_VERSION,
                        "active_profile": "unknown",
                        "profiles": [
                            {
                                "id": "unknown",
                                "name": "Unknown",
                                "path": str(root / "unknown"),
                                "provider": "unsupported_web",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "profile 'unknown'.*claude_web, grok_web",
            ):
                ControlConfig(config_path)

    def test_behavior_update_validates_and_snapshots_persona_atomically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            persona = "  Первая строка\nВторая строка  "
            updated = config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": persona,
                }
            )
            behavior, resolved = config.behavior_snapshot()

            self.assertEqual("custom", updated["persona"])
            self.assertEqual(persona.strip(), updated["custom_persona"])
            self.assertFalse(updated["actor"])
            self.assertFalse(updated["mature"])
            self.assertEqual(updated, behavior)
            self.assertIn(persona.strip(), resolved)
            self.assertIn(
                "User-selected fictional character or response-style card",
                resolved,
            )
            with self.assertRaisesRegex(ValueError, "8000"):
                config.update_behavior({"custom_persona": "x" * 8_001})
            with self.assertRaisesRegex(ValueError, "persona must"):
                config.update_behavior({"persona": "unknown"})

    def test_v2_actor_and_mature_migrate_to_independent_modifiers(
        self,
    ) -> None:
        cases = (
            ("actor", True, False),
            ("mature", False, True),
        )
        for legacy, expected_actor, expected_mature in cases:
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config_path = root / "control_config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "active_profile": "default",
                            "profiles": [
                                {
                                    "id": "default",
                                    "name": "Default",
                                    "path": str(root / "profile"),
                                    "provider": "claude_web",
                                }
                            ],
                            "behavior": {
                                "streaming": True,
                                "thinking": "auto",
                                "privacy": "keep",
                                "persona": legacy,
                                "custom_persona": (
                                    "В этой сцене она — девушка собеседника."
                                ),
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                config = ControlConfig(config_path)
                behavior = config.behavior()

                self.assertEqual("custom", behavior["persona"])
                self.assertTrue(behavior["custom_persona"])
                self.assertEqual(expected_actor, behavior["actor"])
                self.assertEqual(expected_mature, behavior["mature"])
                persisted = json.loads(
                    config_path.read_text(encoding="utf-8")
                )
                self.assertEqual(CONFIG_VERSION, persisted["version"])
                self.assertEqual(behavior, persisted["behavior"])

    def test_v2_modifier_without_saved_card_migrates_to_default_base(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "active_profile": "default",
                        "profiles": [
                            {
                                "id": "default",
                                "name": "Default",
                                "path": str(root / "profile"),
                                "provider": "claude_web",
                            }
                        ],
                        "behavior": {
                            "persona": "actor",
                            "custom_persona": "",
                        },
                    }
                ),
                encoding="utf-8",
            )

            behavior = ControlConfig(config_path).behavior()

            self.assertEqual("default", behavior["persona"])
            self.assertTrue(behavior["actor"])
            self.assertFalse(behavior["mature"])

    def test_custom_actor_and_mature_compose_without_losing_raw_card(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = (
                "В этой сцене она — девушка собеседника, 23 года.\n"
                "Реальный человек, не робот! ИИ не упоминает.\n"
                "Хорошо пишет программный код."
            )
            updated = config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": raw,
                    "actor": True,
                    "mature": True,
                }
            )
            behavior, resolved = config.behavior_snapshot()

            self.assertEqual(raw, updated["custom_persona"])
            self.assertEqual(raw, behavior["custom_persona"])
            self.assertTrue(behavior["actor"])
            self.assertTrue(behavior["mature"])
            self.assertNotIn("Реальный человек", resolved)
            self.assertNotIn("ИИ не упоминает", resolved)
            self.assertIn("In this fictional scene", resolved)
            self.assertIn("девушка собеседника", resolved)
            self.assertIn(
                "The user also selected a roleplay style",
                resolved,
            )
            self.assertIn(
                "The user also selected a candid, grown-up",
                resolved,
            )
            self.assertNotIn("mature fictional themes", resolved)
            self.assertNotIn("consenting adults", resolved)
            self.assertNotIn("Provider safety rules", resolved)
            self.assertLess(
                resolved.index("User-selected fictional"),
                resolved.index("The user also selected a roleplay style"),
            )
            self.assertLess(
                resolved.index("The user also selected a roleplay style"),
                resolved.index(
                    "The user also selected a candid, grown-up"
                ),
            )

    def test_legacy_actor_and_mature_patch_preserve_custom_base(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = "В этой сцене она — девушка собеседника."
            config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": raw,
                }
            )

            actor = config.update_behavior({"persona": "actor"})
            self.assertEqual("custom", actor["persona"])
            self.assertEqual(raw, actor["custom_persona"])
            self.assertTrue(actor["actor"])
            self.assertFalse(actor["mature"])

            mature = config.update_behavior({"persona": "mature"})
            self.assertEqual("custom", mature["persona"])
            self.assertEqual(raw, mature["custom_persona"])
            self.assertFalse(mature["actor"])
            self.assertTrue(mature["mature"])

    def test_legacy_modifier_patch_preserves_base_and_explicit_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            config.update_behavior({"persona": "programmer"})

            updated = config.update_behavior(
                {
                    "persona": "actor",
                    "actor": False,
                    "mature": True,
                }
            )

            self.assertEqual("programmer", updated["persona"])
            self.assertFalse(updated["actor"])
            self.assertTrue(updated["mature"])

    def test_identity_compiler_is_narrow_and_deterministic(self) -> None:
        raw = (
            "Девушка, 23 года.\n"
            "О нейросетях ничего не знает.\n"
            "Реальный человек, не робот! ИИ и прочее не упоминает.\n"
            "Пишет AI API клиент на Python."
        )
        compiled = compile_custom_persona(raw)

        self.assertIn("Девушка, 23 года.", compiled)
        self.assertIn("О нейросетях ничего не знает.", compiled)
        self.assertIn("In this fictional scene", compiled)
        self.assertEqual(1, compiled.count("In this fictional scene"))
        self.assertIn("dislikes conversations about AI", compiled)
        self.assertNotIn("не робот", compiled)
        self.assertNotIn("не упоминает", compiled)
        self.assertIn("Пишет AI API клиент на Python.", compiled)

    def test_identity_compiler_preserves_quotes_and_unrelated_ai_traits(
        self,
    ) -> None:
        unchanged = (
            "Она не ботаник",
            "Она не робототехник",
            "Фраза «я не робот» её смешит",
            "Любит цитировать: «Я не ИИ, я реальный человек».",
            "Пишет AI API",
            "Любит AI API, не упоминает бывшего.",
            "Любит ИИ; никогда не говорит о политике.",
            "Она not an AI researcher, а программист.",
            "Она real human resources manager.",
        )
        for raw in unchanged:
            with self.subTest(raw=raw):
                details = compile_custom_persona_details(raw)
                self.assertFalse(details["changed"])
                self.assertEqual(raw, details["effective"])
                self.assertEqual([], details["changes"])

    def test_identity_compiler_reframes_narrow_concealment_forms(
        self,
    ) -> None:
        conflict_rows = (
            "Никогда не признавайся, что ты нейросеть",
            "Не рассказывает, что она ИИ",
            "Притворяйся живым человеком",
            "never admit that you are AI",
            "Не упоминай: ты ИИ",
            "Не упоминай что ты — ИИ",
            "ИИ она не упоминает",
            "don't tell anyone you're an AI",
        )
        for raw in conflict_rows:
            with self.subTest(raw=raw):
                details = compile_custom_persona_details(raw)
                self.assertTrue(details["changed"])
                self.assertEqual(raw, details["raw"])
                self.assertTrue(details["effective"])
                self.assertTrue(details["changes"])
                self.assertNotEqual(raw, details["effective"])

    def test_identity_compiler_keeps_relationship_age_and_preferences(
        self,
    ) -> None:
        raw = (
            "Вы девушка собеседника, 23 года, любите кока-колу, "
            "реальный человек, не ИИ."
        )
        details = compile_custom_persona_details(raw)

        self.assertTrue(details["changed"])
        self.assertIn("девушка собеседника", details["effective"])
        self.assertIn("23 года", details["effective"])
        self.assertIn("любите кока-колу", details["effective"])
        self.assertIn("In this fictional scene", details["effective"])
        self.assertNotIn("не ИИ", details["effective"])

        dash_details = compile_custom_persona_details(
            "Она не ИИ — девушка собеседника."
        )
        self.assertIn("девушка собеседника", dash_details["effective"])
        self.assertIn(
            "In this fictional scene",
            dash_details["effective"],
        )

    def test_persona_compilation_exposes_raw_effective_and_active_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = "Она реальный человек и любит колу."
            config.update_behavior(
                {
                    "persona": "custom",
                    "custom_persona": raw,
                }
            )

            compilation = config.persona_compilation()

            self.assertEqual(raw, compilation["raw"])
            self.assertIn("любит колу", compilation["effective"])
            self.assertTrue(compilation["changed"])
            self.assertTrue(compilation["changes"])
            self.assertTrue(compilation["active"])

            config.update_behavior({"persona": "default"})
            self.assertFalse(config.persona_compilation()["active"])

    def test_default_and_programmer_retire_saved_custom_card_with_modifiers(
        self,
    ) -> None:
        raw = "В этой сцене она — девушка собеседника."
        default_prompt = ControlConfig.persona_prompt_for(
            {
                "persona": "default",
                "custom_persona": raw,
                "actor": True,
                "mature": True,
            }
        )
        programmer_prompt = ControlConfig.persona_prompt_for(
            {
                "persona": "programmer",
                "custom_persona": raw,
                "actor": False,
                "mature": True,
            }
        )

        self.assertIn("no saved OpenClaude character", default_prompt)
        self.assertIn("modifiers do not restore it", default_prompt)
        self.assertNotIn(raw, default_prompt)
        self.assertIn("replaces any older saved", programmer_prompt)
        self.assertNotIn(raw, programmer_prompt)

    def test_duplicate_profile_never_replaces_original_fingerprint_owner(
        self,
    ) -> None:
        account_fingerprint = "fingerprint-a"
        project_uuid = "33333333-3333-3333-3333-333333333333"
        organization_uuid = "44444444-4444-4444-4444-444444444444"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "control_config.json"
            payload = {
                "version": 1,
                "fingerprint_salt": "test-salt",
                "active_profile": "original",
                "profiles": [
                    {
                        "id": "original",
                        "name": "Original",
                        "path": str(root / "original"),
                        "project_id": project_uuid,
                        "organization_id": organization_uuid,
                        "status": "ready",
                        "enabled": True,
                        "account_fingerprint": account_fingerprint,
                    },
                    {
                        "id": "duplicate",
                        "name": "Duplicate",
                        "path": str(root / "duplicate"),
                        "status": "duplicate",
                        "enabled": False,
                        "account_fingerprint": account_fingerprint,
                    },
                ],
                "behavior": {},
            }
            config_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            config = ControlConfig(config_path)
            owner = config.profile_with_fingerprint(account_fingerprint)
            conflict = config.claim_account_fingerprint(
                "duplicate",
                account_fingerprint,
            )
            self.assertEqual("original", owner["id"])
            self.assertEqual("original", conflict["id"])
            self.assertEqual("ready", config.profile("original")["status"])

            public = config.snapshot()
            serialized = json.dumps(public)
            self.assertNotIn(project_uuid, serialized)
            self.assertNotIn(organization_uuid, serialized)
            original = public["profiles"][0]
            self.assertEqual("33333333", original["project_id_suffix"])
            self.assertEqual(
                "44444444",
                original["organization_id_suffix"],
            )


if __name__ == "__main__":
    unittest.main()
