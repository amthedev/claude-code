from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException

from .config import Settings


class SupportStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.account_data_file)
        self._lock = Lock()
        self._init_db()

    def current_for_customer(self, token: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            account = self._account_by_token(db, token)
            row = db.execute(
                """
                SELECT * FROM support_tickets
                 WHERE account_id = ? AND status IN ('ai', 'waiting', 'active')
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (account["id"],),
            ).fetchone()
            if not row:
                return None
            return self._ticket_with_messages(db, _ticket_from_row(row))

    def open_ticket(self, token: str, values: dict[str, Any]) -> dict[str, Any]:
        message = str(values.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="Message is required.")
        if len(message) > 4000:
            raise HTTPException(status_code=413, detail="Message is too long.")

        with self._lock, self._connect() as db:
            account = self._account_by_token(db, token)
            existing = db.execute(
                """
                SELECT * FROM support_tickets
                 WHERE account_id = ? AND status IN ('ai', 'waiting', 'active')
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (account["id"],),
            ).fetchone()
            if existing:
                ticket = _ticket_from_row(existing)
                self._add_message(db, ticket["id"], "customer", account["name"], message)
                db.commit()
                return self._ticket_with_messages(db, ticket)

            now = _now()
            ticket = {
                "id": f"sup_{secrets.token_hex(12)}",
                "accountId": account["id"],
                "customerName": account["name"],
                "customerLogin": account["login"],
                "status": "ai",
                "subject": message[:90],
                "createdAt": now,
                "updatedAt": now,
                "closedAt": "",
            }
            db.execute(
                """
                INSERT INTO support_tickets (
                    id, account_id, customer_name, customer_login, status,
                    subject, created_at, updated_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _ticket_values(ticket),
            )
            self._add_message(db, ticket["id"], "customer", account["name"], message, now=now)
            db.commit()
            return self._ticket_with_messages(db, ticket)

    def customer_message(self, token: str, ticket_id: str, values: dict[str, Any]) -> dict[str, Any]:
        message = str(values.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="Message is required.")
        with self._lock, self._connect() as db:
            account = self._account_by_token(db, token)
            ticket = self._find_ticket(db, ticket_id)
            if ticket["accountId"] != account["id"]:
                raise HTTPException(status_code=404, detail="Ticket not found.")
            if ticket["status"] == "closed":
                raise HTTPException(status_code=409, detail="Ticket is closed.")
            self._add_message(db, ticket_id, "customer", account["name"], message)
            self._touch_ticket(db, ticket_id)
            db.commit()
            return self._ticket_with_messages(db, self._find_ticket(db, ticket_id))

    def ai_message(self, ticket_id: str, message: str) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="Message is required.")
        with self._lock, self._connect() as db:
            ticket = self._find_ticket(db, ticket_id)
            if ticket["status"] == "closed":
                raise HTTPException(status_code=409, detail="Ticket is closed.")
            self._add_message(db, ticket_id, "support", "Assistente", message[:4000])
            self._touch_ticket(db, ticket_id)
            db.commit()
            return self._ticket_with_messages(db, self._find_ticket(db, ticket_id))

    def escalate_to_human(self, ticket_id: str, message: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as db:
            ticket = self._find_ticket(db, ticket_id)
            if ticket["status"] == "closed":
                raise HTTPException(status_code=409, detail="Ticket is closed.")
            if message.strip():
                self._add_message(db, ticket_id, "support", "Assistente", message.strip()[:4000])
            db.execute(
                "UPDATE support_tickets SET status = 'waiting', updated_at = ? WHERE id = ?",
                (_now(), ticket_id),
            )
            db.commit()
            return self._ticket_with_messages(db, self._find_ticket(db, ticket_id))

    def list_admin_tickets(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock, self._connect() as db:
            waiting_rows = db.execute(
                "SELECT * FROM support_tickets WHERE status = 'waiting' ORDER BY created_at ASC"
            ).fetchall()
            active_rows = db.execute(
                "SELECT * FROM support_tickets WHERE status = 'active' ORDER BY updated_at DESC"
            ).fetchall()
            closed_rows = db.execute(
                "SELECT * FROM support_tickets WHERE status = 'closed' ORDER BY closed_at DESC LIMIT 12"
            ).fetchall()
            return {
                "waiting": [self._ticket_with_messages(db, _ticket_from_row(row)) for row in waiting_rows],
                "active": [self._ticket_with_messages(db, _ticket_from_row(row)) for row in active_rows],
                "closed": [self._ticket_with_messages(db, _ticket_from_row(row)) for row in closed_rows],
            }

    def claim_ticket(self, ticket_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            ticket = self._find_ticket(db, ticket_id)
            active = db.execute("SELECT id FROM support_tickets WHERE status = 'active' LIMIT 1").fetchone()
            if active and active["id"] != ticket_id:
                raise HTTPException(status_code=409, detail="Finish the active support chat first.")
            if ticket["status"] == "closed":
                raise HTTPException(status_code=409, detail="Ticket is closed.")
            if ticket["status"] == "waiting":
                db.execute(
                    "UPDATE support_tickets SET status = 'active', updated_at = ? WHERE id = ?",
                    (_now(), ticket_id),
                )
                db.commit()
            return self._ticket_with_messages(db, self._find_ticket(db, ticket_id))

    def admin_message(self, ticket_id: str, values: dict[str, Any]) -> dict[str, Any]:
        message = str(values.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="Message is required.")
        with self._lock, self._connect() as db:
            ticket = self._find_ticket(db, ticket_id)
            if ticket["status"] != "active":
                raise HTTPException(status_code=409, detail="Claim the ticket before replying.")
            self._add_message(db, ticket_id, "support", "Suporte", message)
            self._touch_ticket(db, ticket_id)
            db.commit()
            return self._ticket_with_messages(db, self._find_ticket(db, ticket_id))

    def close_ticket(self, ticket_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            ticket = self._find_ticket(db, ticket_id)
            if ticket["status"] == "closed":
                return self._ticket_with_messages(db, ticket)
            now = _now()
            db.execute(
                "UPDATE support_tickets SET status = 'closed', updated_at = ?, closed_at = ? WHERE id = ?",
                (now, now, ticket_id),
            )
            db.commit()
            return self._ticket_with_messages(db, self._find_ticket(db, ticket_id))

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA foreign_keys = ON")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    customer_name TEXT NOT NULL,
                    customer_login TEXT NOT NULL,
                    status TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS support_messages (
                    id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE
                )
                """
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _account_by_token(self, db: sqlite3.Connection, token: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
        if not row:
            raise HTTPException(status_code=403, detail="Invalid customer token.")
        if not row["active"]:
            raise HTTPException(status_code=403, detail="Account is paused.")
        return row

    def _find_ticket(self, db: sqlite3.Connection, ticket_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ticket not found.")
        return _ticket_from_row(row)

    def _add_message(
        self,
        db: sqlite3.Connection,
        ticket_id: str,
        sender: str,
        author: str,
        body: str,
        *,
        now: str | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO support_messages (id, ticket_id, sender, author, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"msg_{secrets.token_hex(12)}", ticket_id, sender, author, body, now or _now()),
        )

    def _touch_ticket(self, db: sqlite3.Connection, ticket_id: str) -> None:
        db.execute("UPDATE support_tickets SET updated_at = ? WHERE id = ?", (_now(), ticket_id))

    def _ticket_with_messages(self, db: sqlite3.Connection, ticket: dict[str, Any]) -> dict[str, Any]:
        messages = db.execute(
            "SELECT * FROM support_messages WHERE ticket_id = ? ORDER BY created_at ASC",
            (ticket["id"],),
        ).fetchall()
        return {**ticket, "messages": [_message_from_row(row) for row in messages]}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ticket_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "accountId": row["account_id"],
        "customerName": row["customer_name"],
        "customerLogin": row["customer_login"],
        "status": row["status"],
        "subject": row["subject"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "closedAt": row["closed_at"],
    }


def _ticket_values(ticket: dict[str, Any]) -> tuple[Any, ...]:
    return (
        ticket["id"],
        ticket["accountId"],
        ticket["customerName"],
        ticket["customerLogin"],
        ticket["status"],
        ticket["subject"],
        ticket["createdAt"],
        ticket["updatedAt"],
        ticket["closedAt"],
    )


def _message_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticketId": row["ticket_id"],
        "sender": row["sender"],
        "author": row["author"],
        "body": row["body"],
        "createdAt": row["created_at"],
    }
