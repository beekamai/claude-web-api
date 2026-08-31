"""In-process request journal backing the control panel.

Live counters and a bounded event deque sit in front of the persistent
store, so the panel stays responsive while SQLite writes happen off the
request path.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from claude_web_api.protocol.openai_usage import openai_usage
from claude_web_api.providers.claude_web import CLAUDE_WEB_PROVIDER_ID
from claude_web_api.sanitize import public_error_message, sanitize_public_text
from claude_web_api.telemetry.store import TelemetryStore, stable_session_key


class RuntimeTelemetry:
    """Live counters plus a bounded, secret-free persistent journal."""

    def __init__(self, store: TelemetryStore | None = None) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self._active_by_id: dict[str, dict[str, Any]] = {}
        self.last: dict[str, Any] | None = None
        self.store = store
        self.storage_error: str | None = None
        self._finished_since_prune = 0
        self._store_executor: ThreadPoolExecutor | None = None
        self._pending_store_tasks: set[asyncio.Future[Any]] = set()

    def _executor(self) -> ThreadPoolExecutor:
        if self._store_executor is None:
            self._store_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="openclaude-telemetry",
            )
        return self._store_executor

    def _store_call(
        self,
        method: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if self.store is None:
            return None
        try:
            value = getattr(self.store, method)(*args, **kwargs)
            self.storage_error = None
            return value
        except Exception as exc:
            self.storage_error = public_error_message(exc)
            return None

    async def store_call_async(
        self,
        method: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run every SQLite operation on one ordered worker thread."""
        if self.store is None:
            raise RuntimeError("persistent telemetry storage is unavailable")
        operation = partial(
            getattr(self.store, method),
            *args,
            **kwargs,
        )
        try:
            value = await asyncio.get_running_loop().run_in_executor(
                self._executor(),
                operation,
            )
            self.storage_error = None
            return value
        except Exception as exc:
            self.storage_error = public_error_message(exc)
            raise

    def _store_submit(
        self,
        method: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Queue a write without stalling streaming or Camoufox work."""
        if self.store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._store_call(method, *args, **kwargs)
            return
        operation = partial(
            getattr(self.store, method),
            *args,
            **kwargs,
        )
        future = loop.run_in_executor(self._executor(), operation)
        self._pending_store_tasks.add(future)

        def completed(task: asyncio.Future[Any]) -> None:
            self._pending_store_tasks.discard(task)
            try:
                task.result()
                self.storage_error = None
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.storage_error = public_error_message(exc)

        future.add_done_callback(completed)

    async def flush_store(self) -> None:
        pending = list(self._pending_store_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close_store_executor(self) -> None:
        await self.flush_store()
        executor = self._store_executor
        self._store_executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

    def log(
        self,
        level: str,
        component: str,
        message: str,
        *,
        request_id: str | None = None,
    ) -> None:
        now = time.time()
        safe_message = sanitize_public_text(message)
        event = {
            "time": now,
            "level": str(level or "INFO").upper()[:16],
            "component": str(component or "Service")[:80],
            "message": safe_message,
        }
        self.events.appendleft(event)
        self._store_submit(
            "record_event",
            event_time=now,
            level=event["level"],
            component=event["component"],
            message=safe_message,
            request_id=request_id,
        )

    def begin(
        self,
        request_id: str,
        model: str,
        profile_id: str,
        *,
        provider_id: str = CLAUDE_WEB_PROVIDER_ID,
        client_session_id: str | None = None,
        session_key: str | None = None,
        user_text: str | None = None,
        streaming: bool = False,
        privacy_mode: str = "keep",
        capture_content: bool = False,
    ) -> None:
        started_at = time.time()
        session_key = session_key or stable_session_key(
            client_session_id,
            request_id,
        )
        self._active_by_id[request_id] = {
            "request_id": request_id,
            "model": model,
            "profile_id": profile_id,
            "provider_id": provider_id,
            "session_suffix": session_key[-8:],
            "started_at": started_at,
            "first_token_at": None,
            "text_chars": 0,
            "thinking_chars": 0,
            "estimated_output_tokens": 0,
            "actual_usage": None,
            "status": "streaming" if streaming else "running",
            "streaming": bool(streaming),
            "privacy_mode": privacy_mode,
            "capture_content": bool(capture_content),
            "user_text": user_text,
        }
        self._store_submit(
            "begin_request",
            request_id=request_id,
            session_key=session_key,
            profile_id=profile_id,
            provider_id=provider_id,
            requested_model=model,
            started_at=started_at,
            streaming=streaming,
            privacy_mode=privacy_mode,
            user_text=user_text,
            capture_content=capture_content,
        )

    def native_event(
        self,
        request_id: str,
        event: dict[str, Any],
    ) -> None:
        active = self._active_by_id.get(request_id)
        if active is None:
            return
        event_type = event.get("type")
        if event_type == "text_delta":
            text = str(event.get("text") or "")
            active["text_chars"] += len(text)
            if text and active.get("first_token_at") is None:
                active["first_token_at"] = time.time()
        elif event_type == "thinking_delta":
            thinking = str(event.get("thinking") or "")
            active["thinking_chars"] += len(thinking)
            if thinking and active.get("first_token_at") is None:
                active["first_token_at"] = time.time()
        elif event_type == "usage":
            active["actual_usage"] = openai_usage(
                event.get("usage") or {}
            )
        elif event_type == "model" and event.get("model"):
            active["model"] = str(event["model"])
        # Explicitly an estimate for the live panel only. OpenAI usage is
        # emitted solely from upstream usage fields. Thinking is separate.
        active["estimated_output_tokens"] = (
            int(active["text_chars"]) + 3
        ) // 4

    def finish(
        self,
        request_id: str,
        *,
        status: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        assistant_text: str | None = None,
        thinking_text: str | None = None,
        tool_call_count: int = 0,
        resolved_model: str | None = None,
        final_profile_id: str | None = None,
        final_provider_id: str | None = None,
        capture_content: bool | None = None,
        retention_days: int = 30,
        max_requests: int = 5_000,
    ) -> None:
        active = self._active_by_id.pop(request_id, None)
        if active is None:
            return
        finished_at = time.time()
        active["status"] = status
        active["finished_at"] = finished_at
        active["duration_seconds"] = max(
            0.0,
            finished_at - float(active["started_at"]),
        )
        if usage is not None:
            active["actual_usage"] = usage
        if resolved_model:
            active["model"] = resolved_model
        if final_profile_id:
            active["final_profile_id"] = final_profile_id
        if final_provider_id:
            active["final_provider_id"] = final_provider_id
        if assistant_text is not None:
            active["text_chars"] = len(assistant_text)
        if thinking_text is not None:
            active["thinking_chars"] = len(thinking_text)
        active["estimated_output_tokens"] = (
            int(active["text_chars"]) + 3
        ) // 4
        active["tool_call_count"] = max(0, int(tool_call_count))
        if error:
            active["error"] = sanitize_public_text(error)
        exact_completion = (
            active.get("actual_usage", {}).get("completion_tokens")
            if isinstance(active.get("actual_usage"), dict)
            else None
        )
        first_token_at = active.get("first_token_at")
        generation_seconds = (
            finished_at - float(first_token_at)
            if isinstance(first_token_at, (int, float))
            and finished_at > float(first_token_at)
            else None
        )
        if (
            isinstance(exact_completion, int)
            and generation_seconds is not None
            and generation_seconds > 0
        ):
            active["tokens_per_second"] = (
                exact_completion / generation_seconds
            )
        public_active = {
            key: value
            for key, value in active.items()
            if key not in {"capture_content", "user_text"}
        }
        self.last = dict(public_active)
        should_capture = (
            bool(active.get("capture_content"))
            if capture_content is None
            else bool(capture_content)
        )
        self._store_submit(
            "finish_request",
            request_id=request_id,
            status=status,
            finished_at=finished_at,
            first_token_at=active.get("first_token_at"),
            resolved_model=resolved_model or str(active.get("model") or ""),
            final_profile_id=final_profile_id or active.get("profile_id"),
            final_provider_id=(
                final_provider_id or active.get("provider_id")
            ),
            usage=active.get("actual_usage"),
            estimated_output_tokens=active.get("estimated_output_tokens"),
            output_chars=int(active.get("text_chars") or 0),
            thinking_chars=int(active.get("thinking_chars") or 0),
            tool_call_count=int(active.get("tool_call_count") or 0),
            assistant_text=assistant_text,
            capture_content=should_capture,
            error=active.get("error"),
        )
        self._finished_since_prune += 1
        if self._finished_since_prune >= 25:
            self._finished_since_prune = 0
            self._store_submit(
                "prune",
                retention_days=retention_days,
                max_requests=max_requests,
            )

    def has_active(self, request_id: str) -> bool:
        return request_id in self._active_by_id

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        active_rows = sorted(
            (
                {
                    key: value
                    for key, value in {
                        **row,
                        "elapsed_seconds": max(
                            0.0,
                            now - float(row.get("started_at") or now),
                        ),
                    }.items()
                    if key not in {"capture_content", "user_text"}
                }
                for row in self._active_by_id.values()
            ),
            key=lambda row: float(row.get("started_at") or 0),
            reverse=True,
        )
        return {
            "active": active_rows[0] if active_rows else None,
            "active_requests": active_rows,
            "last": dict(self.last) if self.last else None,
            "events": list(self.events),
            "storage": {
                "persistent": self.store is not None,
                "healthy": self.store is not None
                and self.storage_error is None,
                "error": self.storage_error,
            },
        }

    def clear_events(self) -> None:
        self.events.clear()
        self._store_call("clear_events")

    def clear_all(self) -> None:
        self.events.clear()
        self.last = None
        self._store_call("clear_all")
