from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web_api.enrollment.manager import (
    CLAUDE_WEB_PROVIDER,
    GROK_WEB_PROVIDER,
    Enrollment,
    ProfileEnrollmentManager,
    _ChromeEnrollmentBrowser,
    _normalize_grok_enrollment_models,
)


class FakePage:
    def __init__(self, result: object | None = None) -> None:
        self.result = result
        self.url = "about:blank"
        self.closed = False
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[str] = []

    async def goto(self, url: str, **_: object) -> None:
        self.url = url
        self.goto_calls.append(url)

    async def bring_to_front(self) -> None:
        return None

    async def evaluate(self, script: str, *_: object) -> object:
        self.evaluate_calls.append(script)
        return self.result

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class FakeCamoufox:
    instances: list["FakeCamoufox"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.page = FakePage()
        self.context = FakeContext(self.page)
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeContext:
        return self.context

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


class FakeChromeEnrollmentBrowser:
    instances: list["FakeChromeEnrollmentBrowser"] = []

    def __init__(self, profile_path: Path) -> None:
        self.profile_path = profile_path
        self.page = FakePage()
        self.context = FakeContext(self.page)
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeContext:
        return self.context

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.launch_options: dict[str, object] | None = None

    async def launch_persistent_context(
        self,
        **options: object,
    ) -> FakeContext:
        self.launch_options = options
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


class FakePlaywrightManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright
        self.exited = False

    async def __aenter__(self) -> FakePlaywright:
        return self.playwright

    async def __aexit__(self, *_: object) -> None:
        self.exited = True


def make_enrollment(
    provider: str,
    result: object,
) -> tuple[Enrollment, FakePage]:
    page = FakePage(result)
    camoufox = FakeCamoufox()
    enrollment = Enrollment(
        profile_id="profile-1",
        profile_path=Path("profile-1"),
        provider=provider,
        camoufox=camoufox,
        context=FakeContext(page),
        page=page,
        started_at=1.0,
        browser_engine=(
            "chrome"
            if provider == GROK_WEB_PROVIDER
            else "camoufox"
        ),
    )
    return enrollment, page


class ProviderEnrollmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeCamoufox.instances.clear()
        FakeChromeEnrollmentBrowser.instances.clear()

    async def test_default_launch_keeps_claude_entrypoint(self) -> None:
        manager = ProfileEnrollmentManager()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "claude_web_api.enrollment.manager.AsyncCamoufox",
                FakeCamoufox,
            ):
                snapshot = await manager.launch("claude", directory)
                page = FakeCamoufox.instances[-1].page
                self.assertEqual(["https://claude.ai/new"], page.goto_calls)
                self.assertEqual(CLAUDE_WEB_PROVIDER, snapshot["provider"])
                await manager.finish("claude")
                await asyncio.sleep(0)

    async def test_grok_launch_uses_its_own_persistent_profile(self) -> None:
        manager = ProfileEnrollmentManager()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "claude_web_api.enrollment.manager._ChromeEnrollmentBrowser",
                FakeChromeEnrollmentBrowser,
            ):
                snapshot = await manager.launch(
                    "grok",
                    directory,
                    GROK_WEB_PROVIDER,
                )
                instance = FakeChromeEnrollmentBrowser.instances[-1]
                self.assertEqual(["https://grok.com/"], instance.page.goto_calls)
                self.assertEqual(GROK_WEB_PROVIDER, snapshot["provider"])
                self.assertEqual("chrome", snapshot["browser_engine"])
                self.assertEqual(
                    Path(directory).resolve(),
                    instance.profile_path,
                )
                self.assertEqual([], FakeCamoufox.instances)
                await manager.finish("grok")
                await asyncio.sleep(0)

    async def test_claude_launch_does_not_change_existing_fingerprint(self) -> None:
        manager = ProfileEnrollmentManager()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "claude_web_api.enrollment.manager.AsyncCamoufox",
                FakeCamoufox,
            ):
                await manager.launch("claude", directory)
                instance = FakeCamoufox.instances[-1]
                self.assertNotIn("fingerprint_preset", instance.kwargs)
                self.assertNotIn("os", instance.kwargs)
                self.assertNotIn("geoip", instance.kwargs)
                await manager.finish("claude")
                await asyncio.sleep(0)

    async def test_chrome_backend_uses_visible_persistent_installed_browser(
        self,
    ) -> None:
        page = FakePage()
        context = FakeContext(page)
        chromium = FakeChromium(context)
        playwright_manager = FakePlaywrightManager(
            FakePlaywright(chromium)
        )
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory).resolve()
            with (
                patch(
                    "claude_web_api.enrollment.manager.async_playwright",
                    return_value=playwright_manager,
                ),
                patch(
                    "claude_web_api.enrollment.manager._installed_chrome_executable",
                    return_value=r"C:\Program Files\Google\Chrome\chrome.exe",
                ),
            ):
                browser = _ChromeEnrollmentBrowser(profile_path)
                opened = await browser.__aenter__()
                await browser.__aexit__(None, None, None)

        self.assertIs(context, opened)
        self.assertEqual(
            str(profile_path),
            chromium.launch_options["user_data_dir"],
        )
        self.assertIs(chromium.launch_options["headless"], False)
        self.assertIs(chromium.launch_options["no_viewport"], True)
        self.assertEqual(
            r"C:\Program Files\Google\Chrome\chrome.exe",
            chromium.launch_options["executable_path"],
        )
        self.assertNotIn("channel", chromium.launch_options)
        self.assertTrue(context.closed)
        self.assertTrue(playwright_manager.exited)

    async def test_chrome_backend_falls_back_to_installed_channel(
        self,
    ) -> None:
        context = FakeContext(FakePage())
        chromium = FakeChromium(context)
        playwright_manager = FakePlaywrightManager(
            FakePlaywright(chromium)
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "claude_web_api.enrollment.manager.async_playwright",
                    return_value=playwright_manager,
                ),
                patch(
                    "claude_web_api.enrollment.manager._installed_chrome_executable",
                    return_value=None,
                ),
                patch.dict(
                    os.environ,
                    {"GROK_CHROME_CHANNEL": "chrome-beta"},
                ),
            ):
                browser = _ChromeEnrollmentBrowser(
                    Path(directory).resolve()
                )
                await browser.__aenter__()
                await browser.__aexit__(None, None, None)

        self.assertEqual(
            "chrome-beta",
            chromium.launch_options["channel"],
        )
        self.assertNotIn("executable_path", chromium.launch_options)

    async def test_launch_rejects_an_unknown_provider(self) -> None:
        manager = ProfileEnrollmentManager()
        with self.assertRaisesRegex(ValueError, "unsupported profile provider"):
            await manager.launch("bad", "unused", "unknown_web")

    async def test_grok_model_availability_requires_selected_evidence(
        self,
    ) -> None:
        models = _normalize_grok_enrollment_models(
            [
                {
                    "id": "Grok 5",
                    "available": True,
                    "access_status": "available",
                    "selected": False,
                },
                {
                    "id": "Grok 4 Fast",
                    "access_status": "available",
                    "selected": True,
                },
            ]
        )

        self.assertEqual("unverified", models[0]["access_status"])
        self.assertFalse(models[0]["available"])
        self.assertEqual("available", models[1]["access_status"])
        self.assertTrue(models[1]["available"])

    async def test_grok_ambiguous_state_stays_unverified(self) -> None:
        manager = ProfileEnrollmentManager()
        enrollment, page = make_enrollment(
            GROK_WEB_PROVIDER,
            {
                "authenticated": False,
                "authenticationState": "unverified",
                "reason": "auth_not_verified",
                "hasComposer": True,
                "models": [
                    {
                        "id": "Grok 5",
                        "label": "Grok 5",
                        "available": False,
                        "access_status": "unverified",
                        "source": "account_model_selector_dom",
                    }
                ],
                "evidence": {"accountControl": True},
            },
        )
        manager._enrollments[enrollment.profile_id] = enrollment

        snapshot = await manager.inspect(enrollment.profile_id)

        self.assertFalse(snapshot["authenticated"])
        self.assertEqual("unverified", snapshot["status"])
        self.assertEqual("unverified", snapshot["authentication_state"])
        self.assertTrue(snapshot["has_composer"])
        self.assertFalse(snapshot["models"][0]["available"])
        self.assertIsNone(snapshot["account"]["uuid_suffix"])
        probe = page.evaluate_calls[0]
        self.assertNotIn("document.cookie", probe)
        self.assertNotIn("localStorage", probe)
        self.assertNotIn("fetch(", probe)

    async def test_grok_cloudflare_page_is_terminal_provider_block(
        self,
    ) -> None:
        manager = ProfileEnrollmentManager()
        enrollment, page = make_enrollment(
            GROK_WEB_PROVIDER,
            {
                "providerBlocked": True,
                "accessStatus": "access_denied",
                "authenticationState": "unverified",
                "authenticated": False,
                "reason": "cloudflare_challenge",
                "evidence": {"blockPage": True},
            },
        )
        manager._enrollments[enrollment.profile_id] = enrollment

        snapshot = await manager.inspect(enrollment.profile_id)

        self.assertEqual("provider_blocked", snapshot["status"])
        self.assertEqual("access_denied", snapshot["access_status"])
        self.assertTrue(snapshot["provider_blocked"])
        self.assertTrue(snapshot["terminal"])
        self.assertFalse(snapshot["authenticated"])
        self.assertEqual("cloudflare_challenge", snapshot["reason"])
        self.assertEqual([], snapshot["models"])
        self.assertIsNone(snapshot["account"]["uuid_suffix"])
        probe = page.evaluate_calls[0].lower()
        self.assertIn("sorry", probe)
        self.assertIn("attention required", probe)
        self.assertIn("cloudflare", probe)

    async def test_grok_only_keeps_an_observed_uuid(self) -> None:
        manager = ProfileEnrollmentManager()
        enrollment, _ = make_enrollment(
            GROK_WEB_PROVIDER,
            {
                "authenticated": True,
                "authenticationState": "verified",
                "accountUuid": "not-a-uuid",
                "name": "Test User",
                "emailMasked": "te***@example.com",
                "models": [],
            },
        )
        manager._enrollments[enrollment.profile_id] = enrollment

        snapshot = await manager.inspect(enrollment.profile_id)
        identity = await manager.internal_identity(enrollment.profile_id)

        self.assertTrue(snapshot["authenticated"])
        self.assertEqual("authenticated", snapshot["status"])
        self.assertIsNone(snapshot["account"]["uuid_suffix"])
        self.assertEqual(GROK_WEB_PROVIDER, identity["provider"])
        self.assertIsNone(identity["account_uuid"])
        self.assertIsNone(identity["organization_uuid"])

    async def test_grok_project_setup_is_explicitly_not_required(self) -> None:
        manager = ProfileEnrollmentManager()
        enrollment, page = make_enrollment(
            GROK_WEB_PROVIDER,
            {"authenticated": False},
        )
        manager._enrollments[enrollment.profile_id] = enrollment

        result = await manager.ensure_project(
            enrollment.profile_id,
            "unused instructions",
        )

        self.assertEqual("not_required", result["status"])
        self.assertFalse(result["required"])
        self.assertIsNone(result["project_id"])
        self.assertEqual([], page.evaluate_calls)


if __name__ == "__main__":
    unittest.main()
