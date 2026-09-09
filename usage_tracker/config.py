"""Small, explicit local configuration; no credential discovery is performed."""

from dataclasses import dataclass
from pathlib import Path
import json
import os
import sys


# This project's fixed deployment address. Internal HTTP tests may request an
# ephemeral socket, but the CLI/service never select a different project port.
DEFAULT_PORT = 8787


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    path: Path
    surface: str


def default_data_dir() -> Path:
    if os.environ.get("USAGE_TRACKER_DATA_DIR"):
        return Path(os.environ["USAGE_TRACKER_DATA_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/UsageTracker"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "usage-tracker"


def default_sources() -> list[Source]:
    codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    sources = [Source("Codex sessions", "codex", codex / "sessions", "unknown"),
               Source("Codex archive", "codex", codex / "archived_sessions", "unknown")]
    # CLAUDE_CONFIG_DIR accepts multiple configuration homes, not transcript roots.
    homes = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")).split(",")
    for i, home in enumerate(homes):
        if home.strip():
            sources.append(Source(f"Claude Code {i + 1}", "claude", Path(home.strip()).expanduser() / "projects", "claude_code"))
    sources.append(Source("Claude desktop agents", "claude", Path.home() / "Library/Application Support/Claude/local-agent-mode-sessions", "claude_desktop_agent"))
    # Remote Cowork runs do not necessarily create local agent JSONL. The
    # desktop app caches a window of their provider events in IndexedDB blobs.
    # Keep this source separate so its partial-history limits stay visible.
    sources.append(Source("Claude remote Cowork cache", "claude_cache", Path.home() / "Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.blob", "claude_desktop_agent"))
    sources.append(Source("Claude remote Cowork inline cache", "claude_cache", Path.home() / "Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb", "claude_desktop_agent"))
    return sources


def load_sources(config_path: Path | None = None) -> list[Source]:
    if config_path is None or not config_path.exists():
        return default_sources()
    data = json.loads(config_path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("sources", []), list):
        raise ValueError("Configuration must be an object with a sources list")
    if "include_defaults" in data and not isinstance(data["include_defaults"], bool):
        raise ValueError("include_defaults must be true or false")
    sources = default_sources() if data.get("include_defaults", True) else []
    for i, item in enumerate(data.get("sources", [])):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Each source must include a string path")
        kind = item.get("kind")
        if kind not in ("codex", "claude", "claude_cache"):
            raise ValueError("Source kind must be codex, claude, or claude_cache")
        sources.append(Source(item.get("name", f"Custom source {i + 1}"), kind,
                              Path(item["path"]).expanduser().resolve(), item.get("surface", "unknown")))
    if any(not isinstance(source.name, str) or not source.name.strip() or not isinstance(source.surface, str) for source in sources):
        raise ValueError("Source names and surfaces must be nonempty strings")
    if len({source.name for source in sources}) != len(sources):
        raise ValueError("Source names must be unique")
    return sources
