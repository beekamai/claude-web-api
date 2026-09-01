"""Per-profile outbound proxies.

Each browser profile may carry its own SOCKS5 or HTTP(S) proxy so that two
accounts never share an exit address. Camoufox takes the proxy at launch and
derives WebRTC address and timezone from it, so a proxy that silently fails
would hand the account a mismatched fingerprint rather than an error; the
reachability check here therefore speaks the proxy protocols directly and
reports the exit address it actually got.

Credentials live in the local control config beside the profile they belong
to. They are never returned by the panel API: callers see whether a password
is set, never its value.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

SCHEMES = ("socks5", "http", "https")
SCHEME_ALIASES = {"socks5h": "socks5", "socks": "socks5"}

CHECK_HOST = "api.ipify.org"
CHECK_PORT = 443
CHECK_TIMEOUT_SECONDS = 20.0

_HOST_RE = re.compile(
    r"^(?:"
    r"[0-9A-Fa-f:.]+"
    r"|[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?"
    r")$"
)

_SOCKS5_ERRORS = {
    1: "прокси вернул общую ошибку",
    2: "прокси запретил соединение правилами",
    3: "сеть недоступна со стороны прокси",
    4: "хост недоступен со стороны прокси",
    5: "соединение отклонено целевым хостом",
    6: "истёк TTL",
    7: "прокси не поддерживает эту команду",
    8: "прокси не поддерживает этот тип адреса",
}

DISABLED: dict[str, Any] = {
    "enabled": False,
    "server": "",
    "username": "",
    "password": "",
}


def _split_server(value: str) -> tuple[str, str, int, str, str]:
    """Split a proxy address into scheme, host, port and inline credentials.

    Providers hand out a single ``scheme://user:pass@host:port`` line, so the
    address field accepts that form and lifts the credentials out of it.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("адрес прокси не указан")
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    scheme = SCHEME_ALIASES.get(parts.scheme.lower(), parts.scheme.lower())
    if scheme not in SCHEMES:
        raise ValueError(
            "схема прокси должна быть одной из: " + ", ".join(SCHEMES)
        )
    host = parts.hostname or ""
    if not _HOST_RE.fullmatch(host):
        raise ValueError("некорректный хост прокси")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("некорректный порт прокси") from exc
    if not port:
        raise ValueError("укажите порт прокси")
    return scheme, host, port, parts.username or "", parts.password or ""


def normalize(raw: Any, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a proxy block, keeping a stored password the panel cannot see.

    The panel never receives the password back, so an edit that leaves the
    field untouched arrives without one. Dropping the stored value there would
    quietly break the profile, so an omitted password keeps what was saved.
    """
    kept = previous if isinstance(previous, dict) else {}
    if raw is None:
        return dict(DISABLED) if not kept else _normalized(kept)
    if not isinstance(raw, dict):
        raise ValueError("прокси описывается объектом")

    server = str(raw.get("server", kept.get("server", "")) or "").strip()
    enabled = bool(raw.get("enabled", kept.get("enabled", False)))
    if not server:
        if enabled:
            raise ValueError("адрес прокси не указан")
        return dict(DISABLED)

    scheme, host, port, inline_user, inline_password = _split_server(server)
    username = str(raw.get("username", kept.get("username", "")) or "").strip()
    if "password" in raw:
        password = str(raw.get("password") or "")
    else:
        password = str(kept.get("password", "") or "")
    if inline_user:
        username, password = inline_user, inline_password
    if password and not username:
        raise ValueError("пароль без логина прокси не принимается")
    return {
        "enabled": enabled,
        "server": f"{scheme}://{host}:{port}",
        "username": username[:200],
        "password": password[:400],
    }


def _normalized(stored: dict[str, Any]) -> dict[str, Any]:
    try:
        return normalize(dict(stored))
    except ValueError:
        return dict(DISABLED)


def public(proxy: Any) -> dict[str, Any]:
    """The panel-facing view: everything except the password itself."""
    row = proxy if isinstance(proxy, dict) else {}
    return {
        "enabled": bool(row.get("enabled")),
        "server": str(row.get("server", "") or ""),
        "username": str(row.get("username", "") or ""),
        "password_set": bool(row.get("password")),
    }


def launch_options(proxy: Any) -> dict[str, Any] | None:
    """Camoufox/Playwright proxy options, or None when the profile has none."""
    row = proxy if isinstance(proxy, dict) else {}
    if not row.get("enabled") or not row.get("server"):
        return None
    options: dict[str, Any] = {"server": str(row["server"])}
    username = str(row.get("username", "") or "")
    if username:
        options["username"] = username
        options["password"] = str(row.get("password", "") or "")
    return options


async def _socks5_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    username: str,
    password: str,
) -> None:
    methods = b"\x00\x02" if username else b"\x00"
    writer.write(bytes([5, len(methods)]) + methods)
    await writer.drain()
    greeting = await reader.readexactly(2)
    if greeting[0] != 5:
        raise OSError("это не SOCKS5-прокси")
    if greeting[1] == 0xFF:
        raise OSError(
            "прокси отклонил предложенную аутентификацию"
            + (" (логин/пароль)" if username else " (без логина)")
        )
    if greeting[1] == 2:
        if not username:
            raise OSError("прокси требует логин и пароль")
        credentials = (
            b"\x01"
            + bytes([len(username.encode())])
            + username.encode()
            + bytes([len(password.encode())])
            + password.encode()
        )
        writer.write(credentials)
        await writer.drain()
        answer = await reader.readexactly(2)
        if answer[1] != 0:
            raise OSError("прокси отклонил логин или пароль")
    elif greeting[1] != 0:
        raise OSError("прокси предложил неподдерживаемую аутентификацию")

    target = host.encode()
    writer.write(
        b"\x05\x01\x00\x03"
        + bytes([len(target)])
        + target
        + port.to_bytes(2, "big")
    )
    await writer.drain()
    reply = await reader.readexactly(4)
    if reply[1] != 0:
        raise OSError(_SOCKS5_ERRORS.get(reply[1], "прокси отказал в соединении"))
    if reply[3] == 1:
        await reader.readexactly(4)
    elif reply[3] == 3:
        length = (await reader.readexactly(1))[0]
        await reader.readexactly(length)
    elif reply[3] == 4:
        await reader.readexactly(16)
    await reader.readexactly(2)


async def _http_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    username: str,
    password: str,
) -> None:
    lines = [
        f"CONNECT {host}:{port} HTTP/1.1",
        f"Host: {host}:{port}",
    ]
    if username:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        lines.append(f"Proxy-Authorization: Basic {token}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status.split(" ", 2)
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if code == 407:
        raise OSError("прокси требует другие логин и пароль")
    if code != 200:
        raise OSError(f"прокси ответил на CONNECT: {status.strip() or code}")


async def _exit_address(proxy: dict[str, Any]) -> str:
    scheme, host, port, _, _ = _split_server(str(proxy.get("server") or ""))
    username = str(proxy.get("username", "") or "")
    password = str(proxy.get("password", "") or "")
    reader, writer = await asyncio.open_connection(
        host,
        port,
        ssl=ssl.create_default_context() if scheme == "https" else None,
        server_hostname=host if scheme == "https" else None,
    )
    try:
        if scheme == "socks5":
            await _socks5_connect(
                reader, writer, CHECK_HOST, CHECK_PORT, username, password
            )
        else:
            await _http_connect(
                reader, writer, CHECK_HOST, CHECK_PORT, username, password
            )
        await writer.start_tls(
            ssl.create_default_context(),
            server_hostname=CHECK_HOST,
        )
        writer.write(
            (
                "GET /?format=json HTTP/1.1\r\n"
                f"Host: {CHECK_HOST}\r\n"
                "User-Agent: claude-web-api\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        body = (await reader.read()).split(b"\r\n\r\n", 1)
        payload = (
            json.loads(body[1].decode("utf-8", "replace"))
            if len(body) > 1
            else {}
        )
        address = str(payload.get("ip", "") or "")
        if not address:
            raise OSError("прокси соединился, но ответ проверки не разобрался")
        return address
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass


async def check(proxy: Any) -> dict[str, Any]:
    """Connect through the proxy and report the exit address it gave."""
    row = normalize(proxy if isinstance(proxy, dict) else {})
    if not row["server"]:
        return {"ok": False, "error": "адрес прокси не указан"}
    started = time.monotonic()
    try:
        address = await asyncio.wait_for(
            _exit_address(row),
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return {"ok": False, "error": "прокси не ответил вовремя"}
    except asyncio.IncompleteReadError:
        return {"ok": False, "error": "прокси разорвал соединение"}
    except (OSError, ssl.SSLError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc) or exc.__class__.__name__}
    return {
        "ok": True,
        "exit_ip": address,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
