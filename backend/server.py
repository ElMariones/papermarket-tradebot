"""
server.py — zero-dependency REST API + static dashboard host.

Uses only the Python standard library (http.server) so the whole project
runs on a stock Python install with no pip step. Serves the JSON API under
/api/* and the dashboard (frontend/) for everything else.

Access model (see auth.py):
  * READS are public — anyone can watch every portfolio (spectator mode).
  * WRITES require a session: admins control everything; users control only
    their own portfolio; spectators get a friendly pointer to request an
    account instead of a raw 401.
  * Legacy fallback: if TRADEBOT_AUTH_PASSWORD is set and NO accounts exist
    yet, the whole site is gated behind HTTP Basic Auth exactly like the old
    single-password releases. The first created account retires that mode.

The background agent loop lives in this process; the dashboard's
start/pause/stop buttons drive it.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import agent
import auth
import engine
import strategy
from polymarket_client import fetch_active_markets_cached

# --- legacy access control ------------------------------------------------
# Honored ONLY while no user accounts exist (one-release migration path).
AUTH_USER = os.environ.get("TRADEBOT_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("TRADEBOT_AUTH_PASSWORD", "")

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
PORT = int(os.environ.get("PORT", "8765"))          # Fly injects PORT
HOST = os.environ.get("HOST", "0.0.0.0")
# Standalone = run the agent worker loop in this same process (local default).
# On a true split deploy you'd set this to 0 and run worker.py separately.
STANDALONE = os.environ.get("TRADEBOT_STANDALONE", "1") not in ("0", "false", "")

# Shown whenever someone without the right account clicks a control. Keep the
# copy identical everywhere so it reads as intentional, not as an error.
SPECTATOR_MSG = ("You're viewing this as a spectator. Want your own portfolio? "
                 "Email mariolandaburuclares@gmail.com")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

# --- login rate limiting (in-memory, best-effort) --------------------------
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_FAILS_LOCK = threading.Lock()
_LOGIN_MAX_FAILS = 10
_LOGIN_WINDOW_SEC = 900


def _login_blocked(ip: str) -> bool:
    now = time.monotonic()
    with _LOGIN_FAILS_LOCK:
        fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW_SEC]
        _LOGIN_FAILS[ip] = fails
        return len(fails) >= _LOGIN_MAX_FAILS


def _login_failed(ip: str) -> None:
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.setdefault(ip, []).append(time.monotonic())


# Once any account exists, legacy Basic Auth is permanently retired (cached so
# static file requests don't hit SQLite; flips at most once per process).
_users_exist_cache = {"value": None, "checked": 0.0}


def _users_exist() -> bool:
    if _users_exist_cache["value"] is True:
        return True
    now = time.monotonic()
    if (_users_exist_cache["value"] is None
            or now - _users_exist_cache["checked"] > 15):
        _users_exist_cache["value"] = auth.any_users_exist()
        _users_exist_cache["checked"] = now
    return _users_exist_cache["value"]


class Handler(BaseHTTPRequestHandler):
    server_version = "TradeBOT/1.0"

    # --- helpers ---------------------------------------------------------
    def _send_json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
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

    # --- sessions ----------------------------------------------------------
    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http_cookies.SimpleCookie(raw)
            morsel = jar.get(auth.SESSION_COOKIE)
            return morsel.value if morsel else None
        except http_cookies.CookieError:
            return None

    def _current_user(self) -> dict | None:
        user = auth.get_session_user(self._session_token())
        if user:
            return user
        # Legacy single-password mode (no accounts yet): whoever passed the
        # Basic Auth gate has full control, exactly like the old releases.
        if self._legacy_gate_active() and self._legacy_authed():
            return {"id": None, "username": AUTH_USER, "role": "admin",
                    "legacy": True}
        return None

    def _session_cookie_header(self, token: str, max_age: int) -> tuple[str, str]:
        parts = [
            f"{auth.SESSION_COOKIE}={token}",
            "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}",
        ]
        # Secure cookies break plain-HTTP LAN use, so only mark it when the
        # request actually came over TLS (Fly and most proxies set the header).
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def _can_control(self, user: dict | None, profile: str) -> bool:
        """Admin: everything. User: only their own copies of the four bots.
        Spectator: nothing."""
        if not user:
            return False
        if user["role"] == "admin":
            return True
        owner_id = engine.portfolio_owner(profile)["owner_user_id"]
        return owner_id is not None and owner_id == user["id"]

    def _deny_control(self):
        """403 with the observer message — the frontend turns this into the
        spectator modal instead of a raw error."""
        self._send_json({"error": "forbidden", "spectator": True,
                         "message": SPECTATOR_MSG}, status=403)

    # --- legacy basic auth (only while no accounts exist) ------------------
    def _legacy_gate_active(self) -> bool:
        return bool(AUTH_PASSWORD) and not _users_exist()

    def _legacy_authed(self) -> bool:
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
        except Exception:
            return False
        return (hmac.compare_digest(user, AUTH_USER)
                and hmac.compare_digest(pw, AUTH_PASSWORD))

    def _require_legacy_auth(self) -> bool:
        if not self._legacy_gate_active() or self._legacy_authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="TradeBOT"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required.")
        return False

    # --- routing ---------------------------------------------------------
    def do_GET(self):
        if not self._require_legacy_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        pf = engine.resolve_profile(qs.get("profile", [None])[0])
        try:
            # All reads are public: spectators see every portfolio, bot or
            # human — the gate is on actions, not on viewing.
            if path == "/api/auth/me":
                return self._auth_me()
            if path == "/api/profiles":
                return self._send_json(engine.get_profiles_overview())
            if path == "/api/portfolio":
                return self._send_json(engine.get_portfolio(pf, refresh_prices=True))
            if path == "/api/trades":
                limit = int(qs.get("limit", ["100"])[0])
                return self._send_json(engine.get_trades(pf, limit))
            if path == "/api/decisions":
                limit = int(qs.get("limit", ["80"])[0])
                return self._send_json(engine.get_decisions(pf, limit))
            if path == "/api/equity":
                return self._send_json(engine.get_equity_curve(pf))
            if path == "/api/settings":
                return self._send_json(engine.get_settings(pf))
            if path == "/api/agent":
                return self._send_json(engine.get_agent_state(pf))
            if path == "/api/summary":
                return self._send_json(engine.compute_summary(pf))
            if path == "/api/reports":
                limit = int(qs.get("limit", ["168"])[0])
                return self._send_json(engine.get_hourly_reports(pf, limit))
            if path == "/api/markets":
                limit = max(1, min(300, int(qs.get("limit", ["100"])[0])))
                return self._send_json(self._markets_list(limit))
            if path == "/api/markets/book":
                token = qs.get("token_id", [""])[0]
                return self._send_json(self._book_summary(token))
            if path == "/api/export":
                data = engine.export_all(pf)
                stamp = data["exported_at"][:19].replace(":", "").replace("-", "")
                fname = f"tradebot-{pf}-{stamp}.json"
                body = json.dumps(data, default=str, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._serve_static(path)
        except Exception as exc:
            return self._send_json({"error": str(exc)}, status=500)

    def do_POST(self):
        if not self._require_legacy_auth():
            return
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            # auth endpoints are open — they're how you stop being a spectator
            if path == "/api/auth/login":
                return self._auth_login(body)
            if path == "/api/auth/logout":
                return self._auth_logout()

            pf = engine.resolve_profile(body.get("profile"))
            user = self._current_user()

            # Everything below changes portfolio state → ownership check.
            # Admin controls everything; a user only their own portfolio;
            # spectators (and users poking other portfolios) get the message.
            if not self._can_control(user, pf):
                return self._deny_control()

            if path.startswith("/api/agent/"):
                if path == "/api/agent/start":
                    return self._send_json(agent.start(pf))
                if path == "/api/agent/pause":
                    return self._send_json(agent.pause(pf))
                if path == "/api/agent/resume":
                    return self._send_json(agent.resume(pf))
                if path == "/api/agent/stop":
                    return self._send_json(agent.stop(pf))
                if path == "/api/agent/cycle":
                    return self._send_json(agent.run_cycle(pf))

            if path == "/api/add-funds":
                amt = float(body.get("amount", 0))
                return self._send_json(engine.add_funds(amt, pf))
            if path == "/api/withdraw-funds":
                amt = float(body.get("amount", 0))
                return self._send_json(engine.withdraw_funds(amt, pf))
            if path == "/api/settings":
                # don't let the routing key leak into the strategy config
                updates = {k: v for k, v in body.items() if k != "profile"}
                return self._send_json(engine.save_settings(pf, updates))
            if path == "/api/close":
                token = body.get("token_id"); side = body.get("side")
                return self._send_json(engine.manual_trade(
                    pf, token, side, "sell", None,
                    username=user["username"]))
            if path == "/api/trade":
                return self._send_json(engine.manual_trade(
                    pf, body.get("token_id"), body.get("side"),
                    body.get("action"), body.get("amount"),
                    username=user["username"]))
            if path == "/api/report":
                return self._send_json(engine.record_hourly_report(pf))
            if path == "/api/reset":
                if not body.get("confirm"):
                    return self._send_json(
                        {"error": "reset requires confirm:true"}, status=400)
                bal = body.get("balance")
                bal = float(bal) if bal not in (None, "") else None
                return self._send_json(engine.reset_all(pf, balance=bal))
            return self._send_json({"error": "not found"}, status=404)
        except Exception as exc:
            return self._send_json({"error": str(exc)}, status=400)

    # --- auth endpoints ----------------------------------------------------
    def _auth_login(self, body: dict):
        ip = self.client_address[0]
        if _login_blocked(ip):
            return self._send_json(
                {"error": "Too many failed attempts — try again later."},
                status=429)
        user = auth.verify_login(body.get("username", ""),
                                 body.get("password", ""))
        if not user:
            _login_failed(ip)
            # identical message whether the username exists or not
            return self._send_json(
                {"error": "Invalid username or password."}, status=401)
        token = auth.create_session(user["id"])
        hdr = self._session_cookie_header(
            token, auth.SESSION_TTL_DAYS * 86400)
        return self._send_json({"user": user}, extra_headers=[hdr])

    def _auth_logout(self):
        auth.delete_session(self._session_token())
        hdr = self._session_cookie_header("", 0)  # expire the cookie
        return self._send_json({"user": None}, extra_headers=[hdr])

    def _auth_me(self):
        return self._send_json({"user": self._current_user()})

    # --- markets (manual trading) -------------------------------------------
    def _markets_list(self, limit: int) -> dict:
        """The same live scan the bots run over (Gamma, top 24h volume),
        UNFILTERED — a human may want markets the bots skip. Each row carries
        the baseline pre-book gate flags as a reference, not a restriction.
        Served through the shared scan cache the bot loops use."""
        markets = fetch_active_markets_cached(limit=limit)
        gates = engine.DEFAULT_SETTINGS
        out = []
        for m in markets:
            fav = max(m["yes_price"], m["no_price"])
            out.append({
                "id": m["id"], "question": m["question"], "slug": m["slug"],
                "yes_token": m["yes_token"], "no_token": m["no_token"],
                "yes_price": m["yes_price"], "no_price": m["no_price"],
                "volume24hr": m["volume24hr"], "liquidity": m["liquidity"],
                "end_date": m["end_date"],
                "tags": sorted(strategy.classify_market(m["question"])),
                # pre-book gates only (volume + favorite band); spread/depth
                # need a CLOB fetch per market — done on demand via /book.
                "gate_volume": m["volume24hr"] >= gates["min_volume24h"],
                "gate_band": gates["fav_low"] <= fav <= gates["fav_high"],
            })
        return {"markets": out,
                "gates": {"min_volume24h": gates["min_volume24h"],
                          "fav_low": gates["fav_low"],
                          "fav_high": gates["fav_high"]}}

    def _book_summary(self, token_id: str) -> dict:
        """Live order-book snapshot for one token: spread + walkable depth.
        Used by the Markets page detail row and as a pre-trade sanity view."""
        book = engine.fetch_orderbook(token_id)
        asks, bids = book.get("asks", []), book.get("bids", [])
        best_ask = strategy._best(asks, "ask")
        best_bid = strategy._best(bids, "bid")
        return {
            "token_id": token_id,
            "best_bid": best_bid, "best_ask": best_ask,
            "spread": (round(best_ask - best_bid, 4)
                       if best_ask is not None and best_bid is not None else None),
            "ask_depth_usd": round(strategy._book_depth_usd(asks), 2),
            "bid_depth_usd": round(strategy._book_depth_usd(bids), 2),
        }

    # --- static files ----------------------------------------------------
    def _serve_static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        elif path == "/login":
            path = "/login.html"
        elif path == "/markets":
            path = "/markets.html"
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


def _ensure_demo_bots() -> None:
    """Fresh install with no accounts: create the plain-named demo set so the
    site has something to show. The first admin account claims these, history
    and all (engine.claim_legacy_bots)."""
    for name in engine.PROFILES:
        if engine.portfolio_exists(name):
            continue
        bal = engine.start_balance_for(name)
        engine.init_portfolio(bal, name,
                              engine._risk_for(engine.get_settings(name)))
        engine.save_settings(name, {})  # persist preset + sync hard risk caps
        print(f"Initialized demo bot '{name}' with ${bal:,.2f}")


def main():
    # migrate first (retire personal portfolios, claim demo set for the first
    # admin, ensure every account has its four bots), then only backfill the
    # demo set if there are still no accounts at all.
    engine.migrate_to_per_user_bots()
    if not auth.any_users_exist():
        _ensure_demo_bots()

    if AUTH_PASSWORD and _users_exist():
        print("Note: TRADEBOT_AUTH_PASSWORD is set but accounts exist — the "
              "legacy Basic Auth gate is retired; login handles access now.")

    stop_event = threading.Event()
    if STANDALONE:
        # One agent loop per active bot portfolio (each obeys its own
        # running/paused state), supervised so bots created while the server
        # runs — new accounts, or the admin claiming the demo set — get loops
        # without a restart.
        threading.Thread(target=agent.supervise_forever, args=(stop_event,),
                         daemon=True).start()
        print("Agent supervisor running in-process "
              f"({len(engine.active_bot_names())} bot portfolios)")

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
