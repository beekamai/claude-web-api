"""Per-profile outbound proxies.

The reason a profile carries a proxy at all is that two accounts must not
share an exit address, and the credentials that unlock it must not leak back
out through the panel. Both are pinned here, along with the wire handshakes
against a local stand-in proxy, because a proxy that fails silently would let
the browser out through this machine's own address.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_web_api.control import proxy
from claude_web_api.control.config import ControlConfig
from claude_web_api.sanitize import public_error_message
from claude_web_api.session.browser import BrowserLifecycleMixin

PASSWORD = "s0cks-pass-never-in-a-response"


class NormalizeTests(unittest.TestCase):
    def test_scheme_host_and_port_are_required(self) -> None:
        for value in ("ftp://host:1080", "socks5://host", "socks5://:1080"):
            with self.assertRaises(ValueError, msg=value):
                proxy.normalize({"enabled": True, "server": value})

    def test_a_bare_address_defaults_to_http(self) -> None:
        row = proxy.normalize({"enabled": True, "server": "10.0.0.7:8080"})
        self.assertEqual("http://10.0.0.7:8080", row["server"])

    def test_socks5h_is_accepted_as_socks5(self) -> None:
        row = proxy.normalize({"enabled": True, "server": "socks5h://p.io:1080"})
        self.assertEqual("socks5://p.io:1080", row["server"])

    def test_credentials_pasted_into_the_address_are_lifted_out(self) -> None:
        """Providers hand out one line; retyping it into three fields loses it."""
        row = proxy.normalize(
            {"enabled": True, "server": f"socks5://mara:{PASSWORD}@p.io:1080"}
        )
        self.assertEqual("socks5://p.io:1080", row["server"])
        self.assertEqual("mara", row["username"])
        self.assertEqual(PASSWORD, row["password"])

    def test_an_edit_without_a_password_keeps_the_stored_one(self) -> None:
        stored = proxy.normalize(
            {
                "enabled": True,
                "server": "socks5://p.io:1080",
                "username": "mara",
                "password": PASSWORD,
            }
        )
        edited = proxy.normalize(
            {"enabled": True, "server": "socks5://p.io:1081", "username": "mara"},
            stored,
        )
        self.assertEqual(PASSWORD, edited["password"])

    def test_an_explicit_empty_password_clears_it(self) -> None:
        stored = {
            "enabled": True,
            "server": "socks5://p.io:1080",
            "username": "mara",
            "password": PASSWORD,
        }
        edited = proxy.normalize({**stored, "password": ""}, stored)
        self.assertEqual("", edited["password"])

    def test_the_public_view_never_carries_the_password(self) -> None:
        view = proxy.public(
            {
                "enabled": True,
                "server": "socks5://p.io:1080",
                "username": "mara",
                "password": PASSWORD,
            }
        )
        self.assertTrue(view["password_set"])
        self.assertNotIn(PASSWORD, repr(view))

    def test_a_disabled_proxy_produces_no_launch_options(self) -> None:
        self.assertIsNone(
            proxy.launch_options(
                {"enabled": False, "server": "socks5://p.io:1080"}
            )
        )


class RedactionTests(unittest.TestCase):
    def test_a_password_in_an_address_never_reaches_a_response(self) -> None:
        """Errors quote the address they failed on, and it can carry the
        password the operator pasted into it."""
        message = public_error_message(
            OSError(f"cannot reach socks5://mara:{PASSWORD}@p.io:1080")
        )
        self.assertNotIn(PASSWORD, message)
        self.assertIn("socks5://mara:<redacted>@p.io:1080", message)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = ControlConfig(Path(self.directory.name) / "control.json")
        self.profile = self.config.create_profile("Второй")

    def test_a_saved_password_never_reaches_the_panel(self) -> None:
        self.config.update_profile(
            self.profile["id"],
            {
                "proxy": {
                    "enabled": True,
                    "server": "socks5://p.io:1080",
                    "username": "mara",
                    "password": PASSWORD,
                }
            },
        )
        self.assertNotIn(PASSWORD, repr(self.config.snapshot()))

    def test_the_session_catalog_carries_the_proxy_the_browser_needs(self) -> None:
        self.config.update_profile(
            self.profile["id"],
            {
                "proxy": {
                    "enabled": True,
                    "server": "socks5://p.io:1080",
                    "username": "mara",
                    "password": PASSWORD,
                }
            },
        )
        row = next(
            item
            for item in self.config.session_profiles()
            if item["id"] == self.profile["id"]
        )
        self.assertEqual(PASSWORD, row["proxy"]["password"])

    def test_each_profile_keeps_its_own_exit(self) -> None:
        other = self.config.create_profile("Третий")
        self.config.update_profile(
            self.profile["id"],
            {"proxy": {"enabled": True, "server": "socks5://one.io:1080"}},
        )
        self.config.update_profile(
            other["id"],
            {"proxy": {"enabled": True, "server": "http://two.io:8080"}},
        )
        servers = {
            row["id"]: row["proxy"]["server"]
            for row in self.config.session_profiles()
        }
        self.assertEqual("socks5://one.io:1080", servers[self.profile["id"]])
        self.assertEqual("http://two.io:8080", servers[other["id"]])

    def test_a_rejected_address_does_not_reach_disk(self) -> None:
        with self.assertRaises(ValueError):
            self.config.update_profile(
                self.profile["id"],
                {"proxy": {"enabled": True, "server": "ftp://p.io:1080"}},
            )
        stored = self.config.profile(self.profile["id"])["proxy"]
        self.assertFalse(stored["enabled"])


class LaunchTests(unittest.TestCase):
    """The browser must actually leave through the profile's proxy."""

    def session(self, spec: dict) -> BrowserLifecycleMixin:
        session = BrowserLifecycleMixin.__new__(BrowserLifecycleMixin)
        session.headless = False
        session._humanize_seconds = 0.0
        session.profile_specs = [spec]
        session.profile_index = 0
        return session

    def test_an_enabled_proxy_reaches_camoufox_with_geoip(self) -> None:
        session = self.session(
            {
                "id": "second",
                "proxy": {
                    "enabled": True,
                    "server": "socks5://p.io:1080",
                    "username": "mara",
                    "password": PASSWORD,
                },
            }
        )
        options = session.launch_options(Path("profile"))
        self.assertEqual("socks5://p.io:1080", options["proxy"]["server"])
        self.assertEqual(PASSWORD, options["proxy"]["password"])
        self.assertTrue(options["geoip"])

    def test_a_profile_without_a_proxy_launches_unchanged(self) -> None:
        options = self.session({"id": "default", "proxy": None}).launch_options(
            Path("profile")
        )
        self.assertNotIn("proxy", options)
        self.assertNotIn("geoip", options)


class FakeSocks5Server:
    """A SOCKS5 endpoint that records the credentials it was offered."""

    def __init__(self, *, password: str | None) -> None:
        self.password = password
        self.seen: tuple[str, str] | None = None
        self.server: asyncio.Server | None = None

    async def start(self) -> tuple[str, int]:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[:2]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            header = await reader.readexactly(2)
            await reader.readexactly(header[1])
            if self.password is None:
                writer.write(b"\x05\x00")
                await writer.drain()
            else:
                writer.write(b"\x05\x02")
                await writer.drain()
                await reader.readexactly(1)
                user = (await reader.readexactly(
                    (await reader.readexactly(1))[0]
                )).decode()
                secret = (await reader.readexactly(
                    (await reader.readexactly(1))[0]
                )).decode()
                self.seen = (user, secret)
                ok = secret == self.password
                writer.write(bytes([1, 0 if ok else 1]))
                await writer.drain()
                if not ok:
                    return
            await reader.readexactly(4)
            await reader.readexactly((await reader.readexactly(1))[0] + 2)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def connect(self, host: str, port: int, user: str, secret: str) -> None:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            await proxy._socks5_connect(
                reader, writer, "claude.ai", 443, user, secret
            )
        finally:
            writer.close()

    async def test_socks5_login_and_password_are_offered(self) -> None:
        """The whole point of the feature: an authenticated SOCKS5 exit."""
        server = FakeSocks5Server(password=PASSWORD)
        host, port = await server.start()
        self.addAsyncCleanup(server.stop)
        await self.connect(host, port, "mara", PASSWORD)
        self.assertEqual(("mara", PASSWORD), server.seen)

    async def test_a_wrong_password_is_reported_as_such(self) -> None:
        server = FakeSocks5Server(password=PASSWORD)
        host, port = await server.start()
        self.addAsyncCleanup(server.stop)
        with self.assertRaisesRegex(OSError, "логин или пароль"):
            await self.connect(host, port, "mara", "wrong")

    async def test_a_proxy_demanding_auth_without_credentials_says_so(
        self,
    ) -> None:
        server = FakeSocks5Server(password=PASSWORD)
        host, port = await server.start()
        self.addAsyncCleanup(server.stop)
        with self.assertRaisesRegex(OSError, "требует логин"):
            await self.connect(host, port, "", "")

    async def test_an_open_proxy_still_works(self) -> None:
        server = FakeSocks5Server(password=None)
        host, port = await server.start()
        self.addAsyncCleanup(server.stop)
        await self.connect(host, port, "", "")

    async def test_http_connect_sends_basic_authorization(self) -> None:
        seen: list[bytes] = []

        async def serve(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        host, port = server.sockets[0].getsockname()[:2]
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)

        reader, writer = await asyncio.open_connection(host, port)
        try:
            await proxy._http_connect(
                reader, writer, "claude.ai", 443, "mara", PASSWORD
            )
        finally:
            writer.close()
        request = seen[0].decode()
        self.assertIn("CONNECT claude.ai:443", request)
        self.assertIn("Proxy-Authorization: Basic ", request)
        self.assertNotIn(PASSWORD, request)

    async def test_a_refusing_proxy_check_reports_a_reason(self) -> None:
        server = await asyncio.start_server(
            lambda reader, writer: writer.close(), "127.0.0.1", 0
        )
        host, port = server.sockets[0].getsockname()[:2]
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        result = await proxy.check(
            {"enabled": True, "server": f"socks5://{host}:{port}"}
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])


if __name__ == "__main__":
    unittest.main()
