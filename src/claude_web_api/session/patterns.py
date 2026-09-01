"""Patterns and constants shared by the session modules."""

from __future__ import annotations

import re

LEGACY_DYNAMIC_PROJECT_PROMPT_MARKER = (
    "\n\nDYNAMIC_OPENCLAUDE_SYSTEM_CONTEXT\n"
)
KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256 = {
    # OpenClaude 3.1 stable IDE contract before request-scoped tool metadata.
    "f23ae45d076d179a5dac3a67dd51843f3f4c2aa41f79a969fd0eb537a3c2cb84",
    # Stable native-tool contract immediately before the internal metadata
    # carrier was added. This one-time entry bootstraps the persistent lease.
    "a1f58d1b89de8890b087f87878297589cd612a1e199d0adc698bf62bbc2adfd6",
}
COMPLETION_PATH_RE = re.compile(
    r"/api/organizations/[^/]+/chat_conversations/[^/]+/"
    r"(?:completion2?|retry_completion2?)$"
)
COMPLETION_IDS_RE = re.compile(
    r"/api/organizations/(?P<org>[^/]+)/chat_conversations/"
    r"(?P<conversation>[^/]+)/(?:completion2?|retry_completion2?)$"
)
CONVERSATION_CREATE_PATH_RE = re.compile(
    r"/api/organizations/[^/]+/chat_conversations/?$"
)
PASSTHROUGH_HEADER_NAMES = {
    "anthropic-client-sha",
    "anthropic-client-version",
    "anthropic-client-build",
    "anthropic-anonymous-id",
    "anthropic-device-id",
    "x-activity-session-id",
    "anthropic-client-platform",
}
UUID_TEXT_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
MODEL_SELECTOR_TRANSIENT_REASONS = (
    "selector_database_missing",
    "selector_database_open_failed",
    "selector_store_missing",
    "selector_cache_read_failed",
    "selector_cache_empty",
    "scoped_account_query_missing",
    "selector_query_not_settled",
    "selector_cache_stale",
    "scoped_account_data_missing",
    "effective_selector_missing",
)
