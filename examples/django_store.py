"""Example: a Django-backed Store.

Requires a model:

    class AstraSession(models.Model):
        session_id = models.CharField(max_length=64, primary_key=True)
        user = models.CharField(max_length=255)
        last_active = models.FloatField()
        ended_reason = models.CharField(max_length=255, null=True, blank=True)
        data = models.JSONField()

Django's ORM raises SynchronousOnlyOperation if called directly from
inside a running asyncio event loop — and astra's Conversation calls
store.save()/get() synchronously (no await) from inside Agent.run(),
which *is* an async coroutine. So every ORM call here goes through
asgiref's sync_to_async, run in Django's default thread pool, with the
async wrapper's result waited on synchronously (`async_to_sync` chained
back) so the Store protocol's plain sync methods still return a normal
value.

If you're calling astra from fully synchronous Django code (a
management command, a sync view via asyncio.run()), you don't need any
of this — but this Store has to work either way since it doesn't know
which context it'll be called from, so it always goes through the
thread pool.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync, sync_to_async

from astra.storage import Session, Store  # Protocol — structural, no inheritance needed

from .models import AstraSession  # your Django app's models.py


class DjangoStore:
    def get(self, session_id: str) -> Session | None:
        return async_to_sync(self._get_async)(session_id)

    async def _get_async(self, session_id: str) -> Session | None:
        row = await sync_to_async(AstraSession.objects.filter(session_id=session_id).first)()
        if row is None:
            return None
        return Session.from_dict(row.data)

    def save(self, session: Session) -> None:
        async_to_sync(self._save_async)(session)

    async def _save_async(self, session: Session) -> None:
        await sync_to_async(AstraSession.objects.update_or_create)(
            session_id=session.session_id,
            defaults={
                "user": session.user,
                "last_active": session.last_active,
                "ended_reason": session.ended_reason,
                "data": session.to_dict(),
            },
        )

    def delete(self, session_id: str) -> None:
        async_to_sync(sync_to_async(AstraSession.objects.filter(session_id=session_id).delete))()

    def list_sessions(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[Session]:
        return async_to_sync(self._list_async)(user, limit, offset)

    async def _list_async(self, user: str | None, limit: int, offset: int) -> list[Session]:
        def _query():
            qs = AstraSession.objects.all()
            if user is not None:
                qs = qs.filter(user=user)
            qs = qs.order_by("-last_active")[offset : offset + limit]
            return list(qs)

        rows = await sync_to_async(_query)()
        return [Session.from_dict(r.data) for r in rows]
