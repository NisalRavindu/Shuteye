"""Accounts and login sessions.

* Passwords are stored only as salted PBKDF2-SHA256 hashes.
* A login creates a random session token sent as an HttpOnly cookie; the
  server stores only a SHA-256 hash of it, so a leaked sessions file can't be
  used to log in.
* The first account is the owner and needs no invite code; existing single-user
  data is moved into it. Later accounts need the invite code the owner sets
  (``--invite-code`` / ``ATTENTION_INVITE_CODE``). Without one, sign-ups are closed.
* Repeated wrong passwords are rate-limited per username and per client.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
PBKDF2_ITERATIONS = 240_000
SESSION_SECONDS = 30 * 24 * 3600
MAX_FAILS = 5                 # wrong passwords before a lockout
LOCKOUT_SECONDS = 60
LEGACY_ITEMS = ("timetable.json", "settings.json", "study_settings.json", "days")


class AuthError(Exception):
    """A message that is safe to show to the user."""


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


class Accounts:
    def __init__(self, data_dir: Path, invite_code: Optional[str] = None,
                 clock: Callable[[], float] = time.time):
        self.data_dir = Path(data_dir)
        self.users_file = self.data_dir / "users.json"
        self.sessions_file = self.data_dir / "sessions.json"
        self.invite_code = (invite_code or "").strip() or None
        self.clock = clock
        self.lock = threading.Lock()
        self.users: dict = _load(self.users_file, {})
        self.sessions: dict = _load(self.sessions_file, {})
        self._fails: dict[str, list] = {}       # key -> [count, locked_until]

    # ------------------------------------------------------------------ info
    def user_dir(self, username: str) -> Path:
        if not USERNAME_RE.match(username):
            raise ValueError("bad username")
        return self.data_dir / "users" / username

    def info(self) -> dict:
        first = not self.users
        return {"first_user": first, "registration_open": first or bool(self.invite_code),
                "invite_required": not first}

    # ------------------------------------------------------------------ sign-up / login
    def register(self, username: str, password: str, invite: str = "") -> str:
        username = (username or "").strip().lower()
        if not USERNAME_RE.match(username):
            raise AuthError("Usernames are 3-32 characters: lowercase letters, numbers, - or _.")
        if not 8 <= len(password or "") <= 200:
            raise AuthError("Use a password of at least 8 characters.")
        with self.lock:
            first = not self.users
            if not first:
                if not self.invite_code:
                    raise AuthError("Sign-ups are closed. Ask the owner to set an invite code.")
                if not hmac.compare_digest((invite or "").strip(), self.invite_code):
                    raise AuthError("That invite code isn't right.")
            if username in self.users:
                raise AuthError("That username is taken.")
            salt = secrets.token_bytes(16)
            self.users[username] = {"salt": salt.hex(), "hash": _hash_password(password, salt).hex(),
                                    "iterations": PBKDF2_ITERATIONS, "created": self.clock(), "owner": first}
            _save(self.users_file, self.users)
            udir = self.user_dir(username)
            udir.mkdir(parents=True, exist_ok=True)
            if first:
                self._migrate_legacy(udir)
            return self._new_session(username)

    def login(self, username: str, password: str, client: str = "") -> str:
        username = (username or "").strip().lower()
        keys = [f"u:{username}", f"c:{client}"]
        now = self.clock()
        with self.lock:
            for k in keys:
                count, until = self._fails.get(k, [0, 0])
                if count >= MAX_FAILS and until > now:
                    raise AuthError(f"Too many attempts. Try again in {int(until - now) + 1} seconds.")
            u = self.users.get(username)
        # hash outside the lock (slow on purpose); hash even for unknown users so timing doesn't reveal them
        salt = bytes.fromhex(u["salt"]) if u else b"\0" * 16
        digest = _hash_password(password or "", salt, u["iterations"] if u else PBKDF2_ITERATIONS)
        ok = bool(u) and hmac.compare_digest(digest.hex(), u["hash"])
        with self.lock:
            if not ok:
                for k in keys:
                    count, _ = self._fails.get(k, [0, 0])
                    count += 1
                    self._fails[k] = [count, now + LOCKOUT_SECONDS if count >= MAX_FAILS else 0]
                raise AuthError("Wrong username or password.")
            for k in keys:
                self._fails.pop(k, None)
            return self._new_session(username)

    def change_password(self, username: str, old: str, new: str) -> None:
        self.login(username, old)                       # verifies (and rate-limits) the old one
        if not 8 <= len(new or "") <= 200:
            raise AuthError("Use a password of at least 8 characters.")
        with self.lock:
            salt = secrets.token_bytes(16)
            self.users[username].update(salt=salt.hex(), hash=_hash_password(new, salt).hex(),
                                        iterations=PBKDF2_ITERATIONS)
            _save(self.users_file, self.users)
            # sign out everywhere else
            self.sessions = {k: v for k, v in self.sessions.items() if v["user"] != username}
            _save(self.sessions_file, self.sessions)

    # ------------------------------------------------------------------ sessions
    def _new_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}   # prune
        self.sessions[_token_key(token)] = {"user": username, "expires": now + SESSION_SECONDS, "created": now}
        _save(self.sessions_file, self.sessions)
        return token

    def user_for(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        s = self.sessions.get(_token_key(token))
        if not s or s["expires"] <= self.clock() or s["user"] not in self.users:
            return None
        return s["user"]

    def logout(self, token: Optional[str]) -> None:
        if not token:
            return
        with self.lock:
            if self.sessions.pop(_token_key(token), None) is not None:
                _save(self.sessions_file, self.sessions)

    # ------------------------------------------------------------------ migration
    def _migrate_legacy(self, udir: Path) -> None:
        """Move single-user data from data/ into the owner's folder."""
        for name in LEGACY_ITEMS:
            src = self.data_dir / name
            if src.exists() and not (udir / name).exists():
                shutil.move(str(src), str(udir / name))
