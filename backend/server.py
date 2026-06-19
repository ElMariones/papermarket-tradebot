"""
server.py — zero-dependency REST API + static dashboard host.

Uses only the Python standard library (http.server) so the whole project
runs on a stock Python install with no pip step. Serves the JSON API under
/api/* and the dashboard (frontend/) for everything else.

The background agent loop lives in this process; the dashboard's
start/pause/stop buttons drive it.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import agent
import engine

# --- access control -------------------------------------------------------
# If TRADEBOT_AUTH_PASSWORD is set, the whole site (API + dashboard) requires
# HTTP Basic Auth. Unset => open (local dev). Set it as a Fly secret in prod.
AUTH_USER = os.environ.get("TRADEBOT_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("TRADEBOT_AUTH_PASSWORD", "")

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
PORT = int(os.environ.get("PORT", "8765"))          # Fly injects PORT
HOST = os.environ.get("HOST", "0.0.0.0")
PORTFOLIO = os.environ.get("TRADEBOT_PORTFOLIO", "default")
STARTING_BALANCE = float(os.environ.get("TRADEBOT_START_BALANCE", "200"))
# Standalone = run the agent worker loop in this same process (local default).
# On a true split deploy you'd set this to 0 and run worker.py separately.
STANDALONE = os.environ.get("TRADEBOT_STANDALONE", "1") not in ("0", "false", "")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TradeBOT/1.0"

    # --- helpers ---------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, *args):  # quiet the default noisy logging
        pass

    # --- auth ------------------------------------------------------------
    def _authed(self) -> bool:
        if not AUTH_PASSWORD:
            return True  # auth disabled (local dev)
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
        except Exception:
            return False
        return (hmac.compare_digest(user, AUTH_USER)
                and hmac.compare_digest(pw, AUTH_PASSWORD))

    def _require_auth(self) -> bool:
        if self._authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="TradeBOT"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required.")
        return False

    # --- routing ---------------------------------------------------------
    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/portfolio":
                return self._send_json(engine.get_portfolio(PORTFOLIO, refresh_prices=True))
            if path == "/api/trades":
                limit = int(qs.get("limit", ["100"])[0])
                return self._send_json(engine.get_trades(PORTFOLIO, limit))
            if path == "/api/decisions":
                limit = int(qs.get("limit", ["80"])[0])
                return self._send_json(engine.get_decisions(PORTFOLIO, limit))
            if path == "/api/equity":
                return self._send_json(engine.get_equity_curve(PORTFOLIO))
            if path == "/api/settings":
                return self._send_json(engine.get_settings(PORTFOLIO))
            if path == "/api/agent":
                return self._send_json(engine.get_agent_state(PORTFOLIO))
            if path == "/api/summary":
                return self._send_json(engine.compute_summary(PORTFOLIO))
            if path == "/api/reports":
                limit = int(qs.get("limit", ["168"])[0])
                return self._send_json(engine.get_hourly_reports(PORTFOLIO, limit))
            return self._serve_static(path)
        except Exception as exc:
            return self._send_json({"error": str(exc)}, status=500)

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/add-funds":
                amt = float(body.get("amount", 0))
                return self._send_json(engine.add_funds(amt, PORTFOLIO))
            if path == "/api/settings":
                return self._send_json(engine.save_settings(PORTFOLIO, body))
            if path == "/api/agent/start":
                return self._send_json(agent.start(PORTFOLIO))
            if path == "/api/agent/pause":
                return self._send_json(agent.pause(PORTFOLIO))
            if path == "/api/agent/resume":
                return self._send_json(agent.resume(PORTFOLIO))
            if path == "/api/agent/stop":
                return self._send_json(agent.stop(PORTFOLIO))
            if path == "/api/agent/cycle":
                return self._send_json(agent.run_cycle(PORTFOLIO))
            if path == "/api/close":
                token = body.get("token_id"); side = body.get("side")
                return self._send_json(engine.close_position(
                    token, side, PORTFOLIO, reasoning="Manual close from dashboard."))
            if path == "/api/report":
                return self._send_json(engine.record_hourly_report(PORTFOLIO))
            if path == "/api/reset":
                if not body.get("confirm"):
                    return self._send_json(
                        {"error": "reset requires confirm:true"}, status=400)
                bal = body.get("balance")
                bal = float(bal) if bal not in (None, "") else None
                return self._send_json(engine.reset_all(PORTFOLIO, balance=bal))
            return self._send_json({"error": "not found"}, status=404)
        except Exception as exc:
            return self._send_json({"error": str(exc)}, status=400)

    # --- static files ----------------------------------------------------
    def _serve_static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        target = (FRONTEND_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(FRONTEND_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    if not engine.portfolio_exists(PORTFOLIO):
        engine.init_portfolio(STARTING_BALANCE, PORTFOLIO, {
            "max_position_pct": 0.05, "max_concurrent_positions": 10,
            "max_single_market_pct": 0.10, "daily_loss_limit_pct": 0.05,
            "max_drawdown_pct": 0.30, "human_approval_pct": 0.20,
        })
        print(f"Initialized '{PORTFOLIO}' portfolio with ${STARTING_BALANCE:,.2f}")

    stop_event = threading.Event()
    if STANDALONE:
        # run the agent loop in a background thread within this process
        t = threading.Thread(
            target=agent.run_forever, args=(PORTFOLIO, stop_event), daemon=True)
        t.start()
        print("Agent worker running in-process (TRADEBOT_STANDALONE).")

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"TradeBOT dashboard:  http://127.0.0.1:{PORT}  (bound {HOST}:{PORT})")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_event.set()
        httpd.shutdown()


if __name__ == "__main__":
    main()
