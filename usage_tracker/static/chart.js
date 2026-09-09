"use strict";

/* The monthly chart has one evidence class at a time and four disjoint token
   categories. Input here means uncached input, so cache reads/writes must never
   be added to the inclusive input field. All local-data strings use textContent.
   The renderer is independent of fetching and filtering so the daily report and
   annual chart can share filters without sharing their date windows. */
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const LONG_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const compactFormat = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
  const validCount = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const exact = (value) => validCount(value) ? numberFormat.format(value) : "Unavailable";
  const CATEGORIES = [
    { field: "uncached_input_tokens", name: "Input · uncached", key: "input" },
    { field: "output_tokens", name: "Output", key: "output" },
    { field: "cache_creation_tokens", name: "Cache write", key: "cache-write" },
    { field: "cached_input_tokens", name: "Cache read", key: "cache-read" }
  ];
  let nextId = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function svgElement(tag, attributes = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // Null and missing fields are deliberately not coerced to zero. A known
  // subtotal can still be partial when some events omit the category entirely.
  function categoryValue(aggregate, category) {
    return {
      ...category,
      value: validCount(aggregate[category.field]) ? aggregate[category.field] : null,
      partial: aggregate.unknown_fields?.[category.field] > 0
    };
  }

  function normalizeMonth(row, year, index, quality) {
    const aggregate = row?.[quality] || {};
    const definitions = quality === "estimated"
      ? [{ field: "input_tokens", name: "Input text", key: "input" }, { field: "output_tokens", name: "Output text", key: "output" }]
      : CATEGORIES;
    const categories = definitions.map((category) => categoryValue(aggregate, category));
    // Prefer the aggregate, falling back only when the field is absent. An
    // explicitly unknown aggregate total must not become a known series total.
    const rawTotal = Object.hasOwn(aggregate, "total_tokens") ? aggregate.total_tokens : row?.[`${quality}_tokens`];
    const total = validCount(rawTotal) ? rawTotal : null;
    const events = validCount(aggregate.events) ? aggregate.events : row?.[`${quality}_events`];
    const noEvents = events === 0 || (events === undefined && total === null && categories.every((category) => category.value === null));
    const partialTotal = aggregate.unknown_total_events > 0 || aggregate.unknown_fields?.total_tokens > 0;
    const categorySum = categories.reduce((sum, category) => sum + (category.value ?? 0), 0);
    const inconsistent = total !== null && categorySum > total;
    const unclassified = total === null ? null : inconsistent ? total : total - categorySum;
    const partialCategories = inconsistent || categories.some((category) => category.partial || category.value === null) || (unclassified ?? 0) > 0;
    const monthKey = `${year}-${String(index + 1).padStart(2, "0")}`;
    return {
      monthKey, label: `${LONG_MONTHS[index]} ${year}`, shortLabel: MONTHS[index],
      total, events, categories, noEvents, partialTotal, partialCategories,
      inconsistent, unclassified,
      // If categories exceed the available total they cannot form a truthful
      // partition. Preserve the measured total in gray instead of rescaling or
      // clipping category values to make the drawing appear consistent.
      segments: total === null || noEvents ? [] : inconsistent
        ? [{ name: "Unclassified total", key: "unknown", value: total, partial: true }]
        : [...categories.filter((category) => category.value !== null && category.value > 0),
          ...(unclassified > 0 ? [{ name: "Unclassified", key: "unknown", value: unclassified, partial: true }] : [])]
    };
  }

  function valueLabel(category) {
    return category.value === null ? "Unavailable" : `${exact(category.value)}${category.partial ? " (partial)" : ""}`;
  }

  function monthDescription(month) {
    if (month.noEvents) return `${month.label}: no recorded events. This is a gap, not zero usage.`;
    const counts = month.categories.map((category) => `${category.name}: ${valueLabel(category)}`).join("; ");
    const total = `Total: ${exact(month.total)}${month.partialTotal ? " (partial)" : ""}`;
    const note = month.inconsistent
      ? ". Categories exceed the available total; the bar shows the total as unclassified."
      : month.unclassified > 0 ? `; Unclassified: ${exact(month.unclassified)}.` : ".";
    return `${month.label}. ${counts}; ${total}${note}`;
  }

  function renderTable(body, months, quality) {
    if (!body) return;
    const rows = months.map((month) => {
      const row = element("tr", month.noEvents ? "monthly-data-gap" : "");
      const heading = element("th", "monthly-data-month", month.label);
      heading.scope = "row";
      row.append(heading);
      for (let index = 0; index < 4; index += 1) {
        const category = month.categories[index];
        const unavailable = !category || month.noEvents || category.value === null;
        const cell = element("td", `number monthly-data-${CATEGORIES[index].key}`, unavailable ? "—" : `${exact(category.value)}${category.partial ? " *" : ""}`);
        cell.title = !category && quality === "estimated" ? "Cache usage is not reconstructed from visible-text exports."
          : month.noEvents ? "No recorded events; this is not a measured zero."
          : valueLabel(category);
        row.append(cell);
      }
      const total = element("td", "number total-column", month.noEvents || month.total === null ? "—" : `${exact(month.total)}${month.partialTotal ? " *" : ""}`);
      total.title = monthDescription(month);
      row.append(total);
      return row;
    });
    body.replaceChildren(...rows);
  }

  // Rounded tick steps keep sparse annual data readable without giving large
  // counts arbitrary precision. Five intervals cover even a single small month.
  function scaleFor(maximum) {
    if (!(maximum > 0)) return { step: 1, ceiling: 5 };
    const roughStep = maximum / 5;
    const magnitude = 10 ** Math.floor(Math.log10(roughStep));
    const fraction = roughStep / magnitude;
    const multiplier = [1, 2, 2.5, 5, 10].find((value) => value >= fraction) || 10;
    const step = Math.max(1, multiplier * magnitude);
    return { step, ceiling: Math.ceil(maximum / step) * step };
  }

  function render({ container, tableBody, report, quality = "reported", onMonthSelect }) {
    if (!container) return;
    quality = quality === "estimated" ? "estimated" : "reported";
    const series = Array.isArray(report?.series) ? report.series : [];
    const suppliedYear = Number(report?.period?.year || series[0]?.date?.slice(0, 4));
    const year = Number.isInteger(suppliedYear) && suppliedYear >= 1 && suppliedYear <= 9999 ? suppliedYear : new Date().getFullYear();
    const rowsByMonth = new Map(series.map((row) => [String(row.date || "").slice(0, 7), row]));
    const months = MONTHS.map((_, index) => normalizeMonth(rowsByMonth.get(`${year}-${String(index + 1).padStart(2, "0")}`), year, index, quality));
    renderTable(tableBody, months, quality);

    container.classList.add("monthly-chart");
    const legend = element("div", "monthly-legend");
    legend.setAttribute("aria-label", "Token category legend");
    for (const category of months[0].categories) {
      const item = element("span", "monthly-legend-item");
      const swatch = element("span", `monthly-swatch monthly-fill-${category.key}`);
      swatch.setAttribute("aria-hidden", "true");
      item.append(swatch, element("span", "", category.name));
      legend.append(item);
    }
    if (months.some((month) => month.segments.some((segment) => segment.key === "unknown"))) {
      const item = element("span", "monthly-legend-item");
      const swatch = element("span", "monthly-swatch monthly-fill-unknown");
      swatch.setAttribute("aria-hidden", "true");
      item.append(swatch, element("span", "", "Unclassified"));
      legend.append(item);
    }
    legend.append(element("span", "monthly-legend-unit", quality === "estimated" ? "Visible-text estimates · tokens" : "Reported tokens"));

    const viewport = element("div", "monthly-chart-viewport");
    const tooltip = element("div", "monthly-tooltip");
    const instanceId = `monthly-chart-${++nextId}`;
    tooltip.id = `${instanceId}-tooltip`;
    tooltip.setAttribute("role", "tooltip");
    tooltip.hidden = true;
    const width = Math.max(760, container.clientWidth || 1000);
    const height = 490, left = 75, right = 22, top = 40, bottom = 51;
    const plotHeight = height - top - bottom;
    const plotWidth = width - left - right;
    const bucketWidth = plotWidth / 12;
    const barWidth = Math.min(68, bucketWidth * 0.61);
    const maximum = Math.max(0, ...months.filter((month) => !month.noEvents).map((month) => month.total ?? 0));
    const scale = scaleFor(maximum);
    const chart = svgElement("svg", {
      viewBox: `0 0 ${width} ${height}`, width, height, class: "monthly-chart-svg", role: "group",
      "aria-labelledby": `${instanceId}-title ${instanceId}-description`
    });
    chart.append(svgElement("title", { id: `${instanceId}-title` }, `${year} monthly ${quality === "estimated" ? "visible-text estimated" : "reported"} token usage`));
    chart.append(svgElement("desc", { id: `${instanceId}-description` }, `One bar per month. ${quality === "estimated" ? "Input and output visible-text estimates" : "Uncached input, output, cache writes, and cache reads"} stack without double counting. Dashes show months without observed totals. Exact values are in the chart data table. ${onMonthSelect ? "Activate a month to view its daily report." : ""}`));
    chart.append(svgElement("text", { x: left, y: 17, class: "monthly-axis-title" }, "TOKENS"));

    for (let value = 0; value <= scale.ceiling + scale.step / 100; value += scale.step) {
      const y = top + plotHeight - value / scale.ceiling * plotHeight;
      chart.append(svgElement("line", { x1: left, x2: width - right, y1: y, y2: y, class: "monthly-grid-line" }));
      chart.append(svgElement("text", { x: left - 13, y: y + 4, "text-anchor": "end", class: "monthly-axis-label" }, compactFormat.format(value)));
    }

    function hideTooltip() {
      tooltip.hidden = true;
    }

    function showTooltip(month, activeKey, event, target) {
      tooltip.replaceChildren(element("strong", "monthly-tooltip-heading", month.label));
      if (month.noEvents) tooltip.append(element("p", "monthly-tooltip-note", "No recorded events. This month is a gap, not zero usage."));
      else {
        const list = element("dl", "monthly-tooltip-values");
        for (const category of month.categories) {
          const row = element("div", category.key === activeKey ? "monthly-tooltip-active" : "");
          row.append(element("dt", "", category.name), element("dd", "", valueLabel(category)));
          list.append(row);
        }
        if (month.unclassified > 0) {
          const row = element("div", activeKey === "unknown" ? "monthly-tooltip-active" : "");
          row.append(element("dt", "", month.inconsistent ? "Unclassified total" : "Unclassified"), element("dd", "", exact(month.unclassified)));
          list.append(row);
        }
        const totalRow = element("div", "monthly-tooltip-total");
        totalRow.append(element("dt", "", "Month total"), element("dd", "", `${exact(month.total)}${month.partialTotal ? " (partial)" : ""}`));
        list.append(totalRow);
        tooltip.append(list);
        if (month.inconsistent) tooltip.append(element("p", "monthly-tooltip-note", "Category subtotals exceed the available total. The bar preserves that total in gray."));
        else if (month.partialCategories || month.partialTotal) tooltip.append(element("p", "monthly-tooltip-note", "Some events lack category or total counts. Partial values include known evidence only."));
      }
      if (onMonthSelect) tooltip.append(element("p", "monthly-tooltip-action", "Select month to view daily usage"));
      tooltip.hidden = false;
      // Position in the chart container, not the horizontally scrolling plot,
      // so an edge month cannot place its tooltip outside the visible card.
      const bounds = container.getBoundingClientRect();
      const targetBounds = target.getBoundingClientRect();
      const x = event && Number.isFinite(event.clientX) ? event.clientX - bounds.left : targetBounds.left + targetBounds.width / 2 - bounds.left;
      const y = event && Number.isFinite(event.clientY) ? event.clientY - bounds.top : targetBounds.top - bounds.top + 40;
      const tipWidth = tooltip.offsetWidth || 290;
      const tipHeight = tooltip.offsetHeight || 215;
      tooltip.style.left = `${Math.max(8, Math.min(x + 16, container.clientWidth - tipWidth - 8))}px`;
      tooltip.style.top = `${Math.max(8, Math.min(y - tipHeight - 12, container.clientHeight - tipHeight - 8))}px`;
    }

    months.forEach((month, index) => {
      const center = left + bucketWidth * (index + 0.5);
      const baseline = top + plotHeight;
      const group = svgElement("g", {
        class: `monthly-month${month.noEvents ? " monthly-month-empty" : ""}`,
        tabindex: 0, focusable: "true", role: onMonthSelect ? "button" : "group",
        "aria-label": `${monthDescription(month)}${onMonthSelect ? " View daily usage." : ""}`,
        "data-month": month.monthKey
      });
      // A full-height transparent target keeps tiny or empty months selectable.
      // It sits behind the segments so pointer tooltips can identify categories.
      group.append(svgElement("rect", {
        x: center - bucketWidth / 2 + 3, y: top - 11, width: bucketWidth - 6, height: plotHeight + 44,
        rx: 6, class: "monthly-month-target"
      }));
      let accumulated = 0;
      for (const segment of month.segments) {
        if (!(segment.value > 0)) continue;
        const segmentHeight = segment.value / scale.ceiling * plotHeight;
        const bar = svgElement("rect", {
          x: center - barWidth / 2, y: baseline - accumulated - segmentHeight,
          width: barWidth, height: segmentHeight,
          class: `monthly-segment monthly-fill-${segment.key}`,
          "data-category": segment.key, "data-value": segment.value
        });
        bar.append(svgElement("title", {}, `${month.label} · ${segment.name}: ${exact(segment.value)}${segment.partial ? " (partial)" : ""} tokens; month total: ${exact(month.total)}${month.partialTotal ? " (partial)" : ""}.`));
        group.append(bar);
        accumulated += segmentHeight;
      }
      if (month.total !== null && !month.noEvents) {
        const y = baseline - month.total / scale.ceiling * plotHeight;
        group.append(svgElement("text", { x: center, y: y - 11, "text-anchor": "middle", class: "monthly-bar-total" }, `${compactFormat.format(month.total)}${month.partialTotal ? "*" : ""}`));
        if (month.total === 0) group.append(svgElement("circle", { cx: center, cy: baseline, r: 3, class: "monthly-zero-marker" }));
      } else {
        group.append(svgElement("line", { x1: center - 8, x2: center + 8, y1: baseline - 2, y2: baseline - 2, class: "monthly-gap-marker" }));
        if (!month.noEvents) group.append(svgElement("text", { x: center, y: baseline - 13, "text-anchor": "middle", class: "monthly-unknown-label" }, "Unknown"));
      }
      group.append(svgElement("text", { x: center, y: height - 19, "text-anchor": "middle", class: "monthly-month-label" }, month.shortLabel));
      group.addEventListener("pointermove", (event) => showTooltip(month, event.target.getAttribute?.("data-category"), event, group));
      group.addEventListener("pointerleave", hideTooltip);
      group.addEventListener("focus", () => showTooltip(month, null, null, group));
      group.addEventListener("blur", hideTooltip);
      group.addEventListener("keydown", (event) => {
        if (event.key === "Escape") hideTooltip();
        if (onMonthSelect && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          hideTooltip();
          onMonthSelect(month.monthKey);
        }
      });
      if (onMonthSelect) group.addEventListener("click", () => {
        hideTooltip();
        onMonthSelect(month.monthKey);
      });
      chart.append(group);
    });

    viewport.append(chart);
    viewport.addEventListener("scroll", hideTooltip, { passive: true });
    const note = element("p", "monthly-chart-note", quality === "estimated"
      ? "Visible text only; cache usage is unavailable. A dash means no recorded total. Select a month for its daily report."
      : "Input excludes cache reads and writes; output includes reasoning. A dash means no recorded total. Select a month for its daily report.");
    if (months.some((month) => !month.noEvents && (month.partialCategories || month.partialTotal))) {
      note.append(document.createTextNode(" * Partial counts; gray preserves usage whose category cannot be established."));
    }
    container.replaceChildren(legend, viewport, note, tooltip);
  }

  window.UsageChart = Object.freeze({ render });
})();
