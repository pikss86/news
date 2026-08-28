from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.settings.store import atomic_write, canonical_json


class AdminPasswordStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.hasher = PasswordHasher()

    @property
    def configured(self) -> bool:
        return self.path.exists()

    def set_password(self, password: str) -> None:
        if len(password) < 12:
            raise ValueError("administrator password must contain at least 12 characters")
        document = {"format": 1, "password_hash": self.hasher.hash(password)}
        atomic_write(self.path, canonical_json(document) + b"\n")

    def verify(self, password: str) -> bool:
        if not self.path.exists():
            return False
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            return self.hasher.verify(document["password_hash"], password)
        except (OSError, json.JSONDecodeError, KeyError, InvalidHashError, VerifyMismatchError):
            return False


@dataclass
class Session:
    created_at: float
    last_seen_at: float


class SessionManager:
    def __init__(self, *, idle_seconds: int = 1800, absolute_seconds: int = 28800) -> None:
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self.sessions: dict[str, Session] = {}
        self.csrf_tokens: dict[str, set[str]] = {}
        self.anonymous_csrf: dict[str, set[str]] = {}

    def create(self, now: float | None = None) -> str:
        timestamp = now if now is not None else time.monotonic()
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(timestamp, timestamp)
        return token

    def validate(self, token: str | None, now: float | None = None) -> bool:
        if not token or token not in self.sessions:
            return False
        timestamp = now if now is not None else time.monotonic()
        session = self.sessions[token]
        if (
            timestamp - session.created_at > self.absolute_seconds
            or timestamp - session.last_seen_at > self.idle_seconds
        ):
            self.destroy(token)
            return False
        session.last_seen_at = timestamp
        return True

    def destroy(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)
            self.csrf_tokens.pop(token, None)

    def invalidate_all(self) -> None:
        self.sessions.clear()
        self.csrf_tokens.clear()
        self.anonymous_csrf.clear()

    def issue_csrf(self, principal: str, *, anonymous: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        storage = self.anonymous_csrf if anonymous else self.csrf_tokens
        storage.setdefault(principal, set()).add(token)
        return token

    def consume_csrf(self, principal: str, token: str, *, anonymous: bool = False) -> bool:
        storage = self.anonymous_csrf if anonymous else self.csrf_tokens
        tokens = storage.get(principal)
        if not tokens or token not in tokens:
            return False
        tokens.remove(token)
        return True


class LoginRateLimiter:
    def __init__(self, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.failures: dict[str, list[float]] = {}

    def _recent(self, peer: str, now: float) -> list[float]:
        recent = [item for item in self.failures.get(peer, []) if now - item < self.window_seconds]
        self.failures[peer] = recent
        return recent

    def allowed(self, peer: str, now: float | None = None) -> bool:
        timestamp = now if now is not None else time.monotonic()
        return len(self._recent(peer, timestamp)) < self.attempts

    def fail(self, peer: str, now: float | None = None) -> None:
        timestamp = now if now is not None else time.monotonic()
        self._recent(peer, timestamp).append(timestamp)

    def success(self, peer: str) -> None:
        self.failures.pop(peer, None)
