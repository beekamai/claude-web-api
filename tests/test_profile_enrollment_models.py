from __future__ import annotations

import unittest

from claude_web_api.enrollment.manager import _normalize_enrollment_models


class EnrollmentModelNormalizationTests(unittest.TestCase):
    def test_selector_preserves_unavailable_and_structured_reason(self) -> None:
        reason = {
            "type": "upgrade_required",
            "required_plan": "pro",
            "message": "Upgrade to use this model",
        }

        models = _normalize_enrollment_models(
            [
                {
                    "id": "claude-fable-5",
                    "name": "Fable 5",
                    "available": False,
                    "disabled_reason": reason,
                    "source": "account_model_selector",
                }
            ]
        )

        self.assertEqual(1, len(models))
        self.assertFalse(models[0]["available"])
        self.assertFalse(models[0]["catalog_available"])
        self.assertEqual("unavailable", models[0]["access_status"])
        self.assertEqual(reason, models[0]["disabled_reason"])
        self.assertEqual("account_model_selector", models[0]["source"])

    def test_selector_available_false_without_reason_stays_unselectable(
        self,
    ) -> None:
        models = _normalize_enrollment_models(
            [
                {
                    "id": "claude-opus-test",
                    "available": False,
                    "source": "account_model_selector",
                }
            ]
        )

        self.assertFalse(models[0]["available"])
        self.assertEqual(
            "account_unavailable",
            models[0]["disabled_reason"],
        )

    def test_bootstrap_row_is_catalog_only_even_if_marked_available(
        self,
    ) -> None:
        models = _normalize_enrollment_models(
            [
                {
                    "id": "claude-fable-5",
                    "name": "Fable 5",
                    "available": True,
                    "source": "bootstrap_catalog",
                }
            ]
        )

        self.assertFalse(models[0]["available"])
        self.assertFalse(models[0]["catalog_available"])
        self.assertEqual("unverified", models[0]["access_status"])
        self.assertEqual("catalog_only", models[0]["disabled_reason"])
        self.assertEqual("bootstrap_catalog", models[0]["source"])

    def test_effective_selector_row_remains_selectable(self) -> None:
        models = _normalize_enrollment_models(
            [
                {
                    "id": "claude-sonnet-test",
                    "available": True,
                    "source": "account_model_selector",
                }
            ]
        )

        self.assertTrue(models[0]["available"])
        self.assertTrue(models[0]["catalog_available"])
        self.assertEqual("available", models[0]["access_status"])


if __name__ == "__main__":
    unittest.main()
