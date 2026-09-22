"""Per-user / per-tenant usage quotas.

``Quota`` is a tiny protocol the agent calls before every LLM request
(``check``) and after it (``record``).  ``InMemoryQuota`` is a fixed-window
implementation good for a single process; back the same two methods with
Redis/SQL for a cluster-wide budget.

A tripped quota ends the run with ``error="quota_exceeded"`` — no request
is sent to the provider.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Tuple


class QuotaExceeded(Exception):
    def __init__(self, scope: str, dimension: str, limit: float, retry_after: float) -> None:
        super().__init__(f"{dimension} quota exceeded for {scope} (limit {limit:g}); retry in {int(retry_after)}s")
        self.scope = scope
        self.dimension = dimension
        self.limit = limit
        self.retry_after = retry_after


class Quota(Protocol):
    def check(self, user: Optional[str], tenant: Optional[str]) -> None: ...

    def record(self, user: Optional[str], tenant: Optional[str], input_tokens: int, output_tokens: int, cost: Optional[float]) -> None: ...


@dataclass
class QuotaLimits:
    max_tokens: Optional[int] = None  # input + output
    max_cost: Optional[float] = None
    max_requests: Optional[int] = None
    window_seconds: float = 3600.0


class InMemoryQuota:
    """Fixed-window limits per user and/or per tenant."""

    def __init__(self, *, per_user: Optional[QuotaLimits] = None, per_tenant: Optional[QuotaLimits] = None) -> None:
        self.per_user = per_user
        self.per_tenant = per_tenant
        self._lock = threading.Lock()
        # (scope_kind, id) -> [window_start, tokens, cost, requests]
        self._usage: Dict[Tuple[str, str], list] = {}

    def _slots(self, user, tenant):
        if self.per_user and user:
            yield ("user", user), self.per_user
        if self.per_tenant and tenant:
            yield ("tenant", tenant), self.per_tenant

    def _get(self, key, limits) -> list:
        now = time.monotonic()
        u = self._usage.get(key)
        if u is None or now - u[0] >= limits.window_seconds:
            u = self._usage[key] = [now, 0, 0.0, 0]
        return u

    def check(self, user, tenant) -> None:
        with self._lock:
            for key, lim in self._slots(user, tenant):
                start, tokens, cost, reqs = self._get(key, lim)
                retry = max(0.0, lim.window_seconds - (time.monotonic() - start))
                scope = f"{key[0]} '{key[1]}'"
                if lim.max_tokens is not None and tokens >= lim.max_tokens:
                    raise QuotaExceeded(scope, "token", lim.max_tokens, retry)
                if lim.max_cost is not None and cost >= lim.max_cost:
                    raise QuotaExceeded(scope, "cost", lim.max_cost, retry)
                if lim.max_requests is not None and reqs >= lim.max_requests:
                    raise QuotaExceeded(scope, "request", lim.max_requests, retry)

    def record(self, user, tenant, input_tokens, output_tokens, cost) -> None:
        with self._lock:
            for key, lim in self._slots(user, tenant):
                u = self._get(key, lim)
                u[1] += int(input_tokens or 0) + int(output_tokens or 0)
                u[2] += float(cost or 0.0)
                u[3] += 1

    def usage(self, kind: str, ident: str) -> dict:
        with self._lock:
            u = self._usage.get((kind, ident))
            return {"tokens": u[1], "cost": u[2], "requests": u[3]} if u else {"tokens": 0, "cost": 0.0, "requests": 0}
