"""Authentication and role identity for TransactAlloc.

The dashboard used to accept any username typed into a picker. This module
adds real credentials (PBKDF2-HMAC-SHA256 verifiers stored on Users) and
server-side sessions. A bearer token is issued at login and sent by the
client on every request; nothing about identity is trusted from the browser.

Legacy callers (the pytest suite, CLI) that send no token keep the old
behaviour: they are treated as an implicit operator. Authenticated callers
are scoped to their own role and, for regular users, their own rows.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from psycopg.rows import dict_row

import db

ALGO = "pbkdf2_sha256"
ITERATIONS = 120_000
SESSION_TTL = 8 * 3600

_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}


# ---------------------------------------------------------------------
# password verifiers
# ---------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    ).hex()
    return f"{ALGO}:{ITERATIONS}:{salt}:{digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt, expected = stored.split(":")
        if algo != ALGO:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def ensure_passwords() -> None:
    """Idempotent migration: add the column and provision the three seeded
    accounts when the database was created before authentication existed.
    Existing hashes are never overwritten.
    """
    with db.connect(autocommit=True) as c:
        c.execute("ALTER TABLE Users ADD COLUMN IF NOT EXISTS password_hash TEXT")
    defaults = {
        "admin": hash_password("admin123"),
        "Archita": hash_password("archita123"),
        "student": hash_password("student123"),
    }
    with db.connect(autocommit=True, row_factory=dict_row) as c:
        for username, verifier in defaults.items():
            c.execute(
                "UPDATE Users SET password_hash = %s "
                "WHERE username = %s AND password_hash IS NULL",
                (verifier, username),
            )


# ---------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------
def _public_user(row: dict) -> dict:
    return {
        "user_id": int(row["user_id"]),
        "username": row["username"],
        "role": row["role"],
        "quota_cpus": int(row["quota_cpus"]),
        "quota_ram_mb": int(row["quota_ram_mb"]),
    }


def login(username: str, password: str, expected_role: str | None = None) -> dict:
    """Return {token, user} on success, or None on invalid credentials."""
    with db.connect(row_factory=dict_row) as c:
        row = c.execute(
            "SELECT user_id, username, password_hash, role, quota_cpus, quota_ram_mb "
            "FROM Users WHERE username = %s",
            (username,),
        ).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    if expected_role and row["role"] != expected_role:
        return None
    user = _public_user(row)
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {**user, "expires": time.time() + SESSION_TTL}
    return {"token": token, "user": user}


def logout(token: str | None) -> None:
    if token:
        with _LOCK:
            _SESSIONS.pop(token, None)


def session_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    with _LOCK:
        session = _SESSIONS.get(token)
    if not session:
        return None
    if session["expires"] < time.time():
        with _LOCK:
            _SESSIONS.pop(token, None)
        return None
    return dict(session)


def user_from_request(request) -> dict | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return session_from_token(header[7:].strip())


def bearer_token(request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


def viewer(request) -> dict:
    """The identity an endpoint should use.

    A request with no token is a legacy caller: it sees the whole system, the
    way every endpoint behaved before authentication was added. A request
    with a token gets the role and username from the session -- never from a
    query parameter or a request body.
    """
    session = user_from_request(request)
    if session:
        return {**session, "authenticated": True}
    return {
        "username": None,
        "role": "Admin",
        "user_id": None,
        "authenticated": False,
    }


def is_admin(who: dict) -> bool:
    return who.get("role") == "Admin"


def require_admin(who: dict) -> None:
    if who.get("authenticated") and not is_admin(who):
        from fastapi import HTTPException

        raise HTTPException(403, "this action requires an administrator")
