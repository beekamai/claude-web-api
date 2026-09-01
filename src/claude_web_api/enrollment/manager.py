"""Visible, provider-specific enrollment for browser-account providers."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import async_playwright

from claude_web_api.control import proxy_relay

MODEL_ID_RE = re.compile(
    r"claude-(?:opus|sonnet|haiku)-[a-z0-9][a-z0-9._-]*",
    re.I,
)
GROK_MODEL_RE = re.compile(
    r"\bgrok(?:[\s._-]*[a-z0-9]+){0,4}\b",
    re.I,
)
UUID_TEXT_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
CLAUDE_WEB_PROVIDER = "claude_web"
GROK_WEB_PROVIDER = "grok_web"
PROVIDER_ENTRY_URLS = {
    CLAUDE_WEB_PROVIDER: "https://claude.ai/new",
    GROK_WEB_PROVIDER: "https://grok.com/",
}


def _installed_chrome_executable() -> str | None:
    """Resolve a real Chrome binary without falling back to bundled Chromium."""
    configured = os.getenv("GROK_CHROME_EXECUTABLE")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(
            "GROK_CHROME_EXECUTABLE does not point to an installed browser"
        )

    candidates: list[Path] = []
    if os.name == "nt":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.getenv(variable)
            if root:
                candidates.append(
                    Path(root)
                    / "Google"
                    / "Chrome"
                    / "Application"
                    / "chrome.exe"
                )
    elif sys.platform == "darwin":
        candidates.append(
            Path(
                "/Applications/Google Chrome.app/Contents/MacOS/"
                "Google Chrome"
            )
        )
    else:
        candidates.extend(
            (
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/opt/google/chrome/google-chrome"),
            )
        )

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


class _ChromeEnrollmentBrowser:
    """Lifecycle adapter for one visible, persistent installed-Chrome context."""

    def __init__(
        self,
        profile_path: Path,
        outbound_proxy: dict[str, Any] | None = None,
    ) -> None:
        self.profile_path = profile_path
        self.outbound_proxy = outbound_proxy
        self._playwright_manager: Any = None
        self._playwright: Any = None
        self._context: Any = None

    async def __aenter__(self) -> Any:
        self._playwright_manager = async_playwright()
        self._playwright = await self._playwright_manager.__aenter__()
        executable = _installed_chrome_executable()
        launch_options: dict[str, Any] = {
            "user_data_dir": str(self.profile_path),
            "headless": False,
            "no_viewport": True,
        }
        if self.outbound_proxy:
            launch_options["proxy"] = self.outbound_proxy
        if executable:
            launch_options["executable_path"] = executable
        else:
            # `channel=chrome` resolves the system Chrome installation.  It
            # intentionally does not use Playwright's bundled Chromium.
            launch_options["channel"] = (
                os.getenv("GROK_CHROME_CHANNEL") or "chrome"
            )
        try:
            self._context = (
                await self._playwright.chromium.launch_persistent_context(
                    **launch_options
                )
            )
            return self._context
        except BaseException:
            await self._playwright_manager.__aexit__(None, None, None)
            self._playwright_manager = None
            self._playwright = None
            raise

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = None
            if self._playwright_manager is not None:
                await self._playwright_manager.__aexit__(*exc_info)
            self._playwright_manager = None
            self._playwright = None


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    local, domain = value.rsplit("@", 1)
    visible = local[:2] if len(local) > 1 else local[:1]
    return f"{visible}***@{domain}"


def _normalize_enrollment_models(value: Any) -> list[dict[str, Any]]:
    """Keep account selector entitlement separate from the bootstrap catalog."""
    if not isinstance(value, list):
        return []

    models: list[dict[str, Any]] = []
    for raw_model in value:
        if not isinstance(raw_model, dict):
            continue
        model_id = str(raw_model.get("id") or "").strip()
        if not model_id:
            continue

        source = str(raw_model.get("source") or "")
        section = str(raw_model.get("section") or "")
        inactive = bool(raw_model.get("inactive"))
        disabled_reason = raw_model.get("disabled_reason")
        if source == "bootstrap_catalog":
            # claude_ai_bootstrap_models_config describes the product catalog.
            # It does not prove that this authenticated account may invoke a
            # model, even when a bootstrap row says `available: true`.
            available = False
            catalog_available = False
            access_status = "unverified"
            disabled_reason = "catalog_only"
        else:
            source = "account_model_selector"
            catalog_available = bool(
                raw_model.get("available", True) is not False
                and not inactive
                and section not in {"deprecated", "inactive", "legacy"}
            )
            if not catalog_available and disabled_reason is None:
                disabled_reason = (
                    "account_unavailable"
                    if raw_model.get("available") is False
                    else "inactive"
                )
            available = bool(
                catalog_available and disabled_reason is None
            )
            access_status = (
                "unavailable"
                if not available
                else (
                    "available"
                    if raw_model.get("available") is True
                    else "unverified"
                )
            )

        models.append(
            {
                "id": model_id,
                "label": str(raw_model.get("name") or model_id),
                "available": available,
                "catalog_available": catalog_available,
                "access_status": access_status,
                # Keep structured selector reasons (for example,
                # {"type": "upgrade_required", ...}) intact for the UI.
                "disabled_reason": disabled_reason,
                "section": section or None,
                "capabilities": raw_model.get("capabilities"),
                "thinking": raw_model.get("thinking"),
                "supports_fast_mode": bool(
                    raw_model.get("supports_fast_mode")
                ),
                "source": source,
            }
        )
    return models


def _normalize_grok_enrollment_models(
    value: Any,
) -> list[dict[str, Any]]:
    """Accept only selector evidence, never a guessed Grok catalog."""
    if not isinstance(value, list):
        return []

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_model in value:
        if not isinstance(raw_model, dict):
            continue
        observed_id = str(raw_model.get("id") or "").strip()[:100]
        match = GROK_MODEL_RE.search(observed_id)
        if not match:
            continue
        model_id = match.group(0).strip()
        key = model_id.casefold()
        if key in seen:
            continue
        seen.add(key)

        raw_status = str(
            raw_model.get("access_status") or "unverified"
        ).lower()
        selected = bool(raw_model.get("selected"))
        if raw_status == "unavailable":
            access_status = "unavailable"
        elif raw_status == "available" and selected:
            access_status = "available"
        else:
            access_status = "unverified"
        available = access_status == "available"
        models.append(
            {
                "id": model_id,
                "label": (
                    str(raw_model.get("label") or model_id).strip()[:180]
                    or model_id
                ),
                "available": available,
                "catalog_available": available,
                "access_status": access_status,
                "disabled_reason": (
                    raw_model.get("disabled_reason")
                    if access_status == "unavailable"
                    else None
                ),
                "selected": selected,
                "source": "account_model_selector_dom",
            }
        )
    return models


@dataclass
class Enrollment:
    profile_id: str
    profile_path: Path
    camoufox: Any
    context: Any
    page: Any
    started_at: float
    provider: str = CLAUDE_WEB_PROVIDER
    driver_pid: int | None = None
    status: str = "browser_open"
    last_error: str | None = None
    account_uuid: str | None = None
    organization_uuid: str | None = None
    project_id: str | None = None
    browser_engine: str = "camoufox"
    relay: Any = None


class ProfileEnrollmentManager:
    """Owns at most one temporary visible login browser per profile."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._enrollments: dict[str, Enrollment] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        try:
            self._humanize_seconds = max(
                0.0,
                float(os.getenv("CLAUDE_HUMANIZE_SECONDS", "0.25")),
            )
        except ValueError:
            self._humanize_seconds = 0.25
        try:
            self._enrollment_ttl_seconds = max(
                60.0,
                float(os.getenv("CLAUDE_ENROLLMENT_TTL", "900")),
            )
        except ValueError:
            self._enrollment_ttl_seconds = 900.0

    async def launch(
        self,
        profile_id: str,
        profile_path: str,
        provider: str = CLAUDE_WEB_PROVIDER,
        outbound_proxy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider = str(provider or CLAUDE_WEB_PROVIDER).strip().lower()
        if provider not in PROVIDER_ENTRY_URLS:
            raise ValueError(f"unsupported profile provider: {provider}")
        async with self._lock:
            current = self._enrollments.get(profile_id)
            if current is not None:
                if (
                    current.provider == provider
                    and not self._page_is_closed(current)
                ):
                    try:
                        await current.page.bring_to_front()
                    except Exception:
                        pass
                    return self._snapshot(current)
                await self._close_unlocked(profile_id)

            path = Path(profile_path).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            relay = await proxy_relay.open_relay(outbound_proxy)
            browser_proxy = proxy_relay.browser_proxy(outbound_proxy, relay)
            if provider == GROK_WEB_PROVIDER:
                # xAI currently rejects the Camoufox/Firefox enrollment
                # window. Keep auth inside an installed Chrome process with a
                # dedicated persistent profile; no session material leaves it.
                camoufox: _ChromeEnrollmentBrowser | AsyncCamoufox = (
                    _ChromeEnrollmentBrowser(path, browser_proxy)
                )
                browser_engine = "chrome"
            else:
                # Preserve the established Claude enrollment backend exactly.
                # The login must leave through the same exit the session will
                # use later, or the account is enrolled from one address and
                # then used from another.
                launch: dict[str, Any] = {
                    "headless": False,
                    "persistent_context": True,
                    "user_data_dir": str(path),
                    "humanize": self._humanize_seconds or False,
                }
                if browser_proxy:
                    launch["proxy"] = browser_proxy
                    launch["geoip"] = True
                camoufox = AsyncCamoufox(**launch)
                browser_engine = "camoufox"
            context = None
            driver_pid = None
            try:
                context = await asyncio.wait_for(
                    camoufox.__aenter__(),
                    timeout=120,
                )
                pages = context.pages
                page = pages[0] if pages else await context.new_page()
                driver_pid = self._capture_driver_pid(camoufox)
                await page.goto(
                    PROVIDER_ENTRY_URLS[provider],
                    wait_until="domcontentloaded",
                    timeout=120_000,
                )
                enrollment = Enrollment(
                    relay=relay,
                    profile_id=profile_id,
                    profile_path=path,
                    provider=provider,
                    camoufox=camoufox,
                    context=context,
                    page=page,
                    started_at=time.time(),
                    browser_engine=browser_engine,
                    driver_pid=driver_pid,
                )
                self._enrollments[profile_id] = enrollment
                self._expiry_tasks[profile_id] = asyncio.create_task(
                    self._expire_after(profile_id, enrollment.started_at),
                    name=f"profile-enrollment-expiry-{profile_id}",
                )
                return self._snapshot(enrollment)
            except BaseException:
                driver_pid = driver_pid or self._capture_driver_pid(camoufox)
                try:
                    await asyncio.wait_for(
                        camoufox.__aexit__(None, None, None),
                        timeout=10,
                    )
                except asyncio.CancelledError:
                    await asyncio.shield(self._kill_driver_tree(driver_pid))
                    raise
                except BaseException:
                    await asyncio.shield(self._kill_driver_tree(driver_pid))
                raise

    async def inspect(self, profile_id: str) -> dict[str, Any]:
        async with self._lock:
            enrollment = self._enrollments.get(profile_id)
            if enrollment is None:
                return {
                    "profile_id": profile_id,
                    "status": "not_running",
                    "authenticated": False,
                }
            if self._page_is_closed(enrollment):
                enrollment.status = "browser_closed"
                snapshot = self._snapshot(enrollment)
                await self._close_unlocked(profile_id)
                return snapshot
            if enrollment.provider == GROK_WEB_PROVIDER:
                return await self._inspect_grok_unlocked(enrollment)
            try:
                result = await asyncio.wait_for(
                    enrollment.page.evaluate(
                        """
                        async () => {
                          const uuidRe = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
                          const normalize = (value) => {
                            if (!value) return null;
                            try {
                              const parsed = JSON.parse(value);
                              return typeof parsed === 'string' ? parsed : value;
                            } catch {
                              return value;
                            }
                          };
                          let response;
                          try {
                            response = await fetch('/api/account', {
                              credentials: 'include',
                              cache: 'no-store',
                              headers: {Accept: 'application/json'}
                            });
                          } catch {
                            return {
                              authenticated: false,
                              reason: 'network',
                              url: location.href
                            };
                          }
                          if (!response.ok) return {
                            authenticated: false,
                            status: response.status,
                            reason: 'account_endpoint',
                            url: location.href
                          };
                          let account;
                          try {
                            account = await response.json();
                          } catch {
                            return {
                              authenticated: false,
                              status: response.status,
                              reason: 'bad_json',
                              url: location.href
                            };
                          }
                          const records = [];
                          const findValues = (value, key, output, depth = 0) => {
                            if (!value || depth > 9) return;
                            if (Array.isArray(value)) {
                              for (const child of value) {
                                findValues(child, key, output, depth + 1);
                              }
                              return;
                            }
                            if (typeof value !== 'object') return;
                            if (Object.prototype.hasOwnProperty.call(value, key)) {
                              output.push(value[key]);
                            }
                            for (const child of Object.values(value)) {
                              findValues(child, key, output, depth + 1);
                            }
                          };
                          const walk = (value, depth = 0) => {
                            if (!value || depth > 9) return;
                            if (Array.isArray(value)) {
                              for (const child of value) walk(child, depth + 1);
                              return;
                            }
                            if (typeof value !== 'object') return;
                            const id = String(
                              value.uuid || value.account_uuid || value.id || ''
                            );
                            const identityLike =
                              'email' in value
                              || 'email_address' in value
                              || 'full_name' in value
                              || 'display_name' in value;
                            if (uuidRe.test(id) && identityLike) records.push(value);
                            for (const child of Object.values(value)) {
                              walk(child, depth + 1);
                            }
                          };
                          walk(account);
                          const hinted = String(normalize(
                            localStorage.getItem('__qk_hint_account_uuid')
                          ) || '');
                          const confirmed = String(normalize(
                            localStorage.getItem('rq-cache-confirmed-account')
                          ) || '');
                          if (hinted && confirmed && hinted !== confirmed) return {
                            authenticated: false,
                            status: response.status,
                            reason: 'hint_mismatch',
                            url: location.href
                          };
                          const hint = uuidRe.test(confirmed)
                            ? confirmed
                            : (uuidRe.test(hinted) ? hinted : null);
                          const unique = [...new Map(records.map((item) => [
                            String(item.uuid || item.account_uuid || item.id),
                            item
                          ])).values()];
                          const matching = hint
                            ? unique.find((item) => String(
                                item.uuid || item.account_uuid || item.id
                              ) === hint)
                            : null;
                          if (hint && unique.length && !matching) return {
                            authenticated: false,
                            status: response.status,
                            reason: 'api_hint_mismatch',
                            url: location.href
                          };
                          const record = matching
                            || (!hint && unique.length === 1 ? unique[0] : null);
                          const accountUuid = record
                            ? String(
                                record.uuid || record.account_uuid || record.id
                              )
                            : hint;
                          if (!uuidRe.test(String(accountUuid || ''))) return {
                            authenticated: false,
                            status: response.status,
                            reason: 'missing_uuid',
                            url: location.href
                          };
                          const rawEmail = String(
                            record?.email_address || record?.email || ''
                          );
                          const emailMasked = rawEmail.includes('@')
                            ? rawEmail.replace(/^(.{1,2}).*(@.*)$/, '$1***$2')
                            : null;
                          const configs = [];
                          const states = [];
                          const bootstrapConfigs = [];
                          findValues(
                            account,
                            'model_selector_config',
                            configs
                          );
                          findValues(
                            account,
                            'model_selector_state',
                            states
                          );
                          findValues(
                            account,
                            'claude_ai_bootstrap_models_config',
                            bootstrapConfigs
                          );
                          const findChatRow = (values, requireModels) => {
                            for (const value of values) {
                              const rows = Array.isArray(value) ? value : [value];
                              for (const row of rows) {
                                if (!row || typeof row !== 'object') continue;
                                if (
                                  row.chat
                                  && typeof row.chat === 'object'
                                  && (
                                    !requireModels
                                    || Array.isArray(row.chat.models)
                                  )
                                ) return row.chat;
                                if (
                                  row.id === 'chat'
                                  || (
                                    requireModels
                                    && Array.isArray(row.models)
                                  )
                                ) return row;
                              }
                            }
                            return null;
                          };
                          const chatConfig = findChatRow(configs, true);
                          const chatState = findChatRow(states, false);
                          const selectorModels = Array.isArray(chatConfig?.models)
                            ? chatConfig.models
                            : null;
                          const modelSource = selectorModels !== null
                            ? 'account_model_selector'
                            : 'bootstrap_catalog';
                          const rawModels = selectorModels !== null
                            ? selectorModels
                            : bootstrapConfigs.flat().filter(
                                (item) => item && typeof item === 'object'
                              );
                          const models = rawModels.length
                            ? rawModels.map((item) => ({
                                id: String(item.id || item.model || ''),
                                name: String(
                                  item.name
                                  || item.label
                                  || item.id
                                  || item.model
                                  || ''
                                ),
                                section: item.section
                                  || (item.inactive ? 'inactive' : null),
                                available: item.available,
                                inactive: Boolean(item.inactive),
                                disabled_reason: item.disabled_reason ?? null,
                                capabilities: item.capabilities ?? null,
                                thinking: item.thinking
                                  ?? (Array.isArray(item.thinking_modes)
                                    ? {modes: item.thinking_modes}
                                    : null),
                                supports_fast_mode: Boolean(
                                  item.supports_fast_mode
                                  || (
                                    Array.isArray(item.paprika_modes)
                                    && item.paprika_modes.includes('instant')
                                  )
                                ),
                                source: modelSource
                              })).filter((item) => item.id)
                            : [];
                          const resourceUrls = performance
                            .getEntriesByType('resource')
                            .map((item) => String(item.name || ''));
                          let organizationUuid = null;
                          for (const value of resourceUrls) {
                            const match = value.match(
                              /\\/api\\/organizations\\/([0-9a-f-]{36})(?:\\/|$)/i
                            );
                            if (match) {
                              organizationUuid = match[1];
                              break;
                            }
                          }
                          if (!organizationUuid) {
                            for (let index = 0; index < localStorage.length; index += 1) {
                              const key = String(localStorage.key(index) || '');
                              if (!/organi[sz]ation|active.?org/i.test(key)) continue;
                              const value = String(normalize(
                                localStorage.getItem(key)
                              ) || '');
                              const match = value.match(
                                /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i
                              );
                              if (match) {
                                organizationUuid = match[0];
                                break;
                              }
                            }
                          }
                          if (!organizationUuid) {
                            const organizationIds = [];
                            const findOrganizations = (value, parentKey = '', depth = 0) => {
                              if (!value || depth > 9) return;
                              if (Array.isArray(value)) {
                                for (const child of value) {
                                  findOrganizations(child, parentKey, depth + 1);
                                }
                                return;
                              }
                              if (typeof value !== 'object') return;
                              for (const [key, child] of Object.entries(value)) {
                                const keyLooksLikeOrg =
                                  /organization(?:_uuid|_id)?/i.test(key)
                                  || /organizations|memberships/i.test(parentKey);
                                if (keyLooksLikeOrg && typeof child === 'string'
                                    && uuidRe.test(child)) {
                                  organizationIds.push(child);
                                }
                                if (keyLooksLikeOrg && child
                                    && typeof child === 'object') {
                                  const candidate = String(
                                    child.uuid || child.id || ''
                                  );
                                  if (uuidRe.test(candidate)) {
                                    organizationIds.push(candidate);
                                  }
                                }
                                findOrganizations(child, key, depth + 1);
                              }
                            };
                            findOrganizations(account);
                            organizationUuid = organizationIds[0] || null;
                          }
                          const hasComposer = Boolean(document.querySelector(
                            'div.ProseMirror[contenteditable="true"],'
                            + ' div[contenteditable="true"].ProseMirror,'
                            + ' fieldset div[contenteditable="true"]'
                          ));
                          return {
                            authenticated: true,
                            status: response.status,
                            url: location.href,
                            accountUuid,
                            organizationUuid,
                            name: String(
                              record?.full_name
                              || record?.display_name
                              || record?.name
                              || ''
                            ) || null,
                            emailMasked,
                            hasComposer,
                            models,
                            modelState: chatState
                          };
                        }
                        """
                    ),
                    timeout=15,
                )
                if not isinstance(result, dict):
                    raise RuntimeError("Claude login probe returned invalid data")
                account_uuid = str(result.get("accountUuid") or "")
                authenticated = bool(result.get("authenticated") and account_uuid)
                if authenticated:
                    enrollment.status = "authenticated"
                    enrollment.account_uuid = account_uuid
                    enrollment.organization_uuid = (
                        str(result.get("organizationUuid") or "") or None
                    )
                elif "login" in str(result.get("url", "")).lower():
                    enrollment.status = "waiting_for_login"
                else:
                    enrollment.status = "checking"
                models = _normalize_enrollment_models(result.get("models"))
                return {
                    **self._snapshot(enrollment),
                    "authenticated": authenticated,
                    "account": {
                        "authenticated": authenticated,
                        "name": str(result.get("name") or "") or None,
                        "email": str(result.get("emailMasked") or "") or None,
                        "uuid_suffix": account_uuid[-8:] or None,
                    },
                    "models": models,
                    "organization_uuid_suffix": (
                        enrollment.organization_uuid[-8:]
                        if enrollment.organization_uuid
                        else None
                    ),
                    "has_composer": bool(result.get("hasComposer")),
                    "reason": result.get("reason"),
                }
            except Exception as exc:
                enrollment.status = "error"
                enrollment.last_error = f"{type(exc).__name__}: {exc}"
                return self._snapshot(enrollment)

    async def _inspect_grok_unlocked(
        self,
        enrollment: Enrollment,
    ) -> dict[str, Any]:
        """Inspect only evidence already available inside the Grok page.

        The probe deliberately does not read cookies, copy authorization
        headers, or call a guessed authentication endpoint. Until Grok's web
        session contract is verified, ambiguous page state remains
        ``unverified`` instead of being promoted to an authenticated profile.
        """
        try:
            result = await asyncio.wait_for(
                enrollment.page.evaluate(
                    """
                    async () => {
                      const clean = (value, limit = 160) => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .slice(0, limit);
                      const visible = (element) => {
                        if (!(element instanceof Element)) return false;
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                          style.display !== 'none'
                          && style.visibility !== 'hidden'
                          && Number(style.opacity || 1) !== 0
                          && rect.width > 0
                          && rect.height > 0
                        );
                      };
                      const pageTitle = clean(document.title, 300);
                      const pageText = clean(
                        document.body?.innerText,
                        12_000
                      );
                      const blockSurface = `${pageTitle}\\n${pageText}`;
                      let blockReason = null;
                      if (/sorry,?\\s+you have been blocked/i.test(blockSurface)) {
                        blockReason = 'xai_blocked';
                      } else if (
                        /attention required/i.test(blockSurface)
                        && /cloudflare/i.test(blockSurface)
                      ) {
                        blockReason = 'cloudflare_challenge';
                      } else if (
                        /cloudflare ray id/i.test(blockSurface)
                        || /\\baccess denied\\b/i.test(blockSurface)
                      ) {
                        blockReason = 'access_denied';
                      }
                      if (blockReason) {
                        return {
                          providerBlocked: true,
                          accessStatus: 'access_denied',
                          authenticationState: 'unverified',
                          authenticated: false,
                          reason: blockReason,
                          url: location.href,
                          accountUuid: null,
                          name: null,
                          emailMasked: null,
                          hasComposer: false,
                          models: [],
                          evidence: {
                            blockPage: true,
                            accountControl: false,
                            loginControl: false,
                            logoutControl: false,
                            embeddedIdentity: false,
                            sameOriginActivity: false
                          }
                        };
                      }
                      const emailRe =
                        /[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\\.[a-z]{2,}/i;
                      const uuidRe =
                        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
                      const maskEmail = (value) => {
                        const match = clean(value, 320).match(emailRe);
                        if (!match) return null;
                        const [local, domain] = match[0].split('@');
                        return `${local.slice(0, Math.min(2, local.length))}***@${domain}`;
                      };
                      const controls = [...document.querySelectorAll(
                        'a, button, [role="button"], [role="menuitem"]'
                      )].filter(visible);
                      const controlText = (element) => clean(
                        element.getAttribute('aria-label')
                        || element.getAttribute('title')
                        || element.textContent
                      );
                      const loginControl = controls.find((element) => {
                        const text = controlText(element);
                        const href = String(element.getAttribute('href') || '');
                        return (
                          /\\b(sign[ -]?in|log[ -]?in)\\b/i.test(text)
                          || /\\/(?:sign|log)[-_]?in(?:\\/|$|\\?)/i.test(href)
                        );
                      });
                      const logoutControl = controls.find((element) =>
                        /\\b(sign[ -]?out|log[ -]?out)\\b/i.test(
                          controlText(element)
                        )
                      );
                      const accountControls = [
                        ...document.querySelectorAll(
                          '[data-testid*="account" i],'
                          + '[data-testid*="profile" i],'
                          + '[aria-label*="account" i],'
                          + '[aria-label*="profile" i],'
                          + '[href*="/settings" i]'
                        )
                      ].filter(visible);

                      let identity = null;
                      const acceptIdentity = (value, parentKey = '') => {
                        if (
                          identity
                          || !value
                          || typeof value !== 'object'
                          || Array.isArray(value)
                        ) return;
                        const email = clean(
                          value.email
                          || value.email_address
                          || value.emailAddress,
                          320
                        );
                        const identityContext = /user|account|session|viewer|profile|\\bme\\b/i
                          .test(parentKey)
                          || 'display_name' in value
                          || 'displayName' in value
                          || 'username' in value;
                        if (!identityContext || !emailRe.test(email)) return;
                        const rawUuid = clean(
                          value.uuid || value.account_uuid,
                          80
                        );
                        identity = {
                          emailMasked: maskEmail(email),
                          name: clean(
                            value.display_name
                            || value.displayName
                            || value.full_name
                            || value.name
                            || value.username,
                            120
                          ) || null,
                          accountUuid: uuidRe.test(rawUuid) ? rawUuid : null
                        };
                      };
                      const walkIdentity = (
                        value,
                        parentKey = '',
                        depth = 0,
                        seen = new Set()
                      ) => {
                        if (
                          identity
                          || value === null
                          || typeof value !== 'object'
                          || depth > 8
                          || seen.has(value)
                        ) return;
                        seen.add(value);
                        acceptIdentity(value, parentKey);
                        if (Array.isArray(value)) {
                          for (const child of value) {
                            walkIdentity(child, parentKey, depth + 1, seen);
                          }
                          return;
                        }
                        for (const [key, child] of Object.entries(value)) {
                          walkIdentity(child, key, depth + 1, seen);
                        }
                      };
                      const stateScripts = [
                        ...document.querySelectorAll(
                          '#__NEXT_DATA__, script[type="application/json"]'
                        )
                      ];
                      for (const script of stateScripts) {
                        if (identity) break;
                        const raw = String(script.textContent || '');
                        if (!raw || raw.length > 2_000_000) continue;
                        try {
                          walkIdentity(JSON.parse(raw));
                        } catch {}
                      }
                      if (!identity) {
                        for (const element of accountControls) {
                          const label = controlText(element);
                          const emailMasked = maskEmail(label);
                          if (emailMasked) {
                            identity = {
                              emailMasked,
                              name: null,
                              accountUuid: null
                            };
                            break;
                          }
                        }
                      }

                      const modelPattern =
                        /\\bgrok(?:[\\s._-]*[a-z0-9]+){0,4}\\b/i;
                      const modelElements = [
                        ...document.querySelectorAll(
                          '[data-testid*="model" i],'
                          + '[data-model],'
                          + '[data-model-id],'
                          + '[role="option"],'
                          + '[role="menuitemradio"],'
                          + '[aria-label*="model" i]'
                        )
                      ].filter(visible);
                      const modelsById = new Map();
                      for (const element of modelElements) {
                        const label = clean(
                          element.getAttribute('aria-label')
                          || element.textContent,
                          180
                        );
                        const explicitId = clean(
                          element.getAttribute('data-model-id')
                          || element.getAttribute('data-model')
                          || element.getAttribute('data-value'),
                          100
                        );
                        const observed = (
                          explicitId.match(modelPattern)
                          || label.match(modelPattern)
                        )?.[0];
                        if (!observed) continue;
                        const id = clean(explicitId.match(modelPattern)?.[0]
                          || observed, 100);
                        const selected = (
                          element.getAttribute('aria-selected') === 'true'
                          || element.getAttribute('aria-checked') === 'true'
                          || element.getAttribute('data-state') === 'checked'
                          || element.getAttribute('data-state') === 'selected'
                        );
                        const upgradeText =
                          /upgrade|subscribe|premium required|not available/i
                            .test(label);
                        const disabled = (
                          element.matches(':disabled')
                          || element.getAttribute('aria-disabled') === 'true'
                          || upgradeText
                        );
                        const accessStatus = disabled
                          ? 'unavailable'
                          : (selected ? 'available' : 'unverified');
                        const previous = modelsById.get(id.toLowerCase());
                        const row = {
                          id,
                          label: label || id,
                          available: accessStatus === 'available',
                          catalog_available: accessStatus === 'available',
                          access_status: accessStatus,
                          disabled_reason: disabled
                            ? (upgradeText
                              ? 'upgrade_required'
                              : 'selector_disabled')
                            : null,
                          selected,
                          source: 'account_model_selector_dom'
                        };
                        if (
                          !previous
                          || previous.access_status === 'unverified'
                          || row.access_status === 'available'
                        ) {
                          modelsById.set(id.toLowerCase(), row);
                        }
                      }

                      const hasComposer = Boolean([
                        ...document.querySelectorAll(
                          'textarea,'
                          + '[contenteditable="true"],'
                          + '[role="textbox"]'
                        )
                      ].find(visible));
                      const loginPath =
                        /\\/(?:sign|log)[-_]?in(?:\\/|$)/i.test(location.pathname);
                      const strongAccountEvidence = Boolean(
                        identity?.emailMasked
                        || (logoutControl && accountControls.length)
                      );
                      const authenticationState = strongAccountEvidence
                        ? 'verified'
                        : (
                          loginPath || loginControl
                            ? 'auth_pending'
                            : 'unverified'
                        );
                      const sameOriginActivity = performance
                        .getEntriesByType('resource')
                        .some((entry) => {
                          try {
                            return new URL(entry.name).origin === location.origin;
                          } catch {
                            return false;
                          }
                        });
                      return {
                        authenticated: strongAccountEvidence,
                        authenticationState,
                        reason: strongAccountEvidence
                          ? null
                          : (
                            authenticationState === 'auth_pending'
                              ? 'login_required'
                              : 'auth_not_verified'
                          ),
                        url: location.href,
                        accountUuid: identity?.accountUuid || null,
                        name: identity?.name || null,
                        emailMasked: identity?.emailMasked || null,
                        hasComposer,
                        models: [...modelsById.values()],
                        evidence: {
                          accountControl: accountControls.length > 0,
                          loginControl: Boolean(loginControl),
                          logoutControl: Boolean(logoutControl),
                          embeddedIdentity: Boolean(identity?.emailMasked),
                          sameOriginActivity
                        }
                      };
                    }
                    """
                ),
                timeout=15,
            )
            if not isinstance(result, dict):
                raise RuntimeError("Grok login probe returned invalid data")
            if result.get("providerBlocked"):
                block_reason = str(
                    result.get("reason") or "access_denied"
                )
                if block_reason not in {
                    "xai_blocked",
                    "cloudflare_challenge",
                    "access_denied",
                }:
                    block_reason = "access_denied"
                enrollment.status = "provider_blocked"
                enrollment.account_uuid = None
                enrollment.organization_uuid = None
                enrollment.project_id = None
                enrollment.last_error = None
                return {
                    **self._snapshot(enrollment),
                    "authenticated": False,
                    "authentication_state": "unverified",
                    "access_status": "access_denied",
                    "provider_blocked": True,
                    "terminal": True,
                    "account": {
                        "authenticated": False,
                        "name": None,
                        "email": None,
                        "uuid_suffix": None,
                    },
                    "models": [],
                    "organization_uuid_suffix": None,
                    "has_composer": False,
                    "reason": block_reason,
                    "evidence": (
                        result.get("evidence")
                        if isinstance(result.get("evidence"), dict)
                        else {"blockPage": True}
                    ),
                }

            authentication_state = str(
                result.get("authenticationState") or "unverified"
            )
            if authentication_state not in {
                "verified",
                "auth_pending",
                "unverified",
            }:
                authentication_state = "unverified"
            authenticated = bool(
                result.get("authenticated")
                and authentication_state == "verified"
            )
            account_uuid = str(result.get("accountUuid") or "") or None
            if account_uuid and not UUID_TEXT_RE.fullmatch(account_uuid):
                account_uuid = None
            if authenticated:
                enrollment.status = "authenticated"
                enrollment.account_uuid = account_uuid
            else:
                enrollment.status = authentication_state
                enrollment.account_uuid = None
            enrollment.organization_uuid = None
            enrollment.project_id = None
            enrollment.last_error = None

            models = _normalize_grok_enrollment_models(
                result.get("models")
            )
            return {
                **self._snapshot(enrollment),
                "authenticated": authenticated,
                "authentication_state": authentication_state,
                "account": {
                    "authenticated": authenticated,
                    "name": str(result.get("name") or "") or None,
                    "email": str(result.get("emailMasked") or "") or None,
                    "uuid_suffix": account_uuid[-8:] if account_uuid else None,
                },
                "models": models,
                "organization_uuid_suffix": None,
                "has_composer": bool(result.get("hasComposer")),
                "reason": result.get("reason"),
                "evidence": (
                    result.get("evidence")
                    if isinstance(result.get("evidence"), dict)
                    else {}
                ),
            }
        except Exception as exc:
            enrollment.status = "error"
            enrollment.last_error = f"{type(exc).__name__}: {exc}"
            return self._snapshot(enrollment)

    async def finish(self, profile_id: str) -> None:
        async with self._lock:
            await self._close_unlocked(profile_id)

    async def is_running(self, profile_id: str) -> bool:
        async with self._lock:
            enrollment = self._enrollments.get(profile_id)
            return bool(
                enrollment is not None
                and not self._page_is_closed(enrollment)
            )

    async def internal_identity(
        self,
        profile_id: str,
    ) -> dict[str, str | None]:
        async with self._lock:
            enrollment = self._enrollments.get(profile_id)
            if enrollment is None:
                raise KeyError(profile_id)
            return {
                "provider": enrollment.provider,
                "account_uuid": enrollment.account_uuid,
                "organization_uuid": enrollment.organization_uuid,
                "project_id": enrollment.project_id,
            }

    async def ensure_project(
        self,
        profile_id: str,
        instructions: str,
    ) -> dict[str, Any]:
        async with self._lock:
            enrollment = self._enrollments.get(profile_id)
            if enrollment is None:
                raise KeyError(profile_id)
            if enrollment.provider == GROK_WEB_PROVIDER:
                enrollment.organization_uuid = None
                enrollment.project_id = None
                return {
                    "provider": GROK_WEB_PROVIDER,
                    "required": False,
                    "status": "not_required",
                    "project_id": None,
                    "organization_uuid_suffix": None,
                    "name": None,
                }
            if not enrollment.account_uuid:
                raise RuntimeError("profile is not authenticated")
            if not enrollment.organization_uuid:
                raise RuntimeError(
                    "Claude organization was not observed yet; reload the "
                    "login browser and check authorization again"
                )
            result = await asyncio.wait_for(
                enrollment.page.evaluate(
                    """
                    async ({organizationUuid, instructions}) => {
                      const base = `/api/organizations/${organizationUuid}/projects`;
                      const bridgeDescription =
                        'Dedicated workspace for the local OpenClaude IDE bridge.';
                      const request = async (url, options = {}) => {
                        const response = await fetch(url, {
                          credentials: 'include',
                          cache: 'no-store',
                          headers: {
                            Accept: 'application/json',
                            'Content-Type': 'application/json',
                            ...(options.headers || {})
                          },
                          ...options
                        });
                        const text = await response.text();
                        let body = null;
                        try { body = text ? JSON.parse(text) : null; } catch {}
                        if (!response.ok) {
                          throw new Error(
                            `${options.method || 'GET'} ${url} -> `
                            + `${response.status}: ${text.slice(0, 300)}`
                          );
                        }
                        return body;
                      };
                      const listed = await request(base);
                      const candidates = [];
                      const walk = (value, depth = 0) => {
                        if (!value || depth > 7) return;
                        if (Array.isArray(value)) {
                          for (const child of value) walk(child, depth + 1);
                          return;
                        }
                        if (typeof value !== 'object') return;
                        const id = String(value.uuid || value.id || '');
                        if (
                          id
                          && value.name === 'OpenClaude IDE'
                          && value.description === bridgeDescription
                          && value.is_private !== false
                        ) {
                          candidates.push(value);
                        }
                        for (const child of Object.values(value)) {
                          walk(child, depth + 1);
                        }
                      };
                      walk(listed);
                      let project = candidates[0] || null;
                      let created = false;
                      if (!project) {
                        project = await request(base, {
                          method: 'POST',
                          body: JSON.stringify({
                            name: 'OpenClaude IDE',
                            description: bridgeDescription,
                            is_private: true,
                            prompt_template: instructions
                          })
                        });
                        created = true;
                      }
                      const projectId = String(project?.uuid || project?.id || '');
                      if (!projectId) throw new Error(
                        'Claude Project response did not include an id'
                      );
                      const verified = await request(`${base}/${projectId}`);
                      const promptTemplate = String(
                        verified?.prompt_template || ''
                      );
                      const legacyPrefix =
                        `${instructions}\\n\\n`
                        + 'DYNAMIC_OPENCLAUDE_SYSTEM_CONTEXT\\n';
                      const legacyDynamicPrompt =
                        promptTemplate.startsWith(legacyPrefix)
                        && promptTemplate.length > legacyPrefix.length;
                      if (
                        promptTemplate !== instructions
                        && !legacyDynamicPrompt
                      ) {
                        throw new Error(
                          'Existing OpenClaude IDE Project instructions were '
                          + 'edited externally; the edit was preserved'
                        );
                      }
                      if (
                        !created
                        && (
                          legacyDynamicPrompt
                          || verified?.is_private !== true
                        )
                      ) {
                        await request(`${base}/${projectId}`, {
                          method: 'PUT',
                          body: JSON.stringify({
                            prompt_template: instructions,
                            is_private: true
                          })
                        });
                      }
                      await request(`${base}/${projectId}/permissions`);
                      return {projectId};
                    }
                    """,
                    {
                        "organizationUuid": enrollment.organization_uuid,
                        "instructions": instructions,
                    },
                ),
                timeout=45,
            )
            if not isinstance(result, dict) or not result.get("projectId"):
                raise RuntimeError("Claude Project setup returned invalid data")
            enrollment.project_id = str(result["projectId"])
            return {
                "project_id": enrollment.project_id,
                "organization_uuid_suffix": enrollment.organization_uuid[-8:],
                "name": "OpenClaude IDE",
            }

    async def stop(self) -> None:
        async with self._lock:
            for profile_id in list(self._enrollments):
                await self._close_unlocked(profile_id)

    async def _expire_after(
        self,
        profile_id: str,
        started_at: float,
    ) -> None:
        try:
            await asyncio.sleep(self._enrollment_ttl_seconds)
            async with self._lock:
                enrollment = self._enrollments.get(profile_id)
                if (
                    enrollment is not None
                    and enrollment.started_at == started_at
                ):
                    enrollment.status = "expired"
                    await self._close_unlocked(profile_id)
        except asyncio.CancelledError:
            raise
        finally:
            task = asyncio.current_task()
            if self._expiry_tasks.get(profile_id) is task:
                self._expiry_tasks.pop(profile_id, None)

    async def _close_unlocked(self, profile_id: str) -> None:
        expiry_task = self._expiry_tasks.pop(profile_id, None)
        if (
            expiry_task is not None
            and expiry_task is not asyncio.current_task()
        ):
            expiry_task.cancel()
        enrollment = self._enrollments.pop(profile_id, None)
        if enrollment is None:
            return
        if enrollment.relay is not None:
            await enrollment.relay.stop()
        try:
            await asyncio.wait_for(
                enrollment.camoufox.__aexit__(None, None, None),
                timeout=12,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._kill_driver_tree(enrollment.driver_pid)
            )
            raise
        except BaseException:
            await asyncio.shield(
                self._kill_driver_tree(enrollment.driver_pid)
            )

    @staticmethod
    def _capture_driver_pid(camoufox: Any) -> int | None:
        try:
            transport = camoufox._connection._transport
            pid = int(transport._proc.pid)
            return pid if pid > 0 else None
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _page_is_closed(enrollment: Enrollment) -> bool:
        try:
            return bool(enrollment.page.is_closed())
        except Exception:
            return True

    @staticmethod
    async def _kill_driver_tree(driver_pid: int | None) -> None:
        if not driver_pid or driver_pid <= 0:
            return
        if os.name != "nt":
            return
        try:
            process = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(driver_pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            return

    @staticmethod
    def _snapshot(enrollment: Enrollment) -> dict[str, Any]:
        url = str(getattr(enrollment.page, "url", "") or "")
        try:
            parsed = urlsplit(url)
            url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, "", "")
            )
        except ValueError:
            url = ""
        url = UUID_TEXT_RE.sub(
            lambda match: f"…{match.group(0)[-8:]}",
            url,
        )
        return {
            "profile_id": enrollment.profile_id,
            "provider": enrollment.provider,
            "browser_engine": enrollment.browser_engine,
            "status": enrollment.status,
            "authenticated": enrollment.status == "authenticated",
            "browser_open": not ProfileEnrollmentManager._page_is_closed(
                enrollment
            ),
            "url": url,
            "started_at": enrollment.started_at,
            "last_error": enrollment.last_error,
        }
