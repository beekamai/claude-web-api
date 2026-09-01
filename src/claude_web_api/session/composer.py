"""Driving the claude.ai composer: the pre-native chat path and its helpers."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from claude_web_api.session.errors import (
    ClaudeCompletionRejectedError,
    ClaudeConversationLimitError,
    ClaudeLimitError,
    ClaudeServiceUnavailableError,
    ClaudeTurnOutcomeUnknownError,
    ClaudeUsageLimitError,
)
from claude_web_api.session.state import SessionState


class ChatComposerMixin(SessionState):
    """Driving the claude.ai composer: the pre-native chat path and its helpers."""

    async def _input_locator(self) -> Any:
        selectors = [
            'div.ProseMirror[contenteditable="true"]',
            'div[contenteditable="true"].ProseMirror',
            'fieldset div[contenteditable="true"]',
            'div[contenteditable="true"][data-testid]',
            'div[contenteditable="true"]',
        ]
        for selector in selectors:
            locator = self.page.locator(selector).last
            try:
                if (
                    await asyncio.wait_for(locator.count(), timeout=3)
                    and await asyncio.wait_for(
                        locator.is_visible(),
                        timeout=3,
                    )
                ):
                    return locator
            except Exception:
                continue
        return None
    async def new_chat(self) -> None:
        async with self._lock:
            await self._ensure_healthy_unlocked("new chat requested")
            try:
                await asyncio.wait_for(
                    self._new_chat_unlocked(),
                    timeout=90,
                )
            except Exception as exc:
                await self._recover_browser_unlocked(
                    f"new-chat navigation failed: {exc}"
                )
            self._history_recovery_required = False
            self._operation_id = None
            self._conversation_client_session_id = None
            self._set_phase("idle")
    async def _new_chat_unlocked(self) -> None:
        self._set_phase("navigating")
        self._clear_native_state()
        if self.page.url.rstrip("/") == self._current_start_url().rstrip("/"):
            await self._wait_ready(timeout=60)
            self._conversation_privacy_mode = self._privacy_mode
            self._set_phase("composer_ready")
            return
        await self._goto_start_page(timeout_ms=60_000)
        await self._wait_ready(timeout=60)
        await self._install_sse_tap()
        self._conversation_privacy_mode = self._privacy_mode
        self._set_phase("composer_ready")
    async def _prepare_composer_unlocked(
        self,
        *,
        new_chat: bool,
        native: bool,
    ) -> None:
        await self._ensure_healthy_unlocked("browser was not ready before submit")
        await self._verify_account_unchanged_unlocked()
        self._set_phase("preparing_turn")
        if new_chat:
            await self._new_chat_unlocked()
            self._history_recovery_required = False
        await asyncio.wait_for(
            self._ensure_input(mark_history_recovery=native),
            timeout=90,
        )
        if native:
            await asyncio.wait_for(self._install_sse_tap(), timeout=10)
        self._set_phase("composer_ready")
    async def chat(
        self,
        message: str,
        timeout: float = 300.0,
        new_chat: bool = False,
    ) -> str:
        """Plain text endpoint; Project Instructions supply the trusted role."""
        async with self._lock:
            if self._native_active:
                raise RuntimeError(
                    "a native host-tool response is waiting for tool_result"
                )
            self._operation_id = uuid.uuid4().hex
            try:
                await self._prepare_composer_unlocked(
                    new_chat=new_chat,
                    native=False,
                )
                result = await self._send_and_wait(message, timeout=timeout)
                self._operation_id = None
                self._set_phase("idle")
                return result
            except asyncio.CancelledError:
                if self._phase in {
                    "submit_enter_dispatching",
                    "submit_enter_sent",
                    "submit_acknowledged",
                    "waiting_response",
                }:
                    self._history_recovery_required = True
                    self._mark_browser_dead(
                        "plain chat request was cancelled after submit"
                    )
                else:
                    self._operation_id = None
                    if self.ready:
                        self._set_phase("idle")
                raise
            except ClaudeTurnOutcomeUnknownError as exc:
                self._history_recovery_required = True
                self._mark_browser_dead(
                    f"plain chat delivery became ambiguous: {exc}"
                )
                raise
            except Exception as exc:
                if self._phase in {
                    "submit_enter_dispatching",
                    "submit_enter_sent",
                    "submit_acknowledged",
                    "completion_intercepted",
                    "waiting_first_sse",
                    "waiting_sse",
                    "waiting_response",
                }:
                    operation_id = self._operation_id
                    self._history_recovery_required = True
                    self._mark_browser_dead(
                        f"plain chat failed after submit: {exc}"
                    )
                    raise ClaudeTurnOutcomeUnknownError(
                        "Claude turn was submitted, but its final outcome is "
                        "unknown; it was not replayed",
                        operation_id,
                    ) from exc
                self._operation_id = None
                if self.ready and not self._browser_dead.is_set():
                    self._set_phase("idle")
                raise
    async def _submit_message(self, message: str) -> None:
        enter_dispatch_started = False
        try:
            self._set_phase("submit_pre_enter")
            box = await asyncio.wait_for(self._input_locator(), timeout=5)
            if not box:
                raise RuntimeError(
                    "Chat input disappeared while sending a message"
                )
            before_users = await asyncio.wait_for(
                self._user_count(),
                timeout=5,
            )
            await asyncio.wait_for(box.click(), timeout=5)
            await asyncio.wait_for(
                box.evaluate(
                    """(el, text) => {
                        el.focus();
                        el.innerHTML = '';
                        const p = document.createElement('p');
                        p.textContent = text;
                        el.appendChild(p);
                        el.dispatchEvent(new InputEvent('input', {
                            bubbles: true,
                            data: text,
                            inputType: 'insertText'
                        }));
                    }""",
                    message,
                ),
                timeout=5,
            )
            await asyncio.sleep(0.15)
            enter_dispatch_started = True
            self._set_phase("submit_enter_dispatching")
            await asyncio.wait_for(
                self.page.keyboard.press("Enter"),
                timeout=5,
            )
            self._set_phase("submit_enter_sent")

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (
                    await asyncio.wait_for(
                        self._user_count(),
                        timeout=3,
                    )
                    > before_users
                ):
                    self._set_phase("submit_acknowledged")
                    return
                await asyncio.wait_for(
                    self._raise_if_limited([]),
                    timeout=3,
                )
                await asyncio.sleep(0.2)
            await asyncio.wait_for(
                self._raise_if_limited([]),
                timeout=3,
            )
            raise RuntimeError("Message was not acknowledged by Claude")
        except (
            ClaudeLimitError,
            ClaudeServiceUnavailableError,
            ClaudeCompletionRejectedError,
        ):
            raise
        except ClaudeTurnOutcomeUnknownError:
            raise
        except Exception as exc:
            if enter_dispatch_started:
                raise ClaudeTurnOutcomeUnknownError(
                    "Enter was dispatched to Claude, but message delivery "
                    "could not be confirmed; the turn was not replayed",
                    self._operation_id,
                ) from exc
            raise
    async def _ensure_input(
        self,
        *,
        mark_history_recovery: bool = False,
    ) -> Any:
        box = await self._input_locator()
        if not box:
            if mark_history_recovery:
                self._history_recovery_required = True
            await self._goto_start_page(timeout_ms=60_000)
            await self._wait_ready(timeout=60)
            await self._install_sse_tap()
            box = await self._input_locator()
        if not box:
            raise RuntimeError("Chat input not available — are you logged in?")
        return box
    async def _send_and_wait(self, message: str, timeout: float) -> str:
        completion_errors: list[str] = []
        capture_tasks: list[asyncio.Task[Any]] = []

        async def capture_completion(response: Any) -> None:
            try:
                if "/completion" not in response.url:
                    return
                if response.status >= 400:
                    completion_errors.append(
                        f"HTTP {response.status}: {await response.text()}"
                    )
            except Exception:
                return

        def on_response(response: Any) -> None:
            capture_tasks.append(asyncio.create_task(capture_completion(response)))

        before = await asyncio.wait_for(
            self._assistant_count(),
            timeout=self._watchdog_probe_timeout,
        )
        self.page.on("response", on_response)
        try:
            await self._submit_message(message)
            self._set_phase("waiting_response")
            result = await self._wait_response(
                before,
                timeout=timeout,
                completion_errors=completion_errors,
            )
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)
            return result
        finally:
            self.page.remove_listener("response", on_response)
            for task in capture_tasks:
                if not task.done():
                    task.cancel()
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)
    async def _assistant_count(self) -> int:
        return await self.page.evaluate(
            """() => document.querySelectorAll('[data-is-streaming]').length"""
        )
    async def _user_count(self) -> int:
        return await self.page.evaluate(
            """() => document.querySelectorAll('[data-testid="user-message"]').length"""
        )
    async def _is_generating(self) -> bool:
        return await self.page.evaluate(
            """() => {
                if (document.querySelector('[data-is-streaming="true"]')) return true;
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const button of buttons) {
                    const text = (
                        button.getAttribute('aria-label') || button.textContent || ''
                    ).toLowerCase();
                    if (
                        text.includes('stop') ||
                        text.includes('остановить') ||
                        text.includes('прервать')
                    ) return true;
                }
                return false;
            }"""
        )
    async def _raise_if_limited(self, response_errors: list[str]) -> None:
        visible_errors = await self.page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '[role="alert"], [role="dialog"], [data-testid*="error"], [class*="toast"]'
            )).filter(el => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden';
            }).map(el => (el.innerText || '').trim()).filter(Boolean).join('\\n')"""
        )
        text = "\n".join([*response_errors, visible_errors]).lower()
        if not text:
            return
        conversation_patterns = (
            "maximum conversation length",
            "conversation has reached its maximum",
            "conversation is too long",
            "maximum chat length",
            "достигнута максимальная длина диалога",
            "диалог слишком длинный",
        )
        usage_patterns = (
            "you've reached your limit",
            "you’ve reached your limit",
            "usage limit reached",
            "out of messages",
            "limit will reset",
            "limit resets at",
            "достигнут лимит использования",
            "лимит сбросится",
        )
        if any(pattern in text for pattern in conversation_patterns):
            raise ClaudeConversationLimitError(
                "claude.ai reports that this conversation reached its length limit",
                replay_safe=not self._native_saw_tool,
            )
        if any(
            error.startswith("HTTP 529:")
            for error in response_errors
        ):
            raise ClaudeServiceUnavailableError(
                "claude.ai is temporarily overloaded (HTTP 529)"
            )
        if any(pattern in text for pattern in usage_patterns) or any(
            error.startswith("HTTP 429:")
            for error in response_errors
        ):
            raise ClaudeUsageLimitError(
                "claude.ai reports that the current account reached its usage limit",
                replay_safe=not self._native_saw_tool,
            )
    async def _last_assistant_text(self) -> str:
        return await self.page.evaluate(
            """() => {
                const turns = Array.from(
                    document.querySelectorAll('[data-is-streaming]')
                );
                const latest = turns.pop();
                const response = latest?.querySelector('.font-claude-response');
                const directText = (response?.innerText || '').trim();
                if (directText) return directText;

                const candidates = [];
                const add = (element) => {
                    if (!element) return;
                    const text = (element.innerText || '').trim();
                    if (text) candidates.push(text);
                };
                document.querySelectorAll(
                    '[data-testid="assistant-message"]'
                ).forEach(add);
                if (!candidates.length) {
                    const blocks = Array.from(
                        document.querySelectorAll(
                            '.font-claude-message, .prose, [class*="Message"]'
                        )
                    );
                    if (blocks.length) add(blocks[blocks.length - 1]);
                }
                return candidates.length
                    ? candidates[candidates.length - 1]
                    : '';
            }"""
        )
    async def _wait_response(
        self,
        before_count: int,
        timeout: float,
        completion_errors: list[str] | None = None,
    ) -> str:
        deadline = time.time() + timeout
        saw_generate = False
        response_started = False
        last = ""
        stable = 0
        last_limit_check = 0.0

        while time.time() < deadline:
            if time.time() - last_limit_check >= 1.0:
                await asyncio.wait_for(
                    self._raise_if_limited(completion_errors or []),
                    timeout=self._watchdog_probe_timeout,
                )
                last_limit_check = time.time()
            generating = await asyncio.wait_for(
                self._is_generating(),
                timeout=self._watchdog_probe_timeout,
            )
            if generating:
                saw_generate = True
                response_started = True
                self._last_progress_at = time.monotonic()
            count = await asyncio.wait_for(
                self._assistant_count(),
                timeout=self._watchdog_probe_timeout,
            )
            if count > before_count:
                response_started = True
            text = (
                await asyncio.wait_for(
                    self._last_assistant_text(),
                    timeout=self._watchdog_probe_timeout,
                )
                if response_started
                else ""
            )
            if text and text != last:
                last = text
                stable = 0
                self._last_progress_at = time.monotonic()
            elif text and saw_generate and not generating:
                stable += 1
                if stable >= 3:
                    return self._clean(last)
            elif text and not saw_generate and count > before_count and not generating:
                stable += 1
                if stable >= 4:
                    return self._clean(last)
            await asyncio.sleep(0.3)

        if response_started and last:
            return self._clean(last)
        await asyncio.wait_for(
            self._raise_if_limited(completion_errors or []),
            timeout=self._watchdog_probe_timeout,
        )
        raise TimeoutError("Timed out waiting for a new Claude response")
    @staticmethod
    def _clean(text: str) -> str:
        text = text.strip()
        text = re.sub(
            r"(?m)^(?P<label>[^\n]+)\n[\ue000-\uf8ff]\n(?P=label)\n+",
            "",
            text,
        )
        ui_labels = (
            r"Copy|Retry|Share|Edit|Good response|Bad response|"
            r"Копировать|Повторить|Поделиться|Редактировать"
        )
        text = re.sub(
            rf"(?:\n(?:{ui_labels})\s*)+$",
            "",
            text,
            flags=re.I,
        )
        return text.strip()
