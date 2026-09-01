"""Failures a browser turn can end with."""

from __future__ import annotations


class ClaudeLimitError(RuntimeError):
    """Base class for limits reported by claude.ai."""

    def __init__(self, message: str, *, replay_safe: bool = False) -> None:
        self.replay_safe = replay_safe
        super().__init__(message)


class ClaudeConversationLimitError(ClaudeLimitError):
    """The current web conversation must be continued in a new chat."""


class ClaudeUsageLimitError(ClaudeLimitError):
    """The current account/profile has exhausted its usage allowance."""


class ClaudeServiceUnavailableError(RuntimeError):
    """claude.ai is overloaded; this is not an account quota signal."""


class ClaudeCompletionRejectedError(RuntimeError):
    """claude.ai rejected a completion before emitting any SSE frame."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(
            f"claude.ai rejected the completion (HTTP {status}): {message}"
        )


class ClaudeBrowserUnavailableError(RuntimeError):
    """Camoufox could not be recovered before a turn was submitted."""


class ClaudeAccountIdentityError(RuntimeError):
    """The Camoufox profile changed Claude accounts before a host action."""


class ClaudeTurnOutcomeUnknownError(RuntimeError):
    """A committed browser action failed with an outcome that must not be replayed."""

    def __init__(self, message: str, operation_id: str | None = None) -> None:
        self.operation_id = operation_id
        suffix = f" [operation_id={operation_id}]" if operation_id else ""
        super().__init__(message + suffix)
