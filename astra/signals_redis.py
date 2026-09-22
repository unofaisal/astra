"""Redis-backed SignalStore — share Stop flags and session locks across
worker processes.

    import redis
    from astra.signals_redis import RedisSignalStore
    signals = RedisSignalStore(redis.Redis.from_url("redis://localhost:6379/0"))
    app = create_astra(..., signals=signals)

Uses the synchronous ``redis`` client (a call is ~sub-millisecond; astra's
signal methods are synchronous by contract).  ``set_if_absent`` is
``SET key value NX PX ttl`` — the atomic primitive behind the cross-process
session lock (see ``astra.sessions.SessionGuard``).  Install with
``pip install astra[redis]``.
"""

from __future__ import annotations

from typing import Any, Optional


class RedisSignalStore:
    def __init__(self, client: Any = None, *, url: Optional[str] = None, prefix: str = "") -> None:
        if client is None:
            try:
                import redis  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("RedisSignalStore needs the 'redis' package (pip install astra[redis])") from exc
            client = redis.Redis.from_url(url or "redis://localhost:6379/0")
        self._r = client
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return self._prefix + key

    @staticmethod
    def _px(ttl_seconds: Optional[float]) -> Optional[int]:
        return max(1, int(ttl_seconds * 1000)) if ttl_seconds else None

    def set(self, key: str, value: str, ttl_seconds: float | None = None) -> None:
        self._r.set(self._k(key), value, px=self._px(ttl_seconds))

    def get(self, key: str) -> str | None:
        v = self._r.get(self._k(key))
        if v is None:
            return None
        return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)

    def delete(self, key: str) -> None:
        self._r.delete(self._k(key))

    def set_if_absent(self, key: str, value: str, ttl_seconds: float | None = None) -> bool:
        return bool(self._r.set(self._k(key), value, nx=True, px=self._px(ttl_seconds)))
