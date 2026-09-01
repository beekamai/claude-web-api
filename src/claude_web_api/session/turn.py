"""One native turn: sending it, waiting it out, and carrying tool results."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable

from claude_web_api.session.errors import (
    ClaudeAccountIdentityError,
    ClaudeBrowserUnavailableError,
    ClaudeCompletionRejectedError,
    ClaudeLimitError,
    ClaudeServiceUnavailableError,
    ClaudeTurnOutcomeUnknownError,
)
from claude_web_api.session.models import NativeTurn
from claude_web_api.session.state import SessionState


class NativeTurnMixin(SessionState):
    """One native turn: sending it, waiting it out, and carrying tool results."""

    def last_completion_shape(self) -> dict[str, Any]:
        return dict(self._last_completion_shape)
    def client_session_requires_new(
        self,
        client_session_id: str | None,
    ) -> bool:
        return bool(
            client_session_id
            and client_session_id != self._conversation_client_session_id
        )
    def privacy_mode_requires_new(self, privacy_mode: str) -> bool:
        requested = "ephemeral" if privacy_mode == "ephemeral" else "keep"
        if self._conversation_privacy_mode is None:
            return requested == "ephemeral"
        return requested != self._conversation_privacy_mode
    async def native_request_state(
        self,
        client_session_id: str | None = None,
    ) -> tuple[set[str], bool]:
        """Return pending IDs and whether a reset chat needs history recovery."""
        async with self._lock:
            if self._native_active and self._native_wait_expired():
                await self._expire_native_lease_unlocked()
            if (
                self._native_active
                and self._native_pending_ids
                and client_session_id
                and self._native_client_session_id
                and client_session_id != self._native_client_session_id
            ):
                raise ValueError(
                    "tool_result belongs to another OpenClaude session"
                )
            return (
                set(self._native_pending_ids),
                self._history_recovery_required,
            )
    async def mark_history_recovered(self) -> None:
        async with self._lock:
            self._history_recovery_required = False
    async def abandon_pending_native(
        self,
        expected_ids: set[str],
        *,
        client_session_id: str | None = None,
    ) -> bool:
        """Atomically abandon a stale tool wait before recovering IDE history.

        A real user turn may overtake a host command after OpenClaude's local
        query watchdog fires. Re-check all ownership state under the session
        lock so a concurrent legitimate continuation cannot be discarded.
        """
        async with self._lock:
            if not self._native_active or not self._native_pending_ids:
                return False
            if (
                client_session_id
                and self._native_client_session_id
                and client_session_id != self._native_client_session_id
            ):
                raise ValueError(
                    "pending tool stream belongs to another OpenClaude session"
                )
            if set(expected_ids) != self._native_pending_ids:
                raise RuntimeError(
                    "pending Claude tool_use IDs changed before interruption "
                    "recovery"
                )

            self._history_recovery_required = True
            self._clear_native_state()
            self._operation_id = None
            await self._ensure_healthy_unlocked(
                "recovering from an interrupted OpenClaude host command"
            )
            await asyncio.wait_for(self._new_chat_unlocked(), timeout=90)
            self._history_recovery_required = True
            self._set_phase("idle")
            return True
    async def _expire_native_lease_unlocked(self) -> None:
        """Abandon an expired side-channel before touching the browser."""
        self._history_recovery_required = True
        self._clear_native_state()
        self._operation_id = None
        await self._ensure_healthy_unlocked(
            "Camoufox was unavailable when the tool-result lease expired"
        )
        await asyncio.wait_for(self._new_chat_unlocked(), timeout=90)
        self._history_recovery_required = True
        self._set_phase("idle")
    def _clear_native_state(self) -> None:
        self._native_active = False
        self._native_tools = []
        self._native_internal_tool_names = set()
        self._native_internal_tool_acks = 0
        self._native_internal_text_prefix = []
        self._native_internal_thinking_prefix = []
        self._native_queue = None
        self._native_completion_url = None
        self._native_org_uuid = None
        self._native_conversation_uuid = None
        self._native_headers = {}
        self._native_pending_ids = set()
        self._native_pending_deadline = None
        self._native_parallel_tool_calls = True
        self._native_blocks = {}
        self._native_text_blocks = {}
        self._native_tool_blocks = {}
        self._native_thinking_blocks = {}
        self._native_usage = {}
        self._native_model = None
        self._native_stop_reason = None
        self._native_terminal_seen = False
        self._native_requested_model = None
        self._native_thinking_mode = "auto"
        self._native_effort = None
        self._native_conversation_verified = False
        self._native_event_sink = None
        self._native_client_session_id = None
    async def native_chat(
        self,
        message: str,
        tools: list[dict[str, Any]],
        internal_tool_names: set[str] | None = None,
        timeout: float = 300.0,
        new_chat: bool = False,
        parallel_tool_calls: bool = True,
        recovery_message: str | None = None,
        model: str | None = None,
        thinking_mode: str = "auto",
        effort: str | None = None,
        privacy_mode: str = "keep",
        client_session_id: str | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> NativeTurn:
        """Start a Claude turn with native host tools injected into completion."""
        async with self._lock:
            if model:
                available_ids = {
                    str(item.get("id"))
                    for item in self._available_models
                    if (
                        item.get("available") is True
                        and item.get("access_status") == "available"
                    )
                }
                if not self._available_models:
                    raise ValueError(
                        "claude.ai account model catalog is unavailable; "
                        "use claude-web/auto until account discovery succeeds"
                    )
                if model not in available_ids:
                    raise ValueError(
                        f"model {model!r} is not available to the active "
                        "claude.ai account"
                    )
            if (
                client_session_id
                and self._conversation_client_session_id
                and client_session_id
                != self._conversation_client_session_id
            ):
                new_chat = True
            requested_privacy = (
                "ephemeral"
                if privacy_mode == "ephemeral"
                else "keep"
            )
            if (
                self._conversation_privacy_mode is not None
                and requested_privacy != self._conversation_privacy_mode
            ):
                new_chat = True
            elif (
                self._conversation_privacy_mode is None
                and requested_privacy == "ephemeral"
            ):
                new_chat = True
            if self._fresh_chat_required:
                # Set when a previous turn was cancelled after submitting.
                new_chat = True
                self._fresh_chat_required = False
            if new_chat:
                pass
            elif self._native_active and self._native_wait_expired():
                await self._new_chat_unlocked()
                self._history_recovery_required = True
            if self._native_active:
                raise RuntimeError(
                    "previous native tool_use is still waiting for tool_result"
                )
            self._operation_id = uuid.uuid4().hex
            submitted = False
            self._privacy_mode = requested_privacy
            try:
                for attempt in range(2):
                    try:
                        await self._prepare_composer_unlocked(
                            new_chat=new_chat and attempt == 0,
                            native=True,
                        )
                        outbound_message = message
                        recovering_history = self._history_recovery_required
                        if recovering_history:
                            if recovery_message is None:
                                raise ClaudeBrowserUnavailableError(
                                    "Camoufox recovered before submit, but this "
                                    "caller did not provide IDE history for a "
                                    "safe context rebuild"
                                )
                            outbound_message = recovery_message
                        self._reset_native_parser()
                        self._native_tools = tools
                        self._native_internal_tool_names = set(
                            internal_tool_names or ()
                        )
                        self._native_parallel_tool_calls = parallel_tool_calls
                        self._native_requested_model = model
                        self._native_thinking_mode = thinking_mode
                        self._native_effort = effort
                        await self._activate_trusted_turn_context()
                        self._native_event_sink = event_sink
                        self._native_client_session_id = client_session_id
                        self._native_active = True
                        await self._submit_message(outbound_message)
                        submitted = True
                        if client_session_id:
                            self._conversation_client_session_id = (
                                client_session_id
                            )
                        self._conversation_privacy_mode = requested_privacy
                        if recovering_history:
                            self._history_recovery_required = False
                        break
                    except ClaudeTurnOutcomeUnknownError:
                        raise
                    except ClaudeAccountIdentityError:
                        self._clear_native_state()
                        raise
                    except ClaudeBrowserUnavailableError:
                        self._clear_native_state()
                        raise
                    except Exception as exc:
                        self._clear_native_state()
                        if attempt:
                            raise ClaudeBrowserUnavailableError(
                                f"Camoufox failed before message submission: {exc}"
                            ) from exc
                        await self._recover_browser_unlocked(
                            f"pre-submit failure: {exc}"
                        )
                return await self._await_native_outcome(timeout)
            except asyncio.CancelledError:
                committed = submitted or self._phase in {
                    "submit_enter_dispatching",
                    "submit_enter_sent",
                    "submit_acknowledged",
                    "completion_intercepted",
                    "waiting_first_sse",
                    "waiting_sse",
                }
                self._clear_native_state()
                if committed:
                    # The client hung up — Ctrl+C in the IDE, or a closed
                    # stream. The browser itself is fine, so restarting it
                    # would spend a recovery (and five of those open the
                    # restart circuit). The page may still be generating into
                    # the abandoned chat, so the next turn starts a clean one.
                    self._history_recovery_required = True
                    self._fresh_chat_required = True
                    self._operation_id = None
                    self._last_error = (
                        "native request was cancelled after submit"
                    )
                    if self.ready:
                        self._set_phase("idle")
                else:
                    self._operation_id = None
                    if self.ready:
                        self._set_phase("idle")
                raise
            except (
                ClaudeLimitError,
                ClaudeServiceUnavailableError,
                ClaudeCompletionRejectedError,
            ):
                self._clear_native_state()
                self._operation_id = None
                self._set_phase("idle")
                raise
            except ClaudeTurnOutcomeUnknownError as exc:
                self._history_recovery_required = True
                self._clear_native_state()
                self._mark_browser_dead(
                    f"native submit became ambiguous: {exc}"
                )
                raise
            except Exception as exc:
                operation_id = self._operation_id
                self._clear_native_state()
                if submitted:
                    self._history_recovery_required = True
                    self._mark_browser_dead(
                        f"native turn failed after submit: {exc}"
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
    async def continue_native(
        self,
        results: list[dict[str, Any]],
        timeout: float = 300.0,
        client_session_id: str | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> NativeTurn:
        """Post host results to Claude's open side-channel and await continuation."""
        async with self._lock:
            if not self._native_active:
                raise RuntimeError(
                    "received tool_result but no Claude tool_use stream is pending"
                )
            if (
                client_session_id
                and self._native_client_session_id
                and client_session_id != self._native_client_session_id
            ):
                raise ValueError(
                    "tool_result belongs to another OpenClaude session"
                )
            self._native_event_sink = event_sink
            if self._native_wait_expired():
                await self._expire_native_lease_unlocked()
                raise TimeoutError(
                    "Claude tool_result lease expired; start a fresh IDE turn"
                )
            supplied_id_rows = [
                str(result.get("tool_call_id", ""))
                for result in results
                if result.get("tool_call_id")
            ]
            if len(supplied_id_rows) != len(set(supplied_id_rows)):
                raise ValueError("duplicate tool_result IDs are not allowed")
            supplied_ids = set(supplied_id_rows)
            if supplied_ids != self._native_pending_ids:
                missing = sorted(self._native_pending_ids - supplied_ids)
                unexpected = sorted(supplied_ids - self._native_pending_ids)
                raise ValueError(
                    "tool_result IDs do not match pending Claude tool_use IDs"
                    f"; missing={missing}; unexpected={unexpected}"
                )
            if (
                self._browser_dead.is_set()
                or not self.ready
                or self.page is None
            ):
                operation_id = self._operation_id
                self._history_recovery_required = True
                self._clear_native_state()
                raise ClaudeTurnOutcomeUnknownError(
                    "Camoufox died while a native tool result was pending; "
                    "the result was not posted or replayed",
                    operation_id,
                )
            try:
                await self._verify_account_unchanged_unlocked()
            except (ClaudeAccountIdentityError, ClaudeBrowserUnavailableError):
                self._history_recovery_required = True
                self._clear_native_state()
                raise
            try:
                for result in results:
                    tool_call_id = str(result.get("tool_call_id", ""))
                    self._tool_result_delivery[tool_call_id] = "dispatching"
                    self._set_phase("posting_tool_result")
                    try:
                        await asyncio.wait_for(
                            self._post_tool_result(result),
                            timeout=self._tool_result_post_timeout + 2,
                        )
                    except Exception as exc:
                        self._tool_result_delivery[tool_call_id] = "unknown"
                        operation_id = self._operation_id
                        self._history_recovery_required = True
                        self._clear_native_state()
                        self._mark_browser_dead(
                            f"tool_result delivery became ambiguous: {exc}"
                        )
                        raise ClaudeTurnOutcomeUnknownError(
                            "A Claude tool_result POST was dispatched, but its "
                            "outcome is unknown; it was not sent again",
                            operation_id,
                        ) from exc
                    self._tool_result_delivery[tool_call_id] = "accepted"
                    self._last_progress_at = time.monotonic()
                self._native_pending_ids.clear()
                self._native_pending_deadline = None
                self._set_phase("waiting_continuation_sse")
                return await self._await_native_outcome(timeout)
            except asyncio.CancelledError:
                for tool_call_id, state in list(
                    self._tool_result_delivery.items()
                ):
                    if state == "dispatching":
                        self._tool_result_delivery[tool_call_id] = "unknown"
                self._history_recovery_required = True
                self._clear_native_state()
                self._mark_browser_dead(
                    "native tool_result request was cancelled after dispatch"
                )
                raise
            except ClaudeTurnOutcomeUnknownError:
                raise
            except Exception as exc:
                operation_id = self._operation_id
                accepted = any(
                    state == "accepted"
                    for state in self._tool_result_delivery.values()
                )
                if accepted:
                    self._history_recovery_required = True
                    self._clear_native_state()
                    self._mark_browser_dead(
                        f"continuation failed after tool_result acceptance: {exc}"
                    )
                    raise ClaudeTurnOutcomeUnknownError(
                        "Claude accepted at least one tool_result, but the "
                        "continuation outcome is unknown; results were not "
                        "posted again",
                        operation_id,
                    ) from exc
                self._clear_native_state()
                raise
    def _with_internal_prefix(self, turn: NativeTurn) -> NativeTurn:
        text_parts = [
            *self._native_internal_text_prefix,
            turn.content or "",
        ]
        thinking_parts = [
            *self._native_internal_thinking_prefix,
            turn.thinking or "",
        ]
        content = "".join(text_parts) or None
        thinking = "".join(thinking_parts) or None
        self._native_internal_text_prefix = []
        self._native_internal_thinking_prefix = []
        return NativeTurn(
            content=content,
            tool_uses=turn.tool_uses,
            thinking=thinking,
            usage=turn.usage,
            model=turn.model,
            stop_reason=turn.stop_reason,
        )
    def _internal_tool_result_content(self, name: str) -> str:
        for tool in self._native_tools:
            if (
                isinstance(tool, dict)
                and tool.get("name") == name
            ):
                description = str(tool.get("description", "") or "").strip()
                if description:
                    return (
                        "OpenClaude bridge metadata result. No host action was "
                        "performed.\n\n" + description
                    )
        raise RuntimeError(
            f"internal OpenClaude tool {name!r} has no metadata definition"
        )
    async def _consume_native_tools_if_ready(self) -> NativeTurn | None:
        ready = self._take_native_tools_if_ready()
        if ready is None:
            return None
        await self._verify_native_conversation_binding()
        internal = [
            tool
            for tool in ready.tool_uses
            if tool.name in self._native_internal_tool_names
        ]
        if not internal:
            return self._with_internal_prefix(ready)
        if len(internal) != len(ready.tool_uses):
            raise RuntimeError(
                "Claude mixed an internal OpenClaude metadata carrier with "
                "host tool calls"
            )
        if len(internal) != 1 or self._native_internal_tool_acks:
            raise RuntimeError(
                "Claude invoked the internal OpenClaude metadata carrier more "
                "than once"
            )
        self._native_internal_tool_acks += 1
        if ready.content:
            self._native_internal_text_prefix.append(ready.content)
        if ready.thinking:
            self._native_internal_thinking_prefix.append(ready.thinking)
        tool = internal[0]
        result = {
            "tool_call_id": tool.id,
            "name": tool.name,
            "content": self._internal_tool_result_content(tool.name),
            "is_error": False,
        }
        self._tool_result_delivery[tool.id] = "dispatching"
        self._set_phase("posting_internal_context_result")
        try:
            await asyncio.wait_for(
                self._post_tool_result(result),
                timeout=self._tool_result_post_timeout + 2,
            )
        except asyncio.CancelledError:
            self._tool_result_delivery[tool.id] = "unknown"
            raise
        except Exception as exc:
            self._tool_result_delivery[tool.id] = "unknown"
            raise ClaudeTurnOutcomeUnknownError(
                "The internal OpenClaude metadata result was dispatched, but "
                "its outcome is unknown; it was not sent again",
                self._operation_id,
            ) from exc
        self._tool_result_delivery[tool.id] = "accepted"
        self._native_pending_ids.clear()
        self._native_pending_deadline = None
        self._last_progress_at = time.monotonic()
        self._set_phase("waiting_continuation_sse")
        return None
    async def _await_native_outcome(self, timeout: float) -> NativeTurn:
        if self._native_queue is None:
            raise RuntimeError("native SSE queue is not initialized")
        deadline = time.time() + timeout

        while time.time() < deadline:
            ready = await self._consume_native_tools_if_ready()
            if ready is not None:
                return ready
            remaining = deadline - time.time()
            try:
                envelope = await asyncio.wait_for(
                    self._native_queue.get(),
                    timeout=min(1.0, remaining),
                )
            except asyncio.TimeoutError:
                await asyncio.wait_for(
                    self._raise_if_limited([]),
                    timeout=self._watchdog_probe_timeout,
                )
                continue

            terminal = self._process_native_event(envelope)

            # Drain frames already delivered by the browser. This preserves a
            # true parallel batch when it is present without adding a timing
            # heuristic; a later block is safely serialized into the next turn.
            while self._native_queue is not None:
                try:
                    queued = self._native_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                terminal = self._process_native_event(queued) or terminal

            ready = await self._consume_native_tools_if_ready()
            if ready is not None:
                return ready

            if terminal:
                await self._verify_native_conversation_binding()
                content = self._take_native_text()
                thinking = self._take_native_thinking()
                usage = dict(self._native_usage)
                model = self._native_model
                stop_reason = self._native_stop_reason
                completed = self._with_internal_prefix(
                    NativeTurn(
                        content=content,
                        tool_uses=[],
                        thinking=thinking,
                        usage=usage,
                        model=model,
                        stop_reason=stop_reason,
                    )
                )
                self._clear_native_state()
                self._operation_id = None
                self._set_phase("idle")
                return completed

        await asyncio.wait_for(
            self._raise_if_limited([]),
            timeout=self._watchdog_probe_timeout,
        )
        raise TimeoutError("Timed out waiting for claude.ai SSE response")
    async def _verify_native_conversation_binding(self) -> None:
        """Fail closed unless the native chat has the expected Project/privacy."""
        if self._native_conversation_verified:
            return
        if not self._project_instructions:
            self._native_conversation_verified = True
            return
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if (
            not project_id
            or not self._native_org_uuid
            or not self._native_conversation_uuid
        ):
            raise RuntimeError(
                "native conversation lacks verified Claude Project metadata"
            )
        result = await asyncio.wait_for(
            self.page.evaluate(
                """
                async ({organizationUuid, conversationUuid}) => {
                  const response = await fetch(
                    `/api/organizations/${organizationUuid}/chat_conversations/`
                      + `${conversationUuid}?rendering_mode=messages`,
                    {
                      credentials: 'include',
                      cache: 'no-store',
                      headers: {Accept: 'application/json'}
                    }
                  );
                  if (!response.ok) {
                    return {ok: false, status: response.status};
                  }
                  const body = await response.json();
                  return {
                    ok: true,
                    projectUuid: String(
                      body?.project_uuid
                      || body?.project?.uuid
                      || body?.project?.id
                      || ''
                    ),
                    isTemporary: Boolean(
                      body?.is_temporary ?? body?.temporary ?? false
                    )
                  };
                }
                """,
                {
                    "organizationUuid": self._native_org_uuid,
                    "conversationUuid": self._native_conversation_uuid,
                },
            ),
            timeout=15,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            status = (
                result.get("status")
                if isinstance(result, dict)
                else "invalid"
            )
            raise RuntimeError(
                "could not verify native conversation metadata "
                f"(status={status})"
            )
        if str(result.get("projectUuid") or "") != project_id:
            raise RuntimeError(
                "native conversation is not attached to the verified "
                "OpenClaude Project"
            )
        if (
            self._privacy_mode == "ephemeral"
            and result.get("isTemporary") is not True
        ):
            raise RuntimeError(
                "ephemeral native conversation was persisted unexpectedly"
            )
        self._native_conversation_verified = True
    def _take_native_tools_if_ready(self) -> NativeTurn | None:
        # For host tools claude.ai pauses the same SSE immediately after the
        # tool_use content_block_stop and emits no message_stop until
        # /tool_result resumes it. That block stop is therefore the native
        # execution boundary used by the web client, not a timing heuristic.
        if not self._native_tool_blocks:
            return None
        ordered = sorted(self._native_tool_blocks.items())
        has_internal_tool = any(
            tool.name in self._native_internal_tool_names
            for _, tool in ordered
        )
        selected = (
            ordered
            if self._native_parallel_tool_calls or has_internal_tool
            else ordered[:1]
        )
        for index, _ in selected:
            self._native_tool_blocks.pop(index, None)
        tool_uses = [tool for _, tool in selected]
        content = self._take_native_text()
        thinking = self._take_native_thinking()
        self._native_pending_ids = {tool.id for tool in tool_uses}
        self._native_pending_deadline = (
            time.time() + self._tool_result_lease_seconds
        )
        self._set_phase("waiting_host_result")
        return NativeTurn(
            content=content,
            tool_uses=tool_uses,
            thinking=thinking,
            # The same upstream stream resumes after /tool_result and its
            # final usage may be cumulative. Report it only at message_stop.
            usage={},
            model=self._native_model,
        )
    def _native_wait_expired(self) -> bool:
        return bool(
            self._native_pending_deadline is not None
            and time.time() >= self._native_pending_deadline
        )
    async def _post_tool_result(self, result: dict[str, Any]) -> None:
        if not self._native_org_uuid or not self._native_conversation_uuid:
            raise RuntimeError("Claude completion URL was not captured")
        tool_call_id = str(result.get("tool_call_id", ""))
        body: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": [
                {
                    "type": "text",
                    "text": str(result.get("content", "") or ""),
                }
            ],
        }
        if result.get("is_error"):
            body["is_error"] = True
        url = (
            f"https://claude.ai/api/organizations/{self._native_org_uuid}/"
            f"chat_conversations/{self._native_conversation_uuid}/tool_result"
        )
        response = await self.page.evaluate(
            """
            async ({url, headers, body, timeoutMs}) => {
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), timeoutMs);
              try {
                const response = await fetch(url, {
                  method: 'POST',
                  credentials: 'include',
                  headers: {'Content-Type': 'application/json', ...headers},
                  body: JSON.stringify(body),
                  signal: controller.signal
                });
                return {
                  ok: response.ok,
                  status: response.status,
                  text: await response.text()
                };
              } finally {
                clearTimeout(timer);
              }
            }
            """,
            {
                "url": url,
                "headers": self._native_headers,
                "body": body,
                "timeoutMs": int(self._tool_result_post_timeout * 1000),
            },
        )
        if not response.get("ok"):
            detail = str(response.get("text", ""))
            if (
                int(response.get("status", 0)) == 404
                and "side_channel_waiting_key_absent" in detail
            ):
                raise RuntimeError(
                    "Claude native tool_result window is no longer open"
                )
            raise RuntimeError(
                "Claude rejected native tool_result "
                f"(HTTP {response.get('status')}): {detail}"
            )
