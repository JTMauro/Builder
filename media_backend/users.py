"""User accounts, login sessions, personal access tokens and saved chats (SQLite)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import get_settings

SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
MIN_PASSWORD = 8


class User(BaseModel):
    id: int
    username: str
    display_name: str
    is_admin: bool
    created_at: float


class AuthError(ValueError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  hint TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_used_at REAL
);
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  messages TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chats_owner ON chats(owner, updated_at);
"""


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(test.hex(), digest)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = _hash_password(secrets.token_hex(8))  # keeps timing similar for unknown usernames


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class UserStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        with self._lock:
            self._db.executescript(_SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    @staticmethod
    def _user(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(id=row["id"], username=row["username"], display_name=row["display_name"],
                    is_admin=bool(row["is_admin"]), created_at=row["created_at"])

    # ------------------------------------------------------------ users

    def count(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM users")[0]["n"]

    def create_user(self, username: str, password: str, display_name: str = "", is_admin: bool = False) -> User:
        username = username.strip()
        if not USERNAME_RE.fullmatch(username):
            raise AuthError("Usernames are 3-32 letters, numbers, dots, dashes or underscores")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"Passwords need at least {MIN_PASSWORD} characters")
        display = (display_name or username).strip()[:60] or username
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO users (username, display_name, password_hash, is_admin, created_at) VALUES (?,?,?,?,?)",
                    (username, display, _hash_password(password), int(is_admin), time.time()),
                )
        except sqlite3.IntegrityError as e:
            raise AuthError("That username is taken") from e
        return self.get_user(cur.lastrowid)  # type: ignore[return-value]

    def get_user(self, user_id: int) -> User | None:
        rows = self._q("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._user(rows[0] if rows else None)

    def list_users(self) -> list[User]:
        return [self._user(r) for r in self._q("SELECT * FROM users ORDER BY created_at")]  # type: ignore[misc]

    def delete_user(self, user_id: int) -> None:
        self._q("DELETE FROM users WHERE id = ?", (user_id,))

    def set_password(self, user_id: int, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"Passwords need at least {MIN_PASSWORD} characters")
        self._q("UPDATE users SET password_hash = ? WHERE id = ?", (_hash_password(password), user_id))
        self._q("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # sign out everywhere

    def verify(self, username: str, password: str) -> User:
        key = username.strip().lower()
        now = time.time()
        recent = [t for t in self._failures.get(key, []) if now - t < 600]
        if len(recent) >= 5:
            raise AuthError("Too many failed attempts. Try again in a few minutes.")
        rows = self._q("SELECT * FROM users WHERE username = ?", (username.strip(),))
        ok = _check_password(password, rows[0]["password_hash"] if rows else _DUMMY_HASH)
        if not rows or not ok:
            self._failures[key] = recent + [now]
            raise AuthError("Wrong username or password")
        self._failures.pop(key, None)
        return self._user(rows[0])  # type: ignore[return-value]

    def check_password(self, user_id: int, password: str) -> bool:
        rows = self._q("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        return bool(rows) and _check_password(password, rows[0]["password_hash"])

    # ------------------------------------------------------------ login sessions

    def new_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._q("DELETE FROM sessions WHERE expires_at < ?", (now,))
        self._q("INSERT INTO sessions VALUES (?,?,?,?)", (_sha(token), user_id, now, now + SESSION_DAYS * 86400))
        return token

    def user_for_session(self, token: str) -> User | None:
        if not token:
            return None
        rows = self._q(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
            (_sha(token), time.time()),
        )
        return self._user(rows[0] if rows else None)

    def end_session(self, token: str) -> None:
        self._q("DELETE FROM sessions WHERE token_hash = ?", (_sha(token),))

    # ------------------------------------------------------------ personal access tokens

    def new_api_token(self, user_id: int, name: str) -> tuple[dict[str, Any], str]:
        token = "mbp_" + secrets.token_urlsafe(32)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO api_tokens (token_hash, user_id, name, hint, created_at) VALUES (?,?,?,?,?)",
                (_sha(token), user_id, (name.strip() or "Claude Code")[:60], token[-4:], time.time()),
            )
        return self.list_api_tokens(user_id, only=cur.lastrowid)[0], token

    def list_api_tokens(self, user_id: int, only: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, name, hint, created_at, last_used_at FROM api_tokens WHERE user_id = ?"
        args: tuple = (user_id,)
        if only is not None:
            sql += " AND id = ?"
            args += (only,)
        return [dict(r) for r in self._q(sql + " ORDER BY created_at DESC", args)]

    def delete_api_token(self, user_id: int, token_id: int) -> bool:
        before = len(self.list_api_tokens(user_id))
        self._q("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id))
        return len(self.list_api_tokens(user_id)) < before

    def user_for_api_token(self, token: str) -> User | None:
        if not token.startswith("mbp_"):
            return None
        h = _sha(token)
        rows = self._q("SELECT u.* FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?", (h,))
        if rows:
            self._q("UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?", (time.time(), h))
        return self._user(rows[0] if rows else None)

    # ------------------------------------------------------------ chats

    def load_chat(self, chat_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM chats WHERE id = ?", (chat_id,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "owner": r["owner"], "title": r["title"], "messages": json.loads(r["messages"]),
                "updated_at": r["updated_at"]}

    def save_chat(self, chat_id: str, owner: str, title: str, messages: list[dict[str, Any]]) -> None:
        self._q(
            "INSERT INTO chats (id, owner, title, messages, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, messages = excluded.messages, updated_at = excluded.updated_at",
            (chat_id, owner, title, json.dumps(messages), time.time()),
        )

    def list_chats(self, owner: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._q("SELECT id, title, updated_at FROM chats WHERE owner = ? ORDER BY updated_at DESC LIMIT ?", (owner, limit))
        return [dict(r) for r in rows]

    def delete_chat(self, chat_id: str, owner: str) -> bool:
        if not self._q("SELECT 1 FROM chats WHERE id = ? AND owner = ?", (chat_id, owner)):
            return False
        self._q("DELETE FROM chats WHERE id = ?", (chat_id,))
        return True


_store: UserStore | None = None
_store_path: Path | None = None


def get_store() -> UserStore:
    global _store, _store_path
    path = get_settings().data_dir / "builder.db"
    if _store is None or _store_path != path:
        _store, _store_path = UserStore(path), path
    return _store
