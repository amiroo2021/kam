"""Signed HttpOnly session cookies + login rate limiting for TradeMenu."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from .config import TradeMenuConfig


@dataclass
class LoginGateResult:
    allowed: bool
    retry_after_seconds: int = 0
    message: str = ""


class LoginRateLimiter:
    """In-memory failed-login gate (per client key)."""

    def __init__(self, max_failures: int = 5, lockout_seconds: int = 30) -> None:
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._locked_until: Dict[str, float] = {}

    def check(self, client_key: str) -> LoginGateResult:
        now = time.time()
        until = self._locked_until.get(client_key, 0.0)
        if until > now:
            retry = int(until - now) + 1
            return LoginGateResult(False, retry, f"Too many failed attempts. Retry in {retry}s.")
        # prune old failures
        q = self._failures[client_key]
        while q and now - q[0] > self.lockout_seconds * 3:
            q.popleft()
        return LoginGateResult(True)

    def record_failure(self, client_key: str) -> LoginGateResult:
        now = time.time()
        q = self._failures[client_key]
        q.append(now)
        while q and now - q[0] > self.lockout_seconds * 3:
            q.popleft()
        if len(q) >= self.max_failures:
            self._locked_until[client_key] = now + self.lockout_seconds
            q.clear()
            return LoginGateResult(
                False,
                self.lockout_seconds,
                f"Too many failed attempts. Retry in {self.lockout_seconds}s.",
            )
        return LoginGateResult(True)

    def record_success(self, client_key: str) -> None:
        self._failures.pop(client_key, None)
        self._locked_until.pop(client_key, None)


class SessionManager:
    """HMAC-signed session token. Secrets never leave the server."""

    def __init__(self, config: TradeMenuConfig) -> None:
        self.config = config
        self._secret = config.session_secret.encode("utf-8")

    def issue(self, subject: str = "operator") -> str:
        payload = {
            "sub": subject,
            "iat": int(time.time()),
            "exp": int(time.time()) + int(self.config.session_max_age_seconds),
        }
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        sig = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def verify(self, token: Optional[str]) -> bool:
        if not token or "." not in token:
            return False
        body, _, sig = token.partition(".")
        if not body or not sig:
            return False
        expected = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        try:
            raw = base64.urlsafe_b64decode(body.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return False
        exp = int(payload.get("exp") or 0)
        return exp >= int(time.time())

    def password_ok(self, candidate: str) -> bool:
        # Constant-time compare against configured password.
        a = hashlib.sha256(candidate.encode("utf-8")).digest()
        b = hashlib.sha256(self.config.password.encode("utf-8")).digest()
        return hmac.compare_digest(a, b)
