"""Loopback-only read API and dashboard, with a single background collector."""

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import ipaddress
import json
import mimetypes
import signal
import socket
import threading

from usage_tracker import __version__
from usage_tracker.config import DEFAULT_PORT, load_sources
from usage_tracker.ingest import collect
from usage_tracker.reports import report, quota_history, summary_revisions
from usage_tracker.storage import Store, now

STATIC = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, data_dir, interval, timezone_name, **kwargs):
        self.data_dir, self.interval, self.timezone_name = data_dir, interval, timezone_name
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass  # Query strings can contain local metadata; don't log requests.

    def _send(self, status, body, mime="application/json; charset=utf-8", head=False):
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if not head:
            self.wfile.write(content)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        # A loopback bind alone doesn't stop DNS rebinding. Reject any Host
        # outside our literal loopback origins and do not enable CORS.
        port = self.server.server_address[1]
        bound = self.server.server_address[0]
        bound = f"[{bound}]" if ":" in bound else bound
        allowed = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}", f"{bound}:{port}"}
        if port == 80:
            allowed |= {"localhost", "127.0.0.1", "[::1]"}
        if self.headers.get("Host", "").lower() not in allowed:
            return self._send(403, {"error": "Loopback Host required"}, head=head)
        request = urlsplit(self.path)
        try:
            if request.path == "/api/health":
                with Store(self.data_dir) as store:
                    return self._send(200, {"status": store.get_setting("collection_status", "ok"),
                                           "last_collection": store.get_setting("last_collection"),
                                           "interval_seconds": self.interval, "version": __version__}, head=head)
            if request.path in ("/api/report", "/api/reconciliation"):
                query = parse_qs(request.query, max_num_fields=20)
                args = {key: query[key][-1] for key in ("provider", "surface", "model", "group", "month") if key in query}
                args["days"] = int(query.get("days", ["30"])[-1])
                if "year" in query:
                    args["year"] = int(query["year"][-1])
                args["timezone_name"] = self.timezone_name
                if request.path == "/api/reconciliation":
                    args["summary_limit"] = int(query.get("limit", ["25"])[-1])
                    args["summary_offset"] = int(query.get("offset", ["0"])[-1])
                with Store(self.data_dir) as store:
                    data = report(store, **args)
                    return self._send(200, data["reconciliation"] if request.path == "/api/reconciliation" else data, head=head)
            if request.path == "/api/quota-history":
                query = parse_qs(request.query, max_num_fields=10)
                with Store(self.data_dir) as store:
                    return self._send(200, quota_history(store, provider=query.get("provider", ["all"])[-1],
                        limit=int(query.get("limit", ["25"])[-1]), offset=int(query.get("offset", ["0"])[-1])), head=head)
            if request.path == "/api/summary-revisions":
                query = parse_qs(request.query, max_num_fields=10)
                with Store(self.data_dir) as store:
                    return self._send(200, summary_revisions(store, query.get("summary_key", [""])[-1],
                        limit=int(query.get("limit", ["50"])[-1]), offset=int(query.get("offset", ["0"])[-1])), head=head)
            # Only allow packaged assets. Never resolve a user-controlled path
            # against the filesystem, even after traversal normalization.
            assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css",
                      "/chart.js": "chart.js", "/chart.css": "chart.css",
                      "/evidence.js": "evidence.js", "/evidence.css": "evidence.css"}
            if request.path in assets:
                path = STATIC / assets[request.path]
                return self._send(200, path.read_bytes(), (mimetypes.guess_type(path.name)[0] or "application/octet-stream") + "; charset=utf-8", head=head)
            return self._send(404, {"error": "Not found"}, head=head)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)}, head=head)
        except Exception as exc:
            # Keep tracebacks and local paths out of HTTP responses. Console logs
            # contain exception classes only, never the raw source JSONL payload.
            print(f"Dashboard request failed: {type(exc).__name__}", flush=True)
            return self._send(500, {"error": "Could not read local usage data"}, head=head)


def make_server(data_dir: Path, *, host="127.0.0.1", port=DEFAULT_PORT, interval=30, timezone_name=None):
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("Dashboard host must be loopback")
        except ValueError as exc:
            raise ValueError("Dashboard host must be localhost or a loopback IP") from exc
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    server_class = ThreadingHTTPServer
    if ":" in host:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_class = IPv6Server
    try:
        return server_class((host, port), partial(Handler, data_dir=data_dir, interval=interval, timezone_name=timezone_name))
    except OSError as exc:
        raise ValueError(f"Cannot bind {host}:{port}; the port may be occupied. Usage Tracker does not terminate unrelated listeners or select another port.") from exc


def serve(data_dir: Path, *, host="127.0.0.1", port=DEFAULT_PORT, interval=30, config_path=None, timezone_name=None):
    # Keep the deployed process pinned even when invoked outside argparse. The
    # lower-level factory still permits ephemeral sockets for isolated tests.
    if port != DEFAULT_PORT:
        raise ValueError(f"Usage Tracker must deploy on port {DEFAULT_PORT}")
    if interval < 1:
        raise ValueError("Collection interval must be at least 1 second")
    server = make_server(data_dir, host=host, port=port, interval=interval, timezone_name=timezone_name)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                with Store(data_dir) as store:
                    collect(store, load_sources(config_path))
            except Exception as exc:
                print(f"Collection failed at {now()}: {type(exc).__name__}", flush=True)
                with Store(data_dir) as store, store.conn:
                    store.set_setting("collection_status", "degraded")
            stop.wait(interval)

    thread = threading.Thread(target=worker, name="usage-collector", daemon=True)
    thread.start()

    def shutdown(*args):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, shutdown)
    print(f"Usage Tracker is running at http://{host}:{server.server_address[1]} (local only)", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=10)
