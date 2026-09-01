(() => {
  "use strict";

  const API_ROOT = "/api/control";
  const REFRESH_INTERVAL_MS = 5000;
  const LOGIN_POLL_INTERVAL_MS = 2000;

  const ui = {
    state: null,
    route: "overview",
    selectedModelProfileId: null,
    loginProfileId: null,
    loginSnapshot: null,
    refreshPromise: null,
    loginTimer: null,
    loginPollInFlight: false,
    customPersonaDirty: false,
    customPersonaRevision: 0,
    activity: null,
    activityTab: "metrics",
    activityPromise: null,
    activityRequestSerial: 0,
    activityFilterSignature: "",
    activitySearchTimer: null,
    historyRequestSerial: 0,
    proxyProfileId: null,
  };

  const labels = {
    provider: {
      claude_web: "Claude Web",
      grok_web: "Grok Web",
    },
    privacy: {
      keep: "Обычный",
      ephemeral: "Эфемерный",
    },
    persona: {
      default: "Обычный",
      programmer: "Программист",
      custom: "Своя инструкция",
    },
    modifier: {
      actor: "Актёр",
      mature: "Взрослый тон",
    },
    thinking: {
      off: "Не показывать",
      auto: "Если доступно",
      show: "Показывать",
    },
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function icon(name, className = "") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    if (className) svg.setAttribute("class", className);
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    return svg;
  }

  function node(tag, options = {}, children = []) {
    const element = document.createElement(tag);
    if (options.className) element.className = options.className;
    if (options.text !== undefined && options.text !== null) {
      element.textContent = String(options.text);
    }
    for (const [key, value] of Object.entries(options.attrs || {})) {
      if (value !== undefined && value !== null) {
        element.setAttribute(key, String(value));
      }
    }
    for (const child of Array.isArray(children) ? children : [children]) {
      if (child !== null && child !== undefined) element.append(child);
    }
    return element;
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(`${API_ROOT}${path}`, {
      cache: "no-store",
      ...options,
      headers,
      body:
        options.body !== undefined && typeof options.body !== "string"
          ? JSON.stringify(options.body)
          : options.body,
    });
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (response.status !== 204) {
      payload = contentType.includes("application/json")
        ? await response.json()
        : await response.text();
    }
    if (!response.ok) {
      const message =
        payload?.detail ||
        payload?.message ||
        (typeof payload === "string" && payload) ||
        `HTTP ${response.status}`;
      const error = new Error(String(message));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function unwrap(payload) {
    if (payload && typeof payload === "object" && payload.data) return payload.data;
    return payload || {};
  }

  function profiles() {
    const rows = Array.isArray(ui.state?.config?.profiles)
      ? ui.state.config.profiles
      : Array.isArray(ui.state?.profiles)
        ? ui.state.profiles
        : [];
    return rows.map((profile) => {
      const runtime = profile?.runtime || {};
      const runtimeModels = runtime?.models;
      return {
        ...profile,
        account:
          runtime?.account && typeof runtime.account === "object"
            ? runtime.account
            : profile.account,
        models: Array.isArray(runtimeModels?.available)
          ? runtimeModels.available
          : profile.models,
      };
    });
  }

  function activeProfile() {
    const activeId =
      ui.state?.config?.active_profile ||
      ui.state?.active_profile ||
      ui.state?.activeProfile ||
      ui.state?.active?.id;
    return profiles().find((profile) => profile.id === activeId) || profiles()[0] || null;
  }

  function profileAccount(profile) {
    return profile?.account && typeof profile.account === "object"
      ? profile.account
      : {};
  }

  function profileProvider(profile) {
    const id = String(profile?.provider || "claude_web");
    return {
      id,
      label: labels.provider[id] || id,
      site: id === "grok_web" ? "grok.com" : "claude.ai",
      product: id === "grok_web" ? "Grok" : "Claude",
    };
  }

  function providerCapabilities(profile) {
    const provider = profileProvider(profile);
    const capabilities = ui.state?.providers?.[provider.id];
    return capabilities && typeof capabilities === "object"
      ? capabilities
      : {};
  }

  function providerRuntimeReady(profile) {
    return providerCapabilities(profile).ready !== false;
  }

  function isAuthenticated(profile) {
    const account = profileAccount(profile);
    return Boolean(
      (account.authenticated ?? profile?.authenticated)
      && String(profile?.status || "") === "ready",
    );
  }

  function modelRows(profile) {
    const rows = Array.isArray(profile?.models) ? profile.models : [];
    return rows
      .map((entry) => {
        if (typeof entry === "string") {
          return {
            id: entry,
            label: entry,
            available: false,
            accessStatus: "unverified",
            disabledReason: { type: "catalog_only", requiredPlan: null },
          };
        }
        if (!entry || typeof entry !== "object" || !entry.id) return null;
        const accessStatus = String(
          entry.access_status
            || (entry.available === false ? "unavailable" : "unverified"),
        );
        const available = entry.available === true && accessStatus === "available";
        const rawDisabledReason = entry.disabled_reason;
        const disabledReason = rawDisabledReason && typeof rawDisabledReason === "object"
          ? {
              type: String(rawDisabledReason.type || "account_unavailable"),
              requiredPlan: rawDisabledReason.required_plan
                ? String(rawDisabledReason.required_plan)
                : null,
            }
          : rawDisabledReason
            ? { type: String(rawDisabledReason), requiredPlan: null }
            : null;
        return {
          id: String(entry.id),
          label: String(entry.label || entry.name || entry.id),
          available,
          accessStatus,
          disabledReason,
          source: entry.source || null,
        };
      })
      .filter(Boolean);
  }

  function modelAvailabilityText(model) {
    if (model.available && model.accessStatus === "available") return "Доступна";
    const reason = model.disabledReason || {};
    if (reason.type === "upgrade_required") {
      const plan = reason.requiredPlan
        ? reason.requiredPlan.charAt(0).toUpperCase() + reason.requiredPlan.slice(1)
        : "подписка";
      return `Требуется ${plan}`;
    }
    if (reason.type === "catalog_only" || model.accessStatus === "unverified") {
      return "В каталоге · доступ не подтверждён";
    }
    return "Недоступна аккаунту";
  }

  function behavior() {
    const source = ui.state?.config?.behavior || ui.state?.behavior || {};
    return {
      streaming: Boolean(source.streaming),
      thinking: source.thinking || "auto",
      privacy: source.privacy || "keep",
      persona: source.persona || "programmer",
      custom_persona: source.custom_persona || "",
      actor: Boolean(source.actor),
      mature: Boolean(source.mature),
    };
  }

  function personaCompilation() {
    const source = ui.state?.persona_compilation || {};
    return {
      raw: String(source.raw || ""),
      effective: String(source.effective || ""),
      changed: Boolean(source.changed),
      changes: Array.isArray(source.changes) ? source.changes : [],
      active: Boolean(source.active),
    };
  }

  function communicationModeLabel(value) {
    const parts = [labels.persona[value.persona] || value.persona];
    if (value.actor) parts.push(labels.modifier.actor);
    if (value.mature) parts.push(labels.modifier.mature);
    return parts.join(" · ");
  }

  function healthKind(value) {
    if (typeof value === "boolean") return value ? "healthy" : "error";
    const normalized = String(value || "").toLowerCase();
    if (
      ["ok", "healthy", "ready", "running", "connected", "authenticated", "alive", "up"].includes(
        normalized,
      )
    ) {
      return "healthy";
    }
    if (
      ["warn", "warning", "degraded", "recovering", "starting", "checking", "busy"].includes(
        normalized,
      )
    ) {
      return "warning";
    }
    if (
      ["error", "failed", "dead", "down", "unhealthy", "disconnected", "stopped"].includes(
        normalized,
      )
    ) {
      return "error";
    }
    return "unknown";
  }

  function healthLabel(kind, raw) {
    if (kind === "healthy") return "Здоров";
    if (kind === "warning") return "Внимание";
    if (kind === "error") return "Ошибка";
    return raw ? String(raw) : "Нет данных";
  }

  function normalizeSystems() {
    let source =
      ui.state?.systems ||
      ui.state?.health?.components ||
      ui.state?.diagnostics?.systems ||
      ui.state?.runtime?.systems ||
      [];
    if (
      (!Array.isArray(source) && !source) ||
      (Array.isArray(source) && source.length === 0) ||
      (typeof source === "object" && !Array.isArray(source)
        && Object.keys(source).length === 0)
    ) {
      const health = ui.state?.health || {};
      const browser = health.browser || {};
      const server = ui.state?.server || {};
      source = [
        {
          id: "api",
          name: "API",
          status: true,
          detail: server.version ? `v${server.version} · 127.0.0.1:${server.port}` : "",
        },
        {
          id: "camoufox",
          name: "Camoufox",
          status: health.ok
            ? "healthy"
            : ["auth_required", "account_unknown", "starting_browser", "recovering_browser"].includes(browser.phase)
              ? "warning"
              : "error",
          detail: browser.phase || "Нет данных",
        },
        {
          id: "watchdog",
          name: "Watchdog",
          status: Boolean(browser.watchdog_healthy),
          detail:
            typeof browser.restart_count === "number"
              ? `Перезапусков: ${browser.restart_count}`
              : "",
        },
        {
          id: "working_directory",
          name: "Рабочая директория",
          status: Boolean(server.working_directory),
          detail: server.working_directory || "Не передана",
        },
      ];
    }
    const entries = Array.isArray(source)
      ? source.map((item, index) => [item?.id || item?.name || `system-${index}`, item])
      : source && typeof source === "object"
        ? Object.entries(source)
        : [];

    return entries.map(([id, raw]) => {
      const value =
        raw && typeof raw === "object"
          ? raw
          : { status: raw, detail: null };
      const statusValue =
        value.status ?? value.state ?? value.health ?? value.healthy ?? value.ok;
      const kind = healthKind(statusValue);
      const normalizedId = String(id);
      return {
        id: normalizedId,
        name: value.name || value.label || systemName(normalizedId),
        detail:
          value.detail ||
          value.message ||
          value.path ||
          value.description ||
          value.endpoint ||
          "",
        status: statusValue,
        kind,
      };
    });
  }

  function systemName(id) {
    const key = String(id).toLowerCase();
    if (key.includes("camou") || key.includes("browser")) return "Camoufox";
    if (key.includes("watch")) return "Watchdog";
    if (key.includes("api") || key.includes("server")) return "API";
    if (key.includes("work") || key.includes("director") || key.includes("folder")) {
      return "Рабочая директория";
    }
    return String(id)
      .replace(/[_-]+/g, " ")
      .replace(/^\w/, (letter) => letter.toUpperCase());
  }

  function systemIcon(id) {
    const key = String(id).toLowerCase();
    if (key.includes("work") || key.includes("folder") || key.includes("director")) return "folder";
    if (key.includes("watch")) return "shield";
    if (key.includes("api") || key.includes("server")) return "diagnostics";
    return "model";
  }

  function normalizeEvents() {
    const source =
      ui.state?.activity?.events ||
      ui.state?.events ||
      ui.state?.event_log ||
      ui.state?.diagnostics?.events ||
      ui.state?.runtime?.events ||
      [];
    if (!Array.isArray(source)) return [];
    return source
      .map((event) => {
        if (typeof event === "string") {
          return { time: null, level: "info", component: "Сервис", message: event };
        }
        if (!event || typeof event !== "object") return null;
        return {
          time: event.time ?? event.timestamp ?? event.created_at ?? null,
          level: String(event.level || event.severity || "info").toLowerCase(),
          component: event.component || event.source || event.module || "Сервис",
          message: event.message || event.detail || event.event || "",
        };
      })
      .filter((event) => event && event.message);
  }

  function currentResponse() {
    const value =
      ui.state?.activity?.active ||
      ui.state?.current_response ||
      ui.state?.runtime?.current_response ||
      ui.state?.generation ||
      null;
    return value && typeof value === "object" ? value : null;
  }

  function formatTime(value, withDate = false) {
    if (value === null || value === undefined || value === "") return "—";
    let date;
    if (typeof value === "number") {
      date = new Date(value < 10_000_000_000 ? value * 1000 : value);
    } else {
      date = new Date(value);
    }
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("ru-RU", {
      ...(withDate ? { day: "2-digit", month: "2-digit", year: "numeric" } : {}),
      hour: "2-digit",
      minute: "2-digit",
      second: withDate ? undefined : "2-digit",
    }).format(date);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("ru-RU").format(value);
  }

  function setRoute(route) {
    // A request dialog belongs to the activity section; leaving it open while
    // the section changes hides the section you actually asked for.
    closeHistoryDetail();
    closeProxyDialog();
    const normalizedRoute = route === "diagnostics" ? "activity" : route;
    const allowed = new Set([
      "overview",
      "connect",
      "profiles",
      "models",
      "behavior",
      "activity",
    ]);
    ui.route = allowed.has(normalizedRoute) ? normalizedRoute : "overview";
    $$(".nav-item").forEach((item) => {
      const active = item.dataset.route === ui.route;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $$(".page").forEach((page) => {
      page.classList.toggle("is-active", page.dataset.page === ui.route);
    });
    if (ui.route === "connect") {
      renderClients();
      refreshClients({ quiet: Boolean(ui.clients) }).catch(() => {});
    }
    if (ui.route === "models") renderModels();
    if (ui.route === "activity") {
      renderActivity();
      refreshActivity({ quiet: Boolean(ui.activity) }).catch(() => {});
    }
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  async function refreshState({ quiet = false } = {}) {
    if (ui.refreshPromise) return ui.refreshPromise;
    ui.refreshPromise = (async () => {
      try {
        const payload = await request("/state");
        ui.state = unwrap(payload);
        hideGlobalError();
        render();
        return ui.state;
      } catch (error) {
        if (!quiet || !ui.state) showGlobalError(error);
        updateServiceIndicator("error", "Сервис недоступен");
        throw error;
      } finally {
        ui.refreshPromise = null;
      }
    })();
    return ui.refreshPromise;
  }

  function render() {
    renderSidebar();
    renderOverview();
    renderProfiles();
    renderModels();
    renderBehavior();
    renderActivity();
  }

  function renderSidebar() {
    const systems = normalizeSystems();
    let kind = "unknown";
    if (systems.some((item) => item.kind === "error")) kind = "error";
    else if (systems.some((item) => item.kind === "warning")) kind = "warning";
    else if (systems.length && systems.every((item) => item.kind === "healthy")) kind = "healthy";

    const serviceStatus =
      ui.state?.health?.ok
      ?? (
        ui.state?.service?.status
        || ui.state?.status
        || ui.state?.health?.status
      );
    if (kind === "unknown") kind = healthKind(serviceStatus);
    updateServiceIndicator(
      kind,
      kind === "healthy"
        ? "Сервис запущен"
        : kind === "warning"
          ? "Сервис восстанавливается"
          : kind === "error"
            ? "Есть проблема"
            : "Нет данных о сервисе",
    );
    const version =
      ui.state?.server?.version ||
      ui.state?.version ||
      ui.state?.app?.version ||
      ui.state?.service?.version ||
      "";
    $("#app-version").textContent = version ? `v${String(version).replace(/^v/, "")}` : "";
  }

  function updateServiceIndicator(kind, text) {
    const root = $("#service-indicator");
    const dot = $(".status-dot", root);
    dot.className = `status-dot status-dot--${kind === "unknown" ? "muted" : kind}`;
    $("span:last-child", root).textContent = text;
  }

  function renderOverview() {
    const profile = activeProfile();
    const account = profileAccount(profile);
    $("#overview-profile-name").textContent = profile?.name || "Профиль не выбран";
    $("#overview-account").textContent = account.name || "—";
    $("#overview-email").textContent = account.email || "—";

    const authTarget = $("#overview-auth-state");
    authTarget.replaceChildren();
    if (profile) {
      const authenticated = isAuthenticated(profile);
      authTarget.append(
        node("span", { className: "status-inline" }, [
          node("span", {
            className: `status-dot status-dot--${authenticated ? "healthy" : "muted"}`,
          }),
          node("span", { text: authenticated ? "Подключён" : "Не авторизован" }),
        ]),
      );
    } else {
      authTarget.textContent = "—";
    }

    renderProfileModelSelect($("#overview-model"), profile);
    const b = behavior();
    $("#overview-streaming").checked = b.streaming;
    $("#overview-thinking").value = b.thinking;
    $("#overview-privacy-label").textContent = labels.privacy[b.privacy] || b.privacy;
    $("#overview-persona-label").textContent = communicationModeLabel(b);
    renderCurrentResponse();
    renderSystemList();
    renderEventTable("#overview-events", "#overview-events-empty", normalizeEvents().slice(0, 8));
  }

  function renderProfileModelSelect(select, profile) {
    select.replaceChildren();
    if (!profile) {
      select.append(node("option", { text: "—" }));
      select.disabled = true;
      delete select.dataset.profileId;
      return;
    }
    select.dataset.profileId = profile.id;
    const rows = modelRows(profile);
    select.append(node("option", { text: "Автоматически", attrs: { value: "auto" } }));
    rows.forEach((model) => {
      const option = node("option", {
        text: model.label,
        attrs: { value: model.id },
      });
      option.disabled = !model.available;
      select.append(option);
    });
    const requested = profile.model || "auto";
    const selected = requested === "auto"
      || rows.some((model) => model.id === requested && model.available)
      ? requested
      : "auto";
    select.value = selected;
    select.disabled = !isAuthenticated(profile);
  }

  function renderCurrentResponse() {
    const response = currentResponse();
    const body = $("#current-response");
    const meta = $("#response-meta");
    body.replaceChildren();
    meta.replaceChildren();

    const text = response?.text ?? response?.content ?? response?.output ?? "";
    body.classList.toggle("is-streaming", Boolean(response));
    if (!response) {
      // Idle is the normal state — a turn lasts seconds. Rather than hold a
      // third of the overview to say nothing is happening, report the turn
      // that just finished.
      const last = ui.state?.activity?.last;
      const title = $("#current-response-title");
      if (last) {
        if (title) title.textContent = "Последний ответ";
        body.append(
          node("div", { className: "last-turn" }, [
            node("p", { className: "last-turn-headline", text: [
              formatTime(last.finished_at || last.started_at, true),
              last.model || "Автоматически",
            ].filter(Boolean).join(" · ") }),
            node("dl", { className: "last-turn-facts" }, [
              node("dt", { text: "Статус" }),
              node("dd", {}, [requestStatusBadge(last.status)]),
              node("dt", { text: "Длительность" }),
              node("dd", { text: formatDuration(last.duration_seconds) }),
              node("dt", { text: "Размер ответа" }),
              node("dd", {
                text: last.text_chars
                  ? `${formatNumber(last.text_chars)} символов`
                  : "—",
              }),
              node("dt", { text: "Вызовов инструментов" }),
              node("dd", { text: formatNumber(last.tool_call_count || 0) }),
            ]),
            node("span", {
              className: "last-turn-note",
              text: "Поток ответа появится здесь во время генерации.",
            }),
          ]),
        );
      } else {
        if (title) title.textContent = "Текущий ответ";
        body.append(
          node("div", { className: "empty-state" }, [
            node("p", { text: "Запросов ещё не было" }),
            node("span", {
              text: "Здесь появится поток ответа, как только клиент обратится к мосту.",
            }),
          ]),
        );
      }
      meta.hidden = true;
      return;
    }
    const streamingTitle = $("#current-response-title");
    if (streamingTitle) streamingTitle.textContent = "Текущий ответ";

    if (text) {
      body.append(node("pre", { text }));
    } else {
      const isStreaming = ["streaming", "running"].includes(response.status);
      body.append(
        node("div", { className: "empty-state" }, [
          node("p", { text: isStreaming ? "Claude генерирует ответ" : "Последняя генерация" }),
          node("span", {
            text: [
              response.model,
              response.profile_id ? `профиль ${response.profile_id}` : null,
            ].filter(Boolean).join(" · ") || "Живая телеметрия без сохранения текста ответа",
          }),
        ]),
      );
    }
    const values = [];
    const elapsed = response.elapsed_seconds ?? response.elapsed;
    const exactTokens =
      response.actual_usage?.completion_tokens
      ?? response.actual_usage?.output_tokens;
    const estimatedTokens = response.estimated_output_tokens;
    const rate = response.tokens_per_second ?? response.token_rate;
    if (typeof elapsed === "number") {
      values.push(responseMetaItem("clock", `${elapsed.toFixed(1)} с`));
    }
    if (typeof exactTokens === "number") {
      values.push(responseMetaItem("diagnostics", `${formatNumber(exactTokens)} токенов`));
    } else if (typeof estimatedTokens === "number") {
      values.push(responseMetaItem("diagnostics", `≈ ${formatNumber(estimatedTokens)} токенов`));
    }
    if (typeof rate === "number") {
      values.push(responseMetaItem("behavior", `${rate.toFixed(1)} ток/с`));
    }
    if (response.status) {
      values.push(node("span", { text: String(response.status) }));
    }
    values.forEach((item) => meta.append(item));
    meta.hidden = values.length === 0;
  }

  function responseMetaItem(iconName, text) {
    return node("span", {}, [icon(iconName), node("span", { text })]);
  }

  function renderSystemList() {
    const systems = normalizeSystems();
    const list = $("#overview-system-list");
    const summary = $("#system-summary");
    list.replaceChildren();

    if (!systems.length) {
      list.append(
        node("div", { className: "empty-state" }, [
          node("p", { text: "Нет данных о компонентах" }),
          node("span", { text: "Backend пока не передал systems в снимке состояния." }),
        ]),
      );
      summary.className = "system-summary";
      summary.textContent = "Состояние компонентов неизвестно";
      return;
    }

    systems.slice(0, 5).forEach((system) => {
      list.append(
        node("div", { className: "system-row" }, [
          icon(systemIcon(system.id)),
          node("div", { className: "system-copy" }, [
            node("strong", { text: system.name }),
            node("span", { text: system.detail || "Без дополнительной информации" }),
          ]),
          node("span", {
            className: `health-badge health-badge--${system.kind}`,
            text: healthLabel(system.kind, system.status),
          }),
        ]),
      );
    });

    let summaryKind = "healthy";
    let summaryText = "Все компоненты работают нормально";
    if (systems.some((system) => system.kind === "error")) {
      summaryKind = "error";
      summaryText = "Один или несколько компонентов недоступны";
    } else if (systems.some((system) => system.kind === "warning")) {
      summaryKind = "warning";
      summaryText = "Некоторые компоненты требуют внимания";
    } else if (systems.some((system) => system.kind === "unknown")) {
      summaryKind = "";
      summaryText = "Не для всех компонентов есть данные";
    }
    summary.className = `system-summary ${summaryKind ? `is-${summaryKind}` : ""}`;
    summary.replaceChildren(icon(summaryKind === "error" ? "alert" : "check"), summaryText);
  }

  function renderEventTable(bodySelector, emptySelector, events) {
    const body = $(bodySelector);
    const empty = $(emptySelector);
    body.replaceChildren();
    empty.hidden = events.length > 0;
    if (!events.length) return;
    events.forEach((event) => {
      let level = event.level;
      if (level === "warning") level = "warn";
      if (!["info", "warn", "error"].includes(level)) level = "info";
      body.append(
        node("tr", {}, [
          node("td", { text: formatTime(event.time) }),
          node("td", {}, [
            node("span", {
              className: `event-level event-level--${level}`,
              text: level,
            }),
          ]),
          node("td", { text: event.component }),
          node("td", { text: event.message }),
        ]),
      );
    });
  }

  const connectionLabels = {
    bridge: { text: "Настроен на мост", kind: "healthy" },
    bridge_openai: { text: "Мост через OpenAI-путь", kind: "warning" },
    elsewhere: { text: "Смотрит в другое место", kind: "warning" },
    unset: { text: "Не настроен", kind: "muted" },
  };

  async function refreshClients({ quiet = false } = {}) {
    try {
      ui.clients = unwrap(await request("/clients"));
      renderClients();
    } catch (error) {
      if (!quiet) toast(error.message, "error");
    }
  }

  function commandBlock(title, command, note) {
    return node("article", { className: "command-card" }, [
      node("header", {}, [
        node("strong", { text: title }),
        node("button", {
          className: "button button--quiet",
          attrs: {
            type: "button",
            "data-action": "copy-command",
            "data-command": command,
          },
        }, [node("span", { text: "Скопировать" })]),
      ]),
      node("pre", { text: command }),
      note ? node("small", { text: note }) : null,
    ].filter(Boolean));
  }

  function renderClients() {
    const data = ui.clients;
    const grid = $("#client-grid");
    const commands = $("#connect-commands");
    if (!grid || !commands) return;
    const port = ui.state?.server?.port || 8765;
    const baseUrl = data?.bridge_url || `http://127.0.0.1:${port}`;
    $("#connect-base-url").textContent = baseUrl;
    $("#connect-model").textContent = data?.model || "claude-web";
    const ready = Boolean(ui.state?.health?.ok);
    $("#connect-bridge-state").replaceChildren(
      node("span", { className: "status-inline" }, [
        node("span", {
          className: `status-dot status-dot--${ready ? "healthy" : "warning"}`,
        }),
        node("span", {
          text: ready
            ? "Готов принимать запросы"
            : "Браузерная сессия не готова",
        }),
      ]),
    );

    grid.replaceChildren();
    (data?.clients || []).forEach((client) => {
      const label = connectionLabels[client.connection] || connectionLabels.unset;
      const actions = node("div", { className: "client-actions" });
      if (client.installed) {
        actions.append(
          node("button", {
            className: "button button--outline",
            text: client.connection === "bridge"
              ? "Настроить заново"
              : "Настроить на мост",
            attrs: {
              type: "button",
              "data-action": "configure-client",
              "data-client-id": client.id,
            },
          }),
        );
      } else if (client.can_install) {
        actions.append(
          node("button", {
            className: "button button--primary",
            text: "Установить",
            attrs: {
              type: "button",
              "data-action": "install-client",
              "data-client-id": client.id,
            },
          }),
        );
      }

      const facts = node("dl", { className: "client-facts" });
      const addFact = (term, value) => {
        facts.append(node("dt", { text: term }));
        facts.append(node("dd", { text: value }));
      };
      addFact(
        "Версия",
        client.version || (client.installed ? "не определилась" : "не установлен"),
      );
      addFact("Смотрит на", client.base_url || "—");
      addFact("Модель", client.model || "—");
      if (client.config_path) addFact("Настройки", client.config_path);

      const parts = [
        node("header", { className: "client-card-header" }, [
          node("div", {}, [
            node("h2", { text: client.name }),
            node("span", { className: "status-inline" }, [
              node("span", { className: `status-dot status-dot--${label.kind}` }),
              node("span", { text: label.text }),
            ]),
          ]),
          actions,
        ]),
        facts,
      ];
      if (client.version_error) {
        parts.push(
          node("p", {
            className: "client-note client-note--warning",
            text: `Версию прочитать не удалось: ${client.version_error}`,
          }),
        );
      }
      if (!client.installed && client.install_note) {
        parts.push(
          node("p", {
            className: "client-note client-note--warning",
            text: client.install_note,
          }),
        );
      }
      (client.notes || []).forEach((note) => {
        parts.push(node("p", { className: "client-note", text: note }));
      });
      parts.push(
        node("p", {
          className: "client-install-state",
          attrs: { "data-install-state": client.id },
          text: "",
        }),
      );

      grid.append(node("article", { className: "panel client-card" }, parts));
    });

    commands.replaceChildren(
      commandBlock(
        "PowerShell",
        [
          `$env:ANTHROPIC_BASE_URL = "${baseUrl}"`,
          `$env:ANTHROPIC_AUTH_TOKEN = "local-claude-web"`,
          `claude --model claude-web`,
        ].join("\n"),
        "Для OpenClaude замените последнюю строку на openclaude --model claude-web.",
      ),
      commandBlock(
        "bash / zsh",
        [
          `export ANTHROPIC_BASE_URL="${baseUrl}"`,
          `export ANTHROPIC_AUTH_TOKEN="local-claude-web"`,
          `claude --model claude-web`,
        ].join("\n"),
        "",
      ),
    );
  }

  async function configureClient(clientId, button) {
    button.disabled = true;
    try {
      const result = unwrap(
        await request(`/clients/${clientId}/configure`, { method: "POST" }),
      );
      const backups = (result.backups || []).length;
      toast(
        backups
          ? `Клиент настроен на мост; прежние настройки сохранены (${backups})`
          : "Клиент настроен на мост",
        "success",
      );
      await refreshClients({ quiet: true });
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function pollInstall(clientId, note) {
    for (let attempt = 0; attempt < 180; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      let state;
      try {
        state = unwrap(await request(`/clients/${clientId}/install`));
      } catch {
        continue;
      }
      if (state.status !== "running") {
        if (note) {
          note.textContent = state.status === "completed"
            ? "Установлено"
            : `Ошибка установки: ${(state.output || "").slice(-300)}`;
        }
        return state;
      }
    }
    if (note) note.textContent = "Установка идёт дольше обычного";
    return null;
  }

  async function installClient(clientId, button) {
    button.disabled = true;
    const note = $(`[data-install-state="${clientId}"]`);
    if (note) note.textContent = "Устанавливаем…";
    try {
      unwrap(await request(`/clients/${clientId}/install`, { method: "POST" }));
      const finished = await pollInstall(clientId, note);
      if (finished?.status === "completed") {
        toast("Клиент установлен", "success");
        await refreshClients({ quiet: true });
      } else {
        toast("Установка не удалась, подробности в карточке", "error");
      }
    } catch (error) {
      toast(error.message, "error");
      if (note) note.textContent = "";
    } finally {
      button.disabled = false;
    }
  }

  function renderProfiles() {
    const body = $("#profiles-table-body");
    const empty = $("#profiles-empty");
    const rows = profiles();
    const active = activeProfile();
    body.replaceChildren();
    empty.hidden = rows.length > 0;

    rows.forEach((profile) => {
      const account = profileAccount(profile);
      const authenticated = isAuthenticated(profile);
      const isActive = active?.id === profile.id;
      const providerReady = providerRuntimeReady(profile);
      const providerBlocked = [
        "provider_blocked",
        "access_denied",
      ].includes(String(profile.status || ""));
      const statusKind = providerBlocked
        ? "error"
        : authenticated && !providerReady
          ? "warning"
          : authenticated
            ? "healthy"
            : "muted";
      const statusText = providerBlocked
        ? "Заблокирован провайдером"
        : authenticated && !providerReady
          ? "Вход сохранён · API ещё не готов"
          : authenticated
            ? "Подключён"
            : "Не авторизован";
      const authStatus = node("span", { className: "status-inline" }, [
        node("span", {
          className: `status-dot status-dot--${statusKind}`,
        }),
        node("span", { text: statusText }),
      ]);

      const profileName = node("div", { className: "profile-name-cell" }, [
        node("span", { text: profile.name || profile.id }),
      ]);
      if (isActive) {
        profileName.append(node("span", { className: "active-mark", text: "активен" }));
      }

      const actions = node("div", { className: "profile-actions" });
      if (providerBlocked) {
        actions.append(
          node("button", {
            className: "button button--outline",
            text: "Доступ заблокирован",
            attrs: {
              type: "button",
              disabled: "disabled",
              title: "xAI отклонил автоматизированный браузер. Ручной Chrome на этом ПК работает.",
            },
          }),
        );
      } else if (!authenticated) {
        actions.append(
          actionButton(
            profileProvider(profile).id === "grok_web"
              ? "Проверить доступ"
              : "Войти",
            "open-profile-login",
            profile.id,
            "outline",
          ),
        );
      } else if (!isActive && providerReady) {
        actions.append(
          actionButton("Активировать", "activate-profile", profile.id, "outline"),
        );
        actions.append(
          actionButton("Войти заново", "open-profile-login", profile.id),
        );
      } else if (isActive) {
        // The active profile used to show nothing here, which left no way to
        // re-authenticate a session that had expired.
        actions.append(
          actionButton("Войти заново", "open-profile-login", profile.id),
        );
        actions.append(
          actionButton("Проверить", "check-active-login", profile.id),
        );
      } else if (!isActive) {
        actions.append(
          node("button", {
            className: "button button--outline",
            text: "API ещё не готов",
            attrs: {
              type: "button",
              disabled: "disabled",
              title: "Вход сохранён, но протокол этого web-провайдера ещё не проверен.",
            },
          }),
        );
      }

      actions.append(
        actionButton("Прокси", "open-proxy-dialog", profile.id, "quiet"),
      );

      const accountText = account.name
        ? account.email
          ? `${account.name} (${account.email})`
          : account.name
        : "Не авторизован";

      body.append(
        node("tr", {}, [
          node("td", {}, [profileName]),
          node("td", {}, [
            node("span", {
              className: "provider-badge",
              text: profileProvider(profile).label,
            }),
          ]),
          node("td", { text: accountText }),
          node("td", { text: profile.model && profile.model !== "auto" ? profile.model : "Автоматически" }),
          node("td", {}, [proxyCell(profile)]),
          node("td", {}, [authStatus]),
          node("td", { text: formatTime(profile.last_checked_at, true) }),
          node("td", {}, [actions]),
        ]),
      );
    });
  }

  function profileProxy(profile) {
    const row = profile?.proxy;
    return row && typeof row === "object" ? row : {};
  }

  function proxyCell(profile) {
    const proxy = profileProxy(profile);
    if (!proxy.enabled || !proxy.server) {
      return node("span", { className: "proxy-cell proxy-cell--direct", text: "Напрямую" });
    }
    return node("span", { className: "status-inline proxy-cell" }, [
      node("span", { className: "status-dot status-dot--healthy" }),
      node("span", { text: proxy.server }),
    ]);
  }

  function openProxyDialog(profileId) {
    const profile = profiles().find((row) => row.id === profileId);
    if (!profile) return;
    const proxy = profileProxy(profile);
    ui.proxyProfileId = profileId;
    $("#proxy-dialog-title").textContent = `Прокси · ${profile.name || profile.id}`;
    $("#proxy-enabled").checked = Boolean(proxy.enabled);
    $("#proxy-server").value = proxy.server || "";
    $("#proxy-username").value = proxy.username || "";
    $("#proxy-password").value = "";
    $("#proxy-password-hint").textContent = proxy.password_set
      ? "Пароль сохранён. Пусто — останется прежним."
      : "Пароль не сохранён.";
    setProxyResult(null);
    const dialog = $("#proxy-dialog");
    if (!dialog.open) dialog.showModal();
  }

  function closeProxyDialog() {
    ui.proxyProfileId = null;
    const dialog = $("#proxy-dialog");
    if (dialog.open) dialog.close();
  }

  function setProxyResult(text, kind = "muted") {
    const element = $("#proxy-result");
    element.hidden = !text;
    element.textContent = text || "";
    element.className = `proxy-result proxy-result--${kind}`;
  }

  function proxyFormPayload() {
    const password = $("#proxy-password").value;
    const payload = {
      enabled: $("#proxy-enabled").checked,
      server: $("#proxy-server").value.trim(),
      username: $("#proxy-username").value.trim(),
    };
    // An empty box means "keep the stored password", so it is omitted rather
    // than sent as an empty string, which would clear it.
    if (password) payload.password = password;
    return payload;
  }

  async function testProxy(button) {
    const profileId = ui.proxyProfileId;
    if (!profileId) return;
    button.disabled = true;
    setProxyResult("Проверяем соединение…");
    try {
      const payload = unwrap(
        await request(`/profiles/${profileId}/proxy/test`, {
          method: "POST",
          body: JSON.stringify(proxyFormPayload()),
        }),
      );
      const result = payload.result || {};
      if (result.ok) {
        setProxyResult(
          `Выход через ${result.exit_ip} · ${result.latency_ms} мс`,
          "healthy",
        );
      } else {
        setProxyResult(result.error || "Прокси не ответил", "error");
      }
    } catch (error) {
      setProxyResult(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function saveProxy(button) {
    const profileId = ui.proxyProfileId;
    if (!profileId) return;
    button.disabled = true;
    try {
      const payload = unwrap(
        await request(`/profiles/${profileId}/proxy`, {
          method: "PUT",
          body: JSON.stringify(proxyFormPayload()),
        }),
      );
      toast(
        payload.restarted
          ? "Прокси сохранён, браузер профиля перезапущен"
          : "Прокси сохранён",
        "success",
      );
      closeProxyDialog();
      await refreshState({ quiet: true });
    } catch (error) {
      setProxyResult(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function actionButton(text, action, profileId, variant = "quiet") {
    return node(
      "button",
      {
        className: `button button--${variant}`,
        text,
        attrs: {
          type: "button",
          "data-action": action,
          "data-profile-id": profileId,
        },
      },
    );
  }

  function renderModels() {
    const rows = profiles();
    const current =
      rows.find((profile) => profile.id === ui.selectedModelProfileId) ||
      activeProfile() ||
      rows[0] ||
      null;
    ui.selectedModelProfileId = current?.id || null;

    const selector = $("#model-profile-selector");
    selector.replaceChildren();
    if (!rows.length) {
      selector.append(node("div", { className: "table-empty", text: "Профилей пока нет" }));
    } else {
      rows.forEach((profile) => {
        const account = profileAccount(profile);
        const button = node(
          "button",
          {
            className: `model-profile-option ${profile.id === current?.id ? "is-selected" : ""}`,
            attrs: {
              type: "button",
              "data-action": "select-model-profile",
              "data-profile-id": profile.id,
            },
          },
          [
            node("span", {}, [
              node("strong", { text: profile.name || profile.id }),
              node("small", { text: account.email || (isAuthenticated(profile) ? "Подключён" : "Не авторизован") }),
            ]),
            icon("chevron"),
          ],
        );
        selector.append(button);
      });
    }

    $("#model-catalog-title").textContent = current
      ? `Модели · ${current.name || current.id}`
      : "Доступные модели";
    $("#model-catalog-subtitle").textContent = current
      ? isAuthenticated(current)
        ? providerRuntimeReady(current)
          ? "Доступ проверен для текущего аккаунта; модели с требованием подписки выбрать нельзя."
          : "Вход сохранён, но web-протокол провайдера ещё не проверен и API остаётся выключен."
        : "Сначала войдите в аккаунт этого профиля."
      : "";

    const catalog = $("#model-catalog-list");
    catalog.replaceChildren();
    if (!current) {
      catalog.append(node("div", { className: "table-empty", text: "Добавьте профиль, чтобы проверить модели" }));
      return;
    }
    const models = modelRows(current);
    if (!models.length) {
      const text = isAuthenticated(current)
        ? providerRuntimeReady(current)
          ? "Backend ещё не обнаружил доступные модели. Запустите проверку входа."
          : "Модели появятся после проверки web-протокола; сейчас этот провайдер работает только в режиме входа."
        : "Модели станут доступны после авторизации.";
      catalog.append(node("div", { className: "table-empty", text }));
      if (!isAuthenticated(current)) {
        const provider = profileProvider(current);
        catalog.append(
          node("div", { className: "profile-actions" }, [
            actionButton(`Открыть ${provider.product} для входа`, "open-profile-login", current.id, "outline"),
          ]),
        );
      }
      return;
    }

    const requestedModel = current.model || "auto";
    const selectedModel = models.some(
      (model) => model.id === requestedModel && model.available,
    )
      ? requestedModel
      : "auto";
    models.forEach((model) => {
      const selected = selectedModel === model.id;
      const accessStatus = model.available
        ? model.accessStatus
        : "unavailable";
      const availabilityText = modelAvailabilityText(model);
      const availabilityClass = model.available
        ? "availability availability--ok"
        : model.disabledReason?.type === "upgrade_required"
          ? "availability availability--locked"
          : "availability availability--unknown";
      const selectButton = node(
        "button",
        {
          className: selected ? "button button--primary" : "button button--outline",
          text: selected ? "Выбрана" : "Выбрать",
          attrs: {
            type: "button",
            "data-action": "set-profile-model",
            "data-profile-id": current.id,
            "data-model-id": model.id,
            disabled: model.available ? null : "disabled",
          },
        },
      );
      catalog.append(
        node("div", { className: "model-list-row" }, [
          node("div", {}, [
            node("strong", { text: model.label }),
            node("small", { text: model.id }),
          ]),
          node("span", { className: availabilityClass }, [
            icon(accessStatus === "available" ? "check" : "alert"),
            node("span", { text: availabilityText }),
          ]),
          selectButton,
        ]),
      );
    });
  }

  function renderBehavior() {
    const b = behavior();
    $("#behavior-streaming").checked = b.streaming;
    $("#behavior-thinking").value = b.thinking;
    $$('input[name="privacy"]').forEach((input) => {
      input.checked = input.value === b.privacy;
    });
    $$('input[name="persona"]').forEach((input) => {
      input.checked = input.value === b.persona;
    });
    $("#behavior-actor").checked = b.actor;
    $("#behavior-mature").checked = b.mature;
    $("#custom-persona-wrap").hidden = b.persona !== "custom";
    const textarea = $("#custom-persona");
    if (!ui.customPersonaDirty) textarea.value = b.custom_persona;
    updateCustomPersonaCount();
    renderPersonaCompilation();
  }

  function renderPersonaCompilation() {
    const details = $("#persona-compilation");
    const status = $("#persona-compilation-status");
    const preview = $("#persona-effective-preview");
    const changesList = $("#persona-compilation-changes");
    const compilation = personaCompilation();
    const currentText = $("#custom-persona").value.trim();

    details.hidden = !currentText && !compilation.raw;
    if (details.hidden) return;

    if (ui.customPersonaDirty) {
      status.textContent = "Есть несохранённые изменения. Предпросмотр обновится после сохранения.";
    } else if (compilation.changed) {
      status.textContent =
        `OpenClaude переформулировал ${compilation.changes.length} конфликтных ` +
        "фрагментов, не меняя сохранённый исходник.";
    } else {
      status.textContent = "Карточка отправляется без преобразований.";
    }
    preview.textContent =
      compilation.effective || "После сохранения здесь появится фактическая карточка.";
    changesList.replaceChildren();
    for (const change of compilation.changes) {
      changesList.append(
        node("li", {
          text: `«${String(change.source || "")}» → ${String(change.effective || "")}`,
        }),
      );
    }
    changesList.hidden = ui.customPersonaDirty || !compilation.changes.length;
  }

  function activityFilters() {
    return {
      period: $("#activity-period")?.value || "7d",
      providerId: $("#activity-provider-filter")?.value || "",
      profileId: $("#activity-profile-filter")?.value || "",
      model: $("#activity-model-filter")?.value || "",
      status: $("#activity-status-filter")?.value || "all",
      level: $("#activity-level-filter")?.value || "all",
      search: $("#activity-search")?.value.trim() || "",
    };
  }

  function activityQuery(offset = 0) {
    const filters = activityFilters();
    const params = new URLSearchParams({
      period: filters.period,
      status: filters.status,
      level: filters.level,
      limit: "100",
      offset: String(offset),
    });
    if (filters.providerId) params.set("provider_id", filters.providerId);
    if (filters.profileId) params.set("profile_id", filters.profileId);
    if (filters.model) params.set("model", filters.model);
    if (filters.search) params.set("q", filters.search);
    return params;
  }

  function activityFilterSignature() {
    return JSON.stringify(activityFilters());
  }

  function activityPage(value) {
    if (Array.isArray(value)) {
      return {
        items: value,
        total: value.length,
        offset: 0,
        has_more: false,
      };
    }
    if (value && typeof value === "object") {
      return {
        ...value,
        items: Array.isArray(value.items) ? value.items : [],
        total: Number.isFinite(value.total)
          ? value.total
          : (Array.isArray(value.items) ? value.items.length : 0),
      };
    }
    return { items: [], total: 0, offset: 0, has_more: false };
  }

  function mergeActivityRows(existing, incoming, key) {
    const seen = new Set();
    return [...existing, ...incoming].filter((row) => {
      const identity = String(row?.[key] ?? "");
      if (!identity || seen.has(identity)) return false;
      seen.add(identity);
      return true;
    });
  }

  async function refreshActivity({
    quiet = false,
    append = null,
  } = {}) {
    let appendTarget = append === true ? "history" : append;
    if (!["history", "logs"].includes(appendTarget)) appendTarget = null;
    const signature = activityFilterSignature();
    const existingActivity =
      appendTarget && ui.activityFilterSignature === signature
        ? ui.activity
        : null;
    if (!existingActivity) appendTarget = null;
    const existingRequests = activityPage(existingActivity?.requests);
    const existingEvents = activityPage(existingActivity?.events);
    const offset = appendTarget === "history"
      ? existingRequests.items.length
      : appendTarget === "logs"
        ? existingEvents.items.length
        : 0;
    const requestSerial = ++ui.activityRequestSerial;
    if (!quiet && !appendTarget) $("#activity-loading").hidden = false;
    $("#activity-error").hidden = true;
    const promise = (async () => {
      try {
        let payload = unwrap(
          await request(`/telemetry?${activityQuery(offset).toString()}`),
        );
        if (
          requestSerial !== ui.activityRequestSerial
          || signature !== activityFilterSignature()
        ) {
          return payload;
        }
        if (appendTarget) {
          const responsePage = activityPage(
            appendTarget === "history"
              ? payload?.requests
              : payload?.events,
          );
          const existingPage = appendTarget === "history"
            ? existingRequests
            : existingEvents;
          if (responsePage.total < existingPage.items.length) {
            payload = unwrap(
              await request(`/telemetry?${activityQuery(0).toString()}`),
            );
            appendTarget = null;
          }
        }
        if (
          requestSerial !== ui.activityRequestSerial
          || signature !== activityFilterSignature()
        ) {
          return payload;
        }
        if (appendTarget === "history") {
          const page = activityPage(payload.requests);
          payload.requests = {
            ...page,
            items: mergeActivityRows(
              existingRequests.items,
              page.items,
              "request_id",
            ),
          };
          payload.events = existingActivity.events;
        } else if (appendTarget === "logs") {
          const page = activityPage(payload.events);
          payload.events = {
            ...page,
            items: mergeActivityRows(
              existingEvents.items,
              page.items,
              "id",
            ),
          };
          payload.requests = existingActivity.requests;
        }
        ui.activity = payload;
        ui.activityFilterSignature = signature;
        renderActivity();
        return payload;
      } catch (error) {
        if (requestSerial !== ui.activityRequestSerial) return null;
        const target = $("#activity-error");
        target.textContent = error.message;
        target.hidden = false;
        if (!quiet) toast(error.message, "error");
        throw error;
      } finally {
        if (requestSerial === ui.activityRequestSerial) {
          $("#activity-loading").hidden = true;
          ui.activityPromise = null;
        }
      }
    })();
    ui.activityPromise = promise;
    return promise;
  }

  function syncActivityFilterOptions() {
    const providerSelect = $("#activity-provider-filter");
    const providerValue = providerSelect.value;
    const knownProviders = new Set(
      profiles().map((profile) => profileProvider(profile).id),
    );
    Object.keys(ui.state?.providers || {}).forEach((id) => {
      knownProviders.add(id);
    });
    (ui.activity?.summary?.providers || []).forEach((row) => {
      if (row?.provider_id) knownProviders.add(String(row.provider_id));
    });
    providerSelect.replaceChildren(
      node("option", { text: "Все провайдеры", attrs: { value: "" } }),
    );
    [...knownProviders]
      .sort((left, right) => {
        const leftLabel = labels.provider[left] || left;
        const rightLabel = labels.provider[right] || right;
        return leftLabel.localeCompare(rightLabel, "ru");
      })
      .forEach((id) => {
        providerSelect.append(
          node("option", {
            text: labels.provider[id] || id,
            attrs: { value: id },
          }),
        );
      });
    providerSelect.value = [...providerSelect.options].some(
      (option) => option.value === providerValue,
    )
      ? providerValue
      : "";

    const profileSelect = $("#activity-profile-filter");
    const profileValue = profileSelect.value;
    profileSelect.replaceChildren(
      node("option", { text: "Все профили", attrs: { value: "" } }),
    );
    profiles().forEach((profile) => {
      profileSelect.append(
        node("option", {
          text: profile.name || profile.id,
          attrs: { value: profile.id },
        }),
      );
    });
    profileSelect.value = [...profileSelect.options].some(
      (option) => option.value === profileValue,
    )
      ? profileValue
      : "";

    const modelSelect = $("#activity-model-filter");
    const modelValue = modelSelect.value;
    const knownModels = new Map();
    profiles().forEach((profile) => {
      modelRows(profile).forEach((model) => {
        knownModels.set(model.id, model.label || model.id);
      });
    });
    (ui.activity?.summary?.models || []).forEach((row) => {
      if (row?.model) knownModels.set(String(row.model), String(row.model));
    });
    modelSelect.replaceChildren(
      node("option", { text: "Все модели", attrs: { value: "" } }),
    );
    [...knownModels.entries()]
      .sort((left, right) => left[1].localeCompare(right[1], "ru"))
      .forEach(([id, label]) => {
        modelSelect.append(
          node("option", { text: label, attrs: { value: id } }),
        );
      });
    modelSelect.value = [...modelSelect.options].some(
      (option) => option.value === modelValue,
    )
      ? modelValue
      : "";
  }

  function renderActivity() {
    syncActivityFilterOptions();
    $$(".activity-tab").forEach((tab) => {
      const active = tab.dataset.tab === ui.activityTab;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    $$("[data-activity-panel]").forEach((panel) => {
      const active = panel.dataset.activityPanel === ui.activityTab;
      panel.classList.toggle("is-active", active);
      panel.hidden = !active;
    });
    // One search box serves all three tabs, so it has to say what it searches
    // in the tab you are on: logs carry no model name.
    const search = $("#activity-search");
    if (search) {
      search.placeholder = ui.activityTab === "logs"
        ? "Компонент, сообщение или уровень"
        : "Модель, текст или ошибка";
    }
    $('[data-activity-filter="status"]').hidden = ui.activityTab !== "history";
    $('[data-activity-filter="level"]').hidden = ui.activityTab !== "logs";
    $('[data-activity-filter="provider"]').hidden = ui.activityTab === "logs";
    $('[data-activity-filter="profile"]').hidden = ui.activityTab === "logs";
    $('[data-activity-filter="model"]').hidden = ui.activityTab === "logs";
    $('[data-activity-filter="search"]').hidden = false;

    const settings =
      ui.activity?.settings ||
      ui.state?.config?.telemetry ||
      { store_content: false, retention_days: 30 };
    const contentToggle = $("#activity-store-content");
    const suppressed = Boolean(settings.ephemeral_suppresses_content);
    // Ephemeral privacy overrides the setting, so showing the switch as "on"
    // while nothing is stored is the reason the journal looks broken.
    contentToggle.checked = Boolean(settings.store_content) && !suppressed;
    contentToggle.disabled = suppressed;
    contentToggle.closest("label")?.classList.toggle("is-overridden", suppressed);
    contentToggle.title = suppressed
      ? "Эфемерный режим приватности отключает сохранение текстов. Смените приватность на «Обычный» во вкладке «Поведение»."
      : "";
    const retention = $("#activity-retention");
    const retentionValue = String(settings.retention_days || 30);
    if (![...retention.options].some((option) => option.value === retentionValue)) {
      retention.append(
        node("option", {
          text: `${retentionValue} дней`,
          attrs: { value: retentionValue },
        }),
      );
    }
    retention.value = retentionValue;
    const policyNote = $("#activity-policy-note");
    if (settings.ephemeral_suppresses_content) {
      policyNote.textContent =
        "Включён эфемерный режим: метрики сохраняются, но тексты новых запросов в эту панель не дублируются.";
    } else if (settings.store_content) {
      policyNote.textContent =
        "Тексты новых запросов и финальных ответов сохраняются только в локальной SQLite-базе.";
    } else {
      policyNote.textContent =
        "Сохраняются метаданные, статусы и usage. Тексты запросов и ответов отключены.";
    }

    renderActivityMetrics();
    renderActivityHistory();
    renderActivityLogs();
  }

  function formatPercent(value) {
    return typeof value === "number"
      ? `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value * 100)}%`
      : "—";
  }

  function formatDuration(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (value < 1) return `${Math.round(value * 1000)} мс`;
    if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)} с`;
    return `${Math.floor(value / 60)} мин ${Math.round(value % 60)} с`;
  }

  function renderActivityMetrics() {
    const summary = ui.activity?.summary;
    if (!summary) {
      ["#metric-requests", "#metric-success", "#metric-tokens", "#metric-latency"].forEach(
        (selector) => {
          $(selector).textContent = "—";
        },
      );
      $("#metric-conversations").textContent = "Ожидаем данные";
      $("#metric-errors").textContent = "";
      $("#metric-token-coverage").textContent = "";
      $("#metric-p95").textContent = "";
      renderActivitySeries([]);
      renderModelMetrics([]);
      return;
    }
    $("#metric-requests").textContent = formatNumber(summary.requests || 0);
    $("#metric-conversations").textContent =
      `${formatNumber(summary.conversations || 0)} сессий · ${formatNumber(summary.tool_calls || 0)} tool-вызовов`;
    $("#metric-success").textContent = formatPercent(summary.success_rate);
    $("#metric-errors").textContent =
      `${formatNumber(summary.errors || 0)} ошибок · ${formatNumber(summary.cancelled || 0)} отмен`;
    if ((summary.exact_usage_requests || 0) > 0) {
      $("#metric-tokens").textContent =
        `${formatNumber(summary.prompt_tokens || 0)} → ${formatNumber(summary.completion_tokens || 0)}`;
    } else {
      $("#metric-tokens").textContent = "—";
    }
    const estimateNote = summary.estimated_output_tokens
      ? ` · ещё ≈ ${formatNumber(summary.estimated_output_tokens)} output`
      : "";
    $("#metric-token-coverage").textContent =
      `${formatPercent(summary.usage_coverage)} запросов с exact usage${estimateNote}`;
    $("#metric-latency").textContent = formatDuration(
      summary.average_duration_seconds,
    );
    $("#metric-p95").textContent =
      `p95 ${formatDuration(summary.p95_duration_seconds)}`;
    renderActivitySeries(summary.series || []);
    renderModelMetrics(summary.models || []);
  }

  function renderActivitySeries(series) {
    const root = $("#activity-series");
    const empty = $("#activity-series-empty");
    root.replaceChildren();
    empty.hidden = series.length > 0;
    // Two bars stranded in a tall frame look like a rendering failure.
    root.classList.toggle("trend-chart--sparse", series.length > 0 && series.length < 4);
    if (!series.length) return;
    const maxRequests = Math.max(...series.map((point) => point.requests || 0), 1);
    const period = $("#activity-period").value;
    series.forEach((point, index) => {
      const requests = Number(point.requests || 0);
      const errors = Number(point.errors || 0);
      const column = node("div", {
        className: "trend-column",
        attrs: {
          title: `${formatTime(point.time, true)} · ${requests} запросов · ${errors} ошибок`,
          role: "img",
          tabindex: "0",
          "aria-label": `${formatTime(point.time, true)}: ${requests} запросов, ${errors} ошибок`,
        },
      });
      const bars = node("div", { className: "trend-bars" });
      const goodBar = node("span", { className: "trend-bar trend-bar--ok" });
      const errorBar = node("span", { className: "trend-bar trend-bar--error" });
      const successful = Math.max(0, requests - errors);
      goodBar.style.height = successful
        ? `${Math.max(4, (successful / maxRequests) * 100)}%`
        : "0";
      errorBar.style.height = errors
        ? `${Math.max(4, (errors / maxRequests) * 100)}%`
        : "0";
      bars.append(goodBar, errorBar);
      const showLabel =
        series.length <= 12 ||
        index === 0 ||
        index === series.length - 1 ||
        index % Math.ceil(series.length / 8) === 0;
      const date = new Date(Number(point.time) * 1000);
      const label = period === "1h" || period === "24h"
        ? new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(date)
        : new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit" }).format(date);
      column.append(
        bars,
        node("small", { text: showLabel ? label : "" }),
      );
      root.append(column);
    });
  }

  function renderModelMetrics(rows) {
    const body = $("#activity-model-metrics");
    const empty = $("#activity-model-metrics-empty");
    body.replaceChildren();
    empty.hidden = rows.length > 0;
    rows.forEach((row) => {
      body.append(
        node("tr", {}, [
          node("td", {
            text: `${labels.provider[row.provider_id] || row.provider_id || "—"} · ${row.model || "Автоматически"}`,
          }),
          node("td", { text: formatNumber(row.requests || 0) }),
          node("td", {
            text: row.total_tokens
              ? formatNumber(row.total_tokens)
              : "—",
          }),
        ]),
      );
    });
  }

  const activityStatusLabels = {
    running: "Выполняется",
    completed: "Завершён",
    error: "Ошибка",
    cancelled: "Отменён",
    interrupted: "Прерван",
  };

  function requestStatusBadge(status) {
    const normalized = String(status || "unknown");
    const kind = normalized === "completed"
      ? "healthy"
      : normalized === "running"
        ? "warning"
        : normalized === "error" || normalized === "interrupted"
          ? "error"
          : "unknown";
    return node("span", {
      className: `health-badge health-badge--${kind}`,
      text: activityStatusLabels[normalized] || normalized,
    });
  }

  function requestTokenLabel(item) {
    if (item?.usage) {
      return `${formatNumber(item.usage.prompt_tokens)} → ${formatNumber(item.usage.completion_tokens)}`;
    }
    if (typeof item?.estimated_output_tokens === "number") {
      return `≈ ${formatNumber(item.estimated_output_tokens)} out`;
    }
    return "—";
  }

  function renderActivityHistory() {
    const response = ui.activity?.requests || {};
    const rows = Array.isArray(response.items) ? response.items : [];
    const body = $("#history-table-body");
    const empty = $("#history-empty");
    body.replaceChildren();
    empty.hidden = rows.length > 0;
    $("#history-count").textContent = response.total
      ? `${formatNumber(rows.length)} из ${formatNumber(response.total)}`
      : "";
    rows.forEach((item) => {
      const titleButton = node("button", {
        className: "history-open",
        attrs: {
          type: "button",
          "data-action": "open-history-detail",
          "data-request-id": item.request_id,
        },
      }, [
        node("strong", { text: item.title || "Запрос" }),
        node("small", {
          text: `сессия …${item.session_suffix || "—"}${item.content_saved ? " · текст сохранён" : ""}`,
        }),
      ]);
      body.append(
        node("tr", {}, [
          node("td", {
            text: formatTime(item.started_at, true),
            attrs: { "data-label": "Время" },
          }),
          node("td", { attrs: { "data-label": "Запрос" } }, [titleButton]),
          node("td", {
            text: labels.provider[
              item.final_provider_id || item.provider_id
            ] || item.final_provider_id || item.provider_id || "—",
            attrs: { "data-label": "Провайдер" },
          }),
          node("td", {
            text: item.resolved_model || item.requested_model || "—",
            attrs: { "data-label": "Модель" },
          }),
          node("td", { attrs: { "data-label": "Статус" } }, [
            requestStatusBadge(item.status),
          ]),
          node("td", {
            text: requestTokenLabel(item),
            attrs: { "data-label": "Токены" },
          }),
          node("td", {
            text: formatDuration(item.duration_seconds),
            attrs: { "data-label": "Длительность" },
          }),
        ]),
      );
    });
    $("#history-load-more").hidden = !response.has_more;
  }

  function renderActivityLogs() {
    // Component health lives on the overview; repeating it above the log
    // stream only pushed the log itself further down the page.
    const response = activityPage(ui.activity?.events);
    const events = response.items.length
      ? response.items
      : (ui.activity ? [] : normalizeEvents());
    renderEventTable(
      "#diagnostic-events",
      "#diagnostic-events-empty",
      events,
    );
    $("#logs-count").textContent = response.total
      ? `${formatNumber(events.length)} из ${formatNumber(response.total)}`
      : "";
    $("#logs-load-more").hidden = !response.has_more;
  }

  function setActivityTab(tab) {
    if (!["metrics", "history", "logs"].includes(tab)) return;
    ui.activityTab = tab;
    renderActivity();
  }

  async function openHistoryDetail(requestId) {
    const requestSerial = ++ui.historyRequestSerial;
    const dialog = $("#history-dialog");
    $("#history-dialog-title").textContent = "Загружаем запрос…";
    $("#history-dialog-subtitle").textContent = "";
    $("#history-detail-meta").replaceChildren();
    $("#history-user-text").textContent = "Загрузка…";
    $("#history-assistant-text").textContent = "Загрузка…";
    $("#history-error-section").hidden = true;
    if (!dialog.open) dialog.showModal();
    try {
      const payload = unwrap(
        await request(`/telemetry/${encodeURIComponent(requestId)}`),
      );
      if (requestSerial !== ui.historyRequestSerial) return;
      const item = payload.request || payload;
      $("#history-dialog-title").textContent = item.title || "Запрос";
      $("#history-dialog-subtitle").textContent =
        `${formatTime(item.started_at, true)} · сессия …${item.session_suffix || "—"}`;
      const meta = $("#history-detail-meta");
      const providerId = item.final_provider_id || item.provider_id;
      meta.replaceChildren(
        requestStatusBadge(item.status),
        responseMetaItem("user", labels.provider[providerId] || providerId || "—"),
        responseMetaItem("model", item.resolved_model || item.requested_model || "Автоматически"),
        responseMetaItem("diagnostics", requestTokenLabel(item)),
        responseMetaItem("clock", formatDuration(item.duration_seconds)),
      );
      $("#history-user-text").textContent = item.user_text
        || (item.tool_call_count
          ? "Продолжение после результата инструмента. Содержимое инструмента намеренно не сохраняется."
          : "Текст не сохранялся для этого запроса.");
      $("#history-assistant-text").textContent = item.assistant_text
        || "Финальный текст ответа не сохранялся или отсутствовал.";
      $("#history-error-section").hidden = !item.error;
      $("#history-error-text").textContent = item.error || "";
    } catch (error) {
      if (requestSerial !== ui.historyRequestSerial) return;
      $("#history-dialog-title").textContent = "Не удалось открыть запрос";
      $("#history-user-text").textContent = error.message;
      $("#history-assistant-text").textContent = "";
    }
  }

  function closeHistoryDetail() {
    ui.historyRequestSerial += 1;
    const dialog = $("#history-dialog");
    if (dialog.open) dialog.close();
  }

  async function patchTelemetrySettings(updates) {
    const payload = unwrap(
      await request("/telemetry/settings", {
        method: "PATCH",
        body: updates,
      }),
    );
    if (!ui.state.config) ui.state.config = {};
    ui.state.config.telemetry = payload.settings || {
      ...(ui.state.config.telemetry || {}),
      ...updates,
    };
    toast("Настройки локальной истории сохранены", "success");
    await refreshActivity();
  }

  async function patchBehavior(key, value) {
    const updates =
      key && typeof key === "object" ? key : { [key]: value };
    const saveState = $("#behavior-save-state");
    saveState.className = "save-state is-saving";
    saveState.textContent = "Сохраняем…";
    try {
      const payload = unwrap(
        await request("/behavior", {
          method: "PATCH",
          body: updates,
        }),
      );
      if (!ui.state.config) ui.state.config = {};
      const current = ui.state.config.behavior || {};
      if (payload.behavior) ui.state.config.behavior = payload.behavior;
      else if (payload && typeof payload === "object") {
        ui.state.config.behavior = { ...current, ...payload };
      } else {
        ui.state.config.behavior = { ...current, ...updates };
      }
      if (payload.persona_compilation) {
        ui.state.persona_compilation = payload.persona_compilation;
      }
      renderOverview();
      renderBehavior();
      saveState.className = "save-state is-saved";
      saveState.textContent = "Сохранено";
      window.setTimeout(() => {
        if (saveState.textContent === "Сохранено") saveState.textContent = "";
      }, 1800);
      return payload;
    } catch (error) {
      saveState.className = "save-state is-error";
      saveState.textContent = "Не сохранено";
      toast(error.message, "error");
      await refreshState({ quiet: true }).catch(() => {});
      return null;
    }
  }

  async function createProfile(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = $('button[type="submit"]', form);
    const name = $("#profile-name").value.trim();
    const provider = $("#profile-provider").value;
    if (!name) return;
    setButtonLoading(submit, true, "Создаём профиль…");
    try {
      const payload = unwrap(
        await request("/profiles", {
          method: "POST",
          body: { name, provider },
        }),
      );
      const profile = payload.profile || payload;
      if (!profile?.id) throw new Error("Backend не вернул id созданного профиля");
      ui.loginProfileId = profile.id;
      ui.loginSnapshot = null;
      showLoginProgress(profile);
      await launchProfileLogin(profile.id);
      await refreshState({ quiet: true }).catch(() => {});
    } catch (error) {
      toast(error.message, "error");
      showLoginError(error.message);
    } finally {
      setButtonLoading(submit, false);
    }
  }

  function setButtonLoading(button, loading, loadingText = "Подождите…") {
    if (loading) {
      button.dataset.originalText = button.textContent.trim();
      button.disabled = true;
      const span = $("span", button);
      if (span) span.textContent = loadingText;
      else button.textContent = loadingText;
    } else {
      button.disabled = false;
      const original = button.dataset.originalText;
      if (original) {
        const span = $("span", button);
        if (span) span.textContent = original;
        else button.textContent = original;
      }
      delete button.dataset.originalText;
    }
  }

  function openProfileDrawer(profile = null) {
    const drawer = $("#profile-drawer");
    const backdrop = $("#drawer-backdrop");
    backdrop.hidden = false;
    drawer.setAttribute("aria-hidden", "false");
    requestAnimationFrame(() => {
      backdrop.classList.add("is-open");
      drawer.classList.add("is-open");
    });
    document.body.style.overflow = "hidden";
    if (profile) {
      applyLoginProvider(profile);
      ui.loginProfileId = profile.id;
      $("#drawer-title").textContent = `Войти в «${profile.name || profile.id}»`;
      $("#profile-form").hidden = true;
      $("#login-progress").hidden = false;
      applyLoginSnapshot({ status: "starting", browser_open: false, authenticated: false });
      launchProfileLogin(profile.id).catch((error) => showLoginError(error.message));
    } else {
      ui.loginProfileId = null;
      ui.loginSnapshot = null;
      $("#drawer-title").textContent = "Добавить профиль";
      $("#profile-form").hidden = false;
      $("#login-progress").hidden = true;
      $("#profile-form").reset();
      applyLoginProvider({ provider: $("#profile-provider").value });
      hideLoginError();
      window.setTimeout(() => $("#profile-name").focus(), 220);
    }
  }

  function closeProfileDrawer() {
    const drawer = $("#profile-drawer");
    const backdrop = $("#drawer-backdrop");
    drawer.classList.remove("is-open");
    backdrop.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    stopLoginPolling();
    ui.loginProfileId = null;
    window.setTimeout(() => {
      backdrop.hidden = true;
    }, 220);
  }

  function showLoginProgress(profile) {
    applyLoginProvider(profile);
    $("#drawer-title").textContent = `Войти в «${profile.name || profile.id}»`;
    $("#profile-form").hidden = true;
    $("#login-progress").hidden = false;
    hideLoginError();
    applyLoginSnapshot({ status: "starting", browser_open: false, authenticated: false });
  }

  function applyLoginProvider(profile) {
    const provider = profileProvider(profile);
    $("#login-provider-label").textContent = `Вход в ${provider.site}`;
    const browserHint = $("#profile-browser-hint");
    if (browserHint) {
      browserHint.textContent = provider.id === "grok_web"
        ? "Будет создан диагностический профиль Chrome. Ручной Chrome на этом ПК работает, но xAI блокирует автоматизированные браузеры, поэтому Grok API останется выключен."
        : "Для аккаунта будет создан отдельный локальный профиль Camoufox.";
    }
    $("#login-progress-title").textContent = provider.id === "grok_web"
      ? "Диагностика автоматизированного доступа…"
      : "Проверяем авторизацию…";
    $("#login-models-label").textContent = provider.id === "grok_web"
      ? "Состояние транспорта"
      : "Доступные модели";
    const submitLabel = $("#profile-submit-label");
    if (submitLabel) {
      submitLabel.textContent = provider.id === "grok_web"
        ? "Создать профиль и запустить диагностику"
        : `Создать и открыть ${provider.product} для входа`;
    }
  }

  async function launchProfileLogin(profileId) {
    ui.loginProfileId = profileId;
    stopLoginPolling();
    try {
      const snapshot = unwrap(
        await request(`/profiles/${encodeURIComponent(profileId)}/login`, {
          method: "POST",
        }),
      );
      if (
        ui.loginProfileId !== profileId
        || $("#profile-drawer").getAttribute("aria-hidden") === "true"
      ) {
        return;
      }
      applyLoginSnapshot(snapshot.login || snapshot);
      startLoginPolling();
    } catch (error) {
      if (
        ui.loginProfileId !== profileId
        || $("#profile-drawer").getAttribute("aria-hidden") === "true"
      ) {
        return;
      }
      applyLoginSnapshot({ status: "error", last_error: error.message });
      throw error;
    }
  }

  function startLoginPolling() {
    stopLoginPolling();
    ui.loginTimer = window.setInterval(() => {
      pollLogin().catch(() => {});
    }, LOGIN_POLL_INTERVAL_MS);
  }

  function stopLoginPolling() {
    if (ui.loginTimer) window.clearInterval(ui.loginTimer);
    ui.loginTimer = null;
    ui.loginPollInFlight = false;
  }

  async function pollLogin() {
    if (!ui.loginProfileId || ui.loginPollInFlight) return;
    const profileId = ui.loginProfileId;
    ui.loginPollInFlight = true;
    try {
      const payload = unwrap(
        await request(`/profiles/${encodeURIComponent(profileId)}/login`),
      );
      if (ui.loginProfileId !== profileId) return;
      const snapshot = payload.login || payload;
      applyLoginSnapshot(snapshot);
      if (snapshot.ready === true && snapshot.status === "ready") {
        stopLoginPolling();
        toast("Профиль подключён", "success");
        await refreshState({ quiet: true });
      } else if (
        [
          "browser_closed",
          "error",
          "duplicate",
          "account_changed",
          "project_setup_error",
          "protocol_unverified",
          "provider_blocked",
          "access_denied",
        ].includes(snapshot.status)
      ) {
        stopLoginPolling();
      }
    } catch (error) {
      showLoginError(error.message);
    } finally {
      ui.loginPollInFlight = false;
    }
  }

  function applyLoginSnapshot(snapshot) {
    ui.loginSnapshot = snapshot || {};
    const status = String(snapshot?.status || "checking");
    const browserOpen =
      typeof snapshot?.browser_open === "boolean"
        ? snapshot.browser_open
        : !["starting", "not_running", "browser_closed"].includes(status);
    const authenticated = Boolean(snapshot?.authenticated);
    const ready = snapshot?.ready === true && status === "ready";
    const providerBlocked = [
      "provider_blocked",
      "access_denied",
    ].includes(status);
    const profile = profiles().find(
      (row) => row.id === ui.loginProfileId,
    ) || snapshot;
    const provider = profileProvider(profile);
    const setupError =
      snapshot?.project_error
      || snapshot?.account_error
      || snapshot?.protocol_error
      || (providerBlocked
        ? snapshot?.last_error
          || `${provider.site} отклонил автоматизированный браузер. Ручной Chrome на этом ПК работает.`
        : null)
      || (status === "project_setup_error"
        ? snapshot?.last_error || "Не удалось настроить Claude Project для профиля."
        : null)
      || (status === "duplicate"
        ? (snapshot?.duplicate?.name
          ? `Этот аккаунт уже привязан к профилю «${snapshot.duplicate.name}». Один аккаунт — один профиль: войдите здесь другим аккаунтом или удалите лишний профиль.`
          : "Этот аккаунт уже привязан к другому профилю.")
        : null)
      || (status === "account_changed"
        ? "В браузере этого профиля теперь другой аккаунт. Войдите прежним или создайте для нового отдельный профиль."
        : null);
    const models = Array.isArray(snapshot?.models) ? snapshot.models : [];

    setStepState(
      $("#login-step-browser"),
      providerBlocked
        ? "error"
        : browserOpen
          ? "done"
          : status === "error"
            ? "error"
            : "active",
    );
    setStepState(
      $("#login-step-auth"),
      authenticated
        ? "done"
        : providerBlocked || status === "error" || status === "browser_closed"
          ? "error"
          : browserOpen
            ? "active"
            : "",
    );
    setStepState(
      $("#login-step-models"),
      ready
        ? "done"
        : setupError
          ? "error"
          : authenticated
            ? "active"
            : status === "error"
              ? "error"
              : "",
    );

    const browserDetail = $("small", $("#login-step-browser"));
    browserDetail.textContent = browserOpen
      ? snapshot?.started_at
        ? formatTime(snapshot.started_at, true)
        : snapshot?.browser_engine === "chrome" || provider.id === "grok_web"
          ? "Окно Chrome открыто"
          : "Окно Camoufox открыто"
      : providerBlocked
        ? `${provider.site} отклонил автоматизированный браузер`
        : "Ожидаем запуск";
    $("small", $("#login-step-auth")).textContent = authenticated
      ? profileIdentityText(snapshot?.account)
      : providerBlocked && provider.id === "grok_web"
        ? "Ручной Chrome доступен; управляемое окно отклонено xAI"
      : status === "browser_closed"
        ? "Окно браузера закрыто"
        : provider.id === "grok_web"
          ? "Проверяем, пропускает ли xAI это автоматизированное окно"
          : "Войдите в аккаунт в открытом окне";
    $("small", $("#login-step-models")).textContent = provider.id === "grok_web"
      ? providerBlocked
        ? "Не проверяется: браузерный доступ отклонён"
        : status === "protocol_unverified"
          ? "Вход виден, но completion-транспорт выключен"
          : "Модели не объявляются до проверенного транспорта"
      : authenticated
        ? models.length
          ? `Обнаружено: ${models.length}`
          : "Вход подтверждён, список пока пуст"
        : "Проверим после входа";

    if (setupError) showLoginError(setupError);
    else if (snapshot?.last_error) showLoginError(snapshot.last_error);
    else hideLoginError();
    if (ready) $("#drawer-title").textContent = "Профиль подключён";
    else if (authenticated) {
      $("#drawer-title").textContent = provider.id === "claude_web"
        ? "Настраиваем Claude Project…"
        : "Grok-транспорт не проверен";
    } else if (providerBlocked) {
      $("#drawer-title").textContent = `${provider.product} заблокировал автоматизированное окно`;
    }
  }

  function profileIdentityText(account) {
    if (!account || typeof account !== "object") return "Авторизация подтверждена";
    if (account.name && account.email) return `${account.name} · ${account.email}`;
    return account.name || account.email || "Авторизация подтверждена";
  }

  function setStepState(step, state) {
    step.classList.remove("is-active", "is-done", "is-error");
    if (state) step.classList.add(`is-${state}`);
  }

  function showLoginError(message) {
    const target = $("#login-error");
    target.textContent = message;
    target.hidden = false;
  }

  function hideLoginError() {
    $("#login-error").hidden = true;
  }

  async function activateProfile(profileId, button) {
    setButtonLoading(button, true, "Активируем…");
    try {
      await request(`/profiles/${encodeURIComponent(profileId)}/activate`, {
        method: "POST",
      });
      toast("Активный профиль изменён", "success");
      await refreshState();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function cancelProfileLogin(button) {
    const profileId = ui.loginProfileId;
    if (!profileId) {
      closeProfileDrawer();
      return;
    }
    setButtonLoading(button, true, "Закрываем браузер…");
    try {
      await request(`/profiles/${encodeURIComponent(profileId)}/login`, {
        method: "DELETE",
      });
      closeProfileDrawer();
      toast("Вход отменён, браузер закрыт", "success");
      await refreshState({ quiet: true });
    } catch (error) {
      toast(error.message, "error");
      showLoginError(error.message);
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function setProfileModel(profileId, modelId, control) {
    control.disabled = true;
    try {
      await request(`/profiles/${encodeURIComponent(profileId)}/model`, {
        method: "POST",
        body: { model: modelId },
      });
      toast("Модель профиля изменена", "success");
      await refreshState();
    } catch (error) {
      toast(error.message, "error");
      await refreshState({ quiet: true }).catch(() => {});
    } finally {
      control.disabled = false;
    }
  }

  function toast(message, kind = "success") {
    const root = $("#toast-region");
    const item = node("div", { className: `toast toast--${kind}` }, [
      icon(kind === "error" ? "alert" : "check"),
      node("span", { text: message }),
    ]);
    root.append(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function showGlobalError(error) {
    $("#global-error-text").textContent = error?.message || "Неизвестная ошибка";
    $("#global-error").hidden = false;
  }

  function hideGlobalError() {
    $("#global-error").hidden = true;
  }

  function updateCustomPersonaCount() {
    const length = $("#custom-persona").value.length;
    $("#custom-persona-count").textContent = `${length} / 8000`;
  }

  function handleAction(button) {
    const action = button.dataset.action;
    if (action === "open-profile-drawer") {
      openProfileDrawer();
    } else if (action === "close-profile-drawer") {
      closeProfileDrawer();
    } else if (action === "refresh") {
      button.disabled = true;
      refreshState()
        .catch(() => {})
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "refresh-activity") {
      button.disabled = true;
      Promise.all([
        refreshState({ quiet: true }),
        refreshActivity(),
      ])
        .catch(() => {})
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "clear-events") {
      if (!window.confirm("Очистить локальный журнал событий? История запросов останется.")) {
        return;
      }
      button.disabled = true;
      request("/events", { method: "DELETE" })
        .then(() => Promise.all([
          refreshState({ quiet: true }),
          refreshActivity({ quiet: true }),
        ]))
        .then(() => toast("Журнал очищен", "success"))
        .catch((error) => toast(error.message, "error"))
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "clear-telemetry") {
      if (!window.confirm(
        "Удалить локальные метрики, тексты запросов и журнал? Чаты claude.ai и история самого OpenClaude не изменятся.",
      )) {
        return;
      }
      button.disabled = true;
      request("/telemetry", { method: "DELETE" })
        .then(() => refreshActivity())
        .then(() => toast("Локальные данные активности очищены", "success"))
        .catch((error) => toast(error.message, "error"))
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "set-activity-tab") {
      setActivityTab(button.dataset.tab);
    } else if (action === "load-more-history") {
      button.disabled = true;
      refreshActivity({ quiet: true, append: "history" })
        .catch(() => {})
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "load-more-logs") {
      button.disabled = true;
      refreshActivity({ quiet: true, append: "logs" })
        .catch(() => {})
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "open-history-detail") {
      openHistoryDetail(button.dataset.requestId);
    } else if (action === "close-history-detail") {
      closeHistoryDetail();
    } else if (action === "check-active-login") {
      refreshState()
        .then(() => toast("Состояние профиля обновлено", "success"))
        .catch((error) => toast(error.message, "error"));
    } else if (action === "open-profile-login") {
      const profile = profiles().find((row) => row.id === button.dataset.profileId);
      if (profile) openProfileDrawer(profile);
    } else if (action === "refresh-clients") {
      button.disabled = true;
      refreshClients()
        .then(() => toast("Состояние клиентов обновлено", "success"))
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "configure-client") {
      configureClient(button.dataset.clientId, button);
    } else if (action === "install-client") {
      installClient(button.dataset.clientId, button);
    } else if (action === "copy-command") {
      navigator.clipboard
        ?.writeText(button.dataset.command || "")
        .then(() => toast("Скопировано", "success"))
        .catch(() => toast("Буфер обмена недоступен", "error"));
    } else if (action === "open-proxy-dialog") {
      openProxyDialog(button.dataset.profileId);
    } else if (action === "close-proxy-dialog") {
      closeProxyDialog();
    } else if (action === "test-proxy") {
      testProxy(button);
    } else if (action === "save-proxy") {
      saveProxy(button);
    } else if (action === "activate-profile") {
      activateProfile(button.dataset.profileId, button);
    } else if (action === "select-model-profile") {
      ui.selectedModelProfileId = button.dataset.profileId;
      renderModels();
    } else if (action === "set-profile-model") {
      setProfileModel(button.dataset.profileId, button.dataset.modelId, button);
    } else if (action === "check-login") {
      button.disabled = true;
      pollLogin()
        .catch(() => {})
        .finally(() => {
          button.disabled = false;
        });
    } else if (action === "cancel-login") {
      cancelProfileLogin(button);
    } else if (action === "save-custom-persona") {
      const textarea = $("#custom-persona");
      const value = textarea.value;
      const revision = ui.customPersonaRevision;
      button.disabled = true;
      patchBehavior({ custom_persona: value, persona: "custom" })
        .then((payload) => {
          if (!payload) return;
          if (
            ui.customPersonaRevision === revision
            && textarea.value === value
          ) {
            ui.customPersonaDirty = false;
          }
          renderBehavior();
        })
        .finally(() => {
          button.disabled = false;
        });
    }
  }

  function bindEvents() {
    window.addEventListener("hashchange", () => {
      setRoute(location.hash.slice(1));
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-action]");
      if (button) handleAction(button);
    });
    $("#drawer-backdrop").addEventListener("click", closeProfileDrawer);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && $("#profile-drawer").classList.contains("is-open")) {
        closeProfileDrawer();
      }
    });
    $("#profile-form").addEventListener("submit", createProfile);
    $("#profile-provider").addEventListener("change", (event) => {
      applyLoginProvider({ provider: event.currentTarget.value });
    });
    [
      "#activity-period",
      "#activity-provider-filter",
      "#activity-profile-filter",
      "#activity-model-filter",
      "#activity-status-filter",
      "#activity-level-filter",
    ].forEach((selector) => {
      $(selector).addEventListener("change", () => {
        refreshActivity().catch(() => {});
      });
    });
    $("#activity-search").addEventListener("input", () => {
      if (ui.activitySearchTimer) window.clearTimeout(ui.activitySearchTimer);
      ui.activitySearchTimer = window.setTimeout(() => {
        refreshActivity({ quiet: true }).catch(() => {});
      }, 300);
    });
    $("#activity-store-content").addEventListener("change", (event) => {
      const control = event.currentTarget;
      const next = control.checked;
      if (
        !next
        && !window.confirm(
          "Отключить сохранение текстов и удалить уже сохранённые тексты? Метрики останутся.",
        )
      ) {
        control.checked = true;
        return;
      }
      control.disabled = true;
      patchTelemetrySettings({ store_content: next })
        .catch(async (error) => {
          toast(error.message, "error");
          if (error.status === 503) {
            await refreshActivity({ quiet: true }).catch(() => {});
          } else {
            control.checked = !next;
          }
        })
        .finally(() => {
          control.disabled = false;
        });
    });
    $("#activity-retention").addEventListener("change", (event) => {
      const control = event.currentTarget;
      const previous = Number(
        ui.activity?.settings?.retention_days
        || ui.state?.config?.telemetry?.retention_days
        || 30,
      );
      const next = Number(control.value);
      if (
        next < previous
        && !window.confirm(
          `Сократить срок до ${next} дней? Более старые локальные записи будут удалены.`,
        )
      ) {
        control.value = String(previous);
        return;
      }
      control.disabled = true;
      patchTelemetrySettings({ retention_days: next })
        .catch(async (error) => {
          toast(error.message, "error");
          if (error.status === 503) {
            await refreshActivity({ quiet: true }).catch(() => {});
          } else {
            control.value = String(previous);
          }
        })
        .finally(() => {
          control.disabled = false;
        });
    });
    $(".activity-tabs").addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      const tabs = $$(".activity-tab");
      const current = tabs.indexOf(document.activeElement);
      if (current < 0) return;
      event.preventDefault();
      const direction = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(current + direction + tabs.length) % tabs.length];
      next.focus();
      setActivityTab(next.dataset.tab);
    });
    $("#history-dialog").addEventListener("click", (event) => {
      if (event.target === event.currentTarget) closeHistoryDetail();
    });
    $("#proxy-dialog").addEventListener("click", (event) => {
      if (event.target === event.currentTarget) closeProxyDialog();
    });
    $("#proxy-dialog").addEventListener("close", () => {
      ui.proxyProfileId = null;
    });

    $$("[data-behavior-key]").forEach((control) => {
      control.addEventListener("change", () => {
        const key = control.dataset.behaviorKey;
        let value;
        if (control.type === "checkbox") value = control.checked;
        else value = control.value;
        if (key === "persona") {
          $("#custom-persona-wrap").hidden = value !== "custom";
        }
        patchBehavior(key, value);
      });
    });

    $("#custom-persona").addEventListener("input", () => {
      ui.customPersonaDirty = true;
      ui.customPersonaRevision += 1;
      updateCustomPersonaCount();
      renderPersonaCompilation();
    });

    $("#overview-model").addEventListener("change", (event) => {
      const select = event.currentTarget;
      if (select.dataset.profileId) {
        setProfileModel(select.dataset.profileId, select.value, select);
      }
    });
  }

  function boot() {
    bindEvents();
    setRoute(location.hash.slice(1) || "overview");
    refreshState().catch(() => {});
    window.setInterval(() => {
      if (document.visibilityState === "visible") {
        refreshState({ quiet: true }).catch(() => {});
        const loadedActivityRows = ui.activity?.requests?.items?.length || 0;
        const loadedActivityEvents = activityPage(ui.activity?.events).items.length;
        if (
          ui.route === "activity"
          && !ui.activityPromise
          && loadedActivityRows <= 100
          && loadedActivityEvents <= 100
        ) {
          refreshActivity({ quiet: true }).catch(() => {});
        }
      }
    }, REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        refreshState({ quiet: true }).catch(() => {});
        const loadedActivityRows = ui.activity?.requests?.items?.length || 0;
        const loadedActivityEvents = activityPage(ui.activity?.events).items.length;
        if (
          ui.route === "activity"
          && !ui.activityPromise
          && loadedActivityRows <= 100
          && loadedActivityEvents <= 100
        ) {
          refreshActivity({ quiet: true }).catch(() => {});
        }
      }
    });
  }

  boot();
})();
