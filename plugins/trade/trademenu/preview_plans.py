"""Immutable short-lived order/ladder preview plans for TradeMenu."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from threading import Lock
from typing import Any, Dict, Optional, Set, Tuple


class PreviewPlanStore:
    """HMAC-signed preview tokens + one-shot consume set.

    Plan body is embedded in the token (stateless across workers) while
    consume tracking is process-local to block double execution.
    """

    def __init__(self, secret: str, ttl_seconds: int = 300) -> None:
        self._secret = (secret or "").encode("utf-8")
        self.ttl_seconds = int(ttl_seconds)
        self._consumed: Set[str] = set()
        self._lock = Lock()

    def issue(self, plan: Dict[str, Any]) -> str:
        body = dict(plan)
        body["iat"] = int(time.time())
        body["exp"] = int(time.time()) + self.ttl_seconds
        body["nonce"] = secrets.token_urlsafe(12)
        raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")).decode(
            "ascii"
        )
        sig = hmac.new(self._secret, raw.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def _decode(self, token: str) -> Optional[Dict[str, Any]]:
        if not token or "." not in token:
            return None
        raw, _, sig = token.partition(".")
        if not raw or not sig:
            return None
        expected = hmac.new(self._secret, raw.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        return payload

    def peek(self, token: str) -> Optional[Dict[str, Any]]:
        return self._decode(token)

    def consume(self, token: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Return (plan, error_code). error_code is EXPIRED|INVALID|CONSUMED."""
        plan = self._decode(token)
        if plan is None:
            # Distinguish expired vs bad signature roughly
            if token and "." in token:
                raw, _, sig = token.partition(".")
                try:
                    payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
                    if isinstance(payload, dict) and int(payload.get("exp") or 0) < int(time.time()):
                        expected = hmac.new(self._secret, raw.encode("ascii"), hashlib.sha256).hexdigest()
                        if hmac.compare_digest(expected, sig):
                            return None, "PREVIEW_EXPIRED"
                except Exception:
                    pass
            return None, "PREVIEW_INVALID"
        nonce = str(plan.get("nonce") or "")
        with self._lock:
            if nonce in self._consumed:
                return None, "PREVIEW_CONSUMED"
            self._consumed.add(nonce)
            # Bound memory
            if len(self._consumed) > 5000:
                self._consumed = set(list(self._consumed)[-2500:])
        return plan, None
