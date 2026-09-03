"""Starting, watching and recovering the Camoufox browser."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from camoufox.async_api import AsyncCamoufox

from claude_web_api.control import proxy_relay
from claude_web_api.session.errors import ClaudeBrowserUnavailableError
from claude_web_api.session.patterns import (
    COMPLETION_PATH_RE,
    CONVERSATION_CREATE_PATH_RE,
)
from claude_web_api.session.scripts import SSE_TAP_SCRIPT
from claude_web_api.session.state import SessionState


class BrowserLifecycleMixin(SessionState):
    """Starting, watching and recovering the Camoufox browser."""

    async def bring_to_front(self) -> None:
        async with self._lock:
            if self.page is None or self.page.is_closed():
                raise ClaudeBrowserUnavailableError(
                    "the active Camoufox window is not available"
                )
            await self.page.bring_to_front()
    def _mark_browser_dead(self, reason: str) -> None:
        if self._stopping:
            return
        self.ready = False
        self._last_error = reason
        self._browser_dead.set()
        self._set_phase("browser_dead")
        queue = self._native_queue
        if queue is not None:
            try:
                queue.put_nowait({"event": "__browser_dead", "data": reason})
            except asyncio.QueueFull:
                pass
    def _capture_driver_pid(self) -> int | None:
        try:
            transport = self._camoufox._connection._transport
            pid = int(transport._proc.pid)
            return pid if pid > 0 else None
        except (AttributeError, TypeError, ValueError):
            return None
    def watchdog_running(self) -> bool:
        return bool(
            self._watchdog_task is not None
            and not self._watchdog_task.done()
        )
    def watchdog_alive(self) -> bool:
        """Whether the supervisor's liveness probe should pass.

        Liveness is about this process, not about the browser: a session that
        needs a human — a login, a working proxy, an account fix — is answered
        by the panel, and killing the server would only take that panel away
        while changing nothing. Only a watchdog that stopped ticking is a
        reason to restart.
        """
        if not self.watchdog_running():
            return False
        max_age = max(30.0, self._watchdog_interval * 4)
        if self._phase in {"starting_browser", "recovering_browser"}:
            max_age = self._browser_start_timeout + 30.0
        return time.monotonic() - self._watchdog_heartbeat_at <= max_age

    def watchdog_healthy(self) -> bool:
        """Whether the browser side is both supervised and usable."""
        if self._recovery_exhausted:
            return False
        return self.watchdog_alive()
    def launch_options(self, profile_dir: Path) -> dict[str, Any]:
        """Camoufox arguments for the profile that is about to start."""
        options: dict[str, Any] = {
            "headless": self.headless,
            "persistent_context": True,
            "user_data_dir": str(profile_dir),
            # Camoufox currently treats bool as a numeric maxTime and
            # serializes True, which the browser rejects as non-double.
            "humanize": self._humanize_seconds or False,
        }
        outbound = proxy_relay.browser_proxy(
            self.profile_specs[self.profile_index].get("proxy"),
            self._proxy_relay,
        )
        if outbound:
            # geoip lets Camoufox derive locale, timezone and WebRTC address
            # from the proxy exit, so the profile does not carry this machine's
            # own fingerprint out through someone else's IP.
            options["proxy"] = outbound
            options["geoip"] = True
        return options

    async def start(self) -> None:
        self._stopping = False
        self._session_epoch += 1
        epoch = self._session_epoch
        self._browser_dead.clear()
        self.ready = False
        self._account_uuid = None
        self._account_name = None
        self._account_email_masked = None
        self._organization_uuid = None
        self._project_instructions_synced = False
        self._project_sync_error = None
        self._project_privacy_verified = None
        self._available_models = []
        self._model_selector_state = {}
        self._model_selector_diagnostics = {}
        self._set_phase("starting_browser")
        self._debug("start:launch")
        profile_dir = self.profile_dirs[self.profile_index]
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Firefox cannot authenticate to a SOCKS5 proxy itself, so an
            # authenticated one is fronted by a loopback relay for the
            # lifetime of this browser.
            self._proxy_relay = await proxy_relay.open_relay(
                self.profile_specs[self.profile_index].get("proxy")
            )
            self._camoufox = AsyncCamoufox(**self.launch_options(profile_dir))
            self._context = await asyncio.wait_for(
                self._camoufox.__aenter__(),
                timeout=min(self._browser_start_timeout, 90.0),
            )
            self._driver_pid = self._capture_driver_pid()
            self._debug("start:context")
            await self._context.route(
                COMPLETION_PATH_RE,
                lambda route, request: self._route_completion(
                    route,
                    request,
                    epoch,
                ),
            )
            await self._context.route(
                CONVERSATION_CREATE_PATH_RE,
                lambda route, request: self._route_conversation_create(
                    route,
                    request,
                    epoch,
                ),
            )
            pages = self._context.pages
            self.page = pages[0] if pages else await self._context.new_page()
            self.page.on(
                "close",
                lambda *_: (
                    self._mark_browser_dead("Camoufox page closed")
                    if epoch == self._session_epoch
                    else None
                ),
            )
            self.page.on(
                "crash",
                lambda *_: (
                    self._mark_browser_dead("Camoufox page crashed")
                    if epoch == self._session_epoch
                    else None
                ),
            )
            self._context.on(
                "close",
                lambda *_: (
                    self._mark_browser_dead("Camoufox context closed")
                    if epoch == self._session_epoch
                    else None
                ),
            )
            # Install the bridge before the first claude.ai document loads.
            # The web bundle can capture `fetch` during bootstrap; patching it
            # only after navigation misses those completion streams.
            await self.page.expose_binding(
                "__openclaude_sse",
                self._receive_sse,
            )
            await self.page.add_init_script(SSE_TAP_SCRIPT)
            self._debug("start:binding")
            self._debug("start:navigate")
            await self._goto_start_page(timeout_ms=120_000)
            self._debug(f"start:navigated:{self.page.url}")
            try:
                configured_ready_timeout = float(
                    os.getenv("CLAUDE_READY_TIMEOUT", "180")
                )
            except ValueError:
                configured_ready_timeout = 180.0
            # A proxied claude.ai can take far longer than a direct one to
            # paint the composer; giving up early reported "log in" to an
            # account that was in fact already logged in.
            startup_probe_timeout = max(
                2.0,
                min(configured_ready_timeout, 60.0),
            )
            composer = None
            probe_deadline = time.monotonic() + startup_probe_timeout
            while time.monotonic() < probe_deadline:
                composer = await self._input_locator()
                if composer is not None:
                    break
                url = str(getattr(self.page, "url", "") or "").lower()
                if "login" in url or "auth" in url:
                    break
                await asyncio.sleep(0.5)
            if composer is None:
                # Keep the visible persistent browser alive so the user can
                # log in while FastAPI/control center remain available.
                self._clear_native_state()
                self.ready = False
                self._last_error = (
                    "Claude authentication or browser confirmation is required"
                )
                self._set_phase("auth_required")
                return
            self._debug("start:ready")
            await self._install_sse_tap()
            self._debug("start:tap")
            self._clear_native_state()
            identity_ready = await self._load_account_identity()
            if not identity_ready:
                self.ready = False
                self._last_error = (
                    "Claude composer is visible, but /api/account identity "
                    "has not been verified"
                )
                self._set_phase("account_unknown")
                return
            expected_uuid = self._profile_account_uuids.get(
                self.current_profile_id()
            )
            if expected_uuid and self._account_uuid != expected_uuid:
                self.ready = False
                self._last_error = (
                    "The active Camoufox profile changed Claude accounts"
                )
                self._set_phase("account_changed")
                return
            if self._account_uuid:
                self._profile_account_uuids.setdefault(
                    self.current_profile_id(),
                    self._account_uuid,
                )
            if not await self._sync_trusted_project():
                self.ready = False
                self._last_error = self._project_sync_error
                self._set_phase("project_unavailable")
                return
            if self._browser_dead.is_set() or self.page.is_closed():
                raise ClaudeBrowserUnavailableError(
                    "Camoufox closed while the authenticated session was starting"
                )
            self.ready = True
            self._last_error = None
            self._recovery_exhausted = False
            self._recovery_failures = 0
            self._next_recovery_at = 0.0
            self._set_phase("idle")
        except BaseException as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            await self._stop_browser_unlocked()
            if "login" in str(exc).lower() or "log in" in str(exc).lower():
                self._set_phase("auth_required")
            else:
                self._set_phase("browser_dead")
            raise
    async def stop(self) -> None:
        await self.stop_watchdog()
        await self._stop_browser_unlocked()
    async def _stop_browser_unlocked(self) -> None:
        self._stopping = True
        self._session_epoch += 1
        self._clear_native_state()
        self._conversation_client_session_id = None
        self._conversation_privacy_mode = None
        camoufox = self._camoufox
        driver_pid = self._driver_pid or self._capture_driver_pid()
        self._camoufox = None
        self._context = None
        self.page = None
        self._driver_pid = None
        try:
            if camoufox is not None:
                try:
                    await asyncio.wait_for(
                        camoufox.__aexit__(None, None, None),
                        timeout=self._browser_close_timeout,
                    )
                except asyncio.CancelledError:
                    await asyncio.shield(self._kill_driver_tree(driver_pid))
                    raise
                except Exception:
                    await self._kill_driver_tree(driver_pid)
        finally:
            relay, self._proxy_relay = self._proxy_relay, None
            if relay is not None:
                await relay.stop()
            self.ready = False
            self._browser_dead.clear()
            self._stopping = False
            self._set_phase("stopped")
    async def _kill_driver_tree(self, driver_pid: int | None = None) -> None:
        """Kill only this Playwright driver's exact descendant tree."""
        pid = driver_pid or self._driver_pid
        if not pid or pid <= 0:
            return
        if sys.platform != "win32":
            try:
                os.kill(pid, 15)
            except (OSError, ProcessLookupError):
                pass
            return
        try:
            process = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            return
    async def _recover_browser_unlocked(self, reason: str) -> None:
        now = time.monotonic()
        self._restart_times = [
            started
            for started in self._restart_times
            if now - started <= self._restart_window
        ]
        if not self._restart_times and self._recovery_exhausted:
            # The restart window has passed: an open circuit that is never
            # closed again turns a burst of failures into a permanent outage.
            self._recovery_exhausted = False
        if len(self._restart_times) >= self._restart_limit:
            self._recovery_exhausted = True
            self.ready = False
            self._last_error = (
                f"Camoufox restart circuit opened after "
                f"{len(self._restart_times)} restarts"
            )
            self._set_phase("browser_dead")
            raise ClaudeBrowserUnavailableError(self._last_error)
        self._restart_times.append(now)
        self._last_recovery_reason = reason
        self._last_recovery_at = time.time()
        self._history_recovery_required = True
        self._set_phase("recovering_browser")
        await self._stop_browser_unlocked()
        self._restart_count += 1
        try:
            await asyncio.wait_for(
                self.start(),
                timeout=self._browser_start_timeout,
            )
        except Exception as exc:
            self._recovery_failures += 1
            delay = min(60.0, 5.0 * (2 ** min(self._recovery_failures - 1, 4)))
            self._next_recovery_at = time.monotonic() + delay
            raise ClaudeBrowserUnavailableError(
                f"Camoufox recovery failed: {exc}"
            ) from exc
    async def start_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            return
        self._watchdog_stop.clear()
        self._watchdog_heartbeat_at = time.monotonic()
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(),
            name="claude-camoufox-watchdog",
        )
    async def stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        task = self._watchdog_task
        self._watchdog_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._watchdog_stop.wait(),
                    timeout=self._watchdog_interval,
                )
                continue
            except asyncio.TimeoutError:
                pass
            self._watchdog_heartbeat_at = time.monotonic()
            if self._stopping:
                continue
            now = time.monotonic()
            if self._lock.locked():
                if (
                    self._phase
                    not in {
                        "waiting_host_result",
                        "auth_required",
                        "account_unknown",
                        "account_changed",
                    }
                    and now - self._last_progress_at
                    >= self._watchdog_stall_timeout
                ):
                    reason = (
                        "Camoufox operation made no progress for "
                        f"{self._watchdog_stall_timeout:.0f}s"
                    )
                    self._mark_browser_dead(reason)
                    await self._kill_driver_tree()
                continue
            if now < self._next_recovery_at:
                continue
            try:
                async with self._lock:
                    if (
                        self._native_active
                        and self._native_pending_ids
                    ):
                        if self._native_wait_expired():
                            await self._expire_native_lease_unlocked()
                        continue
                    if self._phase in {
                        "auth_required",
                        "account_unknown",
                        "account_changed",
                        "project_unavailable",
                    }:
                        if self.page is None or self.page.is_closed():
                            await self._recover_browser_unlocked(
                                "authentication page closed"
                            )
                            continue
                        authenticated = await asyncio.wait_for(
                            self.page.evaluate(
                                """
                                () => Boolean(
                                  document.querySelector(
                                    'div.ProseMirror[contenteditable="true"],'
                                    + ' div[contenteditable="true"].ProseMirror,'
                                    + ' fieldset div[contenteditable="true"]'
                                  )
                                )
                                """
                            ),
                            timeout=self._watchdog_probe_timeout,
                        )
                        self._last_probe_at = time.time()
                        self._last_probe_ok = bool(authenticated)
                        if not authenticated:
                            await self._reload_waiting_page()
                        if authenticated:
                            await asyncio.wait_for(
                                self._install_sse_tap(),
                                timeout=10,
                            )
                            identity_ready = (
                                await self._load_account_identity()
                            )
                            if not identity_ready:
                                self.ready = False
                                self._set_phase("account_unknown")
                                continue
                            expected_uuid = self._profile_account_uuids.get(
                                self.current_profile_id()
                            )
                            if (
                                expected_uuid
                                and self._account_uuid != expected_uuid
                            ):
                                self.ready = False
                                self._last_error = (
                                    "The active Camoufox profile changed "
                                    "Claude accounts"
                                )
                                self._set_phase("account_changed")
                                continue
                            if self._account_uuid:
                                self._profile_account_uuids.setdefault(
                                    self.current_profile_id(),
                                    self._account_uuid,
                                )
                            if not await self._sync_trusted_project():
                                # Restarting the browser cannot conjure a
                                # Project; retry the call itself instead.
                                self.ready = False
                                self._last_error = self._project_sync_error
                                self._set_phase("project_unavailable")
                                continue
                            self._browser_dead.clear()
                            self.ready = True
                            self._last_error = None
                            self._recovery_exhausted = False
                            self._recovery_failures = 0
                            self._set_phase("idle")
                        continue
                    if self._browser_dead.is_set() or not self.ready:
                        await self._recover_browser_unlocked(
                            self._last_error or "browser became unavailable"
                        )
                        continue
                    if self._phase != "idle":
                        reason = (
                            "abandoned browser phase without an owning request: "
                            f"{self._phase}"
                        )
                        self._mark_browser_dead(reason)
                        await self._recover_browser_unlocked(reason)
                        continue
                    if self.page is None:
                        self._mark_browser_dead("Camoufox page is missing")
                        await self._recover_browser_unlocked(
                            "Camoufox page is missing"
                        )
                        continue
                    probe = await asyncio.wait_for(
                        self.page.evaluate(
                            """
                            () => ({
                              ready: document.readyState,
                              hasComposer: Boolean(
                                document.querySelector(
                                  'div.ProseMirror[contenteditable="true"],'
                                  + ' div[contenteditable="true"].ProseMirror,'
                                  + ' fieldset div[contenteditable="true"]'
                                )
                              )
                            })
                            """
                        ),
                        timeout=self._watchdog_probe_timeout,
                    )
                    self._last_probe_at = time.time()
                    self._last_probe_ok = bool(
                        isinstance(probe, dict)
                        and probe.get("ready") in {"interactive", "complete"}
                        and probe.get("hasComposer")
                    )
                    if not self._last_probe_ok:
                        url = str(getattr(self.page, "url", ""))
                        if "login" in url or "auth" in url:
                            self.ready = False
                            self._set_phase("auth_required")
                            continue
                        await self._recover_browser_unlocked(
                            "idle browser probe did not find the Claude composer"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_probe_at = time.time()
                self._last_probe_ok = False
                self._last_error = f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, ClaudeBrowserUnavailableError):
                    if self._phase != "auth_required":
                        self._mark_browser_dead(self._last_error)
                    self._recovery_failures += 1
                    delay = min(
                        60.0,
                        5.0 * (2 ** min(self._recovery_failures - 1, 4)),
                    )
                    self._next_recovery_at = time.monotonic() + delay
    async def _wait_ready(self, timeout: float = 180.0) -> None:
        """Wait until chat UI is usable (user must log in in the opened window)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self._input_locator():
                return
            url = self.page.url
            if "login" in url or "auth" in url:
                await asyncio.sleep(1.5)
                continue
            await asyncio.sleep(1.0)
        try:
            page_text = await asyncio.wait_for(
                self.page.evaluate(
                    "() => (document.body?.innerText || '').slice(0, 1200)"
                ),
                timeout=5,
            )
        except Exception:
            page_text = ""
        raise TimeoutError(
            "Claude chat input not found. "
            f"url={getattr(self.page, 'url', '')!r}; page={page_text!r}. "
            "Log in manually in the browser window, then retry."
        )
    async def _reload_waiting_page(self) -> None:
        """Re-open the start page while a session waits for a human.

        The wait is judged by what the current page shows. A page that never
        finished loading — a proxy hiccup, a transport error — shows no
        composer and never will, so the session would report "log in" forever
        against an account that is fine. Reloading is rate-limited because a
        genuine login page must stay put long enough to be used.
        """
        now = time.monotonic()
        if now - self._waiting_reload_at < self._waiting_reload_interval:
            return
        self._waiting_reload_at = now
        try:
            await asyncio.wait_for(self._goto_start_page(timeout_ms=45_000), 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = (
                f"reloading the waiting page failed: "
                f"{type(exc).__name__}: {exc}"
            )

    async def _goto_start_page(self, timeout_ms: int = 60_000) -> None:
        """Open a Project through its authenticated list when possible.

        Direct repeated hits to a Project URL can trigger an avoidable
        Cloudflare verification page. Clicking the project from Claude's own
        Projects route keeps navigation inside the authenticated web app.
        """
        deadline = time.monotonic() + (timeout_ms / 1000)

        def remaining_ms(cap: int | None = None) -> int:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining <= 0:
                raise TimeoutError("Claude Project navigation budget expired")
            return min(remaining, cap) if cap is not None else remaining

        start_url = self._current_start_url()
        project_match = re.fullmatch(
            r"https://claude\.ai/project/(?P<project>[^/?#]+)",
            start_url.rstrip("/"),
        )
        if not project_match:
            await self.page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=remaining_ms(),
            )
            return

        await self.page.goto(
            "https://claude.ai/projects",
            wait_until="domcontentloaded",
            timeout=remaining_ms(),
        )
        project_path = f"/project/{project_match.group('project')}"
        link = self.page.locator(f'a[href$="{project_path}"]').last
        try:
            await link.wait_for(
                state="visible",
                timeout=remaining_ms(30_000),
            )
            await link.click(timeout=remaining_ms(15_000))
            await self.page.wait_for_url(
                re.compile(re.escape(start_url.rstrip("/")) + r"/?$"),
                timeout=remaining_ms(),
            )
        except Exception:
            await self.page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=remaining_ms(),
            )
    async def _install_sse_tap(self) -> None:
        # Install only after Claude's page and any Cloudflare verification have
        # completed. Re-evaluate before every native turn in case the document
        # was replaced; the script itself is idempotent.
        await self.page.evaluate(SSE_TAP_SCRIPT)
    async def _ensure_healthy_unlocked(self, reason: str) -> None:
        page_closed = False
        try:
            page_closed = bool(self.page is None or self.page.is_closed())
        except Exception:
            page_closed = True
        if (
            self.ready
            and not self._browser_dead.is_set()
            and not page_closed
        ):
            return
        if self._phase in {
            "auth_required",
            "account_unknown",
            "account_changed",
            "project_unavailable",
        }:
            raise ClaudeBrowserUnavailableError(
                self._last_error
                or "Claude authentication/account verification is required"
            )
        now = time.monotonic()
        if now < self._next_recovery_at:
            retry_after = max(1, int(self._next_recovery_at - now + 0.999))
            raise ClaudeBrowserUnavailableError(
                "Camoufox recovery is cooling down after a failed launch; "
                f"retry in {retry_after}s"
            )
        if self._recovery_exhausted and self._restart_circuit_still_open(now):
            raise ClaudeBrowserUnavailableError(
                self._last_error or "Camoufox restart circuit is open"
            )
        await self._recover_browser_unlocked(reason)

    def _restart_circuit_still_open(self, now: float) -> bool:
        """Whether recent restarts still justify refusing another launch."""
        recent = [
            started
            for started in self._restart_times
            if now - started <= self._restart_window
        ]
        if recent:
            return True
        self._recovery_exhausted = False
        self._restart_times = []
        return False
