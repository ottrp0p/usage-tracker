# Usage Tracker

Local usage tracking for OpenAI and Anthropic across chat, coding apps, and CLIs.
One Python service collects logs into SQLite and serves a dashboard at
**[localhost:8787](http://localhost:8787)**. Reported token counts, visible-text
estimates, and account quota evidence remain separate.

The dashboard opens with a **ccusage-style daily report** for the selected month:
daily All totals, Claude/Codex subrows, model lists, and a month total. Uncached
input, output, cache writes, and cache reads each have their own column. A large
annual chart shows **one stacked bar per month** using those same categories;
select a bar to open its daily report. Reported usage and text estimates are
separate selections.

Requires Python **3.11+**. Core collection has no Python runtime dependencies.
Claude remote Cowork cache decoding additionally uses a local **Node.js** runtime
and its built-in modules (no npm packages). No API keys, network requests, or cloud
storage are used. macOS is the initial target; foreground operation also works on
Linux. Source logs, application caches, and exports are read without modification.

## Run

From this directory:

```sh
python3 -m usage_tracker collect
python3 -m usage_tracker report
python3 -m usage_tracker serve
```

Open [localhost:8787](http://localhost:8787). The service collects every 30 seconds;
the dashboard refreshes every 30 seconds. Refresh in the browser rereads the
database. `Ctrl-C` stops foreground operation. **This project always deploys on
8787.** Redeployment restarts the project's own service. If an unrelated process
occupies 8787, deployment reports the conflict. The server only binds to loopback;
it rejects external Host headers and provides no write API.

For ongoing collection at login:

```sh
python3 -m usage_tracker service install
python3 -m usage_tracker service status
python3 -m usage_tracker service uninstall
```

Installation is also the redeployment command: it unloads the existing project
LaunchAgent, verifies TCP **8787** can be bound, and starts the updated service
there. It does not terminate unrelated processes or fall back to another port.
The user corrected the mistaken 8447 instruction; Usage Tracker belongs on 8787.

Installation starts the service immediately and registers a macOS LaunchAgent.
Uninstall stops it and removes the generated LaunchAgent; **usage data stays**.
The login service uses the current Python executable and this source directory,
so reinstall it after moving the project or replacing Python. Don't run a second
foreground server on the same port while the login service is active.

Optional: `python3 -m pip install -e .` provides the `usage-tracker` command.

## What is tracked

| Source | Collection | Meaning |
| --- | --- | --- |
| Codex desktop, CLI, IDE, subagents | Current and archived local session JSONL | Reported cumulative telemetry reconciled into increments; copied histories and resets handled |
| Claude Code | Local project and nested agent JSONL | Provider message usage; repeated observations merge and final usage wins |
| Claude desktop / Cowork agents | Audit and nested project JSONL | Same-message audit/transcript overlap deduplicated |
| Claude remote Cowork | Desktop IndexedDB blobs and inline LevelDB values | Cached provider-message usage; repeated snapshots deduplicate; history and output may be incomplete |
| Claude completed turns | Persistent result-summary revisions | Verified main-turn summaries fill only missing message usage; broader model totals remain separate |
| ChatGPT and Claude regular chats | Explicit JSON or ZIP export import | **Visible-text estimate**, `ceil(UTF-8 bytes / 4)` per message; not a tokenizer or historical bill |
| Account quotas/summaries | Quota observations in logs; explicit account snapshot import | Separate evidence, never added to token totals |

Regular chat history is **not automatically accessible through agent logs**.
Import an export to see those estimates. Other devices and missing/deleted logs
are outside local coverage. Surface/model attribution is `unknown` when evidence
doesn't establish it. A missing category is unknown; observed zero remains zero.
Remote Cowork snapshots often contain only the latest 100 events and provisional
output counts. Collection retains the available observations, but cannot recover
remote history that never reached a local log or cache. A successful scan is not
a claim of complete account coverage.

OpenAI input already includes cached input; output includes reasoning. Anthropic
inclusive input is ordinary input + cache read + cache creation. The dashboard
shows these subsets without adding them a second time. Counts from different
tokenizers are operational measures, not identical units of work.

Reported telemetry is not guaranteed to match provider billing. Incomplete
cumulative history may have an unknown date and appears only in all-time totals.
Claude provisional usage can change when final records arrive. Quota snapshots
are timestamped observations, not live account queries. Costs and authenticated
account polling are intentionally outside v1.

New and changed result summaries and quota observations are retained on each
scan. **Saved summaries & reconciliation** compares summaries with messages,
shows any verified adjustment, and preserves unresolved larger figures for
inspection. **Quota history** records allowance observations and reset windows.
Repeated pulls do not duplicate usage; late message records shrink the relevant
summary adjustment. See [how reconciliation works](docs/reconciliation.md).

## Reports and imports

```sh
python3 -m usage_tracker report --days 7 --refresh
python3 -m usage_tracker report --days 0 --group week
python3 -m usage_tracker report --month 2026-09 --json
python3 -m usage_tracker report --year 2026 --group month
python3 -m usage_tracker report --provider openai --surface codex_desktop
python3 -m usage_tracker report --timezone America/Los_Angeles --json
python3 -m usage_tracker import /absolute/path/to/chatgpt-export.zip
python3 -m usage_tracker import /absolute/path/to/conversations.json --provider claude
python3 -m usage_tracker import-snapshot /absolute/path/to/snapshot.json --provider openai
```

Reports use local calendar days and Monday-based weeks. `--days 0` includes all
dates and undated records. The JSON report includes provider/app/model breakdowns,
category completeness, source coverage, and separate account snapshots.
`--month YYYY-MM` (or `current`) and `--year YYYY` select complete calendar windows;
they are mutually exclusive and override `--days`.

Imports use stable message IDs, so repeated exports do not duplicate estimates;
changed text replaces that message's estimate. ChatGPT imports include **all
exported branches**, including regenerated alternatives. Removed messages in a
later export do not delete previously stored history. Text, titles, attachments,
and tool bodies are never saved to the usage database.

See [supported import formats](docs/import-formats.md),
[Codex reconciliation details](docs/codex-formats.md),
[Claude accounting details](docs/claude-formats.md), and
[dashboard behavior](docs/dashboard.md). Summary revisions and quota history are
also available through the [read-only evidence endpoints](docs/reconciliation.md#inspecting-saved-evidence).

## Configuration and local state

macOS data defaults to `~/Library/Application Support/UsageTracker/`. Override it
with `USAGE_TRACKER_DATA_DIR` or put `--data-dir /absolute/path` **before** the
subcommand. The directory holds `usage.sqlite3`, a collector lock, optional
`config.json`, and service logs. Private file modes are used by default.

`python3 -m usage_tracker sources` lists the actual configured paths. Defaults:

- `$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions`, defaulting to `~/.codex`.
- Each comma-separated `$CLAUDE_CONFIG_DIR/projects`, defaulting to `~/.claude`.
- `~/Library/Application Support/Claude/local-agent-mode-sessions`.
- Claude's `IndexedDB/https_claude.ai_0.indexeddb.blob` and sibling
  `https_claude.ai_0.indexeddb.leveldb` under `~/Library/Application Support/Claude`.

The cache decoder finds Node in standard Homebrew/system locations or `PATH`.
Set `CLAUDE_CACHE_NODE=/absolute/path/to/node` for another installation; reinstall
the login service to carry that override into launchd. Missing Node or unsupported
cache formats produce source warnings and are retried, while JSONL collection
continues. A custom cache root uses source kind `claude_cache`.

Create `config.json` in the data directory to extend or replace those roots:

```json
{
  "include_defaults": true,
  "sources": [
    {"name": "Extra Claude", "kind": "claude", "path": "/path/to/projects", "surface": "unknown"}
  ]
}
```

Or use `--config /absolute/path/config.json` before the subcommand. The service
reloads source configuration on each scan. Use `include_defaults: false` for
fully isolated inputs. [An example is included](docs/config.example.json).

Offsets and parsed evidence commit together. A partially written final JSONL line
waits for its newline. File replacement/truncation resets the reader; stable
identities prevent duplicate imports. Prefix and checkpoint-tail fingerprints
detect ordinary rewrites. Changes strictly in an old file's middle, outside
those fingerprints, require a fresh data directory to backfill again.

Source coverage lists missing paths, read failures, malformed records, and last
successful scans separately from the latest recorded usage. The daily report
identifies provisional counts and incomplete cached history. If macOS blocks
Desktop or application data access, grant the
running Python/service appropriate access in System Settings, then restart it.
Inspect `service-error.log` and `service status` for launch failures.

## Development

```sh
python3 -m unittest discover -v
node --check usage_tracker/static/app.js
node --check usage_tracker/static/chart.js
```

Accounting and integration fixtures cover duplicates, forks, cumulative resets,
partial writes, final/provisional usage, crash checkpoints, timezone boundaries,
import revisions, HTTP boundaries, and launchd failure handling. They use
temporary directories and synthetic data.

See the [Git initialization plan](docs/plans/2026-09-09-git-initialization.md)
and the public technical notes in `docs/`. Historical personal audit notes are
retained locally outside Git. Write plans in `docs/plans/` before implementation.
Work on a development branch and use human PR control. Never push to main/master.
