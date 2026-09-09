"use strict";

/* The daily report and annual chart are separate calendar windows sharing the
   same provider/app/model and evidence filters. No reported/estimated sum exists.
   All log-derived labels are text nodes; the browser has no write API. */
(() => {
  const $ = (id) => document.getElementById(id);
  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const state = { report: null, annual: null, controller: null, loading: false, timezone: null, loadedView: null, defaultMonth: null, evidence: null };
  const dimensions = ["provider", "surface", "model"];
  const knownCount = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const formatCount = (value) => knownCount(value) ? numberFormat.format(value) : "Unknown";
  const plural = (value, singular, multiple = `${singular}s`) => `${formatCount(value)} ${value === 1 ? singular : multiple}`;
  const categoryFields = ["uncached_input_tokens", "output_tokens", "cache_creation_tokens", "cached_input_tokens", "total_tokens"];

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function name(value) {
    const names = { openai: "OpenAI", anthropic: "Anthropic", codex: "Codex", claude: "Claude",
      codex_cli: "Codex CLI", codex_desktop: "Codex Desktop", codex_subagent: "Codex subagent", codex_ide: "Codex IDE",
      claude_code: "Claude Code", claude_desktop_agent: "Claude desktop agent", chatgpt: "ChatGPT", claude_chat: "Claude chat",
      unknown: "Unknown", account_scope_unverified: "Account scope unverified", quota: "Quota snapshot", account_summary: "Account summary" };
    return value === null || value === undefined || value === "" ? "Unknown" : names[value] || String(value).replace(/_/g, " ");
  }

  function formatTime(value) {
    if (!value) return "Unknown";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return "Unknown";
    const options = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
    if (state.timezone) options.timeZone = state.timezone;
    return new Intl.DateTimeFormat(undefined, options).format(date);
  }

  function monthLabel(value) {
    return new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" }).format(new Date(`${value}-01T12:00:00Z`));
  }

  function cellValue(aggregate, field) {
    const value = aggregate?.[field];
    const unknown = aggregate?.unknown_fields?.[field] || 0;
    const partial = knownCount(value) && unknown > 0;
    const caveats = [];
    if (aggregate?.provisional_events) caveats.push("includes provisional usage; output may be unfinished");
    if (aggregate?.incomplete_events) caveats.push("includes partial cached history; only available records are counted");
    const title = knownCount(value) ? `${formatCount(value)} tokens${partial ? `; ${unknown} event(s) lack this category` : ""}` : "This category is not known for these records";
    return { text: `${formatCount(value)}${partial ? "*" : ""}`, title: [title, ...caveats].join("; ") };
  }

  function countCell(aggregate, field, tag = "td") {
    const value = cellValue(aggregate, field);
    const cell = element(tag, `number${field === "total_tokens" ? " total-column" : ""}`, value.text);
    cell.title = value.title;
    return cell;
  }

  function selectedFields() {
    return $("quality").value === "estimated" ? ["input_tokens", ...categoryFields.slice(1)] : categoryFields;
  }

  function renderOptions(options) {
    for (const [id, key, label] of [["provider", "providers", "All providers"], ["surface", "surfaces", "All surfaces"], ["model", "models", "All models"]]) {
      const selected = $(id).value;
      const values = [...new Set([...(options?.[key] || []), ...(selected !== "all" ? [selected] : [])])].sort();
      const first = element("option", "", label); first.value = "all";
      const nodes = [first, ...values.map((value) => { const option = element("option", "", id === "model" ? value : name(value)); option.value = value; return option; })];
      $(id).replaceChildren(...nodes);
      $(id).value = selected;
    }
  }

  function renderDaily(report) {
    const quality = $("quality").value;
    const estimated = quality === "estimated";
    const aggregate = report.totals[quality];
    // ISO calendar dates sort lexically; show the newest day's group first.
    const rows = (report.daily || []).filter((row) => row.quality === quality)
      .sort((a, b) => b.date.localeCompare(a.date));
    const fields = selectedFields();
    $("summary-label").textContent = estimated ? "Visible text this month"
      : aggregate.provisional_events || aggregate.incomplete_events ? "Observed this month" : "Reported this month";
    for (const [id, field] of [["month-total", "total_tokens"], ["month-input", fields[0]], ["month-output", "output_tokens"], ["month-write", "cache_creation_tokens"], ["month-read", "cached_input_tokens"]]) {
      const value = cellValue(aggregate, field); $(id).textContent = value.text; $(id).title = value.title;
    }
    $("month-events").textContent = `${plural(aggregate.events, "event")} · ${plural(aggregate.sessions, "session")}`;
    document.querySelectorAll(".cache-summary").forEach((node) => { node.hidden = estimated; });
    $("input-heading").textContent = estimated ? "Input text" : "Input (uncached)";
    $("daily-heading").textContent = estimated ? "Daily text estimates" : "Daily report";
    $("daily-period").textContent = `${monthLabel(report.period.month)} · ${report.timezone} · ${estimated ? "Visible-text estimates" : "Reported logs"}`;
    $("accounting-note").textContent = estimated
      ? "Visible text only; these are heuristic estimates, not reported consumption. Cache, hidden context, reasoning, and media are not reconstructed."
      : "Total = uncached input + output + cache writes + cache reads. Output includes reasoning. * marks a partial category; unknown is not zero.";
    // A numerical token field can be known yet provisional, and a cached
    // conversation can omit older records even when its fields are complete.
    // Keep that evidence warning distinct from the '*' for unknown categories.
    let evidenceNote = $("usage-evidence-note");
    if (!evidenceNote) {
      evidenceNote = element("p", "notice"); evidenceNote.id = "usage-evidence-note";
      evidenceNote.setAttribute("role", "status");
      $("accounting-note").insertAdjacentElement("beforebegin", evidenceNote);
    }
    const evidenceMessages = [];
    if (aggregate.provisional_events) evidenceMessages.push(`${plural(aggregate.provisional_events, "event")} still ${aggregate.provisional_events === 1 ? "has" : "have"} provisional counts; output may be unfinished.`);
    if (aggregate.incomplete_events) evidenceMessages.push(`${plural(aggregate.incomplete_events, "event")} come${aggregate.incomplete_events === 1 ? "s" : ""} from partial cached history; totals cover only the records available on this machine.`);
    if (aggregate.summary_remainders) evidenceMessages.push(`${plural(aggregate.summary_remainders, "summary adjustment")} fill missing usage from verified completed turns; amounts adjust as message records arrive.`);
    evidenceNote.hidden = !evidenceMessages.length;
    evidenceNote.replaceChildren(element("strong", "", "Usage evidence is incomplete. "), document.createTextNode(evidenceMessages.join(" ")));
    $("daily-empty").hidden = rows.length > 0;
    $("daily-empty-copy").textContent = estimated ? "Import a ChatGPT or Claude conversation export to see visible-text estimates, or choose another month." : "Choose another month or check source coverage below.";
    $("daily-table-wrap").hidden = !rows.length;
    const body = $("daily-body"); body.replaceChildren();
    for (const day of rows) {
      const total = element("tr", "daily-subtotal");
      const date = element("th", "daily-date", day.date); date.scope = "row";
      total.append(date, element("td", "app-label", "All"), element("td", "model-list", ""), ...fields.map((field) => countCell(day, field)));
      body.append(total);
      if ($("show-apps").checked) {
        for (const app of day.apps || []) {
          const row = element("tr", "daily-app");
          const appName = element("th", "app-label", `↳ ${name(app.app)}`); appName.scope = "row";
          const models = element("td", "model-list");
          (app.models || []).forEach((model) => models.append(element("span", "", model)));
          row.append(element("td", "daily-date", ""), appName, models, ...fields.map((field) => countCell(app, field)));
          body.append(row);
        }
      }
    }
    const footer = element("tr");
    const label = element("th", "", "Month total"); label.scope = "row"; label.colSpan = 3;
    footer.append(label, ...fields.map((field) => countCell(aggregate, field)));
    $("daily-total").replaceChildren(footer);
  }

  function renderAnnual() {
    if (!state.annual) return;
    const quality = $("quality").value;
    const aggregate = state.annual.totals[quality];
    const evidence = quality === "reported"
      ? aggregate.provisional_events || aggregate.incomplete_events ? "Observed logs; includes incomplete usage" : "Reported logs"
      : "Visible-text estimates";
    $("annual-description").textContent = `${state.annual.period.year} · ${evidence} · ${formatCount(aggregate.total_tokens)} tokens · Select a month to view its daily report.`;
    window.UsageChart.render({ container: $("chart-container"), tableBody: $("chart-data-body"), report: state.annual, quality,
      onMonthSelect: (month) => { $("month").value = month; refresh(); $("daily-heading").scrollIntoView({ behavior: "smooth", block: "start" }); } });
  }

  function renderCoverage(coverage) {
    const list = $("coverage-list");
    const sources = Array.isArray(coverage) ? coverage : [];
    list.replaceChildren();
    if (!sources.length) {
      const box = element("div", "empty-state");
      box.append(element("h3", "", "No source evidence yet"), element("p", "", "The first collection will discover configured local log locations. Use the CLI to configure additional roots or import a chat export."));
      list.append(box);
      return;
    }
    const statuses = { ok: "Collected", healthy: "Collected", ready: "Ready", missing: "Not found", not_found: "Not found", error: "Needs attention", warning: "Check source", degraded: "Check source", partial: "Partial coverage", collecting: "Collecting", empty: "No events", imported: "Imported", disabled: "Disabled", unsupported: "Unsupported" };
    for (const source of sources) {
      const row = element("article", "source-row");
      const main = element("div", "source-main");
      const heading = element("div", "source-name-line");
      const status = String(source.status || "unknown");
      const statusClass = ["ok", "healthy", "ready", "imported"].includes(status) ? "ok" : ["error", "warning", "degraded", "partial"].includes(status) ? "warning" : "";
      heading.append(element("span", "source-name", name(source.source)), element("span", `source-status ${statusClass}`, statuses[status] || name(status)));
      main.append(heading, element("code", "source-path", source.path || "Source path unavailable"));
      if (source.error) main.append(element("p", "source-warning", String(source.error)));
      const warnings = Array.isArray(source.warnings) ? source.warnings : source.warnings ? [source.warnings] : [];
      if (warnings.length) {
        const details = element("details", "source-warnings");
        details.append(element("summary", "", plural(warnings.length, "collection note")));
        const ul = element("ul");
        warnings.forEach((warning) => ul.append(element("li", "", String(warning))));
        details.append(ul);
        main.append(details);
      }
      const meta = element("div", "source-meta");
      meta.append(element("span", "", `${plural(source.files, "file")} · ${plural(source.events, "event")}`),
        element("span", "source-time", source.last_success ? `Last scan ${formatTime(source.last_success)}` : "Last scan unknown"),
        element("span", "source-time", source.latest_usage ? `Latest usage ${formatTime(source.latest_usage)}` : "Latest usage unknown"));
      if (source.summaries || source.quota_observations) meta.append(element("span", "", `${plural(source.summaries || 0, "saved summary", "saved summaries")} · ${plural(source.quota_observations || 0, "quota observation")}`));
      row.append(main, meta);
      list.append(row);
    }
  }

  function flattenEvidence(value, prefix = "", depth = 0) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [[prefix || "Value", value]];
    return Object.entries(value).flatMap(([key, item]) => {
      const label = prefix ? `${prefix} / ${name(key)}` : name(key);
      return item && typeof item === "object" && !Array.isArray(item) && depth < 2
        ? flattenEvidence(item, label, depth + 1) : [[label, item]];
    });
  }

  function renderQuotaWindows(data) {
    // Only adapter-defined percentage fields create quota bars. Neither a
    // token summary nor a raw utilization ratio is interpreted as a quota.
    const windows = Array.isArray(data?.windows) ? data.windows : [];
    const rows = [...windows];
    for (const key of ["primary", "secondary"]) {
      if (data?.[key] && typeof data[key] === "object") rows.push({ ...data[key], name: key === "primary" ? "Primary" : "Secondary" });
    }
    if (!rows.length) return null;
    const container = element("div", "quota-windows");
    for (const window of rows) {
      const minutes = window.window_minutes;
      const duration = knownCount(minutes) && minutes > 0
        ? minutes % 1440 === 0 ? `${minutes / 1440}-day window` : minutes % 60 === 0 ? `${minutes / 60}-hour window` : `${minutes}-minute window`
        : name(window.name);
      const card = element("div", "quota-window");
      const heading = element("div", "quota-window-heading");
      const percent = window.used_percent;
      const known = knownCount(percent);
      const percentText = known ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(percent)}% used` : "Usage unknown";
      heading.append(element("span", "", duration), element("strong", "", percentText));
      card.append(heading);
      if (known) {
        const track = element("div", "quota-track");
        track.setAttribute("aria-hidden", "true");
        const fill = element("div", "quota-fill");
        fill.style.width = `${Math.min(100, percent)}%`;
        track.append(fill);
        card.append(track);
      }
      card.append(element("p", "", window.resets_at ? `Reset recorded for ${formatTime(window.resets_at)}` : "Reset time unknown"));
      container.append(card);
    }
    return container;
  }

  function renderSnapshots(snapshots) {
    const list = $("snapshots-list");
    list.replaceChildren();
    const rows = Array.isArray(snapshots) ? snapshots : [];
    if (!rows.length) {
      const box = element("div", "snapshot-empty");
      const copy = element("p");
      copy.append(element("strong", "", "No account snapshots collected"), document.createTextNode("When available, quota and account summaries appear here as separate evidence. Quota percentages are not token counts and do not establish a subscription bill."));
      box.append(copy);
      list.append(box);
      return;
    }
    for (const snapshot of rows) {
      const article = element("article", "snapshot");
      const header = element("div", "snapshot-header");
      const time = element("time", "", formatTime(snapshot.timestamp));
      if (snapshot.timestamp) time.dateTime = snapshot.timestamp;
      header.append(element("h3", "", `${name(snapshot.provider)} · ${name(snapshot.kind)}`), time);
      article.append(header, element("p", "snapshot-scope", `Scope: ${name(snapshot.scope)}. Separate evidence; excluded from usage totals.`));
      if (snapshot.kind === "quota") {
        const windows = renderQuotaWindows(snapshot.data);
        if (windows) article.append(windows);
      }
      const details = element("details");
      details.append(element("summary", "", "Inspect recorded evidence"));
      const dl = element("dl", "snapshot-data");
      for (const [key, value] of flattenEvidence(snapshot.data || {})) {
        const text = value === null || value === undefined ? "Unknown" : typeof value === "object" ? JSON.stringify(value) : String(value);
        dl.append(element("dt", "", key), element("dd", "", text));
      }
      if (!dl.childElementCount) dl.append(element("dt", "", "Data"), element("dd", "", "No snapshot details available"));
      details.append(dl);
      article.append(details);
      list.append(article);
    }
    list.append(element("p", "snapshot-scope", "Quota percentages are not token counts. Account summaries may overlap local logs; no subscription bill is inferred."));
  }

  function renderHealth(health) {
    const status = health?.status;
    $("health").className = `status-pill ${["ok", "collecting", "degraded"].includes(status) ? status : "degraded"}`;
    $("health-label").textContent = status === "ok" ? "Collector connected" : status === "collecting" ? "Collecting" : status === "degraded" ? "Check source coverage" : "Status unavailable";
    const lastCollection = typeof health?.last_collection === "object" ? health.last_collection?.finished_at : health?.last_collection;
    $("last-collection").textContent = lastCollection ? `Last scan ${formatTime(lastCollection)}` : health ? "No completed scan recorded" : "Collection status unavailable";
    if (health?.interval_seconds) $("last-collection").title = `Collection interval: ${health.interval_seconds} seconds`;
  }


  async function fetchJSON(path, signal) {
    const response = await fetch(path, { signal, cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`Local service returned HTTP ${response.status}`);
    return response.json();
  }

  function renderEvidence() {
    if (!state.evidence || !window.UsageEvidence) return;
    window.UsageEvidence.render({ summaries: state.evidence.summaries, quotaHistory: state.evidence.quotaHistory,
      timezone: state.timezone, onMoreSummaries: () => loadOlderEvidence("summaries"), onMoreQuota: () => loadOlderEvidence("quotaHistory") });
  }

  async function loadOlderEvidence(kind) {
    // Pagination belongs to the loaded filter view. A concurrent refresh aborts
    // this request, preventing an old month's evidence from entering a new one.
    const owner = state.controller;
    const page = state.evidence?.[kind];
    if (!page || page.items.length >= page.total) return;
    const query = new URLSearchParams({ offset: String(page.items.length), limit: "25" });
    if (kind === "summaries") {
      dimensions.forEach((key) => query.set(key, $(key).value));
      query.set("month", state.report.period.month);
    } else {
      // Quotas apply to an account/provider, not a model or report month. Honor
      // the provider so a busy Codex stream cannot bury Claude's observations.
      query.set("provider", $("provider").value);
    }
    try {
      const next = await fetchJSON(`${kind === "summaries" ? "/api/reconciliation" : "/api/quota-history"}?${query}`, owner.signal);
      if (state.controller !== owner) return;
      const key = kind === "summaries" ? "summary_key" : "snapshot_key";
      const merged = new Map([...page.items, ...next.items].map((item) => [item[key], item]));
      state.evidence[kind] = { ...next, offset: 0, items: [...merged.values()] };
      renderEvidence();
    } catch (error) {
      if (state.controller !== owner) return;
      $(kind === "summaries" ? "reconciliation-status" : "quota-history-status").textContent = "Could not load older evidence. Try again.";
    }
  }

  async function refresh() {
    // A single refresh owns both windows, so a slow response from a previous
    // filter cannot mix one month's table with a different provider's chart.
    if (state.controller) state.controller.abort();
    const controller = new AbortController(); state.controller = controller;
    state.loading = true;
    $("refresh").disabled = true; $("refresh-label").textContent = "Refreshing";
    $("report-content").setAttribute("aria-busy", "true");
    const common = new URLSearchParams(); dimensions.forEach((key) => common.set(key, $(key).value));
    const daily = new URLSearchParams(common); daily.set("month", $("month").value || "current"); daily.set("group", "day");
    const annual = new URLSearchParams(common); annual.set("year", $("chart-year").value); annual.set("group", "month");
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const [dailyResult, annualResult, healthResult] = await Promise.allSettled([
        fetchJSON(`/api/report?${daily}`, controller.signal), fetchJSON(`/api/report?${annual}`, controller.signal), fetchJSON("/api/health", controller.signal)
      ]);
      if (state.controller !== controller) return;
      if (dailyResult.status === "rejected") throw dailyResult.reason;
      if (annualResult.status === "rejected") throw annualResult.reason;
      const report = dailyResult.value;
      if (!Array.isArray(report.daily) || !report.period?.month || !annualResult.value.series?.every((row) => row.reported && row.estimated)) {
        throw new Error("The service needs the updated calendar report API");
      }
      state.report = report; state.annual = annualResult.value; state.timezone = report.timezone;
      if (!state.defaultMonth) {
        state.defaultMonth = report.period.month;
        // The first request uses the server's current month, not the browser's
        // timezone. Around midnight those two calendars can differ.
        $("month").value = report.period.month;
        if ($("chart-year").value !== report.period.month.slice(0, 4)) { $("chart-year").value = report.period.month.slice(0, 4); refresh(); return; }
      }
      renderOptions(report.options); renderDaily(report); renderAnnual();
      renderCoverage(report.coverage); renderSnapshots(report.snapshots);
      state.evidence = { summaries: report.reconciliation || { items: [], total: 0 }, quotaHistory: report.quota_history || { items: [], total: 0 } };
      renderEvidence();
      renderHealth(healthResult.status === "fulfilled" ? healthResult.value : null);
      state.loadedView = `${monthLabel(report.period.month)} / ${state.annual.period.year} · ${$("quality").selectedOptions[0].textContent}`;
      $("report-notes").replaceChildren(...(report.notes || []).map((note) => element("p", "", note)));
      $("refresh-status").textContent = `Updated ${formatTime(report.generated_at)} · Refreshes every 30s`;
      $("error-banner").hidden = healthResult.status === "fulfilled";
      if (healthResult.status === "rejected") $("error-banner").textContent = "Usage loaded, but collector status is unavailable. The next refresh will check again.";
    } catch (error) {
      if (state.controller !== controller) return;
      $("error-banner").hidden = false;
      $("error-banner").textContent = state.report
        ? `Could not refresh. The previous view (${state.loadedView}) is still shown. Check the local service and try Refresh.`
        : "Could not load the daily report. Check that the updated Usage Tracker service is running, then select Refresh.";
      $("refresh-status").textContent = state.report ? `Last loaded ${formatTime(state.report.generated_at)} · Refresh failed` : "Connection unavailable · Retrying every 30s";
      renderHealth(null);
    } finally {
      clearTimeout(timeout);
      if (state.controller === controller) {
        state.loading = false; $("refresh").disabled = false; $("refresh-label").textContent = "Refresh";
        $("report-content").setAttribute("aria-busy", "false");
        $("reset-filters").hidden = dimensions.every((key) => $(key).value === "all") && $("quality").value === "reported" && (!state.defaultMonth || $("month").value === state.defaultMonth) && $("chart-year").value === state.defaultMonth?.slice(0, 4);
      }
    }
  }

  // Calendar controls intentionally remain explicit: changing the table month
  // follows that year in the chart; changing only the chart year keeps the table.
  $("chart-year").value = String(new Date().getFullYear());
  $("month").addEventListener("change", () => {
    if (!/^\d{4}-\d{2}$/.test($("month").value)) return;
    $("chart-year").value = $("month").value.slice(0, 4); refresh();
  });
  for (const [id, step] of [["previous-month", -1], ["next-month", 1]]) {
    $(id).addEventListener("click", () => {
      if (!$("month").value) return;
      const [year, month] = $("month").value.split("-").map(Number);
      const next = new Date(Date.UTC(year, month - 1 + step, 1));
      $("month").value = `${next.getUTCFullYear()}-${String(next.getUTCMonth() + 1).padStart(2, "0")}`;
      $("chart-year").value = String(next.getUTCFullYear()); refresh();
    });
  }
  $("chart-year").addEventListener("change", () => { if ($("chart-year").checkValidity() && $("chart-year").value) refresh(); });
  dimensions.forEach((key) => $(key).addEventListener("change", refresh));
  $("quality").addEventListener("change", refresh);
  $("show-apps").addEventListener("change", () => { if (state.report) renderDaily(state.report); });
  $("refresh").addEventListener("click", refresh);
  $("reset-filters").addEventListener("click", () => {
    dimensions.forEach((key) => { $(key).value = "all"; });
    $("quality").value = "reported"; $("month").value = state.defaultMonth || "";
    $("chart-year").value = (state.defaultMonth || String(new Date().getFullYear())).slice(0, 4); refresh();
  });
  setInterval(() => { if (!document.hidden && !state.loading) refresh(); }, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.loading) refresh(); });
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderAnnual, 120); });
  refresh();
})();
