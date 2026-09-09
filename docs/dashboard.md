# Local dashboard

The dashboard is served by the local Python service at `http://localhost:8787`.
The corrected September 9 deployment rule restores 8787: redeploy via
`python3 -m usage_tracker service install`, which restarts only this project's
service and reports conflicts with unrelated listeners.
Its three assets live in `usage_tracker/static/`; there are no external scripts,
fonts, images, frameworks, analytics, or build steps.

- Reports refresh every 30 seconds while the page is visible. Refresh and filter
  changes make read-only requests; the server's collector has its own schedule.
- Reported tokens and visible-text estimates are separate cards, chart series,
  and table rows. No combined headline is computed.
- Null token counts display **Unknown**. A category with some missing event
  values has an asterisk and a partial-count explanation. Cached input and
  reasoning are subsets, not additional usage.
- Daily and weekly views use the report's calendar timezone. The chart has an
  expandable table for exact values, including unknowns. All time also exposes
  usage with unknown dates in totals; the report notes explain that limitation.
- Coverage shows configured source paths, statuses, counts, warnings, and last
  successful collection. Account and quota evidence remains independent of the
  usage filters and is never included in usage totals.
- Only adapter-defined `used_percent` values create quota bars. Claude `windows`
  and Codex `primary`/`secondary` snapshots are supported. Percentages above 100
  remain visible as text while the bar is capped; null remains unknown. Snapshot
  observation times and recorded reset times are shown without claiming live
  account state.
- Failed refreshes retain the previous report and identify its original view.
  First-load failures show a connection state without substituting zero usage.

All data-driven text uses DOM text nodes rather than interpreted HTML. Accessible
form labels, focus indicators, reduced-motion support, responsive layouts, and a
skip link are included.

## Validation — 2026-09-07

`node --check usage_tracker/static/app.js` passes. A temporary in-memory fixture
server verified the desktop and 390-pixel layouts, daily/weekly controls, empty
filters, unknown counts, service-outage recovery, both quota payload shapes,
percentage overflow, unknown quota windows, and object-shaped collection status.
Fixture data is illustrative and was not saved to the usage database.

Live-service verification at `http://localhost:8787` passed for all available
provider, surface, and model filters (18 selections), the 30-day daily report,
the all-time weekly report, the health endpoint, and all three packaged assets.
Health returned a completed scan with zero errors; live quota payload shapes
match the shapes verified with fixtures. Sparse all-time chart buckets retain
calendar gaps, and the chart-data table includes full dates for year clarity.

Live visual verification could not run because CUA reported no available browsers
after the fixture session ended. No live screenshot was captured, and the
fixture screenshot checks must not be described as verification of live totals.
The real service was left running; no personal browser tabs were changed.

## Daily default and annual chart — 2026-09-07 update

The daily table now leads the dashboard, replacing the large summary cards and
period-wide breakdown. A month selector controls its dates; each active date has
an All subtotal followed by Claude/Codex app rows and distinct model names.
Dates display newest first, with each day's app rows grouped beneath its total.
App details can be collapsed, and a sticky footer shows the selected month's total.
The table scrolls on narrow screens while preserving the date column.

Four disjoint reported categories appear in the summary, daily columns, and
annual chart: uncached input, output, cache writes, and cache reads. Uncached input
is computed per event only when all required counts are known and consistent;
partial aggregates are marked. Output continues to include reasoning. The chart
preserves unclassified usage in gray when a complete category split is unknown.

The annual chart is 490 pixels tall and has one stacked bar per calendar month,
January through December. Its year is independent of the table's month; changing
that month follows its year in the chart. Click or keyboard-activate a month to
open its daily report. The same provider/app/model filters apply to both windows.
The Evidence selector changes both to either reported usage or text estimates;
these are never combined. Exports cannot reconstruct cache usage.

Live validation passed: current-month All/Claude/Codex rows, all 12 annual buckets,
category totals matching the reported total, monthly-bar navigation to August,
empty estimates showing Unknown, and Reset restoring September/reporting defaults.
Browser console had no warnings or errors. The table was checked at the normal
narrow panel width and a temporary desktop breakpoint, with no page-wide overflow;
the viewport override was reset. Synthetic chart checks covered partial counts,
unknown totals, zero-vs-gap markers, conflicting components, and keyboard selection.
All 119 Python tests passed, along with syntax checks for both JavaScript assets.
This update supersedes the earlier default-card and daily/weekly chart behavior.

## Source freshness and partial Cowork evidence — September 7 update

Each source now shows **Last scan** separately from **Latest usage**, derived
from event provenance rather than file modification times. A recent successful
scan cannot establish that every application surface was covered. Remote Cowork
cache sources explain partial history and provisional output counts.

When the selected daily report includes provisional counts or partial cached
history, a note beside the accounting explanation gives the affected event
counts and the summary reads **Observed this month**. These evidence limits are
separate from a missing token category, which continues to use Unknown or `*`.

## Saved summaries and quota history

Two expandable panels retain the existing daily-table/annual-chart default.
**Saved summaries & reconciliation** shows main-turn usage, matched requests,
any included remainder, broader per-model totals, capture timestamps, source
files, and saved revision history. Scope/conflict reasons remain visible.
**Quota history** shows timestamped native allowance windows and reset times,
with first/last capture times distinguished from the source observation time.
Both lists page through older records and preserve expanded cards on refresh.
The evidence's presentation timezone follows the report's timezone.
