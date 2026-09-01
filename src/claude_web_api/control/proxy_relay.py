"""A local SOCKS5 relay in front of a proxy that needs a password.

Firefox, and therefore Playwright and Camoufox, refuse SOCKS5 proxies that
require username/password authentication: ``launch_persistent_context``
fails with "Browser does not support socks5 proxy authentication". The
relay listens on the loopback interface without authentication, performs
the authenticated handshake with the real proxy itself, and then pipes
bytes both ways. The browser is handed the loopback address, so the
credentials never leave this process.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_web_api.control.proxy import _split_server

_NO_AUTH = b"\x05\x00"
_GENERAL_FAILURE = b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00"
_PIPE_CHUNK = 64 * 1024


def needs_relay(proxy: dict[str, Any] | None) -> bool:
    """Only an authenticated SOCKS5 proxy needs the relay."""
    row = proxy if isinstance(proxy, dict) else {}
    if not row.get("enabled") or not row.get("server") or not row.get("username"):
        return False
    scheme, _, _, _, _ = _split_server(str(row["server"]))
    return scheme == "socks5"


async def _read_client_request(reader: asyncio.StreamReader) -> bytes:
    """Return the client's CONNECT request verbatim, so any address type
    it used is forwarded unchanged."""
    head = await reader.readexactly(4)
    if head[0] != 5 or head[1] != 1:
        raise OSError("only SOCKS5 CONNECT is relayed")
    atyp = head[3]
    if atyp == 1:
        rest = await reader.readexactly(4 + 2)
    elif atyp == 3:
        length = await reader.readexactly(1)
        rest = length + await reader.readexactly(length[0] + 2)
    elif atyp == 4:
        rest = await reader.readexactly(16 + 2)
    else:
        raise OSError("unsupported SOCKS5 address type")
    return head + rest


async def _read_upstream_reply(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readexactly(4)
    atyp = head[3]
    if atyp == 1:
        rest = await reader.readexactly(4 + 2)
    elif atyp == 3:
        length = await reader.readexactly(1)
        rest = length + await reader.readexactly(length[0] + 2)
    elif atyp == 4:
        rest = await reader.readexactly(16 + 2)
    else:
        rest = b""
    return head + rest


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(_PIPE_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class Socks5Relay:
    """One loopback listener bound to one upstream proxy."""

    def __init__(self, proxy: dict[str, Any]) -> None:
        _, self.host, self.port, _, _ = _split_server(str(proxy["server"]))
        self.username = str(proxy.get("username", "") or "")
        self.password = str(proxy.get("password", "") or "")
        self._server: asyncio.AbstractServer | None = None
        self.listen_port: int | None = None

    @property
    def server_url(self) -> str:
        return f"socks5://127.0.0.1:{self.listen_port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.listen_port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            pass

    async def _authenticate_upstream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(b"\x05\x02\x00\x02")
        await writer.drain()
        greeting = await reader.readexactly(2)
        if greeting[1] == 2:
            user = self.username.encode()
            secret = self.password.encode()
            writer.write(
                b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret
            )
            await writer.drain()
            answer = await reader.readexactly(2)
            if answer[1] != 0:
                raise OSError("upstream proxy rejected the credentials")
        elif greeting[1] != 0:
            raise OSError("upstream proxy offered no usable authentication")

    async def _serve(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            greeting = await client_reader.readexactly(2)
            await client_reader.readexactly(greeting[1])
            client_writer.write(_NO_AUTH)
            await client_writer.drain()
            request = await _read_client_request(client_reader)

            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.host, self.port
            )
            await self._authenticate_upstream(upstream_reader, upstream_writer)
            upstream_writer.write(request)
            await upstream_writer.drain()
            reply = await _read_upstream_reply(upstream_reader)
            client_writer.write(reply)
            await client_writer.drain()
            if reply[1] != 0:
                return
            await asyncio.gather(
                _pipe(client_reader, upstream_writer),
                _pipe(upstream_reader, client_writer),
            )
        except (OSError, asyncio.IncompleteReadError, ValueError):
            try:
                client_writer.write(_GENERAL_FAILURE)
                await client_writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            for writer in (client_writer, upstream_writer):
                if writer is not None:
                    try:
                        writer.close()
                    except OSError:
                        pass


async def open_relay(proxy: dict[str, Any] | None) -> Socks5Relay | None:
    """Start a relay for the profile's proxy when the browser needs one."""
    if not needs_relay(proxy):
        return None
    relay = Socks5Relay(proxy)  # type: ignore[arg-type]
    await relay.start()
    return relay


def browser_proxy(
    proxy: dict[str, Any] | None,
    relay: Socks5Relay | None,
) -> dict[str, Any] | None:
    """Launch options for the browser: the relay's address when one runs."""
    from claude_web_api.control.proxy import launch_options

    if relay is not None:
        return {"server": relay.server_url}
    return launch_options(proxy)
