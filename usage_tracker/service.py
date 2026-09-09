"""Reversible macOS launchd setup. Uninstall removes only our generated plist."""

from pathlib import Path
import os
import plistlib
import subprocess
import sys
import time

from usage_tracker.config import DEFAULT_PORT

LABEL = "local.usage-tracker"


def plist_payload(data_dir: Path, *, port=DEFAULT_PORT, interval=30, config_path=None, timezone_name=None) -> dict:
    if port != DEFAULT_PORT:
        raise ValueError(f"Usage Tracker must deploy on TCP {DEFAULT_PORT}; alternate ports are not supported")
    root = Path(__file__).resolve().parent.parent
    args = [sys.executable, "-m", "usage_tracker", "--data-dir", str(data_dir.resolve())]
    if config_path and config_path.exists():
        args.extend(["--config", str(config_path.resolve())])
    args.extend(["serve", "--port", str(port), "--interval", str(interval)])
    if timezone_name:
        args.extend(["--timezone", timezone_name])
    environment = {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    # launchd doesn't inherit a shell's configured source roots. Carry only the
    # non-secret path/timezone settings needed to reproduce the installer's view.
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CACHE_NODE", "TZ"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return {"Label": LABEL, "ProgramArguments": args, "WorkingDirectory": str(root),
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15, "Umask": 0o077,
            "StandardOutPath": str(data_dir.resolve() / "service.log"),
            "StandardErrorPath": str(data_dir.resolve() / "service-error.log"),
            "EnvironmentVariables": environment}


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _unload_job(target: str, failure_message: str) -> None:
    """Stop only the named Usage Tracker job and verify launchd released it."""
    if launchctl("print", target).returncode:
        return
    stopped = launchctl("bootout", target)
    if stopped.returncode:
        raise ValueError(failure_message)
    for delay in (0, 0.1, 0.2, 0.5, 1.0):
        if delay:
            time.sleep(delay)
        if launchctl("print", target).returncode:
            return
    raise ValueError(failure_message)


def manage_service(action: str, data_dir: Path, *, port=DEFAULT_PORT, interval=30, config_path=None, timezone_name=None) -> str:
    if sys.platform != "darwin":
        raise ValueError("Login service installation requires macOS; use serve on this platform")
    if not 1 <= port <= 65535 or interval < 1:
        raise ValueError("Use port 1–65535 and interval >=1 second")
    if action not in {"install", "status", "uninstall"}:
        raise ValueError("Service action must be install, status, or uninstall")
    if action == "install" and port != DEFAULT_PORT:
        raise ValueError(f"Usage Tracker must deploy on TCP {DEFAULT_PORT}; alternate ports are not supported")
    target = f"gui/{os.getuid()}/{LABEL}"
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if action == "status":
        result = launchctl("print", target)
        if result.returncode:
            return f"Not running. Login service {'is installed' if plist.exists() else 'is not installed'}."
        details = [line.strip() for line in result.stdout.splitlines()
                   if line.strip().startswith(("state =", "pid =", "last exit code ="))]
        return "Login service loaded. " + "; ".join(details)
    if action == "uninstall":
        if plist.exists():
            saved = plistlib.loads(plist.read_bytes())
            if saved.get("Label") != LABEL or "usage_tracker" not in saved.get("ProgramArguments", []):
                raise ValueError("Existing launch agent is not recognized as Usage Tracker")
        # A loaded job can outlive a removed plist. Always unload a live job and
        # verify it stopped before claiming success or removing its definition.
        _unload_job(target, "Could not unload the login service; its plist and data were preserved")
        if plist.exists():
            plist.unlink()
        return "Login service removed; collected data is preserved."
    if plist.exists():
        saved = plistlib.loads(plist.read_bytes())
        if saved.get("Label") != LABEL or "usage_tracker" not in saved.get("ProgramArguments", []):
            raise ValueError("Existing launch agent is not recognized as Usage Tracker")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    payload = plist_payload(data_dir, port=port, interval=interval, config_path=config_path, timezone_name=timezone_name)
    # Stop only our recognized job, including the temporary 8447 deployment.
    # Leave the prior plist intact until the required 8787 bind probe succeeds.
    # An unrelated listener is a deployment conflict, never a termination target.
    _unload_job(target, "Could not unload the previous login service; its configuration was preserved")
    from usage_tracker.server import make_server
    try:
        probe = make_server(data_dir, port=DEFAULT_PORT)
    except ValueError as exc:
        raise ValueError(f"TCP {DEFAULT_PORT} is unavailable; the previous service plist was preserved and no alternate port was selected") from exc
    probe.server_close()
    plist.write_bytes(plistlib.dumps(payload))
    plist.chmod(0o600)
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
    # bootout can acknowledge removal before launchd has finished releasing the
    # old job. An immediate replacement then transiently returns EIO (exit 5).
    # Retry only that known race, with a small bound; other failures stay errors.
    for delay in (0.2, 0.5, 1.0, 2.0):
        if result.returncode != 5:
            break
        time.sleep(delay)
        result = launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
    if result.returncode:
        raise ValueError(f"launchd could not start the service (exit {result.returncode}); inspect service-error.log and service status")
    return f"Login service installed at {plist}. Dashboard: http://localhost:{port}"
