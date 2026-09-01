"""Authenticated Camoufox session for claude.ai web chat.

OpenClaude tools use claude.ai's native side-channel:

``completion.tools -> SSE tool_use -> POST tool_result -> same SSE continues``.

The browser remains the owner of authentication cookies. The gateway never
fabricates a tool decision or a final answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from camoufox.async_api import AsyncCamoufox

from claude_web_api.paths import (
    LEGACY_PROFILE_DIR as PROFILE_DIR,
)
from claude_web_api.paths import (
    LEGACY_PROJECT_FILE as PROJECT_CONFIG_FILE,
)

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

SSE_TAP_SCRIPT = r"""
(() => {
  if (globalThis.__openclaudeTapInstalled) return;

  // Camoufox runs automation scripts in a world isolated from the page, so a
  // fetch patched here never sees the application's own requests. The tap is
  // injected into the page world through a <script> element and reports back
  // over a DOM event, which both worlds share. claude.ai's CSP forbids eval
  // and new Function(), so the worker copy is serialised with toString()
  // rather than assembled from source strings.
  const EVENT_NAME = "__openclaude_sse_evt";
  const CHANNEL_NAME = "__openclaude_sse_channel";

  const deliver = (payload) => {
    if (!payload || typeof payload !== "object") return;
    try {
      void globalThis.__openclaude_sse(payload);
    } catch {}
  };

  document.addEventListener(EVENT_NAME, (event) => {
    try {
      deliver(JSON.parse(String(event.detail)));
    } catch {}
  });

  try {
    const inbound = new BroadcastChannel(CHANNEL_NAME);
    inbound.onmessage = (event) => deliver(event.data);
  } catch {}

  const PAGE_SOURCE = String.raw`
(() => {
  if (window.__openclaudeFetchTapped) return;
  window.__openclaudeFetchTapped = true;

  var EVENT_NAME = "__openclaude_sse_evt";
  var CHANNEL_NAME = "__openclaude_sse_channel";

  function installTap(emit, baseHref) {
    var COMPLETION_RE = new RegExp(
      "/api/organizations/[^/]+/chat_conversations/[^/]+/" +
        "(?:completion2?|retry_completion2?)$"
    );
    var nativeFetch = globalThis.fetch.bind(globalThis);

    async function emitFrame(frame, url) {
      var event = "message";
      var data = [];
      var lines = frame.split(/\r?\n/);
      for (var i = 0; i < lines.length; i += 1) {
        var line = lines[i];
        if (line.indexOf("event:") === 0) event = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) {
          data.push(line.slice(5).replace(/^ /, ""));
        }
      }
      if (data.length) {
        await emit({url: url, event: event, data: data.join("\n")});
        return true;
      }
      return false;
    }

    async function pump(response, url) {
      var body = response.body;
      if (!body) return;
      var reader = body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      var rawBody = "";
      var byteCount = 0;
      var chunkCount = 0;
      var frameCount = 0;
      try {
        for (;;) {
          var step = await reader.read();
          if (step.done) break;
          byteCount += (step.value && step.value.byteLength) || 0;
          chunkCount += 1;
          if (chunkCount === 1) {
            await emit({
              url: url,
              event: "__tap_chunk",
              data: JSON.stringify({
                byteCount: byteCount,
                chunkCount: chunkCount
              })
            });
          }
          var decoded = decoder.decode(step.value, {stream: true});
          buffer += decoded;
          if (rawBody.length < 8192) {
            rawBody += decoded.slice(0, 8192 - rawBody.length);
          }
          for (;;) {
            var match = /\r?\n\r?\n/.exec(buffer);
            if (!match) break;
            var frame = buffer.slice(0, match.index);
            buffer = buffer.slice(match.index + match[0].length);
            if (frame && await emitFrame(frame, url)) frameCount += 1;
          }
        }
        var trailing = decoder.decode();
        buffer += trailing;
        if (rawBody.length < 8192) {
          rawBody += trailing.slice(0, 8192 - rawBody.length);
        }
        if (buffer.trim() && await emitFrame(buffer, url)) frameCount += 1;
        if (frameCount === 0) {
          var message = "";
          var contentType = response.headers.get("content-type") || "";
          if (/json/i.test(contentType)) {
            try {
              var parsed = JSON.parse(rawBody);
              var error = parsed && parsed.error;
              message =
                (typeof error === "string" && error) ||
                (error && error.message) ||
                (parsed && parsed.message) ||
                (parsed && parsed.detail) ||
                "";
            } catch (e) {}
          }
          await emit({
            url: url,
            event: "__tap_http_error",
            data: JSON.stringify({
              status: response.status,
              message: String(
                message ||
                  "completion returned no SSE frames (" +
                    (contentType || "unknown content type") + ")"
              ).slice(0, 1000)
            })
          });
        }
        await emit({
          url: url,
          event: "__tap_eof",
          data: JSON.stringify({
            byteCount: byteCount,
            chunkCount: chunkCount,
            frameCount: frameCount,
            trailingChars: buffer.length
          })
        });
      } catch (error) {
        try {
          await emit({url: url, event: "__tap_error", data: String(error)});
        } catch (e) {}
      }
    }

    globalThis.fetch = async function () {
      var args = Array.prototype.slice.call(arguments);
      var response = await nativeFetch.apply(null, args);
      try {
        var input = args[0];
        var raw =
          typeof input === "string" || input instanceof URL
            ? String(input)
            : input.url;
        var url = new URL(raw, baseHref);
        if (COMPLETION_RE.test(url.pathname)) {
          await emit({
            url: url.href,
            event: "__tap_seen",
            data: JSON.stringify({
              status: response.status,
              contentType: response.headers.get("content-type"),
              hasBody: Boolean(response.body)
            })
          });
          if (response.body) {
            void pump(response.clone(), url.href);
          } else {
            await emit({
              url: url.href,
              event: "__tap_http_error",
              data: JSON.stringify({
                status: response.status,
                message: "completion returned an empty response body"
              })
            });
          }
        }
      } catch (e) {}
      return response;
    };
  }

  var emit = function (payload) {
    try {
      document.dispatchEvent(
        new CustomEvent(EVENT_NAME, {detail: JSON.stringify(payload)})
      );
    } catch (e) {}
    return Promise.resolve();
  };

  try {
    installTap(emit, location.href);
    emit({url: location.href, event: "__tap_ready", data: "page-world"});
  } catch (e) {
    emit({url: location.href, event: "__tap_error", data: "install " + e});
  }

  var NativeWorker = window.Worker;
  if (typeof NativeWorker === "function") {
    window.__openclaudeWorkerCount = 0;
    var workerSource =
      "var __ch = new BroadcastChannel(" + JSON.stringify(CHANNEL_NAME) + ");" +
      "var __emit = function (payload) {" +
      "  __ch.postMessage(payload); return Promise.resolve(); };" +
      "(" + installTap.toString() + ")(__emit, " +
      JSON.stringify(location.origin) + ");";
    var loaderFor = function (scriptUrl, isModule) {
      var tail = isModule
        ? "import(" + JSON.stringify(scriptUrl) + ");"
        : "importScripts(" + JSON.stringify(scriptUrl) + ");";
      return URL.createObjectURL(
        new Blob(["try {" + workerSource + "} catch (e) {}" + tail],
          {type: "text/javascript"})
      );
    };
    var PatchedWorker = function (scriptUrl, options) {
      window.__openclaudeWorkerCount += 1;
      var target = scriptUrl;
      try {
        var isModule = Boolean(options && options.type === "module");
        target = loaderFor(
          new URL(String(scriptUrl), location.href).href,
          isModule
        );
      } catch (e) {
        target = scriptUrl;
      }
      return new NativeWorker(target, options);
    };
    PatchedWorker.prototype = NativeWorker.prototype;
    window.Worker = PatchedWorker;
    emit({
      url: location.href,
      event: "__tap_ready",
      data: "worker-patch"
    });
  }
})();
`;

  const inject = () => {
    try {
      const host = document.documentElement || document.head || document.body;
      if (!host) return false;
      const element = document.createElement("script");
      element.textContent = PAGE_SOURCE;
      host.appendChild(element);
      element.remove();
      return true;
    } catch {
      return false;
    }
  };

  // The flag is set only once the page world actually received the tap: at
  // document_start there may be no element to attach to yet, and the bridge
  // re-evaluates this script before every turn.
  if (inject()) {
    globalThis.__openclaudeTapInstalled = true;
  } else {
    document.addEventListener(
      "readystatechange",
      () => {
        if (inject()) globalThis.__openclaudeTapInstalled = true;
      },
      {once: true}
    );
  }
})();
"""

def _string_list(value: Any) -> list[Any]:
    """Read a field that upstream may omit or send as a non-list."""
    return value if isinstance(value, list) else []


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

ACCOUNT_AND_SELECTOR_SCRIPT = r"""
async ({
  organizationUuid,
  selectorMaxAgeMs,
  selectorWaitMs,
  selectorTransientReasons
}) => {
  const uuidRe =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const normalize = (value) => {
    if (!value) return null;
    try {
      const parsed = JSON.parse(value);
      return typeof parsed === "string" ? parsed : value;
    } catch {
      return value;
    }
  };
  const hinted = normalize(
    localStorage.getItem("__qk_hint_account_uuid")
  );
  const confirmed = normalize(
    localStorage.getItem("rq-cache-confirmed-account")
  );
  let profile = null;
  let status = 0;
  try {
    const response = await fetch("/api/account", {
      credentials: "include",
      cache: "no-store",
      headers: {Accept: "application/json"}
    });
    status = response.status;
    if (response.ok) profile = await response.json();
  } catch {}

  const fail = (reason, extra = {}) => ({
    ok: false,
    reason,
    ...extra
  });
  const readSelector = async () => {
    if (hinted && confirmed && hinted !== confirmed) {
      return fail("identity_hint_mismatch");
    }
    const accountUuid = uuidRe.test(String(confirmed || ""))
      ? String(confirmed)
      : (
        uuidRe.test(String(hinted || ""))
          ? String(hinted)
          : ""
      );
    if (!accountUuid) return fail("account_uuid_missing");
    if (!uuidRe.test(String(organizationUuid || ""))) {
      return fail("organization_uuid_missing");
    }

    const cookies = {};
    for (const row of document.cookie.split(";")) {
      const index = row.indexOf("=");
      const key = (index < 0 ? row : row.slice(0, index)).trim();
      if (!key) continue;
      const rawValue = index < 0 ? "" : row.slice(index + 1);
      try {
        cookies[key] = decodeURIComponent(rawValue);
      } catch {
        cookies[key] = rawValue;
      }
    }
    const cookieOrganization = String(
      cookies.lastActiveOrg || ""
    );
    if (
      cookieOrganization
      && cookieOrganization !== organizationUuid
    ) {
      return fail("organization_cookie_mismatch");
    }

    if (typeof indexedDB.databases === "function") {
      let databases;
      try {
        databases = await indexedDB.databases();
      } catch {
        return fail("selector_database_list_failed");
      }
      if (
        !Array.isArray(databases)
        || !databases.some(
          (database) => database?.name === "keyval-store"
        )
      ) {
        return fail("selector_database_missing");
      }
    }

    let database = null;
    try {
      database = await new Promise((resolve, reject) => {
        const request = indexedDB.open("keyval-store");
        request.onupgradeneeded = () => {
          try {
            request.transaction?.abort();
          } catch {}
          reject(new Error("selector_database_missing"));
        };
        request.onerror = () => reject(
          request.error || new Error("selector_database_open_failed")
        );
        request.onsuccess = () => resolve(request.result);
      });
    } catch {
      return fail("selector_database_open_failed");
    }
    if (!database.objectStoreNames.contains("keyval")) {
      database.close();
      return fail("selector_store_missing");
    }

    let cache = null;
    try {
      cache = await new Promise((resolve, reject) => {
        const transaction = database.transaction(
          "keyval",
          "readonly"
        );
        const request = transaction
          .objectStore("keyval")
          .get("react.query.cache");
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(
          request.error || new Error("selector_cache_read_failed")
        );
        transaction.onabort = () => reject(
          transaction.error || new Error("selector_cache_aborted")
        );
      });
    } catch {
      database.close();
      return fail("selector_cache_read_failed");
    }
    const normalizeCacheValue = (value) => {
      if (typeof value !== "string") return value;
      try {
        const parsedCache = JSON.parse(value);
        if (parsedCache && typeof parsedCache === "object") {
          return parsedCache;
        }
      } catch {}
      return value;
    };
    cache = normalizeCacheValue(cache);
    let exactCacheKeyPresent = null;
    let cacheKeyCount = null;
    let cacheShapeCandidates = null;
    if (!cache || typeof cache !== "object") {
      try {
        const keys = await new Promise((resolve, reject) => {
          const transaction = database.transaction(
            "keyval",
            "readonly"
          );
          const request = transaction
            .objectStore("keyval")
            .getAllKeys();
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(
            request.error || new Error("selector_keys_read_failed")
          );
        });
        cacheKeyCount = Array.isArray(keys) ? keys.length : null;
        exactCacheKeyPresent = (
          Array.isArray(keys)
          && keys.some(
            (key) => String(key) === "react.query.cache"
          )
        );
      } catch {}
    }
    if (!cache || typeof cache !== "object") {
      try {
        const values = await new Promise((resolve, reject) => {
          const transaction = database.transaction(
            "keyval",
            "readonly"
          );
          const request = transaction
            .objectStore("keyval")
            .getAll();
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(
            request.error || new Error("selector_values_read_failed")
          );
        });
        const candidates = (
          Array.isArray(values) ? values : []
        ).map(normalizeCacheValue).filter((value) => (
          value
          && typeof value === "object"
          && Array.isArray(value?.clientState?.queries)
        ));
        cacheShapeCandidates = candidates.length;
        if (candidates.length === 1) {
          cache = candidates[0];
        } else if (candidates.length > 1) {
          database.close();
          return fail("selector_cache_conflict", {
            cache: {
              key_count: cacheKeyCount,
              shape_candidates: candidates.length,
              exact_key_present: exactCacheKeyPresent
            }
          });
        }
      } catch {}
    }
    database.close();
    if (!cache || typeof cache !== "object") {
      return fail("selector_cache_empty", {
        cache: {
          value_type: cache === null ? "null" : typeof cache,
          string_length: (
            typeof cache === "string" ? cache.length : null
          ),
          exact_key_present: exactCacheKeyPresent,
          key_count: cacheKeyCount,
          shape_candidates: cacheShapeCandidates
        }
      });
    }

    const primitiveTokens = (
      value,
      output = [],
      seen = new WeakSet(),
      depth = 0
    ) => {
      if (value == null || depth > 8) return output;
      if (
        typeof value === "string"
        || typeof value === "number"
        || typeof value === "boolean"
      ) {
        output.push(String(value));
        return output;
      }
      if (typeof value !== "object" || seen.has(value)) {
        return output;
      }
      seen.add(value);
      const children = Array.isArray(value)
        ? value
        : Object.values(value);
      for (const child of children) {
        primitiveTokens(child, output, seen, depth + 1);
      }
      return output;
    };
    const queries = Array.isArray(cache?.clientState?.queries)
      ? cache.clientState.queries
      : [];
    const scopedQueries = queries.filter((query) => {
      const tokens = primitiveTokens(query?.queryKey);
      return (
        tokens.includes("current_account")
        && tokens.includes(organizationUuid)
        && query?.state?.status === "success"
      );
    }).sort(
      (left, right) => Number(
        right?.state?.dataUpdatedAt || 0
      ) - Number(left?.state?.dataUpdatedAt || 0)
    );
    if (!scopedQueries.length) {
      return fail("scoped_account_query_missing");
    }
    const query = scopedQueries[0];
    if (
      query?.state?.fetchStatus === "fetching"
      || query?.state?.isInvalidated === true
    ) {
      return fail("selector_query_not_settled");
    }
    const updatedAt = Number(query?.state?.dataUpdatedAt || 0);
    const ageMs = updatedAt ? Date.now() - updatedAt : null;
    if (
      !updatedAt
      || ageMs < 0
      || ageMs > selectorMaxAgeMs
    ) {
      return fail("selector_cache_stale", {age_ms: ageMs});
    }
    const data = query?.state?.data;
    if (!data || typeof data !== "object") {
      return fail("scoped_account_data_missing");
    }

    let visitedNodes = 0;
    const findObject = (
      value,
      predicate,
      seen = new WeakSet(),
      depth = 0
    ) => {
      if (
        !value
        || typeof value !== "object"
        || seen.has(value)
        || depth > 18
        || ++visitedNodes > 150000
      ) {
        return null;
      }
      seen.add(value);
      if (predicate(value)) return value;
      const children = Array.isArray(value)
        ? value
        : Object.values(value);
      for (const child of children) {
        const match = findObject(
          child,
          predicate,
          seen,
          depth + 1
        );
        if (match) return match;
      }
      return null;
    };
    const account = findObject(data, (value) => {
      const candidateUuid = String(
        value.uuid || value.account_uuid || value.id || ""
      );
      return (
        candidateUuid === accountUuid
        && (
          "memberships" in value
          || "membership" in value
          || "email_address" in value
          || "full_name" in value
        )
      );
    });
    if (!account) return fail("cached_account_mismatch");

    const memberships = [
      ...(
        Array.isArray(account.memberships)
          ? account.memberships
          : []
      ),
      ...(
        Array.isArray(account.membership)
          ? account.membership
          : (account.membership ? [account.membership] : [])
      )
    ];
    const membership = memberships.find((value) => String(
      value?.organization?.uuid
      || value?.organization_uuid
      || value?.uuid
      || ""
    ) === organizationUuid);
    if (!membership) return fail("cached_membership_mismatch");

    const chatValues = (value, output = []) => {
      if (!value || typeof value !== "object") return output;
      if (Array.isArray(value)) {
        for (const child of value) chatValues(child, output);
        return output;
      }
      if (
        value.id === "chat"
        && Array.isArray(value.models)
      ) {
        output.push(value);
      }
      if (value.chat && typeof value.chat === "object") {
        chatValues(value.chat, output);
      }
      return output;
    };
    const collectNamed = (
      value,
      key,
      output,
      seen = new WeakSet(),
      depth = 0
    ) => {
      if (
        !value
        || typeof value !== "object"
        || seen.has(value)
        || depth > 18
      ) {
        return;
      }
      seen.add(value);
      if (
        !Array.isArray(value)
        && Object.prototype.hasOwnProperty.call(value, key)
      ) {
        chatValues(value[key], output);
      }
      const children = Array.isArray(value)
        ? value
        : Object.values(value);
      for (const child of children) {
        collectNamed(child, key, output, seen, depth + 1);
      }
    };
    const configs = [];
    collectNamed(data, "model_selector_config", configs);
    const uniqueConfigs = [...new Set(configs)];
    if (!uniqueConfigs.length) {
      return fail("effective_selector_missing");
    }

    const safeReason = (reason) => {
      if (reason == null) return null;
      if (typeof reason !== "object") return String(reason);
      const output = {};
      for (
        const key of ["type", "required_plan", "message", "title"]
      ) {
        const child = reason[key];
        if (
          typeof child === "string"
          || typeof child === "boolean"
          || typeof child === "number"
        ) {
          output[key] = child;
        }
      }
      return Object.keys(output).length
        ? output
        : "account_unavailable";
    };
    const safeModels = (config) => config.models
      .filter((model) => model && model.id)
      .map((model) => ({
        id: String(model.id),
        name: String(model.name || model.label || model.id),
        section: model.section == null
          ? null
          : String(model.section),
        available: model.available,
        inactive: model.inactive === true,
        disabled_reason: safeReason(model.disabled_reason),
        capabilities: (
          model.capabilities
          && typeof model.capabilities === "object"
        ) ? model.capabilities : null,
        thinking: (
          model.thinking
          && typeof model.thinking === "object"
        ) ? model.thinking : null,
        supports_fast_mode: Boolean(model.supports_fast_mode)
      }));
    const signature = (models) => JSON.stringify(
      models.map((model) => [
        model.id,
        model.section,
        model.available,
        model.inactive,
        model.disabled_reason
      ]).sort((left, right) => left[0].localeCompare(right[0]))
    );
    const modelSets = uniqueConfigs.map(safeModels);
    if (
      new Set(modelSets.map(signature)).size !== 1
    ) {
      return fail("selector_conflict");
    }

    const stateValues = (value, output = []) => {
      if (!value || typeof value !== "object") return output;
      if (Array.isArray(value)) {
        for (const child of value) stateValues(child, output);
        return output;
      }
      if (value.id === "chat") output.push(value);
      if (value.chat && typeof value.chat === "object") {
        stateValues(value.chat, output);
      }
      return output;
    };
    const collectStates = (
      value,
      output,
      seen = new WeakSet(),
      depth = 0
    ) => {
      if (
        !value
        || typeof value !== "object"
        || seen.has(value)
        || depth > 18
      ) {
        return;
      }
      seen.add(value);
      if (
        !Array.isArray(value)
        && Object.prototype.hasOwnProperty.call(
          value,
          "model_selector_state"
        )
      ) {
        stateValues(value.model_selector_state, output);
      }
      const children = Array.isArray(value)
        ? value
        : Object.values(value);
      for (const child of children) {
        collectStates(child, output, seen, depth + 1);
      }
    };
    const states = [];
    collectStates(data, states);
    const uniqueStates = [...new Set(states)];
    const state = uniqueStates[0] || null;
    return {
      ok: true,
      source: "react_query_effective_selector",
      cache: {
        buster: String(cache.buster || ""),
        persisted_at: Number(cache.timestamp || 0) || null,
        data_updated_at: updatedAt,
        age_ms: ageMs,
        data_update_count: Number(
          query?.state?.dataUpdateCount || 0
        ),
        status: query?.state?.status,
        fetch_status: query?.state?.fetchStatus || null
      },
      identity: {
        account_match: true,
        organization_query_match: true,
        membership_match: true,
        cookie_match: cookieOrganization ? true : null
      },
      config: {
        id: "chat",
        models: modelSets[0]
      },
      state: state ? {
        id: "chat",
        model: state.model ?? null,
        thinking: state.thinking ?? null,
        thinking_by_model: state.thinking_by_model ?? null,
        preset_key: state.preset_key ?? null,
        org_enforced_default_model:
          state.org_enforced_default_model ?? null,
        selection_source: state.selection_source ?? null
      } : null
    };
  };

  // The composer can render before React Query finishes rebuilding its
  // persisted cache after a cache-buster change. Wait only for states that
  // can settle without a user action; identity mismatches and conflicting
  // selectors must fail immediately.
  const transientReasons = new Set(
    Array.isArray(selectorTransientReasons)
      ? selectorTransientReasons
      : []
  );
  const waitBudgetMs = Math.max(
    0,
    Number(selectorWaitMs) || 0
  );
  const waitDeadline = Date.now() + waitBudgetMs;
  let selector;
  while (true) {
    try {
      selector = await readSelector();
    } catch {
      selector = fail("selector_read_failed");
    }
    if (
      selector?.ok
      || !transientReasons.has(selector?.reason)
      || Date.now() >= waitDeadline
    ) {
      break;
    }
    const remainingMs = waitDeadline - Date.now();
    await new Promise((resolve) => setTimeout(
      resolve,
      Math.min(500, Math.max(1, remainingMs))
    ));
  }
  return {hinted, confirmed, status, profile, selector};
}
"""


def _legacy_project_id() -> str | None:
    if PROJECT_CONFIG_FILE.exists():
        try:
            config = json.loads(PROJECT_CONFIG_FILE.read_text(encoding="utf-8"))
            project_id = str(config.get("project_id", "") or "").strip()
            return project_id or None
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _claude_start_url(project_id: str | None = None) -> str:
    explicit_url = os.getenv("CLAUDE_START_URL")
    if explicit_url:
        return explicit_url
    project_id = (
        project_id
        or os.getenv("CLAUDE_PROJECT_ID")
        or _legacy_project_id()
    )
    if project_id:
        return f"https://claude.ai/project/{project_id}"
    return "https://claude.ai/new"


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


@dataclass(frozen=True)
class NativeToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class NativeTurn:
    content: str | None
    tool_uses: list[NativeToolUse]
    thinking: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    stop_reason: str | None = None


class ClaudeSession:
    def __init__(
        self,
        headless: bool = False,
        profiles: list[dict[str, Any]] | None = None,
        active_profile_id: str | None = None,
        project_instructions: str | None = None,
        project_prompt_lease_file: str | Path | None = None,
    ) -> None:
        self.headless = headless
        configured_profiles = os.getenv("CLAUDE_PROFILE_DIRS", "")
        if profiles:
            self.profile_specs = [
                {
                    "id": str(row.get("id") or f"profile-{index + 1}"),
                    "name": str(row.get("name") or f"Profile {index + 1}"),
                    "path": str(Path(str(row["path"])).expanduser().resolve()),
                    "project_id": (
                        str(row.get("project_id") or "").strip() or None
                    ),
                    "organization_id": (
                        str(row.get("organization_id") or "").strip() or None
                    ),
                    "model": str(row.get("model") or "auto"),
                }
                for index, row in enumerate(profiles)
                if isinstance(row, dict) and row.get("path")
            ]
        else:
            profile_paths = (
                [
                    Path(item).expanduser()
                    for item in configured_profiles.split(os.pathsep)
                    if item
                ]
                if configured_profiles
                else [PROFILE_DIR]
            )
            self.profile_specs = [
                {
                    "id": "default" if index == 0 else f"profile-{index + 1}",
                    "name": (
                        "Основной" if index == 0 else f"Profile {index + 1}"
                    ),
                    "path": str(path.resolve()),
                    "project_id": _legacy_project_id()
                    if index == 0
                    else None,
                    "organization_id": None,
                    "model": "auto",
                }
                for index, path in enumerate(profile_paths)
            ]
        if not self.profile_specs:
            raise ValueError("at least one browser profile is required")
        self.profile_dirs = [
            Path(str(row["path"])) for row in self.profile_specs
        ]
        self.profile_index = next(
            (
                index
                for index, row in enumerate(self.profile_specs)
                if row["id"] == active_profile_id
            ),
            0,
        )
        self._camoufox: Any = None
        self._context: Any = None
        self.page: Any = None
        self._lock = asyncio.Lock()
        self.ready = False
        self._stopping = False
        self._browser_dead = asyncio.Event()
        self._watchdog_stop = asyncio.Event()
        self._watchdog_task: asyncio.Task[Any] | None = None
        self._watchdog_heartbeat_at = time.monotonic()
        self._phase = "stopped"
        self._phase_started_at = time.monotonic()
        self._last_progress_at = self._phase_started_at
        self._operation_id: str | None = None
        self._session_epoch = 0
        self._restart_count = 0
        self._restart_times: list[float] = []
        self._recovery_exhausted = False
        self._recovery_failures = 0
        self._next_recovery_at = 0.0
        self._last_recovery_reason: str | None = None
        self._last_recovery_at: float | None = None
        self._last_error: str | None = None
        self._last_probe_at: float | None = None
        self._last_probe_ok: bool | None = None
        self._account_uuid: str | None = None
        self._account_name: str | None = None
        self._account_email_masked: str | None = None
        self._organization_uuid: str | None = None
        self._project_instructions = str(project_instructions or "")
        self._project_prompt_lease_file = (
            Path(project_prompt_lease_file).expanduser().resolve()
            if project_prompt_lease_file is not None
            else None
        )
        self._project_lease_error: str | None = None
        self._project_instructions_synced = False
        self._project_sync_error: str | None = None
        self._project_privacy_verified: bool | None = None
        self._profile_account_uuids: dict[str, str] = {}
        self._available_models: list[dict[str, Any]] = []
        self._model_selector_state: dict[str, Any] = {}
        self._model_selector_diagnostics: dict[str, Any] = {}
        self._model_selector_cache_max_age_ms = max(
            30_000,
            int(
                float(
                    os.getenv(
                        "CLAUDE_MODEL_SELECTOR_MAX_AGE_SECONDS",
                        "300",
                    )
                )
                * 1_000
            ),
        )
        self._model_selector_wait_ms = max(
            0,
            int(
                float(
                    os.getenv(
                        "CLAUDE_MODEL_SELECTOR_WAIT_SECONDS",
                        "45",
                    )
                )
                * 1_000
            ),
        )
        self._driver_pid: int | None = None
        self._tool_result_delivery: dict[str, str] = {}
        self._watchdog_interval = max(
            2.0, float(os.getenv("CLAUDE_WATCHDOG_INTERVAL", "10"))
        )
        self._watchdog_probe_timeout = max(
            1.0, float(os.getenv("CLAUDE_WATCHDOG_PROBE_TIMEOUT", "4"))
        )
        self._watchdog_stall_timeout = max(
            30.0, float(os.getenv("CLAUDE_WATCHDOG_STALL_TIMEOUT", "90"))
        )
        self._browser_close_timeout = max(
            2.0, float(os.getenv("CLAUDE_BROWSER_CLOSE_TIMEOUT", "10"))
        )
        self._tool_result_post_timeout = max(
            3.0, float(os.getenv("CLAUDE_TOOL_RESULT_POST_TIMEOUT", "20"))
        )
        self._browser_start_timeout = max(
            60.0, float(os.getenv("CLAUDE_BROWSER_START_TIMEOUT", "330"))
        )
        self._restart_window = max(
            60.0, float(os.getenv("CLAUDE_RESTART_WINDOW", "600"))
        )
        self._restart_limit = max(
            2, int(os.getenv("CLAUDE_RESTART_LIMIT", "5"))
        )
        try:
            self._humanize_seconds = max(
                0.0,
                float(os.getenv("CLAUDE_HUMANIZE_SECONDS", "0.25")),
            )
        except ValueError:
            self._humanize_seconds = 0.25

        self._native_active = False
        self._native_queue: asyncio.Queue[dict[str, str]] | None = None
        self._native_tools: list[dict[str, Any]] = []
        self._native_internal_tool_names: set[str] = set()
        self._native_internal_tool_acks = 0
        self._native_internal_text_prefix: list[str] = []
        self._native_internal_thinking_prefix: list[str] = []
        self._native_completion_url: str | None = None
        self._native_org_uuid: str | None = None
        self._native_conversation_uuid: str | None = None
        self._native_headers: dict[str, str] = {}
        self._native_pending_ids: set[str] = set()
        self._native_pending_deadline: float | None = None
        self._native_parallel_tool_calls = True
        self._history_recovery_required = False
        self._tool_result_lease_seconds = float(
            os.getenv("CLAUDE_TOOL_RESULT_TIMEOUT", "600")
        )
        self._native_blocks: dict[int, dict[str, Any]] = {}
        self._native_text_blocks: dict[int, str] = {}
        self._native_tool_blocks: dict[int, NativeToolUse] = {}
        self._native_thinking_blocks: dict[int, str] = {}
        self._native_usage: dict[str, Any] = {}
        self._native_model: str | None = None
        self._native_stop_reason: str | None = None
        self._native_requested_model: str | None = None
        self._native_thinking_mode = "auto"
        self._native_effort: str | None = None
        self._native_conversation_verified = False
        self._native_event_sink: (
            Callable[[dict[str, Any]], None] | None
        ) = None
        self._privacy_mode = "keep"
        self._conversation_privacy_mode: str | None = None
        self._conversation_client_session_id: str | None = None
        self._native_client_session_id: str | None = None
        self._last_completion_shape: dict[str, Any] = {}
        self._observed_models: set[str] = set()
        self._native_saw_content = False
        self._native_saw_tool = False
        self._native_terminal_seen = False
        self._sse_tap_event_count = 0
        self._sse_tap_rejected_count = 0
        self._sse_tap_last_at: float | None = None
        self._sse_tap_last_event: str | None = None
        self._sse_tap_last_url: str | None = None
        self._sse_tap_last_data: str | None = None

    def current_profile_spec(self) -> dict[str, Any]:
        return dict(self.profile_specs[self.profile_index])

    def current_profile_id(self) -> str:
        return str(self.profile_specs[self.profile_index]["id"])

    def _current_start_url(self) -> str:
        project_id = self.profile_specs[self.profile_index].get("project_id")
        if self._privacy_mode == "ephemeral" and not project_id:
            return "https://claude.ai/new?incognito=true"
        return _claude_start_url(project_id)

    async def sync_profiles(
        self,
        profiles: list[dict[str, Any]],
        active_profile_id: str | None = None,
        *,
        restart: bool = False,
    ) -> None:
        """Refresh the runtime profile catalog without exposing browser data."""
        normalized = [
            {
                "id": str(row.get("id") or f"profile-{index + 1}"),
                "name": str(row.get("name") or f"Profile {index + 1}"),
                "path": str(Path(str(row["path"])).expanduser().resolve()),
                "project_id": (
                    str(row.get("project_id") or "").strip() or None
                ),
                "organization_id": (
                    str(row.get("organization_id") or "").strip() or None
                ),
                "model": str(row.get("model") or "auto"),
            }
            for index, row in enumerate(profiles)
            if isinstance(row, dict) and row.get("path")
        ]
        if not normalized:
            raise ValueError("at least one browser profile is required")
        async with self._lock:
            current_id = self.current_profile_id()
            target_id = active_profile_id or current_id
            target_index = next(
                (
                    index
                    for index, row in enumerate(normalized)
                    if row["id"] == target_id
                ),
                0,
            )
            current_path = self.current_profile_spec()["path"]
            target_path = normalized[target_index]["path"]
            needs_restart = restart or current_path != target_path
            if needs_restart and self._native_active:
                raise RuntimeError(
                    "cannot switch profile while Claude is waiting for tool_result"
                )
            if needs_restart:
                await self._stop_browser_unlocked()
            self.profile_specs = normalized
            self.profile_dirs = [
                Path(str(row["path"])) for row in normalized
            ]
            self.profile_index = target_index
            if needs_restart:
                await self.start()

    async def activate_profile(self, profile_id: str) -> None:
        target_index = next(
            (
                index
                for index, row in enumerate(self.profile_specs)
                if row["id"] == profile_id
            ),
            None,
        )
        if target_index is None:
            raise KeyError(profile_id)
        async with self._lock:
            if target_index == self.profile_index and self.ready:
                return
            if self._native_active:
                raise RuntimeError(
                    "cannot switch profile while Claude is waiting for tool_result"
                )
            await self._stop_browser_unlocked()
            self.profile_index = target_index
            await self.start()

    def observed_models(self) -> list[str]:
        return sorted(self._observed_models)

    def account_uuid_for_internal_use(self) -> str | None:
        """Return the UUID only for local salted duplicate detection."""
        return self._account_uuid

    def organization_uuid_for_internal_use(self) -> str | None:
        """Return the verified Project organization for local profile config."""
        return self._organization_uuid

    async def bring_to_front(self) -> None:
        async with self._lock:
            if self.page is None or self.page.is_closed():
                raise ClaudeBrowserUnavailableError(
                    "the active Camoufox window is not available"
                )
            await self.page.bring_to_front()

    def selectable_models(self) -> list[dict[str, Any]]:
        return [
            dict(model)
            for model in self._available_models
            if (
                model.get("available") is True
                and model.get("access_status") == "available"
            )
        ]

    def selected_model_for_runtime(self) -> str | None:
        model = str(self._model_selector_state.get("model") or "").strip()
        return model or None

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

    @staticmethod
    def _debug(message: str) -> None:
        if os.getenv("CLAUDE_DEBUG_BROWSER", "0").lower() in ("1", "true", "yes"):
            print(f"CLAUDE_BROWSER {message}", flush=True)

    def _set_phase(self, phase: str, *, progress: bool = True) -> None:
        now = time.monotonic()
        if phase != self._phase:
            self._phase = phase
            self._phase_started_at = now
        if progress:
            self._last_progress_at = now

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

    @staticmethod
    def _mask_email(value: str | None) -> str | None:
        if not value or "@" not in value:
            return None
        local, domain = value.rsplit("@", 1)
        visible = local[:2] if len(local) > 1 else local[:1]
        return f"{visible}***@{domain}"

    @staticmethod
    def _mask_identifiers(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return UUID_TEXT_RE.sub(
            lambda match: f"…{match.group(0)[-8:]}",
            value,
        )

    @staticmethod
    def _find_identity_record(
        value: Any,
        account_uuid: str | None,
    ) -> dict[str, Any] | None:
        if isinstance(value, dict):
            candidate_uuid = str(
                value.get("uuid")
                or value.get("account_uuid")
                or value.get("id")
                or ""
            )
            if (
                (account_uuid and candidate_uuid == account_uuid)
                or (
                    not account_uuid
                    and any(
                        key in value
                        for key in (
                            "full_name",
                            "display_name",
                            "email",
                            "email_address",
                        )
                    )
                )
            ):
                return value
            for child in value.values():
                found = ClaudeSession._find_identity_record(child, account_uuid)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = ClaudeSession._find_identity_record(child, account_uuid)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _find_named_value(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = ClaudeSession._find_named_value(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = ClaudeSession._find_named_value(child, key)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _normalize_disabled_reason(value: Any) -> str | dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key in ("type", "required_plan", "message", "title"):
                child = value.get(key)
                if child is None or isinstance(child, (str, bool, int, float)):
                    if child is not None:
                        safe[key] = child
            return safe or "account_unavailable"
        return str(value)

    async def _load_account_identity(self) -> bool:
        """Read identity from the authenticated page without exposing cookies."""
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(
                    ACCOUNT_AND_SELECTOR_SCRIPT,
                    {
                        "organizationUuid": self.current_profile_spec().get(
                            "organization_id"
                        ),
                        "selectorMaxAgeMs": (
                            self._model_selector_cache_max_age_ms
                        ),
                        "selectorWaitMs": self._model_selector_wait_ms,
                        "selectorTransientReasons": list(
                            MODEL_SELECTOR_TRANSIENT_REASONS
                        ),
                    },
                ),
                timeout=max(
                    10,
                    self._model_selector_wait_ms / 1_000 + 15,
                ),
            )
        except Exception:
            self._clear_account_identity()
            return False
        if (
            not isinstance(result, dict)
            or result.get("status") != 200
            or not isinstance(result.get("profile"), (dict, list))
        ):
            self._clear_account_identity()
            return False
        hinted = str(result.get("hinted") or "")
        confirmed = str(result.get("confirmed") or "")
        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$",
            re.I,
        )
        if hinted and confirmed and hinted != confirmed:
            self._last_error = "Claude account identity hints disagree"
            self._clear_account_identity()
            return False
        account_uuid = confirmed or hinted
        if not uuid_re.fullmatch(account_uuid):
            account_uuid = ""
        record = self._find_identity_record(
            result.get("profile"),
            account_uuid or None,
        )
        if record is None:
            self._clear_account_identity()
            return False
        if not account_uuid:
            candidate_uuid = str(
                record.get("uuid")
                or record.get("account_uuid")
                or record.get("id")
                or ""
            )
            if uuid_re.fullmatch(candidate_uuid):
                account_uuid = candidate_uuid
        if not account_uuid:
            self._clear_account_identity()
            return False
        account_name = str(
            record.get("full_name")
            or record.get("display_name")
            or record.get("name")
            or ""
        ) or None
        account_email_masked = self._mask_email(
            str(
                record.get("email_address")
                or record.get("email")
                or ""
            )
            or None
        )
        available_models: list[dict[str, Any]] = []
        model_selector_state: dict[str, Any] = {}
        account_payload = result.get("profile")
        selector_payload = result.get("selector")
        selector_config: Any = None
        selector_state: Any = None
        selector_diagnostics: dict[str, Any] = {
            "verified": False,
            "source": None,
            "reason": "selector_result_missing",
            "cache": {},
        }
        if isinstance(selector_payload, dict):
            selector_diagnostics["source"] = selector_payload.get("source")
            selector_diagnostics["reason"] = selector_payload.get("reason")
            cache = selector_payload.get("cache")
            identity = selector_payload.get("identity")
            cache_age = (
                cache.get("age_ms")
                if isinstance(cache, dict)
                else None
            )
            identity_verified = bool(
                isinstance(identity, dict)
                and identity.get("account_match") is True
                and identity.get("organization_query_match") is True
                and identity.get("membership_match") is True
            )
            cache_fresh = bool(
                isinstance(cache_age, (int, float))
                and 0 <= cache_age
                <= self._model_selector_cache_max_age_ms
            )
            selector_verified = bool(
                selector_payload.get("ok") is True
                and selector_payload.get("source")
                == "react_query_effective_selector"
                and identity_verified
                and cache_fresh
                and isinstance(selector_payload.get("config"), dict)
            )
            selector_diagnostics = {
                "verified": selector_verified,
                "source": selector_payload.get("source"),
                "reason": (
                    None
                    if selector_verified
                    else str(
                        selector_payload.get("reason")
                        or (
                            "selector_identity_unverified"
                            if not identity_verified
                            else (
                                "selector_cache_stale"
                                if not cache_fresh
                                else "selector_config_invalid"
                            )
                        )
                    )
                ),
                "cache": (
                    {
                        key: cache.get(key)
                        for key in (
                            "persisted_at",
                            "data_updated_at",
                            "age_ms",
                            "data_update_count",
                            "status",
                            "fetch_status",
                            "value_type",
                            "string_length",
                            "exact_key_present",
                            "key_count",
                            "shape_candidates",
                        )
                        if key in cache
                    }
                    if isinstance(cache, dict)
                    else {}
                ),
            }
            if selector_verified:
                selector_config = selector_payload.get("config")
                selector_state = selector_payload.get("state")
        chat_config: dict[str, Any] | None = None
        if isinstance(selector_config, dict):
            nested_chat = selector_config.get("chat")
            if isinstance(nested_chat, dict):
                chat_config = nested_chat
            elif (
                selector_config.get("id") == "chat"
                or isinstance(selector_config.get("models"), list)
            ):
                chat_config = selector_config
        elif isinstance(selector_config, list):
            chat_config = next(
                (
                    item
                    for item in selector_config
                    if isinstance(item, dict) and item.get("id") == "chat"
                ),
                None,
            )
        if isinstance(chat_config, dict):
            rows = chat_config.get("models")
            if isinstance(rows, list):
                for raw_model in rows:
                    if not isinstance(raw_model, dict):
                        continue
                    model_id = str(raw_model.get("id") or "").strip()
                    if not model_id:
                        continue
                    section = str(raw_model.get("section") or "")
                    catalog_available = bool(
                        raw_model.get("available", True) is not False
                        and not raw_model.get("inactive")
                        and section
                        not in {"deprecated", "inactive", "legacy"}
                    )
                    disabled_reason = self._normalize_disabled_reason(
                        raw_model.get("disabled_reason")
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
                    model = {
                        "id": model_id,
                        "name": str(
                            raw_model.get("name")
                            or raw_model.get("label")
                            or model_id
                        ),
                        "section": section or None,
                        "available": available,
                        "catalog_available": catalog_available,
                        "access_status": (
                            "unavailable"
                            if not available
                            else "available"
                        ),
                        "source": "account_selector",
                        "disabled_reason": (
                            disabled_reason
                        ),
                        "capabilities": (
                            raw_model.get("capabilities")
                            if isinstance(
                                raw_model.get("capabilities"),
                                (dict, list),
                            )
                            else None
                        ),
                        "thinking": (
                            raw_model.get("thinking")
                            if isinstance(raw_model.get("thinking"), dict)
                            else None
                        ),
                        "supports_fast_mode": bool(
                            raw_model.get("supports_fast_mode")
                        ),
                    }
                    available_models.append(model)
        if not available_models:
            bootstrap_rows = self._find_named_value(
                account_payload,
                "claude_ai_bootstrap_models_config",
            )
            if isinstance(bootstrap_rows, list):
                for raw_model in bootstrap_rows:
                    if not isinstance(raw_model, dict):
                        continue
                    model_id = str(
                        raw_model.get("model")
                        or raw_model.get("id")
                        or ""
                    ).strip()
                    if not model_id:
                        continue
                    inactive = bool(raw_model.get("inactive"))
                    disabled_reason = self._normalize_disabled_reason(
                        raw_model.get("disabled_reason")
                    )
                    # The bootstrap list is a product catalog, not proof that
                    # the active account is entitled to invoke a model.
                    catalog_available = False
                    if inactive and disabled_reason is None:
                        disabled_reason = "inactive"
                    elif disabled_reason is None:
                        disabled_reason = (
                            "account_unavailable"
                            if raw_model.get("available") is False
                            else "catalog_only"
                        )
                    available = False
                    thinking_modes = raw_model.get("thinking_modes")
                    model = {
                        "id": model_id,
                        "name": str(
                            raw_model.get("name")
                            or raw_model.get("label")
                            or model_id
                        ),
                        "section": "inactive" if inactive else None,
                        "available": available,
                        "catalog_available": catalog_available,
                        "access_status": (
                            "unavailable"
                            if inactive
                            or raw_model.get("available") is False
                            or raw_model.get("disabled_reason") is not None
                            else "unverified"
                        ),
                        "source": "bootstrap_catalog",
                        "disabled_reason": (
                            disabled_reason
                        ),
                        "capabilities": (
                            raw_model.get("capabilities")
                            if isinstance(
                                raw_model.get("capabilities"),
                                (dict, list),
                            )
                            else None
                        ),
                        "thinking": (
                            {"modes": thinking_modes}
                            if isinstance(thinking_modes, list)
                            else None
                        ),
                        "supports_fast_mode": bool(
                            raw_model.get("supports_fast_mode")
                            or "instant" in _string_list(
                                raw_model.get("paprika_modes")
                            )
                        ),
                    }
                    available_models.append(model)
        state: dict[str, Any] | None = None
        if isinstance(selector_state, dict):
            nested_chat = selector_state.get("chat")
            if isinstance(nested_chat, dict):
                state = nested_chat
            elif selector_state.get("id") == "chat":
                state = selector_state
        elif isinstance(selector_state, list):
            state = next(
                (
                    item
                    for item in selector_state
                    if isinstance(item, dict) and item.get("id") == "chat"
                ),
                None,
            )
        if isinstance(state, dict):
            model_selector_state = {
                key: state.get(key)
                for key in (
                    "model",
                    "thinking",
                    "thinking_by_model",
                    "preset_key",
                    "org_enforced_default_model",
                    "selection_source",
                )
                if key in state
            }
        # Commit atomically only after the account record has been fully
        # verified. Partial `/api/account` data must never look authenticated.
        organization_hint = str(
            self.current_profile_spec().get("organization_id") or ""
        ).strip()
        if not uuid_re.fullmatch(organization_hint):
            organization_hint = ""
        self._account_uuid = account_uuid
        self._account_name = account_name
        self._account_email_masked = account_email_masked
        self._organization_uuid = organization_hint or None
        self._available_models = available_models
        self._model_selector_state = model_selector_state
        self._model_selector_diagnostics = selector_diagnostics
        self._observed_models.update(
            str(model["id"]) for model in available_models
        )
        return True

    async def _sync_trusted_project(self) -> bool:
        """Verify the account-owned Project and sync its trusted instructions."""
        if not self._project_instructions:
            return True
        spec = self.current_profile_spec()
        project_id = str(spec.get("project_id") or "").strip()
        if not project_id:
            self._project_instructions_synced = False
            self._project_sync_error = (
                "the active profile has no Claude Project"
            )
            return False
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(
                    """
                    async ({projectId, organizationHint}) => {
                      const uuidRe =
                        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
                      const candidates = [];
                      const add = (value) => {
                        const normalized = String(value || '');
                        if (
                          uuidRe.test(normalized)
                          && !candidates.includes(normalized)
                        ) candidates.push(normalized);
                      };
                      add(organizationHint);
                      for (const resource of performance.getEntriesByType('resource')) {
                        const match = String(resource.name || '').match(
                          /\\/api\\/organizations\\/([0-9a-f-]{36})(?:\\/|$)/i
                        );
                        if (match) add(match[1]);
                      }
                      let account = null;
                      try {
                        const response = await fetch('/api/account', {
                          credentials: 'include',
                          cache: 'no-store',
                          headers: {Accept: 'application/json'}
                        });
                        if (response.ok) account = await response.json();
                      } catch {}
                      const walk = (value, parentKey = '', depth = 0) => {
                        if (!value || depth > 9) return;
                        if (Array.isArray(value)) {
                          for (const child of value) {
                            walk(child, parentKey, depth + 1);
                          }
                          return;
                        }
                        if (typeof value !== 'object') return;
                        for (const [key, child] of Object.entries(value)) {
                          const organizationScope =
                            /organization(?:_uuid|_id)?/i.test(key)
                            || /organizations|memberships/i.test(parentKey);
                          if (organizationScope && typeof child === 'string') {
                            add(child);
                          }
                          if (
                            organizationScope
                            && child
                            && typeof child === 'object'
                          ) {
                            add(child.uuid || child.id);
                          }
                          walk(child, key, depth + 1);
                        }
                      };
                      walk(account);
                      for (const organizationUuid of candidates) {
                        const url =
                          `/api/organizations/${organizationUuid}/projects/${projectId}`;
                        let response;
                        try {
                          response = await fetch(url, {
                            credentials: 'include',
                            cache: 'no-store',
                            headers: {Accept: 'application/json'}
                          });
                        } catch {
                          continue;
                        }
                        if (!response.ok) continue;
                        let project;
                        try {
                          project = await response.json();
                        } catch {
                          return {ok: false, reason: 'project_bad_json'};
                        }
                        const actualId = String(
                          project?.uuid || project?.id || ''
                        );
                        if (actualId !== projectId) continue;
                        return {
                          ok: true,
                          organizationUuid,
                          promptTemplate: String(
                            project?.prompt_template || ''
                          ),
                          privacyVerified: project?.is_private === true
                        };
                      }
                      return {ok: false, reason: 'project_not_owned'};
                    }
                    """,
                    {
                        "projectId": project_id,
                        "organizationHint": spec.get("organization_id"),
                    },
                ),
                timeout=30,
            )
        except Exception as exc:
            self._project_instructions_synced = False
            self._project_sync_error = (
                f"Claude Project verification failed: {type(exc).__name__}"
            )
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (
                str(result.get("reason") or "unknown")
                if isinstance(result, dict)
                else "invalid_response"
            )
            self._project_instructions_synced = False
            self._project_sync_error = (
                f"Claude Project verification failed: {reason}"
            )
            return False
        organization_uuid = str(result.get("organizationUuid") or "")
        self._organization_uuid = organization_uuid or None
        if self._organization_uuid:
            self.profile_specs[self.profile_index]["organization_id"] = (
                self._organization_uuid
            )
        current_prompt = str(result.get("promptTemplate") or "")
        managed_prompt_kind = self._managed_project_prompt_kind(
            current_prompt
        )
        if managed_prompt_kind is None:
            self._project_instructions_synced = False
            self._project_sync_error = (
                "Claude Project instructions differ from the configured "
                "OpenClaude IDE contract; the external edit was preserved"
            )
            return False
        privacy_verified = result.get("privacyVerified") is True
        if managed_prompt_kind != "current" or not privacy_verified:
            try:
                await self._write_verified_project_prompt(
                    self._project_instructions,
                    expected_current=current_prompt,
                )
            except Exception as exc:
                self._project_instructions_synced = False
                self._project_sync_error = (
                    "Claude Project stable-instruction recovery failed: "
                    + type(exc).__name__
                )
                return False
            privacy_verified = True
        self._project_instructions_synced = True
        self._project_sync_error = None
        self._project_privacy_verified = privacy_verified
        self._record_project_prompt_lease(self._project_instructions)
        return True

    @staticmethod
    def _project_prompt_hash(prompt_template: str) -> str:
        return hashlib.sha256(
            prompt_template.encode("utf-8")
        ).hexdigest()

    def _leased_project_prompt_hash(
        self,
        project_id: str,
    ) -> str | None:
        path = self._project_prompt_lease_file
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("lease document must be an object")
            leases = payload.get("leases")
            if not isinstance(leases, dict):
                raise ValueError("leases must be an object")
            self._project_lease_error = None
            row = leases.get(project_id)
            if not isinstance(row, dict):
                return None
            prompt_hash = str(row.get("prompt_sha256") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", prompt_hash):
                raise ValueError("prompt_sha256 is invalid")
            self._project_lease_error = None
            return prompt_hash
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._project_lease_error = (
                "OpenClaude Project prompt lease could not be read: "
                + type(exc).__name__
            )
            return None

    def _record_project_prompt_lease(
        self,
        prompt_template: str,
    ) -> bool:
        path = self._project_prompt_lease_file
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        ).strip()
        if path is None or not project_id:
            return True
        payload: dict[str, Any] = {"schema": 1, "leases": {}}
        try:
            if path.exists():
                raw_candidate = path.read_text(encoding="utf-8")
                try:
                    candidate = json.loads(raw_candidate)
                except (TypeError, json.JSONDecodeError):
                    candidate = {}
                if (
                    isinstance(candidate, dict)
                    and isinstance(candidate.get("leases"), dict)
                ):
                    payload = {
                        "schema": 1,
                        "leases": dict(candidate["leases"]),
                    }
            payload["leases"][project_id] = {
                "profile_id": self.current_profile_id(),
                "prompt_sha256": self._project_prompt_hash(
                    prompt_template
                ),
                "updated_at": time.time(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._project_lease_error = None
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._project_lease_error = (
                "OpenClaude Project prompt lease could not be persisted: "
                + type(exc).__name__
            )
            return False

    def _managed_project_prompt_kind(self, prompt_template: str) -> str | None:
        """Recognize current and exact prior OpenClaude-owned prompt formats."""
        if prompt_template == self._project_instructions:
            return "current"
        prompt_hash = self._project_prompt_hash(prompt_template)
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        ).strip()
        if (
            project_id
            and prompt_hash == self._leased_project_prompt_hash(project_id)
        ):
            return "leased"
        if prompt_hash in KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256:
            return "previous"
        base, marker, dynamic_context = prompt_template.partition(
            LEGACY_DYNAMIC_PROJECT_PROMPT_MARKER
        )
        if not marker or not dynamic_context:
            return None
        base_hash = hashlib.sha256(base.encode("utf-8")).hexdigest()
        if (
            base == self._project_instructions
            or base_hash in KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256
        ):
            return "legacy_dynamic"
        return None

    async def _read_verified_project_prompt(self) -> str:
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if not project_id or not self._organization_uuid:
            raise ClaudeBrowserUnavailableError(
                "verified Claude Project identity is unavailable"
            )
        result = await asyncio.wait_for(
            self.page.evaluate(
                """
                async ({organizationUuid, projectId}) => {
                  const url =
                    `/api/organizations/${organizationUuid}/projects/${projectId}`;
                  const response = await fetch(url, {
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {Accept: 'application/json'}
                  });
                  if (!response.ok) {
                    return {ok: false, status: response.status};
                  }
                  const project = await response.json();
                  return {
                    ok: true,
                    projectId: String(project?.uuid || project?.id || ''),
                    promptTemplate: String(project?.prompt_template || ''),
                    privacyVerified: project?.is_private === true
                  };
                }
                """,
                {
                    "organizationUuid": self._organization_uuid,
                    "projectId": project_id,
                },
            ),
            timeout=30,
        )
        if (
            not isinstance(result, dict)
            or not result.get("ok")
            or result.get("projectId") != project_id
        ):
            status = (
                result.get("status")
                if isinstance(result, dict)
                else None
            )
            raise ClaudeBrowserUnavailableError(
                "Claude Project prompt could not be read and verified "
                f"(status={status})"
            )
        if result.get("privacyVerified") is True:
            self._project_privacy_verified = True
        return str(result.get("promptTemplate") or "")

    async def _write_verified_project_prompt(
        self,
        prompt_template: str,
        *,
        expected_current: str | None = None,
    ) -> None:
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if not project_id or not self._organization_uuid:
            raise ClaudeBrowserUnavailableError(
                "verified Claude Project identity is unavailable"
            )
        if expected_current is not None:
            current = await self._read_verified_project_prompt()
            if current != expected_current:
                raise ClaudeBrowserUnavailableError(
                    "Claude Project instructions changed before the verified "
                    "repair; the newer edit was preserved"
                )
        result = await asyncio.wait_for(
            self.page.evaluate(
                """
                async ({organizationUuid, projectId, promptTemplate}) => {
                  const url =
                    `/api/organizations/${organizationUuid}/projects/${projectId}`;
                  const update = await fetch(url, {
                    method: 'PUT',
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {
                      Accept: 'application/json',
                      'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                      prompt_template: promptTemplate,
                      is_private: true
                    })
                  });
                  return {ok: update.ok, status: update.status};
                }
                """,
                {
                    "organizationUuid": self._organization_uuid,
                    "projectId": project_id,
                    "promptTemplate": prompt_template,
                },
            ),
            timeout=30,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            status = (
                result.get("status")
                if isinstance(result, dict)
                else None
            )
            raise ClaudeBrowserUnavailableError(
                "Claude Project prompt update failed "
                f"(status={status})"
            )
        self._project_privacy_verified = None
        verified = await self._read_verified_project_prompt()
        if (
            verified != prompt_template
            or self._project_privacy_verified is not True
        ):
            raise ClaudeBrowserUnavailableError(
                "Claude Project prompt update could not be verified"
            )

    async def _activate_trusted_turn_context(
        self,
    ) -> None:
        """Verify the stable Project before request-scoped IDE context is sent."""
        if not self._project_instructions:
            return
        current = await self._read_verified_project_prompt()
        if current != self._project_instructions:
            self._project_instructions_synced = False
            self._project_sync_error = (
                "Claude Project instructions differ from the configured "
                "OpenClaude IDE contract; the external edit was preserved"
            )
            self.ready = False
            self._set_phase("project_unavailable")
            raise ClaudeBrowserUnavailableError(self._project_sync_error)
        self._project_instructions_synced = True
        self._project_sync_error = None

    def _clear_account_identity(self) -> None:
        self._account_uuid = None
        self._account_name = None
        self._account_email_masked = None
        self._organization_uuid = None
        self._available_models = []
        self._model_selector_state = {}
        self._model_selector_diagnostics = {}

    async def _verify_account_unchanged_unlocked(self) -> None:
        profile_id = self.current_profile_id()
        expected_uuid = (
            self._profile_account_uuids.get(profile_id)
            or self._account_uuid
        )
        if not expected_uuid:
            self.ready = False
            self._set_phase("account_unknown")
            raise ClaudeBrowserUnavailableError(
                "Claude account identity is not verified"
            )
        identity_ready = await self._load_account_identity()
        if not identity_ready:
            self.ready = False
            self._last_error = (
                "Claude /api/account identity could not be revalidated"
            )
            self._set_phase("account_unknown")
            raise ClaudeBrowserUnavailableError(self._last_error)
        if self._account_uuid != expected_uuid:
            self.ready = False
            self._last_error = (
                "The active Camoufox profile changed Claude accounts; "
                "the IDE request was blocked before submission"
            )
            self._set_phase("account_changed")
            raise ClaudeAccountIdentityError(self._last_error)
        self._profile_account_uuids.setdefault(profile_id, expected_uuid)

    def health_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "ok": bool(
                self.ready
                and not self._browser_dead.is_set()
                and self._last_probe_ok is not False
                and self._phase
                not in {
                    "stopped",
                    "starting_browser",
                    "recovering_browser",
                    "browser_dead",
                    "auth_required",
                    "account_unknown",
                    "account_changed",
                    "project_unavailable",
                }
            ),
            "url": self._mask_identifiers(
                getattr(self.page, "url", None)
            ),
            "profile": self.profile_index + 1,
            "profile_count": len(self.profile_dirs),
            "profile_id": self.current_profile_id(),
            "profile_name": self.current_profile_spec().get("name"),
            "account": {
                "authenticated": bool(self._account_uuid),
                "uuid_suffix": (
                    self._account_uuid[-8:] if self._account_uuid else None
                ),
                "name": self._account_name,
                "email": self._account_email_masked,
            },
            "project": {
                "configured": bool(
                    self.current_profile_spec().get("project_id")
                ),
                "instructions_synced": self._project_instructions_synced,
                "privacy_verified": self._project_privacy_verified,
                "lease_error": self._project_lease_error,
                "turn_context_active": False,
                "dynamic_context_channel": "native_tool_description",
                "error": self._project_sync_error,
            },
            "browser": {
                "phase": self._phase,
                "session_epoch": self._session_epoch,
                "driver_pid": self._driver_pid,
                "operation_id": self._operation_id,
                "phase_age_seconds": round(now - self._phase_started_at, 1),
                "progress_age_seconds": round(now - self._last_progress_at, 1),
                "restart_count": self._restart_count,
                "last_probe_ok": self._last_probe_ok,
                "last_probe_at": self._last_probe_at,
                "last_recovery_reason": self._last_recovery_reason,
                "last_recovery_at": self._last_recovery_at,
                "last_error": self._last_error,
                "watchdog_running": self.watchdog_running(),
                "watchdog_healthy": self.watchdog_healthy(),
                "recovery_exhausted": self._recovery_exhausted,
            },
            "native": {
                "active": self._native_active,
                "pending_tool_ids": sorted(self._native_pending_ids),
                "tool_result_delivery": dict(self._tool_result_delivery),
                "model": self._native_model,
                "usage": dict(self._native_usage),
                "tap": {
                    "event_count": self._sse_tap_event_count,
                    "rejected_count": self._sse_tap_rejected_count,
                    "last_at": self._sse_tap_last_at,
                    "last_event": self._sse_tap_last_event,
                    "last_url": self._mask_identifiers(
                        self._sse_tap_last_url
                    ),
                    "last_data": self._sse_tap_last_data,
                },
            },
            "models": {
                "available": [
                    dict(model) for model in self._available_models
                ],
                "observed": self.observed_models(),
                "state": dict(self._model_selector_state),
                "selector": dict(self._model_selector_diagnostics),
            },
        }

    def watchdog_running(self) -> bool:
        return bool(
            self._watchdog_task is not None
            and not self._watchdog_task.done()
        )

    def watchdog_healthy(self) -> bool:
        if not self.watchdog_running() or self._recovery_exhausted:
            return False
        max_age = max(30.0, self._watchdog_interval * 4)
        if self._phase in {"starting_browser", "recovering_browser"}:
            max_age = self._browser_start_timeout + 30.0
        return time.monotonic() - self._watchdog_heartbeat_at <= max_age

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
            self._camoufox = AsyncCamoufox(
                headless=self.headless,
                persistent_context=True,
                user_data_dir=str(profile_dir),
                # Camoufox currently treats bool as a numeric maxTime and
                # serializes True, which the browser rejects as non-double.
                humanize=self._humanize_seconds or False,
            )
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
            startup_probe_timeout = max(
                2.0,
                min(configured_ready_timeout, 20.0),
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

    async def rotate_profile(
        self,
        eligible_profile_ids: set[str] | None = None,
    ) -> bool:
        """Move to the next pre-authenticated browser profile, if configured."""
        async with self._lock:
            if len(self.profile_dirs) < 2:
                return False
            target_index: int | None = None
            for offset in range(1, len(self.profile_dirs) + 1):
                candidate = (self.profile_index + offset) % len(
                    self.profile_dirs
                )
                candidate_id = str(self.profile_specs[candidate]["id"])
                if (
                    eligible_profile_ids is None
                    or candidate_id in eligible_profile_ids
                ):
                    target_index = candidate
                    break
            if target_index is None or target_index == self.profile_index:
                return False
            await self._stop_browser_unlocked()
            self.profile_index = target_index
            try:
                await self.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ClaudeBrowserUnavailableError(
                    f"rotated Camoufox profile failed to start: {exc}"
                ) from exc
            if not self.ready:
                raise ClaudeBrowserUnavailableError(
                    "rotated Camoufox profile requires authentication or "
                    "account verification"
                )
            return True

    async def _receive_sse(self, source: Any, payload: Any) -> None:
        del source
        if (
            not self._native_active
            or self._native_queue is None
            or not isinstance(payload, dict)
        ):
            return
        url = str(payload.get("url", ""))
        event = str(payload.get("event", ""))
        self._sse_tap_last_at = time.time()
        self._sse_tap_last_event = event
        self._sse_tap_last_url = url
        if event == "__tap_http_error":
            try:
                diagnostic = json.loads(str(payload.get("data", "")))
            except json.JSONDecodeError:
                diagnostic = {}
            self._sse_tap_last_data = json.dumps(
                {"status": diagnostic.get("status")},
                ensure_ascii=True,
            )
        elif event.startswith("__tap_"):
            self._sse_tap_last_data = str(payload.get("data", ""))[:500]
        if (
            self._native_completion_url
            and url.split("?", 1)[0]
            != self._native_completion_url.split("?", 1)[0]
        ):
            self._sse_tap_rejected_count += 1
            return
        self._sse_tap_event_count += 1
        self._set_phase("waiting_sse")
        await self._native_queue.put(
            {
                "event": event,
                "data": str(payload.get("data", "")),
            }
        )

    async def _route_conversation_create(
        self,
        route: Any,
        request: Any,
        epoch: int | None = None,
    ) -> None:
        if (
            epoch is not None
            and epoch != self._session_epoch
        ) or request.method != "POST":
            await route.continue_()
            return
        if self._privacy_mode != "ephemeral":
            await route.continue_()
            return
        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            await route.continue_()
            return
        payload["is_temporary"] = True
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if project_id:
            if "project_id" in payload and "project_uuid" not in payload:
                payload["project_id"] = project_id
            else:
                payload["project_uuid"] = project_id
        headers = dict(request.headers)
        headers.pop("content-length", None)
        headers["content-type"] = "application/json"
        await route.continue_(
            headers=headers,
            post_data=json.dumps(payload, ensure_ascii=False),
        )

    async def _route_completion(
        self,
        route: Any,
        request: Any,
        epoch: int | None = None,
    ) -> None:
        if epoch is not None and epoch != self._session_epoch:
            await route.continue_()
            return
        self._debug(
            "route:"
            f"{request.method}:{request.url}:"
            f"active={self._native_active}:tools={len(self._native_tools)}"
        )
        if request.method != "POST" or not self._native_active:
            await route.continue_()
            return
        match = COMPLETION_IDS_RE.search(request.url)
        if not match:
            await route.continue_()
            return

        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            await route.continue_()
            return

        original_model = payload.get("model")
        if isinstance(original_model, str) and original_model:
            self._observed_models.add(original_model)
        create_params = payload.get("create_conversation_params")
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if isinstance(create_params, dict):
            if project_id:
                if (
                    "project_id" in create_params
                    and "project_uuid" not in create_params
                ):
                    create_params["project_id"] = project_id
                else:
                    create_params["project_uuid"] = project_id
            if self._privacy_mode == "ephemeral":
                create_params["is_temporary"] = True
        self._last_completion_shape = {
            "keys": sorted(str(key) for key in payload),
            "model": original_model,
            "has_thinking": any(
                key in payload
                for key in (
                    "thinking",
                    "thinking_enabled",
                    "extended_thinking",
                    "effort",
                )
            ),
            "thinking_keys": [
                key
                for key in (
                    "thinking",
                    "thinking_enabled",
                    "extended_thinking",
                    "effort",
                )
                if key in payload
            ],
            "create_conversation": (
                {
                    "keys": sorted(str(key) for key in create_params),
                    "has_project": bool(
                        create_params.get("project_uuid")
                        or create_params.get("project_id")
                    ),
                    "is_temporary": bool(
                        create_params.get("is_temporary")
                    ),
                }
                if isinstance(create_params, dict)
                else None
            ),
            "context_channel": "native_tool_description",
        }
        if self._native_requested_model:
            payload["model"] = self._native_requested_model
            self._observed_models.add(self._native_requested_model)
        payload["thinking_mode"] = {
            "off": "off",
            "show": "extended",
        }.get(self._native_thinking_mode, "auto")
        if self._native_thinking_mode == "off":
            payload.pop("effort", None)
        elif self._native_effort:
            payload["effort"] = {
                "xhigh": "max",
                "ultra": "max",
            }.get(self._native_effort, self._native_effort)

        if (
            self._native_completion_url
            and request.url != self._native_completion_url
            and "retry_completion" in request.url
        ):
            if bool(
                getattr(self._native_event_sink, "visible_seen", False)
            ):
                self._emit_native_event(
                    {
                        "type": "retract",
                        "from_index": 0,
                        "reason": "claude_retry_completion",
                    }
                )
            # A retry is a replacement stream. Drop partial blocks and queued
            # frames from the failed generation before accepting its events.
            self._native_queue = asyncio.Queue()
            self._native_blocks.clear()
            self._native_text_blocks.clear()
            self._native_tool_blocks.clear()
            self._native_thinking_blocks.clear()
            self._native_usage.clear()
            self._native_model = None
            self._native_stop_reason = None
            self._native_saw_content = False
            self._native_saw_tool = False
            self._native_terminal_seen = False

        custom_names = {
            tool["name"]
            for tool in self._native_tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        original_tools = [
            tool
            for tool in payload.get("tools", [])
            if isinstance(tool, dict) and tool.get("name") not in custom_names
        ]
        payload["tools"] = [*self._native_tools, *original_tools]
        self._debug(
            "route:inject:"
            + ",".join(
                str(tool.get("name", ""))
                for tool in self._native_tools
            )
        )

        self._native_completion_url = request.url
        self._native_org_uuid = match.group("org")
        self._native_conversation_uuid = match.group("conversation")
        self._set_phase("completion_intercepted")
        self._native_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in PASSTHROUGH_HEADER_NAMES
        }

        headers = dict(request.headers)
        headers.pop("content-length", None)
        headers["content-type"] = "application/json"
        await route.continue_(
            headers=headers,
            post_data=json.dumps(payload, ensure_ascii=False),
        )
        self._set_phase("waiting_first_sse")

    def _reset_native_parser(self) -> None:
        self._native_queue = asyncio.Queue()
        self._native_completion_url = None
        self._native_org_uuid = None
        self._native_conversation_uuid = None
        self._native_headers = {}
        self._native_pending_ids = set()
        self._native_pending_deadline = None
        self._native_blocks = {}
        self._native_text_blocks = {}
        self._native_tool_blocks = {}
        self._native_thinking_blocks = {}
        self._native_usage = {}
        self._native_model = None
        self._native_stop_reason = None
        self._tool_result_delivery = {}
        self._native_internal_tool_acks = 0
        self._native_internal_text_prefix = []
        self._native_internal_thinking_prefix = []
        self._native_saw_content = False
        self._native_saw_tool = False
        self._native_terminal_seen = False
        self._native_conversation_verified = False
        self._sse_tap_event_count = 0
        self._sse_tap_rejected_count = 0
        self._sse_tap_last_at = None
        self._sse_tap_last_event = None
        self._sse_tap_last_url = None
        self._sse_tap_last_data = None

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
        if self._recovery_exhausted:
            raise ClaudeBrowserUnavailableError(
                self._last_error or "Camoufox restart circuit is open"
            )
        await self._recover_browser_unlocked(reason)

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
                    self._history_recovery_required = True
                    self._mark_browser_dead(
                        "native request was cancelled after submit"
                    )
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

    def _emit_native_event(self, payload: dict[str, Any]) -> None:
        sink = self._native_event_sink
        if sink is None:
            return
        try:
            sink(dict(payload))
        except Exception:
            # Streaming observers must never be able to corrupt Claude's
            # authoritative parser/tool state.
            return

    def _update_native_usage(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        changed = False
        for key, raw in value.items():
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                self._native_usage[str(key)] = int(raw)
                changed = True
            elif isinstance(raw, dict):
                current = self._native_usage.get(str(key))
                if not isinstance(current, dict):
                    current = {}
                for nested_key, nested_value in raw.items():
                    if isinstance(nested_value, (int, float)) and not isinstance(
                        nested_value,
                        bool,
                    ):
                        current[str(nested_key)] = int(nested_value)
                        changed = True
                self._native_usage[str(key)] = current
        if changed:
            self._emit_native_event(
                {"type": "usage", "usage": dict(self._native_usage)}
            )

    def _process_native_event(self, envelope: dict[str, str]) -> bool:
        event = envelope.get("event", "")
        raw = envelope.get("data", "")
        if event == "__browser_dead":
            raise ClaudeBrowserUnavailableError(raw or "Camoufox became unavailable")
        if event == "__tap_error":
            if "abort" not in raw.lower():
                raise RuntimeError(f"claude.ai SSE tap failed: {raw}")
            return False
        if event == "__tap_http_error":
            try:
                error_payload = json.loads(raw)
            except json.JSONDecodeError:
                error_payload = {}
            try:
                status = int(error_payload.get("status") or 500)
            except (TypeError, ValueError):
                status = 500
            message = str(
                error_payload.get("message")
                or "completion returned no SSE frames"
            )
            if status == 429:
                raise ClaudeUsageLimitError(
                    "claude.ai rejected the completion because the current "
                    "account reached a usage or rate limit",
                    replay_safe=True,
                )
            if status == 529:
                raise ClaudeServiceUnavailableError(
                    "claude.ai rejected the completion because the service "
                    "is overloaded"
                )
            raise ClaudeCompletionRejectedError(status, message)
        if event == "__tap_eof":
            if self._native_terminal_seen:
                return False
            raise RuntimeError("claude.ai SSE ended before message_stop")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            if event == "error":
                raise RuntimeError(f"claude.ai stream error: {raw}") from None
            return False
        if event in ("", "message"):
            event = str(payload.get("type", event))
        if os.getenv("CLAUDE_DEBUG_SSE", "0").lower() in ("1", "true", "yes"):
            delta = payload.get("delta")
            delta_type = (
                str(delta.get("type", ""))
                if isinstance(delta, dict)
                else None
            )
            lengths: dict[str, int] = {}
            if isinstance(delta, dict):
                for key in ("text", "thinking", "summary", "partial_json"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        lengths[key] = len(value)
            print(
                "CLAUDE_SSE "
                + json.dumps(
                    {
                        "event": event,
                        "index": payload.get("index"),
                        "delta_type": delta_type,
                        "lengths": lengths,
                        "usage": (
                            payload.get("usage")
                            if event in {"message_start", "message_delta"}
                            else None
                        ),
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )

        if event == "message_start":
            message = payload.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model:
                    self._native_model = model
                    self._observed_models.add(model)
                    self._emit_native_event({"type": "model", "model": model})
                self._update_native_usage(message.get("usage"))
            return False

        if event == "message_delta":
            self._update_native_usage(payload.get("usage"))
            delta = payload.get("delta")
            if isinstance(delta, dict):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    self._native_stop_reason = stop_reason
                self._emit_native_event(
                    {
                        "type": "message_delta",
                        "stop_reason": delta.get("stop_reason"),
                    }
                )
            return False

        if event in {"model_update", "model_fallback"}:
            candidate = (
                payload.get("model")
                or payload.get("to_model")
                or (
                    payload.get("to", {}).get("model")
                    if isinstance(payload.get("to"), dict)
                    else None
                )
            )
            if isinstance(candidate, str) and candidate:
                self._native_model = candidate
                self._observed_models.add(candidate)
                self._emit_native_event(
                    {"type": "model", "model": candidate}
                )
            return False

        if event == "content_block_start":
            index = payload.get("index")
            block = payload.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                return False
            block_type = block.get("type")
            if block_type == "text":
                self._native_saw_content = True
                self._native_blocks[index] = {
                    "type": "text",
                    "text": str(block.get("text", "") or ""),
                }
                initial_text = str(block.get("text", "") or "")
                if initial_text:
                    self._emit_native_event(
                        {
                            "type": "text_delta",
                            "index": index,
                            "text": initial_text,
                        }
                    )
            elif block_type in {"thinking", "thinking_summary"}:
                # Expose only provider-authored summaries, never raw/opaque
                # chain-of-thought fields.
                initial_thinking = (
                    str(block.get("summary") or "")
                    if block_type == "thinking_summary"
                    else ""
                )
                self._native_blocks[index] = {
                    "type": "thinking",
                    "thinking": initial_thinking,
                }
                if initial_thinking:
                    self._emit_native_event(
                        {
                            "type": "thinking_delta",
                            "index": index,
                            "thinking": initial_thinking,
                        }
                    )
            elif block_type == "redacted_thinking":
                # ``data`` in this block is provider-owned opaque material,
                # not a user-visible summary. Never surface or log it.
                self._native_blocks[index] = {
                    "type": "redacted_thinking",
                }
            elif block_type == "tool_use":
                self._native_saw_tool = True
                self._native_blocks[index] = {
                    "type": "tool_use",
                    "id": str(block.get("id", "") or ""),
                    "name": str(block.get("name", "") or ""),
                    "initial_input": block.get("input"),
                    "json_parts": [],
                }
            return False

        if event == "content_block_delta":
            index = payload.get("index")
            block = self._native_blocks.get(index)
            delta = payload.get("delta")
            if not isinstance(block, dict) or not isinstance(delta, dict):
                return False
            if block.get("type") == "text" and delta.get("type") == "text_delta":
                text_delta = str(delta.get("text", "") or "")
                block["text"] += text_delta
                if text_delta:
                    self._emit_native_event(
                        {
                            "type": "text_delta",
                            "index": index,
                            "text": text_delta,
                        }
                    )
            elif (
                block.get("type") == "thinking"
                and delta.get("type") == "thinking_summary_delta"
            ):
                thinking_delta = str(
                    delta.get("summary")
                    or delta.get("text")
                    or ""
                )
                block["thinking"] += thinking_delta
                if thinking_delta:
                    self._emit_native_event(
                        {
                            "type": "thinking_delta",
                            "index": index,
                            "thinking": thinking_delta,
                        }
                    )
            elif (
                block.get("type") == "tool_use"
                and delta.get("type") == "input_json_delta"
            ):
                block["json_parts"].append(str(delta.get("partial_json", "") or ""))
            return False

        if event == "content_block_stop":
            index = payload.get("index")
            if not isinstance(index, int):
                return False
            block = self._native_blocks.pop(index, None)
            if not isinstance(block, dict):
                return False
            if block.get("type") == "text":
                self._native_text_blocks[index] = str(block.get("text", "") or "")
            elif block.get("type") == "thinking":
                self._native_thinking_blocks[index] = str(
                    block.get("thinking", "") or ""
                )
            elif block.get("type") == "tool_use":
                raw_input = "".join(block.get("json_parts", []))
                if not raw_input:
                    buffered = payload.get("buffered_input")
                    raw_input = str(buffered or "")
                if raw_input:
                    try:
                        tool_input = json.loads(raw_input)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            "Claude returned invalid native tool input JSON"
                        ) from exc
                else:
                    tool_input = block.get("initial_input") or {}
                if not isinstance(tool_input, dict):
                    raise RuntimeError("Claude native tool input must be a JSON object")
                tool_id = str(block.get("id", "") or "")
                tool_name = str(block.get("name", "") or "")
                allowed_names = {
                    str(tool.get("name"))
                    for tool in self._native_tools
                    if isinstance(tool, dict) and tool.get("name")
                }
                if not tool_id:
                    raise RuntimeError("Claude native tool_use is missing an id")
                if tool_name not in allowed_names:
                    # Browser-owned built-ins remain in the completion catalog.
                    # Claude's UI executes those itself; their tool_result and
                    # continuation stay on this same SSE, so the host bridge
                    # must simply observe and wait.
                    return False
                self._native_tool_blocks[index] = NativeToolUse(
                    id=tool_id,
                    name=tool_name,
                    input=tool_input,
                )
            return False

        if event == "content_block_retract":
            from_index = payload.get("from_index")
            if isinstance(from_index, int):
                self._emit_native_event(
                    {"type": "retract", "from_index": from_index}
                )
                self._native_blocks = {
                    index: block
                    for index, block in self._native_blocks.items()
                    if index < from_index
                }
                self._native_text_blocks = {
                    index: text
                    for index, text in self._native_text_blocks.items()
                    if index < from_index
                }
                self._native_tool_blocks = {
                    index: tool
                    for index, tool in self._native_tool_blocks.items()
                    if index < from_index
                }
                self._native_thinking_blocks = {
                    index: text
                    for index, text in self._native_thinking_blocks.items()
                    if index < from_index
                }
            return False

        if event == "message_limit":
            lowered = raw.lower()
            if any(
                marker in lowered
                for marker in (
                    '"status":"exceeded"',
                    '"status": "exceeded"',
                    '"type":"exceeded"',
                    '"type": "exceeded"',
                )
            ):
                raise ClaudeUsageLimitError(
                    "claude.ai reports that the current account reached its usage limit",
                    replay_safe=not self._native_saw_tool,
                )
            return False

        if event == "error":
            lowered = raw.lower()
            if "maximum conversation" in lowered or "conversation_too_long" in lowered:
                raise ClaudeConversationLimitError(
                    "claude.ai reports that this conversation reached its length limit",
                    replay_safe=not self._native_saw_tool,
                )
            if "rate_limit" in lowered or "usage_limit" in lowered:
                raise ClaudeUsageLimitError(
                    "claude.ai reports that the current account reached its usage limit",
                    replay_safe=not self._native_saw_tool,
                )
            raise RuntimeError(f"claude.ai stream error: {raw}")

        if event == "message_stop":
            self._native_terminal_seen = True
            return True
        return False

    def _take_native_text(self) -> str | None:
        text = "".join(
            value for _, value in sorted(self._native_text_blocks.items())
        )
        self._native_text_blocks.clear()
        return text or None

    def _take_native_thinking(self) -> str | None:
        thinking = "".join(
            value
            for _, value in sorted(self._native_thinking_blocks.items())
        )
        self._native_thinking_blocks.clear()
        return thinking or None

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
