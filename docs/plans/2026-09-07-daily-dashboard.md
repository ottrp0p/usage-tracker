# Daily report default and larger chart — 2026-09-07

User request: make the default dashboard resemble the attached ccusage daily
table and provide a larger monthly bar graph. The reference is visual guidance,
not an instruction to import its values or infer billing prices.

## Implementation

1. Add a calendar-month report selection and day/provider-family breakdowns to
   the existing read-only API. Keep CLI ranges backward compatible.
2. Make a dense daily table the primary view: Date, App, Models, uncached Input,
   Output, Cache write, Cache read, Total. Each date has an All subtotal, indented
   Claude/Codex (or regular-chat) rows, and a period total. Keep estimates separate.
3. Expand the full-width chart substantially. The user selected one bar per month
   across the year. The daily table selects a calendar month; the chart selects
   a calendar year and always opens with monthly buckets.
4. Keep coverage and account evidence available below the primary report, with
   compact summary information instead of large summary cards dominating the page.
5. Test local-calendar month boundaries, token category sums/unknowns, filters,
   monthly grouping, and existing reports. Verify the live layout and restart
   the login service to load the new report API.

## Accounting

Normalized stored input includes cache reads and creation. The ccusage-style
table's Input is uncached input = inclusive input - cache read - cache creation,
computed per event only when those components are known. Never combine estimated
visible text with reported tokens or fabricate cost values from the screenshot.

The directory remains without Git. Existing source logs and collected data are
preserved. Changes are limited to reporting, dashboard presentation, and docs.

## Validation

Append observed outcomes and any revised decisions here when work is complete.

## Additional direction

- The user explicitly requested clearer separation of cache and input/output.
  Both the table and the monthly chart will distinguish uncached input, output,
  cache writes, and cache reads. Monthly bars will stack these disjoint categories
  with an explanatory legend and exact values. Unknown category allocations must
  remain explicit, and visible-text estimates stay a separate selectable series.

## Completed validation

- Added calendar month/year report windows, daily app-family rows, per-event
  uncached input, and full category aggregates for monthly chart buckets.
- Default presentation now matches the reference's daily All/app/model structure,
  with compact totals, separate cache/I/O columns, and a month-total footer.
- The annual chart has 12 monthly positions, 490px height, four category colors,
  exact-count tooltips, explicit unknown allocation, and month navigation.
- Existing reports remain compatible; CLI gained --month/--year/--group month.
- 119 Python tests pass (13 new calendar/accounting tests), both JavaScript assets
  pass syntax checks, and synthetic chart rendering/keyboard cases pass.
- Live browser checks verified daily rows, category arithmetic, 12 monthly bars,
  bar-to-day navigation, empty estimates, Reset, and responsive table layout.
  No browser console warnings/errors were observed. Temporary viewport reset.
- The existing login service was restarted to load the updated local API.
  Source logs, stored token evidence, and Git state were unchanged.

## Date-order adjustment

User requested newest dates at the top. Sort daily display groups descending by
their ISO date, keeping each day's app rows together and the month total last.
Verify JavaScript syntax and the first/last dates in the live table.
