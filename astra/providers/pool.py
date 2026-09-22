"""Shared LLM clients and per-endpoint gates.

Before: every ``build_agent`` (i.e. every chat turn) and every delegate
call constructed a brand-new ``AsyncOpenAI`` — a new HTTP connection pool,
a new TLS handshake, ~20 ms of blocking CPU, and an un-closed pool left to
the GC.  Now clients are cached and shared.

Cache key
  * endpoint (provider + base URL), a hash of the API key (secrets are
    never stored as dict keys), the timeout settings — and the **running
    event loop**: an httpx pool is bound to the loop that first used it,
    so callers that run ``asyncio.run()`` per request (Django/Frappe sync
    views) each get a client for their own loop instead of "Event loop is
    closed" errors.

Gates (shared across loops/threads)
  * an optional cap on concurrent requests to an endpoint/key
    (``AgentConfig.llm_max_concurrency``) with a bounded wait;
  * a circuit breaker so a dead/rate-limited provider fails fast instead
    of every session burning its full retry budget against it.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..concurrency import CircuitBreaker, Limiter


def _build_timeout(total: float, connect: float) -> Any:
    """A per-request-total + per-connect timeout, without hard-depending on
    httpx: the openai SDK vendors its own HTTP transport (its exact package
    name has changed across SDK generations — e.g. some builds use a
    transport package other than plain ``httpx``), so importing ``httpx``
    directly here can fail even though the SDK itself works fine. Try the
    transport object openai itself would build one from; fall back to a
    plain float (every openai SDK generation accepts a bare number as a
    total-request timeout)."""
    for modname in ("httpx", "httpx2"):
        try:
            mod = __import__(modname)
            return mod.Timeout(total, connect=connect)
        except ImportError:
            continue
    return total


@dataclass
class LLMGate:
    limiter: Optional[Limiter]
    breaker: CircuitBreaker
    queue_timeout: float = 30.0


class ProviderUnavailable(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"LLM provider temporarily unavailable (circuit open); retry in ~{int(retry_after) + 1}s")
        self.retry_after = retry_after


def _key_hash(api_key: Optional[str]) -> str:
    return hashlib.sha256((api_key or "").encode()).hexdigest()[:16]


class ProviderPool:
    def __init__(self, max_clients: int = 256) -> None:
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._by_loop: "weakref.WeakKeyDictionary[Any, Dict[Tuple, Any]]" = weakref.WeakKeyDictionary()
        self._gates: Dict[Tuple, LLMGate] = {}

    # ── clients ──────────────────────────────────────────────────
    def client_for(self, config: Any, base_url: str, provider_name: str) -> Any:
        from openai import AsyncOpenAI

        creds = config.credentials
        key = (
            provider_name,
            base_url,
            _key_hash(creds.api_key),
            float(getattr(config, "request_timeout", 300.0)),
            float(getattr(config, "connect_timeout", 10.0)),
            int(getattr(config, "sdk_max_retries", 0)),
        )

        def make() -> Any:
            timeout = _build_timeout(float(getattr(config, "request_timeout", 300.0)), float(getattr(config, "connect_timeout", 10.0)))
            return AsyncOpenAI(
                api_key=creds.api_key,
                base_url=base_url,
                max_retries=int(getattr(config, "sdk_max_retries", 0)),
                timeout=timeout,
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return make()  # no loop yet: don't share (would bind to the wrong loop)
        with self._lock:
            clients = self._by_loop.setdefault(loop, {})
            client = clients.get(key)
            if client is None:
                if len(clients) >= self.max_clients:
                    clients.pop(next(iter(clients)))
                client = clients[key] = make()
            return client

    def client_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._by_loop.values())

    async def aclose(self) -> None:
        """Close the clients that belong to the *current* loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._lock:
            clients = list(self._by_loop.pop(loop, {}).values())
        for c in clients:
            try:
                await c.close()
            except Exception:
                pass

    # ── gates ────────────────────────────────────────────────────
    def gate_for(self, config: Any, base_url: str, provider_name: str) -> LLMGate:
        creds = config.credentials
        key = (provider_name, base_url, _key_hash(creds.api_key))
        with self._lock:
            gate = self._gates.get(key)
            if gate is None:
                cap = getattr(config, "llm_max_concurrency", None)
                gate = self._gates[key] = LLMGate(
                    limiter=Limiter(cap, max_queue=10_000, name=f"llm:{provider_name}") if cap else None,
                    breaker=CircuitBreaker(getattr(config, "llm_breaker_threshold", 8), getattr(config, "llm_breaker_cooldown", 20.0)),
                    queue_timeout=float(getattr(config, "llm_queue_timeout", 30.0)),
                )
            return gate

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "clients": sum(len(v) for v in self._by_loop.values()),
                "gates": {
                    f"{k[0]}@{k[1]}": {
                        "breaker": g.breaker.state,
                        "active": g.limiter.active if g.limiter else None,
                        "waiting": g.limiter.waiting if g.limiter else None,
                    }
                    for k, g in self._gates.items()
                },
            }


_default_pool = ProviderPool()


def get_default_pool() -> ProviderPool:
    return _default_pool