"""The published endpoint map is part of the contract with clients."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from claude_web_api.app import app

EXPECTED_PATHS = {
    "/",
    "/api/control/behavior",
    "/api/control/events",
    "/api/control/profiles",
    "/api/control/profiles/{profile_id}/activate",
    "/api/control/profiles/{profile_id}/login",
    "/api/control/profiles/{profile_id}/model",
    "/api/control/state",
    "/api/control/telemetry",
    "/api/control/telemetry/settings",
    "/api/control/telemetry/{request_id}",
    "/chat",
    "/control/",
    "/health",
    "/health/live",
    "/health/ready",
    "/new",
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/models",
}


class RouteMapTests(unittest.TestCase):
    """Splitting routers must not silently drop or re-bind an endpoint."""

    def setUp(self) -> None:
        self.spec = TestClient(app).get("/openapi.json").json()

    def test_every_expected_path_is_served(self) -> None:
        self.assertEqual(EXPECTED_PATHS, set(self.spec["paths"]))

    def test_control_state_is_a_get_endpoint(self) -> None:
        self.assertEqual(
            ["get"],
            list(self.spec["paths"]["/api/control/state"]),
        )

    def test_completions_accept_post_only(self) -> None:
        self.assertEqual(
            ["post"],
            list(self.spec["paths"]["/v1/chat/completions"]),
        )


if __name__ == "__main__":
    unittest.main()
