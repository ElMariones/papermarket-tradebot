"""
auth.py — accounts + sessions, stdlib only.

Multi-user login for the dashboard: named accounts (created by the admin via
create_user.py — there is deliberately no signup form), opaque DB-backed
session tokens in an httpOnly cookie, and three roles:

  * admin     — full control of everything, including other users' portfolios
  * user      — controls only their OWN portfolio (manual trading, funds, reset)
  * spectator — the default for anyone without a session: read everything,
                control nothing

Passwords are hashed with hashlib.pbkdf2_hmac (no bcrypt dependency); session
tokens come from secrets.token_urlsafe and are revocable by deleting the row.
Tables live in the same SQLite file as the trading ledger.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import engine

# Session lifetime. 30 days is right for a personal tool: long enough that
# Mario's friends aren't re-logging weekly, short enough that a leaked cookie
# eventually dies on its own.
SESSION_TTL_DAYS = 30
SESSION_COOKIE = "tradebot_session"

_PBKDF2_ITERATIONS = 600_000  # OWASP-recommended order of magnitude for sha256


def _now() -> datetime:
    return datetime.now(timezone.utc)


# The users/sessions tables are created by engine._ensure_extra_schema()
# (imported above) so every entrypoint that writes portfolios — including ones
# that never import auth — sees them before the portfolios.owner_user_id FK.


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def any_users_exist() -> bool:
    conn = engine._conn()
    try:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    conn = engine._conn()
    try:
        row = conn.execute(
            "SELECT id, username, role, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_name(username: str) -> dict | None:
    conn = engine._conn()
    try:
        row = conn.execute(
            "SELECT id, username, role, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_user(username: str, password: str, role: str = "user") -> dict:
    """Insert a user row. Portfolio creation is the caller's job
    (create_user.py pairs this with engine.create_user_portfolio)."""
    if role not in ("admin", "user"):
        raise ValueError("role must be 'admin' or 'user'")
    username = (username or "").strip()
    if not username or not username.replace("_", "").replace("-", "").isalnum():
        raise ValueError("username must be alphanumeric (plus - or _)")
    if len(password or "") < 8:
        raise ValueError("password must be at least 8 characters")
    # A user's personal portfolio is named after them, so a username that
    # shadows a bot profile would be ambiguous everywhere.
    if username.lower() in (p.lower() for p in engine.PROFILES):
        raise ValueError(f"'{username}' collides with a bot profile name")

    salt = secrets.token_hex(16)
    conn = engine._conn()
    try:
        cur = conn.execute(
            """INSERT INTO users (username, password_hash, salt, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, _hash_password(password, salt), salt, role,
             _now().isoformat()),
        )
        conn.commit()
        uid = cur.lastrowid
    finally:
        conn.close()
    return {"id": uid, "username": username, "role": role}


def verify_login(username: str, password: str) -> dict | None:
    """Constant-behavior check: same code path whether the username exists or
    not, so responses don't leak which usernames are real."""
    conn = engine._conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username or "",)
        ).fetchone()
    finally:
        conn.close()
    salt = row["salt"] if row else secrets.token_hex(16)
    computed = _hash_password(password or "", salt)
    if row and secrets.compare_digest(computed, row["password_hash"]):
        return {"id": row["id"], "username": row["username"], "role": row["role"]}
    return None


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn = engine._conn()
    try:
        # opportunistic cleanup so the table doesn't grow forever
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(),
             (now + timedelta(days=SESSION_TTL_DAYS)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def get_session_user(token: str | None) -> dict | None:
    """Resolve a session token to its user, or None (expired/unknown)."""
    if not token:
        return None
    conn = engine._conn()
    try:
        row = conn.execute(
            """SELECT u.id, u.username, u.role
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at >= ?""",
            (token, _now().isoformat()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_session(token: str | None) -> None:
    if not token:
        return
    conn = engine._conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()
