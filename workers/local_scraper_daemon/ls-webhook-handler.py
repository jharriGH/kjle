"""
KJLE — Local Scraper webhook handler (one-shot)
File: workers/local_scraper_daemon/ls-webhook-handler.py

Tiny stdlib HTTP server that the LocalScraper.exe POSTs to when a scrape run
completes. On first POST, it writes the parsed payload to a marker file at
{LS_HANDLER_SAVE_DIR}/{LS_HANDLER_RUN_ID}_webhook.json and exits.

Listens on 127.0.0.1 only — never exposed to the network. Spawned by daemon.py
as a per-job subprocess.

Env vars:
  LS_HANDLER_SAVE_DIR     (required) directory to write the marker file
  LS_HANDLER_RUN_ID       (required) used as marker filename prefix
  LS_HANDLER_PORT         (default 8765)
  LS_HANDLER_MAX_MINUTES  (default 90) self-timeout
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SAVE_DIR = os.environ.get("LS_HANDLER_SAVE_DIR", "").strip()
RUN_ID = os.environ.get("LS_HANDLER_RUN_ID", "").strip()
PORT = int(os.environ.get("LS_HANDLER_PORT", "8765"))
MAX_MIN = int(os.environ.get("LS_HANDLER_MAX_MINUTES", "90"))

if not SAVE_DIR or not RUN_ID:
    sys.stderr.write(
        "ls-webhook-handler: LS_HANDLER_SAVE_DIR and LS_HANDLER_RUN_ID required\n"
    )
    sys.exit(2)

MARKER_PATH = Path(SAVE_DIR) / f"{RUN_ID}_webhook.json"

_received = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"_raw_b64": raw.hex(), "_parse_error": True}

        try:
            Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
            with open(MARKER_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except OSError as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"marker_write_failed: {e}".encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
        _received.set()

    def do_GET(self) -> None:
        # Health probe for local diagnostics.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ls-webhook-handler\n")

    def log_message(self, fmt: str, *args) -> None:  # silence stderr noise
        return


def main() -> int:
    server = HTTPServer(("127.0.0.1", PORT), _Handler)
    server.timeout = 1.0

    deadline = time.time() + (MAX_MIN * 60)

    def serve() -> None:
        while not _received.is_set() and time.time() < deadline:
            server.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    while not _received.is_set() and time.time() < deadline:
        time.sleep(0.5)

    server.server_close()
    return 0 if _received.is_set() else 1


if __name__ == "__main__":
    sys.exit(main())
