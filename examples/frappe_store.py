"""Example: a Frappe-backed Store — for teams that already run Frappe
and want sessions to live alongside everything else in the same site
database, with Desk visibility (list view, permissions, etc.) for free.

Requires a doctype "Astra Session" with fields:
    session_id    (Data, unique)
    user          (Data)
    last_active   (Float)
    ended_reason  (Data)
    data          (Long Text)   -- the whole Session, as JSON

Must run inside an initialized Frappe context (a request, a bench
console, or your own `frappe.init(site=...); frappe.connect()`) — same
requirement any Frappe code has, nothing astra-specific about it.
"""

from __future__ import annotations

import json

import frappe

from astra.storage import Session, Store  # Protocol — structural, no inheritance needed

DOCTYPE = "Astra Session"


class FrappeStore:
    def get(self, session_id: str) -> Session | None:
        if not frappe.db.exists(DOCTYPE, session_id):
            return None
        raw = frappe.db.get_value(DOCTYPE, session_id, "data")
        if not raw:
            return None
        return Session.from_dict(json.loads(raw))

    def save(self, session: Session) -> None:
        payload = json.dumps(session.to_dict(), default=str)
        if frappe.db.exists(DOCTYPE, session.session_id):
            frappe.db.set_value(
                DOCTYPE,
                session.session_id,
                {
                    "user": session.user,
                    "last_active": session.last_active,
                    "ended_reason": session.ended_reason,
                    "data": payload,
                },
                update_modified=False,
            )
        else:
            frappe.get_doc(
                {
                    "doctype": DOCTYPE,
                    "session_id": session.session_id,
                    "user": session.user,
                    "last_active": session.last_active,
                    "ended_reason": session.ended_reason,
                    "data": payload,
                }
            ).insert(ignore_permissions=True)
        frappe.db.commit()

    def delete(self, session_id: str) -> None:
        if frappe.db.exists(DOCTYPE, session_id):
            frappe.delete_doc(DOCTYPE, session_id, ignore_permissions=True)
            frappe.db.commit()

    def list_sessions(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[Session]:
        filters = {"user": user} if user else {}
        rows = frappe.get_list(
            DOCTYPE,
            filters=filters,
            fields=["data"],
            order_by="last_active desc",
            limit_page_length=limit,
            limit_start=offset,
        )
        return [Session.from_dict(json.loads(r["data"])) for r in rows]
