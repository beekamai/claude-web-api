"""Redaction of text that leaves the bridge.

Local diagnostics stay useful, but account identifiers, cookies, tokens and
key material must never reach an API response, the control panel or the
persistent journal.
"""

from __future__ import annotations

import re
from typing import Any

UUID_TEXT_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
SECRET_QUERY_RE = re.compile(
    r"([?&](?:"
    r"code|token|access_token|refresh_token|id_token|"
    r"api_key|api-key|session|key|secret|credential|auth"
    r")=)[^&\s]+",
    re.IGNORECASE,
)
COOKIE_HEADER_RE = re.compile(
    r"(?im)\b(cookie|set-cookie)(\s*:\s*)[^\r\n]*"
)
SECRET_HEADER_RE = re.compile(
    r"(?im)\b(authorization|x-api-key|api-key)"
    r"(\s*[:=]\s*)[^\r\n,;]+"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"access_token|refresh_token|id_token|api_key|api-key|"
    r"client_secret|secret_key|credential|password|passwd"
    r")(\s*[:=]\s*)[^&\s,;}]+"
)
JSON_SECRET_RE = re.compile(
    r"""(?i)(["'](?:access_token|refresh_token|id_token|api_key|api-key|"""
    r"""client_secret|secret_key|credential|password|passwd)["']\s*:\s*)"""
    r"""(["'])(.*?)\2"""
)
SECRET_TOKEN_RE = re.compile(
    r"(?i)\b("
    r"sk-[a-z0-9_-]{12,}|"
    r"gh[pousr]_[a-z0-9]{12,}|"
    r"AKIA[0-9A-Z]{12,}|"
    r"eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}"
    r")\b"
)
URL_CREDENTIALS_RE = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def sanitize_public_text(value: Any, limit: int = 1_000) -> str:
    text = str(value or "").strip()
    text = UUID_TEXT_RE.sub(
        lambda match: f"…{match.group(0)[-8:]}",
        text,
    )
    text = SECRET_QUERY_RE.sub(r"\1<redacted>", text)
    text = COOKIE_HEADER_RE.sub(r"\1\2<redacted>", text)
    text = SECRET_HEADER_RE.sub(r"\1\2<redacted>", text)
    text = JSON_SECRET_RE.sub(r"\1\2<redacted>\2", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    text = URL_CREDENTIALS_RE.sub(r"\1\2:<redacted>@", text)
    text = SECRET_TOKEN_RE.sub("<redacted-token>", text)
    text = PRIVATE_KEY_RE.sub("<redacted-private-key>", text)
    return text[:limit]


def public_error_message(error: BaseException) -> str:
    """Keep local diagnostics useful without returning account identifiers."""
    return sanitize_public_text(
        str(error).strip() or type(error).__name__
    )

