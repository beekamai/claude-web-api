"""Translation of provider token counts into the OpenAI usage shape."""

from __future__ import annotations

import math
from typing import Any


def usage_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value >= 0
        and value.is_integer()
    ):
        return int(value)
    return None

def openai_usage(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    prompt_tokens: int | None
    completion = raw.get("output_tokens", raw.get("completion_tokens"))
    completion_tokens = usage_integer(completion)
    if "input_tokens" in raw:
        input_tokens = usage_integer(raw.get("input_tokens"))
        has_cache_read = "cache_read_input_tokens" in raw
        has_cache_creation = "cache_creation_input_tokens" in raw
        cache_read_tokens = (
            usage_integer(raw.get("cache_read_input_tokens"))
            if has_cache_read
            else 0
        )
        cache_creation_tokens = (
            usage_integer(raw.get("cache_creation_input_tokens"))
            if has_cache_creation
            else 0
        )
        if (
            input_tokens is None
            or cache_read_tokens is None
            or cache_creation_tokens is None
        ):
            return None
        # Anthropic reports uncached input, cache reads, and cache writes as
        # separate buckets. OpenAI's prompt_tokens is their combined total.
        prompt_tokens = (
            input_tokens
            + (cache_read_tokens or 0)
            + (cache_creation_tokens or 0)
        )
    else:
        prompt_tokens = usage_integer(raw.get("prompt_tokens"))
        prompt_details = raw.get("prompt_tokens_details")
        cache_read_tokens = (
            usage_integer(prompt_details.get("cached_tokens"))
            if isinstance(prompt_details, dict)
            else None
        )
    if prompt_tokens is None or completion_tokens is None:
        return None
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        # Derive this field so malformed or cumulative upstream totals cannot
        # make the public response internally inconsistent.
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if (
        cache_read_tokens is not None
        and (
            "cache_read_input_tokens" in raw
            or "prompt_tokens_details" in raw
        )
    ):
        usage["prompt_tokens_details"] = {
            "cached_tokens": cache_read_tokens
        }
    output_details = raw.get("output_tokens_details")
    if isinstance(output_details, dict):
        safe_details = {
            str(key): parsed
            for key, value in output_details.items()
            if (parsed := usage_integer(value)) is not None
        }
        if safe_details:
            usage["completion_tokens_details"] = safe_details
    return usage
