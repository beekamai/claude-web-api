"""Browser-side scripts evaluated inside the claude.ai page.

Both are plain JavaScript kept verbatim; reflowing them would change the
code that actually runs in the page.
"""

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

ENSURE_PROJECT_SCRIPT = r"""
/* Find or create the account's private 'OpenClaude IDE' Project and make
   sure it carries exactly the bridge instructions. Runs in the page so the
   session's own cookies authenticate the calls. */
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
    `${instructions}\n\n`
    + 'DYNAMIC_OPENCLAUDE_SYSTEM_CONTEXT\n';
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
"""
