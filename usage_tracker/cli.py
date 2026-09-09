"""Command-line entry points; all collection and import operations remain local."""

from pathlib import Path
import argparse
import json
import os
import sys

from usage_tracker import __version__
from usage_tracker.config import DEFAULT_PORT, default_data_dir, load_sources
from usage_tracker.ingest import collect
from usage_tracker.reports import format_report, report
from usage_tracker.storage import Store, now


def _period_arguments(parser):
    parser.add_argument("--days", type=int, default=30, help="Days including today; 0 means all time")
    calendar_period = parser.add_mutually_exclusive_group()
    calendar_period.add_argument("--month", help="Calendar month YYYY-MM or current; overrides --days")
    calendar_period.add_argument("--year", type=int, help="Calendar year; overrides --days")
    parser.add_argument("--provider", default="all")
    parser.add_argument("--surface", default="all")
    parser.add_argument("--model", default="all")
    parser.add_argument("--group", choices=["day", "week", "month"], default="day")
    parser.add_argument("--timezone", dest="timezone_name", help="IANA timezone; defaults to the system timezone")


def parser():
    root = argparse.ArgumentParser(prog="usage-tracker", description="Local usage reports for Codex, Claude Code, desktop agents, and chat exports.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-dir", type=Path, default=default_data_dir())
    root.add_argument("--config", type=Path, help="Source configuration JSON (default: DATA_DIR/config.json)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("collect", help="Backfill logs, or collect appended records using saved offsets")
    report_parser = commands.add_parser("report", help="Show daily/weekly/monthly usage and source coverage")
    _period_arguments(report_parser)
    report_parser.add_argument("--json", action="store_true")
    report_parser.add_argument("--refresh", action="store_true", help="Collect before reading the report")
    commands.add_parser("sources", help="List configured source paths and availability")
    importer = commands.add_parser("import", help="Import ChatGPT/Claude conversation JSON or ZIP as estimates")
    importer.add_argument("path", type=Path)
    importer.add_argument("--provider", default="auto", choices=["auto", "openai", "chatgpt", "anthropic", "claude"])
    snapshot = commands.add_parser("import-snapshot", help="Import a separate, provider account summary/quota JSON")
    snapshot.add_argument("path", type=Path)
    snapshot.add_argument("--provider", required=True, choices=["openai", "anthropic"])
    serve_parser = commands.add_parser("serve", help="Run the local dashboard and periodic collector")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT, choices=[DEFAULT_PORT],
                              help=f"Project deployment port (fixed at {DEFAULT_PORT})")
    serve_parser.add_argument("--interval", type=int, default=30)
    serve_parser.add_argument("--timezone", dest="timezone_name")
    service = commands.add_parser("service", help="Manage the macOS login service")
    service.add_argument("action", choices=["install", "status", "uninstall"])
    service.add_argument("--port", type=int, default=DEFAULT_PORT, choices=[DEFAULT_PORT],
                         help=f"Project deployment port (fixed at {DEFAULT_PORT})")
    service.add_argument("--interval", type=int, default=30)
    service.add_argument("--timezone", dest="timezone_name")
    return root


def main(argv=None) -> int:
    # Apply privacy defaults before SQLite WAL files or launchd logs are created.
    os.umask(0o077)
    args = parser().parse_args(argv)
    args.data_dir = args.data_dir.expanduser().resolve()
    config = args.config.expanduser().resolve() if args.config else args.data_dir / "config.json"
    try:
        if args.config is not None and not config.is_file():
            raise ValueError(f"Explicit source configuration does not exist: {config}")
        if args.command == "sources":
            for source in load_sources(config):
                print(f"{source.name}: {'present' if source.path.exists() else 'missing'} · {source.path}")
            return 0
        if args.command == "serve":
            from usage_tracker.server import serve
            serve(args.data_dir, host=args.host, port=args.port, interval=args.interval,
                  config_path=config, timezone_name=args.timezone_name)
            return 0
        if args.command == "service":
            from usage_tracker.service import manage_service
            print(manage_service(args.action, args.data_dir, port=args.port, interval=args.interval,
                                 config_path=config, timezone_name=args.timezone_name))
            return 0
        with Store(args.data_dir) as store:
            if args.command == "collect":
                outcome = collect(store, load_sources(config))
                print(json.dumps(outcome, indent=2))
                return 1 if outcome["errors"] else 0
            if args.command == "report":
                if args.refresh:
                    collect(store, load_sources(config))
                outcome = report(store, days=args.days, provider=args.provider, surface=args.surface,
                                 model=args.model, group=args.group, timezone_name=args.timezone_name,
                                 month=args.month, year=args.year)
                print(json.dumps(outcome, indent=2) if args.json else format_report(outcome))
            elif args.command in ("import", "import-snapshot"):
                from usage_tracker.importers import import_account_snapshot, import_export
                path = args.path.expanduser().resolve()
                result = (import_export if args.command == "import" else import_account_snapshot)(path, provider=args.provider)
                with store.conn:
                    changed = store.ingest(result, str(path))
                    from usage_tracker.models import stable_key
                    source_name = f"Import: {path.name} [{stable_key(str(path))[:8]}]"
                    stored_events = store.conn.execute("SELECT COUNT(*) FROM event_sources WHERE source_path=?", (str(path),)).fetchone()[0]
                    store.health(source=source_name, path=str(path), status="partial" if result.warnings else "ok",
                                 files=1, events=stored_events, last_success=now(), warnings=result.warnings)
                print(json.dumps({"changed_events": changed, "snapshots": len(result.snapshots), "warnings": result.warnings}, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"usage-tracker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
