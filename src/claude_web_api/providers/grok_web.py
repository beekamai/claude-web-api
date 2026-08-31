"""Fail-closed provider seam for an authenticated grok.com browser profile.

This module intentionally contains no guessed Grok endpoint and no generic
HTTP client. Authentication material remains inside the persistent installed-
Chrome profile. A browser transport may execute a verified request only from the
grok.com page (for example, with ``window.fetch`` in that page's context).

Completions stay disabled until a sanitized protocol descriptor produced from
an observed browser trace has been validated and installed atomically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

from claude_web_api.providers.contracts import (
    ProviderCapabilities,
    ProviderEvent,
    ProviderEventKind,
    ProviderEventSink,
    ProviderHealth,
    ProviderProfileIdentity,
    ProviderToolResult,
    ProviderTurn,
    ProviderTurnRequest,
    ToolContinuation,
)

GROK_WEB_PROVIDER_ID = "grok_web"
GROK_WEB_ORIGIN = "https://grok.com"
GROK_PROTOCOL_SCHEMA = "openclaude.grok_web_protocol.v1"
GROK_PROBE_SCHEMA = "openclaude.grok_web_probe.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPTURE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TRACE_EVENT_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,96}$")
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_PATH_TEMPLATE_RE = re.compile(
    r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/{}/-]{1,510}$"
)
_SENSITIVE_DESCRIPTOR_KEY_RE = re.compile(
    r"(?:authorization|cookie|credential|password|secret|session|"
    r"signature|token|xsrf|csrf)",
    re.IGNORECASE,
)
_TRUSTED_VERIFICATION_MARKER = object()
_MAX_SANITIZED_TRACE_BYTES = 16 * 1024 * 1024


class GrokWebProviderError(RuntimeError):
    """Base error for the fail-closed Grok web adapter."""


class GrokProtocolUnverifiedError(GrokWebProviderError):
    """The provider has no installed, verified browser protocol."""


class GrokProviderNotReadyError(GrokWebProviderError):
    """The browser profile is stopped, unauthenticated, or unhealthy."""


class GrokCapabilityUnavailableError(GrokWebProviderError):
    """The verified browser trace does not support a requested capability."""


class GrokProtocolViolationError(GrokWebProviderError):
    """The browser transport returned data outside verified capabilities."""


class GrokStreamProtocol(str, Enum):
    """Bounded stream decoders supported by a browser transport."""

    NONE = "none"
    SSE = "sse"
    NDJSON = "ndjson"
    JSON_SEQUENCE = "json_sequence"
    READABLE_STREAM = "readable_stream"


class GrokModelEntitlement(str, Enum):
    """Observed account entitlement for a model selector entry."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class GrokModelCatalogEntry:
    """A model whose availability was observed in the authenticated web UI."""

    model_id: str
    display_name: str
    entitlement: GrokModelEntitlement
    evidence_sha256: str
    entitlement_verified: bool = True

    def validated(self) -> "GrokModelCatalogEntry":
        if not _MODEL_ID_RE.fullmatch(str(self.model_id)):
            raise ValueError("invalid Grok model id")
        if not str(self.display_name).strip():
            raise ValueError("Grok model display name must not be empty")
        if not isinstance(self.entitlement, GrokModelEntitlement):
            raise ValueError("invalid Grok model entitlement")
        if not isinstance(self.entitlement_verified, bool):
            raise ValueError("entitlement_verified must be a boolean")
        if not _valid_sha256(self.evidence_sha256):
            raise ValueError("model evidence_sha256 must be a SHA-256 digest")
        return self


@dataclass(frozen=True)
class GrokWebProtocolDescriptor:
    """Sanitized facts verified from a grok.com browser trace.

    No request values, response text, cookies, headers, CSRF values, account
    identifiers, or bearer material are representable by this schema.
    """

    schema: str
    verified: bool
    verified_at: str
    evidence_sha256: str
    origin: str
    endpoint_path: str
    method: str
    stream_protocol: GrokStreamProtocol
    streaming_verified: bool
    stream_evidence_sha256: str | None
    thinking_verified: bool
    thinking_evidence_sha256: str | None
    tool_continuation: ToolContinuation
    custom_tools_accepted: bool
    tool_call_observed: bool
    tool_result_continuation_observed: bool
    tool_evidence_sha256: str | None
    _verification_marker: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_sanitized_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "GrokWebProtocolDescriptor":
        """Parse a strict allow-list so accidental secrets cannot be loaded."""

        if not isinstance(raw, Mapping):
            raise TypeError("Grok protocol descriptor must be a mapping")
        allowed = {
            "schema",
            "verified",
            "verified_at",
            "evidence_sha256",
            "origin",
            "endpoint_path",
            "method",
            "stream_protocol",
            "streaming_verified",
            "stream_evidence_sha256",
            "thinking_verified",
            "thinking_evidence_sha256",
            "tool_continuation",
            "custom_tools_accepted",
            "tool_call_observed",
            "tool_result_continuation_observed",
            "tool_evidence_sha256",
        }
        keys = {str(key) for key in raw}
        unknown = keys - allowed
        missing = allowed - keys
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown Grok descriptor fields: {names}")
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"missing Grok descriptor fields: {names}")
        for key in keys:
            if _SENSITIVE_DESCRIPTOR_KEY_RE.search(key):
                raise ValueError(
                    f"sensitive field is forbidden in Grok descriptor: {key}"
                )

        try:
            stream_protocol = GrokStreamProtocol(
                str(raw["stream_protocol"])
            )
            tool_continuation = ToolContinuation(
                str(raw["tool_continuation"])
            )
        except ValueError as exc:
            raise ValueError("unsupported Grok protocol capability") from exc

        descriptor = cls(
            schema=_required_text(raw["schema"], "schema"),
            verified=_required_bool(raw["verified"], "verified"),
            verified_at=_required_text(raw["verified_at"], "verified_at"),
            evidence_sha256=_required_text(
                raw["evidence_sha256"],
                "evidence_sha256",
            ),
            origin=_required_text(raw["origin"], "origin"),
            endpoint_path=_required_text(
                raw["endpoint_path"],
                "endpoint_path",
            ),
            method=_required_text(raw["method"], "method"),
            stream_protocol=stream_protocol,
            streaming_verified=_required_bool(
                raw["streaming_verified"],
                "streaming_verified",
            ),
            stream_evidence_sha256=_optional_text(
                raw["stream_evidence_sha256"],
                "stream_evidence_sha256",
            ),
            thinking_verified=_required_bool(
                raw["thinking_verified"],
                "thinking_verified",
            ),
            thinking_evidence_sha256=_optional_text(
                raw["thinking_evidence_sha256"],
                "thinking_evidence_sha256",
            ),
            tool_continuation=tool_continuation,
            custom_tools_accepted=_required_bool(
                raw["custom_tools_accepted"],
                "custom_tools_accepted",
            ),
            tool_call_observed=_required_bool(
                raw["tool_call_observed"],
                "tool_call_observed",
            ),
            tool_result_continuation_observed=_required_bool(
                raw["tool_result_continuation_observed"],
                "tool_result_continuation_observed",
            ),
            tool_evidence_sha256=_optional_text(
                raw["tool_evidence_sha256"],
                "tool_evidence_sha256",
            ),
        )
        return descriptor.validated()

    def validated(self) -> "GrokWebProtocolDescriptor":
        if self.schema != GROK_PROTOCOL_SCHEMA:
            raise ValueError("unsupported Grok protocol descriptor schema")
        if self.verified is not True:
            raise GrokProtocolUnverifiedError(
                "Grok descriptor is not marked verified"
            )
        _validate_timestamp(self.verified_at)
        if not _valid_sha256(self.evidence_sha256):
            raise ValueError("evidence_sha256 must be a SHA-256 digest")
        if self.origin != GROK_WEB_ORIGIN:
            raise ValueError("Grok transport is restricted to grok.com")
        _validate_endpoint_path(self.endpoint_path)
        if self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("unsupported verified Grok request method")
        if not isinstance(self.stream_protocol, GrokStreamProtocol):
            raise ValueError("invalid Grok stream protocol")
        if not isinstance(self.streaming_verified, bool):
            raise ValueError("streaming_verified must be a boolean")
        if self.streaming_verified:
            if self.stream_protocol is GrokStreamProtocol.NONE:
                raise ValueError(
                    "verified streaming requires an observed stream protocol"
                )
            if not _valid_sha256(self.stream_evidence_sha256):
                raise ValueError(
                    "verified streaming requires stream evidence"
                )
        elif (
            self.stream_protocol is not GrokStreamProtocol.NONE
            or self.stream_evidence_sha256 is not None
        ):
            raise ValueError(
                "unverified streaming must use the none stream protocol"
            )

        if not isinstance(self.thinking_verified, bool):
            raise ValueError("thinking_verified must be a boolean")
        if self.thinking_verified:
            if not _valid_sha256(self.thinking_evidence_sha256):
                raise ValueError(
                    "verified thinking requires thinking evidence"
                )
        elif self.thinking_evidence_sha256 is not None:
            raise ValueError(
                "unverified thinking cannot include thinking evidence"
            )

        if not isinstance(self.tool_continuation, ToolContinuation):
            raise ValueError("invalid Grok tool continuation mode")
        tool_flags = (
            self.custom_tools_accepted,
            self.tool_call_observed,
            self.tool_result_continuation_observed,
        )
        if any(not isinstance(value, bool) for value in tool_flags):
            raise ValueError("tool evidence flags must be booleans")
        if self.tool_continuation is not ToolContinuation.UNSUPPORTED:
            if not all(tool_flags):
                raise ValueError(
                    "tool continuation requires schema, call, and result "
                    "evidence"
                )
            if not _valid_sha256(self.tool_evidence_sha256):
                raise ValueError(
                    "verified tool continuation requires tool evidence"
                )
        elif any(tool_flags) and not _valid_sha256(
            self.tool_evidence_sha256
        ):
            raise ValueError("partial tool observations require tool evidence")
        elif not any(tool_flags) and self.tool_evidence_sha256 is not None:
            raise ValueError(
                "tool evidence digest requires an observed tool behavior"
            )
        return self


class GrokProtocolVerifier:
    """Attest a descriptor from one complete sanitized probe capture.

    Structural descriptor validation alone is intentionally insufficient:
    callers cannot turn arbitrary booleans and digest-shaped strings into an
    executable browser protocol.  The verifier checks the exact JSONL capture,
    binds its SHA-256 to the descriptor, and adds a non-serializable in-process
    attestation consumed by :meth:`GrokWebProvider.load_verified_protocol`.
    """

    @classmethod
    def verify_sanitized_trace(
        cls,
        raw: Mapping[str, Any],
        *,
        trace_jsonl: bytes | str,
    ) -> GrokWebProtocolDescriptor:
        descriptor = GrokWebProtocolDescriptor.from_sanitized_mapping(raw)
        trace_digest = _verify_sanitized_probe_trace(
            trace_jsonl,
            descriptor=descriptor,
        )
        if trace_digest != descriptor.evidence_sha256:
            raise ValueError(
                "Grok descriptor evidence_sha256 does not match the "
                "sanitized probe capture"
            )
        for enabled, evidence, capability in (
            (
                descriptor.streaming_verified,
                descriptor.stream_evidence_sha256,
                "stream",
            ),
            (
                descriptor.thinking_verified,
                descriptor.thinking_evidence_sha256,
                "thinking",
            ),
            (
                descriptor.tool_continuation
                is not ToolContinuation.UNSUPPORTED,
                descriptor.tool_evidence_sha256,
                "tool",
            ),
        ):
            if enabled and evidence != trace_digest:
                raise ValueError(
                    f"Grok {capability} evidence is not bound to the "
                    "sanitized probe capture"
                )
        object.__setattr__(
            descriptor,
            "_verification_marker",
            _TRUSTED_VERIFICATION_MARKER,
        )
        return descriptor


@runtime_checkable
class GrokBrowserTransport(Protocol):
    """Browser-owned transport; auth is never returned to Python callers.

    Implementations own one browser/context/page for the supplied persistent
    profile and execute requests from the grok.com page.  The interface has no
    cookie, auth-header, CSRF-token, or arbitrary-URL escape hatch.
    """

    async def start_browser(
        self,
        *,
        profile_path: Path,
        profile_id: str,
        origin: str,
    ) -> None:
        ...

    async def stop_browser(self) -> None:
        ...

    async def new_browser_conversation(self) -> None:
        ...

    def browser_health(self) -> ProviderHealth:
        ...

    def authenticated_profile_identity(
        self,
    ) -> ProviderProfileIdentity | None:
        ...

    def observed_model_catalog(
        self,
    ) -> Sequence[GrokModelCatalogEntry]:
        ...

    async def complete_same_origin(
        self,
        request: ProviderTurnRequest,
        *,
        descriptor: GrokWebProtocolDescriptor,
        event_sink: ProviderEventSink | None,
    ) -> ProviderTurn:
        ...

    async def continue_same_origin(
        self,
        results: Sequence[ProviderToolResult],
        *,
        descriptor: GrokWebProtocolDescriptor,
        timeout_seconds: float,
        client_session_id: str | None,
        event_sink: ProviderEventSink | None,
    ) -> ProviderTurn:
        ...


class GrokWebProvider:
    """Provider-neutral adapter around one persistent Grok Chrome profile."""

    def __init__(
        self,
        transport: GrokBrowserTransport,
        *,
        profile_id: str,
        profile_path: str | Path,
    ) -> None:
        if not isinstance(transport, GrokBrowserTransport):
            raise TypeError("transport does not implement GrokBrowserTransport")
        normalized_profile_id = str(profile_id).strip()
        if not _PROFILE_ID_RE.fullmatch(normalized_profile_id):
            raise ValueError("invalid Grok profile id")

        self._transport = transport
        self._profile_id = normalized_profile_id
        self._profile_path = _dedicated_profile_path(profile_path)
        self._protocol_lock = threading.RLock()
        self._protocol: GrokWebProtocolDescriptor | None = None
        self._protocol_revision = 0
        self._lifecycle_lock = asyncio.Lock()
        self._started = False

    @property
    def profile_path(self) -> Path:
        return self._profile_path

    @property
    def protocol_revision(self) -> int:
        with self._protocol_lock:
            return self._protocol_revision

    @property
    def protocol_descriptor(self) -> GrokWebProtocolDescriptor | None:
        with self._protocol_lock:
            return self._protocol

    def load_verified_protocol(
        self,
        descriptor: GrokWebProtocolDescriptor | Mapping[str, Any],
    ) -> int:
        """Install only a descriptor attested from a sanitized probe trace."""

        candidate = (
            GrokWebProtocolDescriptor.from_sanitized_mapping(descriptor)
            if isinstance(descriptor, Mapping)
            else descriptor
        )
        if not isinstance(candidate, GrokWebProtocolDescriptor):
            raise TypeError(
                "descriptor must be a GrokWebProtocolDescriptor or mapping"
            )
        candidate = candidate.validated()
        if (
            candidate._verification_marker
            is not _TRUSTED_VERIFICATION_MARKER
        ):
            raise GrokProtocolUnverifiedError(
                "Grok protocol descriptor must be produced by "
                "GrokProtocolVerifier from a complete sanitized probe trace"
            )
        with self._protocol_lock:
            self._protocol = candidate
            self._protocol_revision += 1
            return self._protocol_revision

    @property
    def capabilities(self) -> ProviderCapabilities:
        descriptor = self.protocol_descriptor
        if descriptor is None:
            return ProviderCapabilities(
                tool_continuation=ToolContinuation.UNSUPPORTED,
                streaming=False,
                thinking=False,
                profiles=True,
            )
        return ProviderCapabilities(
            tool_continuation=descriptor.tool_continuation,
            streaming=descriptor.streaming_verified,
            thinking=descriptor.thinking_verified,
            profiles=True,
        )

    @property
    def profile_identity(self) -> ProviderProfileIdentity | None:
        if not self._started:
            return None
        try:
            identity = self._transport.authenticated_profile_identity()
        except Exception:
            return None
        if identity is None:
            return None
        if (
            identity.provider != GROK_WEB_PROVIDER_ID
            or identity.profile_id != self._profile_id
            or not str(identity.display_name).strip()
        ):
            return None
        return identity

    @property
    def model_catalog(self) -> tuple[GrokModelCatalogEntry, ...]:
        """Return only entries backed by current account-entitlement evidence."""

        if self.profile_identity is None:
            return ()
        try:
            observed = self._transport.observed_model_catalog()
        except Exception:
            return ()
        verified: list[GrokModelCatalogEntry] = []
        seen: set[str] = set()
        for entry in observed:
            if not isinstance(entry, GrokModelCatalogEntry):
                continue
            try:
                entry.validated()
            except (TypeError, ValueError):
                continue
            if not entry.entitlement_verified or entry.model_id in seen:
                continue
            seen.add(entry.model_id)
            verified.append(entry)
        return tuple(verified)

    def selectable_models(self) -> tuple[str, ...]:
        return tuple(
            entry.model_id
            for entry in self.model_catalog
            if entry.entitlement is GrokModelEntitlement.AVAILABLE
        )

    def health(self) -> ProviderHealth:
        if not self._started:
            return ProviderHealth(
                live=False,
                ready=False,
                phase="stopped",
            )
        try:
            browser = self._transport.browser_health()
        except Exception as exc:
            return ProviderHealth(
                live=False,
                ready=False,
                phase="health_error",
                detail=str(exc),
            )
        if not isinstance(browser, ProviderHealth):
            return ProviderHealth(
                live=False,
                ready=False,
                phase="health_error",
                detail="Browser transport returned invalid health",
            )
        if not browser.live:
            return ProviderHealth(
                live=False,
                ready=False,
                phase=browser.phase,
                detail=browser.detail,
            )
        if self.profile_identity is None:
            return ProviderHealth(
                live=True,
                ready=False,
                phase="authentication_required",
                detail="Log in to grok.com in this Chrome profile",
            )
        if self.protocol_descriptor is None:
            return ProviderHealth(
                live=True,
                ready=False,
                phase="protocol_unverified",
                detail=(
                    "Capture and install a sanitized verified Grok web "
                    "protocol descriptor"
                ),
            )
        if not browser.ready:
            return ProviderHealth(
                live=True,
                ready=False,
                phase=browser.phase,
                detail=browser.detail,
            )
        return ProviderHealth(
            live=True,
            ready=True,
            phase=browser.phase,
            detail=browser.detail,
        )

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            await self._transport.start_browser(
                profile_path=self._profile_path,
                profile_id=self._profile_id,
                origin=GROK_WEB_ORIGIN,
            )
            self._started = True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                return
            try:
                await self._transport.stop_browser()
            finally:
                self._started = False

    async def new_conversation(self) -> None:
        self._require_started()
        await self._transport.new_browser_conversation()

    async def complete(
        self,
        request: ProviderTurnRequest,
        *,
        event_sink: ProviderEventSink | None = None,
    ) -> ProviderTurn:
        descriptor = self._require_verified_protocol()
        self._require_ready()
        if request.tools and (
            descriptor.tool_continuation is ToolContinuation.UNSUPPORTED
        ):
            raise GrokCapabilityUnavailableError(
                "Grok tools are disabled until tool-call and continuation "
                "behavior is verified"
            )
        if (
            request.reasoning_mode not in {"auto", "off"}
            and not descriptor.thinking_verified
        ):
            raise GrokCapabilityUnavailableError(
                "Grok thinking mode has not been verified"
            )
        sink = self._verified_event_sink(event_sink, descriptor)
        turn = await self._transport.complete_same_origin(
            request,
            descriptor=descriptor,
            event_sink=sink,
        )
        return _validate_turn_against_descriptor(turn, descriptor)

    async def continue_with_tool_results(
        self,
        results: Sequence[ProviderToolResult],
        *,
        timeout_seconds: float = 300.0,
        client_session_id: str | None = None,
        event_sink: ProviderEventSink | None = None,
    ) -> ProviderTurn:
        descriptor = self._require_verified_protocol()
        self._require_ready()
        if descriptor.tool_continuation is ToolContinuation.UNSUPPORTED:
            raise GrokCapabilityUnavailableError(
                "Grok tool-result continuation has not been verified"
            )
        sink = self._verified_event_sink(event_sink, descriptor)
        turn = await self._transport.continue_same_origin(
            results,
            descriptor=descriptor,
            timeout_seconds=timeout_seconds,
            client_session_id=client_session_id,
            event_sink=sink,
        )
        return _validate_turn_against_descriptor(turn, descriptor)

    def _require_started(self) -> None:
        if not self._started:
            raise GrokProviderNotReadyError(
                "Grok browser profile is not started"
            )

    def _require_ready(self) -> None:
        self._require_started()
        health = self.health()
        if not health.ready:
            raise GrokProviderNotReadyError(
                f"Grok browser profile is not ready: {health.phase}"
            )

    def _require_verified_protocol(self) -> GrokWebProtocolDescriptor:
        descriptor = self.protocol_descriptor
        if descriptor is None:
            raise GrokProtocolUnverifiedError(
                "Grok web protocol is unverified; completion is disabled"
            )
        return descriptor

    @staticmethod
    def _verified_event_sink(
        event_sink: ProviderEventSink | None,
        descriptor: GrokWebProtocolDescriptor,
    ) -> ProviderEventSink | None:
        if event_sink is None or not descriptor.streaming_verified:
            return None

        def guarded(event: ProviderEvent) -> None:
            if not isinstance(event, ProviderEvent):
                raise GrokProtocolViolationError(
                    "Grok transport emitted an invalid provider event"
                )
            if (
                event.kind is ProviderEventKind.THINKING_DELTA
                and not descriptor.thinking_verified
            ):
                raise GrokProtocolViolationError(
                    "Grok transport emitted unverified thinking"
                )
            event_sink(event)

        return guarded


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _verify_sanitized_probe_trace(
    trace_jsonl: bytes | str,
    *,
    descriptor: GrokWebProtocolDescriptor,
) -> str:
    """Validate the probe envelope and return the digest of its exact bytes."""
    if isinstance(trace_jsonl, str):
        raw = trace_jsonl.encode("utf-8")
    elif isinstance(trace_jsonl, bytes):
        raw = trace_jsonl
    else:
        raise TypeError("sanitized Grok trace must be bytes or UTF-8 text")
    if not raw:
        raise ValueError("sanitized Grok trace must not be empty")
    if len(raw) > _MAX_SANITIZED_TRACE_BYTES:
        raise ValueError("sanitized Grok trace exceeds the verifier limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("sanitized Grok trace must be valid UTF-8") from exc

    capture_id: str | None = None
    events: list[str] = []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid sanitized Grok trace JSONL at line {line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"sanitized Grok trace line {line_number} is not an object"
            )
        if record.get("schema") != GROK_PROBE_SCHEMA:
            raise ValueError(
                f"unsupported sanitized Grok trace schema at line {line_number}"
            )
        observed_capture_id = str(record.get("capture_id") or "")
        if not _CAPTURE_ID_RE.fullmatch(observed_capture_id):
            raise ValueError(
                f"invalid sanitized Grok capture id at line {line_number}"
            )
        if capture_id is None:
            capture_id = observed_capture_id
        elif observed_capture_id != capture_id:
            raise ValueError(
                "sanitized Grok trace mixes multiple probe captures"
            )
        event = str(record.get("event") or "")
        if not _TRACE_EVENT_RE.fullmatch(event):
            raise ValueError(
                f"invalid sanitized Grok trace event at line {line_number}"
            )
        events.append(event)
        records.append(record)

    required_events = {
        "probe_started",
        "browser_ready",
        "login_status",
        "capture_armed",
        "capture_disarmed",
        "probe_finished",
    }
    if not required_events.issubset(events):
        missing = ", ".join(sorted(required_events - set(events)))
        raise GrokProtocolUnverifiedError(
            f"sanitized Grok trace is incomplete; missing: {missing}"
        )
    if events[-1] != "probe_finished":
        raise GrokProtocolUnverifiedError(
            "sanitized Grok trace did not finish cleanly"
        )
    terminal_errors = {
        "probe_error",
        "probe_cancelled",
        "browser_close_error",
    }
    if terminal_errors.intersection(events):
        raise GrokProtocolUnverifiedError(
            "sanitized Grok trace contains a probe or browser error"
        )

    def matching_endpoint(record: Mapping[str, Any]) -> bool:
        url = record.get("url")
        return bool(
            isinstance(url, Mapping)
            and url.get("origin") == descriptor.origin
            and url.get("path_template") == descriptor.endpoint_path
        )

    matching_requests = [
        record
        for record in records
        if record.get("event") == "request"
        and matching_endpoint(record)
        and str(record.get("method") or "").upper() == descriptor.method
    ]
    matching_responses = [
        record
        for record in records
        if record.get("event") == "response"
        and matching_endpoint(record)
        and isinstance(record.get("status"), int)
        and 200 <= int(record["status"]) < 300
    ]
    if not matching_requests or not matching_responses:
        raise GrokProtocolUnverifiedError(
            "sanitized Grok trace does not verify the descriptor endpoint"
        )
    if descriptor.streaming_verified:
        stream_protocol = descriptor.stream_protocol.value
        stream_open = any(
            record.get("event") == "stream_open"
            and record.get("protocol") == stream_protocol
            and matching_endpoint(record)
            for record in records
        )
        stream_complete = any(
            record.get("event") == "stream_complete"
            and matching_endpoint(record)
            for record in records
        )
        if not stream_open or not stream_complete:
            raise GrokProtocolUnverifiedError(
                "sanitized Grok trace does not verify stream completion"
            )
    if descriptor.thinking_verified and not any(
        record.get("event") == "stream_frame"
        and isinstance(record.get("data_shape"), Mapping)
        and matching_endpoint(record)
        for record in records
    ):
        raise GrokProtocolUnverifiedError(
            "sanitized Grok trace has no structured frame for thinking "
            "verification"
        )
    if (
        descriptor.tool_continuation is not ToolContinuation.UNSUPPORTED
        and len(matching_requests) < 2
    ):
        raise GrokProtocolUnverifiedError(
            "sanitized Grok trace does not contain a continuation request"
        )
    return hashlib.sha256(raw).hexdigest()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _required_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("verified_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("verified_at must include a timezone")


def _validate_endpoint_path(value: str) -> None:
    if not _PATH_TEMPLATE_RE.fullmatch(value):
        raise ValueError("invalid sanitized Grok endpoint path template")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or ".." in value.split("/")
        or "<" in value
        or ">" in value
    ):
        raise ValueError("Grok endpoint must be a sanitized relative path")


def _dedicated_profile_path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    resolved = raw.resolve(strict=False)
    if resolved.parent == resolved:
        raise ValueError("Grok profile path cannot be a filesystem root")
    try:
        if resolved == Path.home().resolve():
            raise ValueError("Grok profile path cannot be the home directory")
    except RuntimeError:
        # A platform-specific home-resolution failure must not weaken the root
        # check above; the explicit path still remains dedicated by ownership.
        pass
    return resolved


def _validate_turn_against_descriptor(
    turn: ProviderTurn,
    descriptor: GrokWebProtocolDescriptor,
) -> ProviderTurn:
    if not isinstance(turn, ProviderTurn):
        raise GrokProtocolViolationError(
            "Grok transport returned an invalid provider turn"
        )
    if turn.tool_uses and (
        descriptor.tool_continuation is ToolContinuation.UNSUPPORTED
    ):
        raise GrokProtocolViolationError(
            "Grok transport returned unverified tool calls"
        )
    if turn.thinking is not None and not descriptor.thinking_verified:
        raise GrokProtocolViolationError(
            "Grok transport returned unverified thinking"
        )
    return turn


__all__ = [
    "GROK_PROBE_SCHEMA",
    "GROK_PROTOCOL_SCHEMA",
    "GROK_WEB_ORIGIN",
    "GROK_WEB_PROVIDER_ID",
    "GrokBrowserTransport",
    "GrokCapabilityUnavailableError",
    "GrokModelCatalogEntry",
    "GrokModelEntitlement",
    "GrokProtocolUnverifiedError",
    "GrokProtocolVerifier",
    "GrokProtocolViolationError",
    "GrokProviderNotReadyError",
    "GrokStreamProtocol",
    "GrokWebProtocolDescriptor",
    "GrokWebProvider",
    "GrokWebProviderError",
]
