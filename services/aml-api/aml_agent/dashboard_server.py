"""Read-only dashboard server: the static app and its data file, no credentials or data access needed.

Serves the same paths as the API (/dashboard, /dashboard/static/*, /dashboard/data.json) from
.runtime/dashboard/data.json when it exists, otherwise from the committed snapshot. Live
investigation needs the full API (`python -m aml_agent.cli serve`) and answers 503 here.
"""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .dashboard import data_path
from .live import NEEDS_SETUP
from .settings import ROOT

APP = ROOT / "apps/dashboard"


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/dashboard", "/dashboard/"):
            return self.file(APP / "index.html", "text/html; charset=utf-8")
        if path == "/dashboard/data.json":
            return self.file(data_path(), "application/json")
        if path.startswith("/dashboard/static/"):
            target = (APP / path.removeprefix("/dashboard/static/")).resolve()
            if APP.resolve() in target.parents and target.is_file():
                kind = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(target.suffix, "application/octet-stream")
                return self.file(target, kind + "; charset=utf-8")
        if path in ("/dashboard/api/alerts", "/dashboard/api/alerts/summary"):
            return self.alerts(path)
        if path.startswith("/dashboard/api/"):
            return self.reply(503, {"detail": NEEDS_SETUP})
        self.reply(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        self.reply(503, {"detail": NEEDS_SETUP})

    def alerts(self, path: str) -> None:
        from urllib.parse import parse_qs

        from . import alert_index

        db = alert_index.connect()
        if db is None:
            return self.reply(404, {"detail": "No alert index; run: python -m aml_agent.cli alert-index"})
        if path.endswith("/summary"):
            return self.reply(200, alert_index.summary(db))
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        self.reply(200, alert_index.page(db, alert_index.investigated(data_path()), query))

    def file(self, path, kind: str) -> None:
        if not path.exists():
            return self.reply(404, {"detail": f"Missing {path.relative_to(ROOT)}"})
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def serve(port: int = 8020) -> None:
    print(f"Dashboard: http://127.0.0.1:{port}/dashboard  (data: {data_path().relative_to(ROOT)})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
