"use strict";

/* Local evidence views are deliberately separate from the daily report. A saved
   summary can overlap request logs, and a quota percentage is not a token count.
   Every provider/model/path/identifier below becomes a text node, never HTML. */
(() => {
  const counts = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const percents = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const dateOptions = { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" };
  let dates = new Intl.DateTimeFormat(undefined, dateOptions);
  function setTimezone(timezone) {
    try { dates = new Intl.DateTimeFormat(undefined, { ...dateOptions, ...(typeof timezone === "string" && timezone ? { timeZone: timezone } : {}) }); }
    catch { dates = new Intl.DateTimeFormat(undefined, dateOptions); }
  }
  const fields = [
    ["Uncached input", "uncached_input_tokens", ["input_tokens", "inputTokens"]],
    ["Output", "output_tokens", ["output_tokens", "outputTokens"]],
    ["Cache writes", "cache_creation_tokens", ["cache_creation_input_tokens", "cacheCreationInputTokens"]],
    ["Cache reads", "cached_input_tokens", ["cache_read_input_tokens", "cacheReadInputTokens"]],
  ];
  const statuses = {
    reconciled: ["Reconciled", "included", "The verified remainder is included in usage totals."],
    covered: ["Already covered", "included", "Request logs already cover this summary."],
    already_covered: ["Already covered", "included", "Request logs already cover this summary."],
    matched: ["Matched", "included", "The summary matches the recorded requests."],
    conflict: ["Counts disagree", "review", "The summary and request counts need review."],
    conflicting: ["Counts disagree", "review", "The summary and request counts need review."],
    ambiguous: ["Scope needs review", "review", "The matching request window is not confirmed."],
    ambiguous_scope: ["Scope needs review", "review", "The matching request window is not confirmed."],
    unsupported_scope: ["Scope needs review", "review", "The matching request window is not confirmed."],
    pending: ["Awaiting comparison", "pending", "Saved for comparison with request logs."],
    unreconciled: ["Not reconciled", "pending", "Saved for comparison with request logs."],
    unmatched: ["No confirmed match", "pending", "Saved for comparison with request logs."],
    incomplete: ["Incomplete evidence", "review", "More evidence is needed to confirm the matching requests."],
    conflicting_revisions: ["Saved versions disagree", "review", "Conflicting saved versions need review."],
    missing_window: ["Turn boundary missing", "review", "The matching request window is not confirmed."],
    nonterminal: ["Completion unverified", "pending", "A completed turn is not confirmed by this summary."],
    overlapping_runs: ["Turn windows overlap", "review", "Overlapping request windows need review."],
    undated_messages: ["Request dates missing", "review", "Requests without dates cannot be assigned to this turn."],
    incomplete_summary: ["Summary counts incomplete", "review", "The summary lacks one or more token categories."],
    incomplete_messages: ["Request counts incomplete", "review", "Matched requests lack one or more token categories."],
    count_conflict: ["Counts disagree", "review", "The summary and request counts need review."],
  };
  const providerNames = { anthropic: "Anthropic", openai: "OpenAI" };
  const surfaceNames = { claude_desktop_agent: "Claude desktop agent", claude_code: "Claude Code", codex_desktop: "Codex desktop", codex_cli: "Codex CLI" };
  const windowNames = { five_hour: "5-hour window", seven_day: "7-day window", seven_day_overage_included: "7-day window including extra usage" };
  const known = (value) => Number.isSafeInteger(value) && value >= 0;
  const count = (value) => known(value) ? counts.format(value) : "Unknown";
  const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const text = (value, fallback = "Unknown") => typeof value === "string" && value.length ? value : fallback;
  const human = (value) => text(value).replace(/_/g, " ").replace(/^./, (char) => char.toUpperCase());

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = String(value);
    return element;
  }
  function read(source, keys) {
    const data = object(source);
    for (const key of keys) if (Object.prototype.hasOwnProperty.call(data, key)) return data[key];
    return undefined;
  }
  function nativeUsage(source) {
    const result = {};
    for (const [, key, nativeKeys] of fields) result[key] = read(source, nativeKeys);
    // These four categories are disjoint. A missing component keeps the total
    // unknown; neither absent values nor malformed counts silently become zero.
    const parts = fields.map(([, key]) => result[key]);
    const total = parts.every(known) ? parts.reduce((sum, value) => sum + value, 0) : undefined;
    result.total_tokens = known(total) ? total : undefined;
    return result;
  }
  function dateValue(value) {
    if (typeof value !== "string" && typeof value !== "number") return null;
    if (value === "") return null;
    const converted = typeof value === "number" ? value * (Math.abs(value) < 1e11 ? 1000 : 1) : value;
    const date = new Date(converted);
    return Number.isFinite(date.valueOf()) ? date : null;
  }
  function formatDate(value) { const date = dateValue(value); return date ? dates.format(date) : "Unknown"; }
  function time(value) {
    const element = node("time", "ev-time", formatDate(value));
    const date = dateValue(value);
    if (date) { element.dateTime = date.toISOString(); element.title = date.toISOString(); }
    return element;
  }
  function sorted(items) {
    return (Array.isArray(items) ? items : []).map((item, index) => ({ item: object(item), index }))
      .sort((a, b) => (dateValue(b.item.timestamp)?.valueOf() ?? -Infinity) - (dateValue(a.item.timestamp)?.valueOf() ?? -Infinity) || a.index - b.index)
      .map(({ item }) => item);
  }
  function knownLabel(labels, value) { return Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : human(value); }
  function status(item) { return Object.prototype.hasOwnProperty.call(statuses, item.status) ? statuses[item.status] : [human(item.status || "pending"), "pending", "Saved for comparison with request logs."]; }
  function tokenText(value) { return `${count(value)} tokens`; }
  function detailRow(list, label, value, code = false) {
    list.append(node("dt", "", label));
    const definition = node("dd");
    definition.append(node(code ? "code" : "span", "", value));
    list.append(definition);
  }

  function provenance(item, key, quota = false) {
    const detail = node("details", "ev-provenance"); detail.dataset.evidenceKey = `${key}:provenance`;
    detail.append(node("summary", "", "Capture history & source details"));
    const list = node("dl", "ev-facts");
    detailRow(list, quota ? "Observation ID" : "Summary ID", text(quota ? item.snapshot_key : item.summary_key), true);
    if (!quota) detailRow(list, "Session ID", text(item.session_id), true);
    detailRow(list, "Source observation", formatDate(item.timestamp));
    if (!quota) detailRow(list, "Recorded scope start", formatDate(item.started_at));
    detailRow(list, "First captured", formatDate(item.first_seen));
    detailRow(list, "Last captured", formatDate(item.last_seen));
    if (!quota) detailRow(list, "Saved revisions", count(item.revision_count));
    // Show useful, bounded adapter metadata without dumping arbitrary nested
    // state. UUIDs are evidence links between records; they do not imply scope.
    const metadata = { ...object(item.native), ...object(item.metadata) };
    for (const [field, label] of [
      ["result_id", "Provider result ID"], ["user_message_uuid", "Trigger message ID"],
      ["scope", "Native scope"], ["source_kind", "Source format"],
      ["boundary_kind", "Boundary evidence"], ["boundary_method", "Boundary method"], ["subtype", "Result type"], ["terminal_reason", "Completion reason"],
    ]) {
      if (typeof metadata[field] === "string" && metadata[field].length) detailRow(list, label, metadata[field], field.endsWith("id") || field.endsWith("uuid"));
    }
    const source = object(item.data).source;
    if (quota && typeof source === "string") detailRow(list, "Source", source);
    if (quota && typeof item.scope === "string") detailRow(list, "Scope", human(item.scope));
    if (Array.isArray(metadata.user_message_uuids) && metadata.user_message_uuids.length > 1) {
      detailRow(list, "Linked trigger IDs", metadata.user_message_uuids.filter((id) => typeof id === "string").join(" · "), true);
    }
    if (typeof metadata.window_verified === "boolean") detailRow(list, "Request window verified", metadata.window_verified ? "Yes" : "No");
    detail.append(list);
    const paths = [...new Set((Array.isArray(item.source_paths) ? item.source_paths : []).filter((value) => typeof value === "string"))];
    if (paths.length) {
      detail.append(node("p", "ev-source-label", "Source files"));
      const sources = node("ul", "ev-sources");
      for (const value of paths) { const row = node("li"); row.append(node("code", "", value)); sources.append(row); }
      detail.append(sources);
    }
    return detail;
  }

  function comparison(item, isIncluded) {
    const wrap = node("div", "ev-table-scroll"); wrap.tabIndex = 0;
    wrap.setAttribute("aria-label", "Summary and matched request token comparison");
    const table = node("table", "ev-comparison");
    table.append(node("caption", "visually-hidden", "Separate token categories; only a reconciled remainder contributes to usage totals."));
    const head = node("thead"), heading = node("tr");
    for (const [index, label] of ["Token category", "Main-turn summary", "Matched requests", isIncluded ? "Remainder included" : "Unresolved remainder"].entries()) {
      const cell = node("th", index ? "number" : "", label); cell.scope = "col"; heading.append(cell);
    }
    head.append(heading); table.append(head);
    const body = node("tbody"), native = nativeUsage(item.usage);
    for (const [label, key] of [...fields, ["Total tokens", "total_tokens"]]) {
      const row = node("tr", key === "total_tokens" ? "ev-total-row" : "");
      const heading = node("th", "", label); heading.scope = "row"; row.append(heading);
      for (const data of [native, object(item.matched_usage), object(item.remainder)]) row.append(node("td", "number", count(data[key])));
      body.append(row);
    }
    table.append(body); wrap.append(table); return wrap;
  }
  function broaderModels(item) {
    const models = Object.entries(object(item.model_usage));
    if (!models.length) return null;
    const section = node("section", "ev-model-section");
    section.append(node("h4", "", "Broader model totals"));
    section.append(node("p", "ev-muted", "These provider totals can include subagents and earlier turns in the same call. They are saved as evidence and are not added to the daily report."));
    const wrap = node("div", "ev-table-scroll"); wrap.tabIndex = 0; wrap.setAttribute("aria-label", "Provider model totals kept separate from usage totals");
    const table = node("table", "ev-model-table"), head = node("thead"), heading = node("tr");
    for (const [index, label] of ["Native model name", "Input", "Output", "Cache write", "Cache read", "Total tokens"].entries()) {
      const cell = node("th", index ? "number" : "", label); cell.scope = "col"; heading.append(cell);
    }
    head.append(heading); table.append(head);
    const body = node("tbody");
    // Sum categories within a single model observation only. Never accumulate
    // modelUsage across summaries: consecutive results may carry running totals.
    for (const [model, data] of models) {
      const normalized = nativeUsage(data), row = node("tr"), name = node("th", "ev-model-name", model); name.scope = "row"; row.append(name);
      for (const key of [...fields.map(([, key]) => key), "total_tokens"]) row.append(node("td", "number", count(normalized[key])));
      body.append(row);
    }
    table.append(body); wrap.append(table); section.append(wrap); return section;
  }
  function revisionHistory(item, key) {
    const page = object(item.revisions), revisions = Array.isArray(page.items) ? page.items : [];
    if (!revisions.length) return null;
    const detail = node("details", "ev-provenance ev-revision-history"); detail.dataset.evidenceKey = `${key}:revisions`;
    detail.append(node("summary", "", `Saved versions (${count(known(page.total) ? page.total : revisions.length)})`));
    let built = false;
    detail.addEventListener("toggle", () => {
      if (!detail.open || built) return;
      built = true;
      const body = node("div", "ev-revision-list");
      body.append(node("p", "ev-muted", "Each version is a saved observation of this summary. Versions are not added together."));
      // Captures can share the same provider timestamp. Order versions by their
      // first capture, not by the turn's unchanged completion timestamp.
      const ordered = [...revisions].map(object).sort((a, b) =>
        (dateValue(b.first_seen || b.last_seen || b.timestamp)?.valueOf() ?? -Infinity) -
        (dateValue(a.first_seen || a.last_seen || a.timestamp)?.valueOf() ?? -Infinity));
      for (const revision of ordered) {
        const section = node("section", "ev-revision");
        const heading = node("h5", ""); heading.append(node("span", "", "Captured "), time(revision.first_seen)); section.append(heading);
        const facts = node("dl", "ev-facts");
        detailRow(facts, "Last captured", formatDate(revision.last_seen));
        detailRow(facts, "Source observation", formatDate(revision.timestamp));
        detailRow(facts, "Recorded scope start", formatDate(revision.started_at));
        detailRow(facts, "Completion verified", typeof revision.terminal === "boolean" ? revision.terminal ? "Yes" : "No" : "Unknown");
        section.append(facts);
        const normalized = nativeUsage(revision.usage), categories = node("dl", "ev-revision-counts");
        for (const [label, field] of [...fields, ["Total tokens", "total_tokens"]]) {
          const pair = node("div"); pair.append(node("dt", "", label), node("dd", "", count(normalized[field]))); categories.append(pair);
        }
        section.append(categories);
        const models = broaderModels(revision); if (models) section.append(models);
        body.append(section);
      }
      if (known(page.total) && page.total > revisions.length) body.append(node("p", "ev-muted", `Showing ${count(revisions.length)} of ${count(page.total)} saved versions.`));
      detail.append(body);
    });
    return detail;
  }
  function quotaWindows(data) {
    const windows = Array.isArray(data.windows) ? [...data.windows] : [];
    for (const name of ["primary", "secondary"]) {
      if (data[name] && typeof data[name] === "object" && !Array.isArray(data[name])) windows.push({ ...data[name], name });
    }
    return windows;
  }
  function quotaName(window) {
    const name = knownLabel(windowNames, window.name), minutes = window.window_minutes;
    // Equal durations can describe different allowance buckets. Keep the
    // provider's extra-usage distinction instead of showing two "7-day" gauges.
    if (Object.prototype.hasOwnProperty.call(windowNames, window.name)) return name;
    if (!known(minutes) || minutes === 0) return name;
    const duration = minutes % 1440 === 0 ? `${minutes / 1440}-day window` : minutes % 60 === 0 ? `${minutes / 60}-hour window` : `${minutes}-minute window`;
    return `${name} · ${duration}`;
  }

  function summaryItem(item, index) {
    const key = `summary:${item.summary_key || `${item.session_id}:${item.timestamp}:${index}`}`;
    const detail = node("details", "ev-item"); detail.dataset.evidenceKey = key;
    const heading = node("summary", "ev-item-summary"), topline = node("div", "ev-topline"), state = status(item);
    const isIncluded = item.status === "reconciled";
    topline.append(time(item.timestamp), node("span", `ev-state ev-state-${state[1]}`, state[0]));
    const byline = node("div", "ev-byline", `${knownLabel(providerNames, item.provider)} · ${knownLabel(surfaceNames, item.surface)}`);
    const metrics = node("div", "ev-metrics");
    const main = node("span"); main.append(node("span", "ev-metric-label", "Main turn "), node("strong", "", tokenText(nativeUsage(item.usage).total_tokens)));
    const matched = node("span", "ev-muted", `${count(item.matched_message_count)} matched ${item.matched_message_count === 1 ? "request" : "requests"}`);
    metrics.append(main, matched);
    if (isIncluded) metrics.append(node("span", "ev-included", `${tokenText(object(item.remainder).total_tokens)} remainder included`));
    heading.append(topline, byline, metrics);
    const previewModels = Object.entries(object(item.model_usage));
    if (previewModels.length) {
      const preview = node("div", "ev-broader-preview");
      preview.append(node("span", "", "Saved model totals · "));
      preview.append(node("span", "", previewModels.slice(0, 2).map(([model, usage]) => `${model}: ${tokenText(nativeUsage(usage).total_tokens)}`).join(" · ")));
      preview.append(node("span", "", `${previewModels.length > 2 ? ` · ${previewModels.length - 2} more in details` : ""} · Not added`));
      heading.append(preview);
    }
    detail.append(heading);
    const body = node("div", "ev-item-body");
    body.append(node("p", `ev-reason${state[1] === "review" ? " ev-review-copy" : ""}`, text(item.reason, state[2]).replace(/_/g, " ")));
    if (!isIncluded) body.append(node("p", "ev-muted", "This summary adds no separate remainder to the daily report."));
    body.append(comparison(item, isIncluded));
    const models = broaderModels(item); if (models) body.append(models);
    const revisions = revisionHistory(item, key); if (revisions) body.append(revisions);
    body.append(provenance(item, key)); detail.append(body); return detail;
  }

  function quotaItem(item, index) {
    const key = `quota:${item.snapshot_key || `${item.provider}:${item.timestamp}:${index}`}`;
    const row = node("details", "ev-item ev-quota-item"); row.dataset.evidenceKey = key;
    const heading = node("summary", "ev-item-summary"), topline = node("div", "ev-topline"), data = object(item.data);
    topline.append(time(item.timestamp), node("span", "ev-provider", knownLabel(providerNames, item.provider)));
    heading.append(topline);
    const windows = quotaWindows(data);
    const windowsLine = node("div", "ev-window-line");
    for (const rawWindow of windows) {
      const window = object(rawWindow), value = window.used_percent;
      const valid = typeof value === "number" && Number.isFinite(value) && value >= 0;
      const windowNode = node("span", "ev-window");
      // Native window names remain visible. A general seven-day percentage
      // must never be renamed Fable, Opus, or another unsupported model label.
      windowNode.append(node("span", "", `${quotaName(window)} `), node("strong", "", valid ? `${percents.format(value)}% used` : "Unknown utilization"));
      windowNode.title = `Reset: ${formatDate(window.resets_at)}`; windowsLine.append(windowNode);
    }
    if (!windows.length) windowsLine.append(node("span", "ev-muted", "No window percentages recorded"));
    heading.append(windowsLine);
    if (typeof data.status === "string") heading.append(node("div", "ev-quota-status", `Status: ${human(data.status)}`));
    row.append(heading);
    const body = node("div", "ev-item-body");
    const facts = node("dl", "ev-facts");
    for (const rawWindow of windows) {
      const window = object(rawWindow);
      detailRow(facts, `${quotaName(window)} resets`, formatDate(window.resets_at));
    }
    for (const [field, label] of [["status", "Provider status"], ["overage_status", "Extra usage status"], ["rate_limit_type", "Limit type"]]) {
      if (typeof data[field] === "string") detailRow(facts, label, human(data[field]));
    }
    if (typeof data.is_using_overage === "boolean") detailRow(facts, "Using extra usage", data.is_using_overage ? "Yes" : "No");
    body.append(facts);
    body.append(node("p", "ev-muted", "Quota observations describe allowance utilization. They do not establish token consumption."));
    body.append(provenance(item, key, true)); row.append(body); return row;
  }

  function empty(copy) { const box = node("div", "ev-empty"); box.append(node("p", "", copy)); return box; }
  function paging(container, page, label, callback, statusNode) {
    const loaded = Array.isArray(page.items) ? page.items.length : 0;
    const total = known(page.total) ? page.total : null;
    if (total !== null && loaded >= total) return;
    if (!loaded || typeof callback !== "function") return;
    const footer = node("div", "ev-pagination"), button = node("button", "button", `Load older ${label}`);
    button.type = "button";
    button.addEventListener("click", async () => {
      // The caller owns transport, filters, and pagination state. Keep one
      // request active per button and give a retry path if that callback fails.
      if (button.disabled) return;
      button.disabled = true; button.textContent = "Loading…";
      try { await callback(); }
      catch { if (statusNode) statusNode.textContent = `Could not load older ${label}. Try again.`; }
      finally { button.disabled = false; button.textContent = `Load older ${label}`; }
    });
    footer.append(button); container.append(footer);
  }
  function renderPanel(containerId, statusId, pageValue, callback, kind) {
    const container = document.getElementById(containerId), statusNode = document.getElementById(statusId);
    if (!container) return;
    const page = object(pageValue), items = sorted(page.items);
    // Polling and pagination replace the nodes. Retain expanded evidence by its
    // stable key so opening a source path does not collapse every thirty seconds.
    const open = new Set([...container.querySelectorAll("details[data-evidence-key]")].filter((detail) => detail.open).map((detail) => detail.dataset.evidenceKey));
    container.replaceChildren();
    container.classList.add("ev-list");
    if (statusNode) {
      statusNode.setAttribute("role", "status");
      statusNode.textContent = known(page.total) ? `${count(items.length)} of ${count(page.total)} ${kind === "summary" ? "summaries" : "observations"} shown` : `${count(items.length)} ${kind === "summary" ? "summaries" : "observations"} shown`;
    }
    if (!items.length) {
      container.append(empty(kind === "summary"
        ? "No saved run summaries yet. Complete a Claude Code or Cowork turn and keep the tracker running to capture available summaries."
        : "No saved quota observations yet. Keep the tracker running while using Claude; available local allowance observations will appear here."));
      return;
    }
    for (const [index, item] of items.entries()) container.append(kind === "summary" ? summaryItem(item, index) : quotaItem(item, index));
    for (const detail of container.querySelectorAll("details[data-evidence-key]")) detail.open = open.has(detail.dataset.evidenceKey);
    paging(container, page, kind === "summary" ? "summaries" : "observations", callback, statusNode);
  }

  window.UsageEvidence = Object.freeze({
    render({ summaries, quotaHistory, onMoreSummaries, onMoreQuota, timezone } = {}) {
      setTimezone(timezone);
      renderPanel("reconciliation-list", "reconciliation-status", summaries, onMoreSummaries, "summary");
      renderPanel("quota-history-list", "quota-history-status", quotaHistory, onMoreQuota, "quota");
    },
  });
})();
