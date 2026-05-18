from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException

from .config import Settings


class ConversationStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.account_data_file)
        self._lock = Lock()
        self._init_db()

    def list_for_customer(self, token: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            account = self._account_by_token(db, token)
            rows = db.execute(
                """
                SELECT * FROM conversations
                 WHERE account_id = ?
                 ORDER BY updated_at DESC
                 LIMIT 80
                """,
                (account["id"],),
            ).fetchall()
        return [_conversation_from_row(row, include_messages=False) for row in rows]

    def get_for_customer(self, token: str, conversation_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            account = self._account_by_token(db, token)
            conversation = self._find_conversation(db, account["id"], conversation_id)
        return conversation

    def save_for_customer(self, token: str, values: dict[str, Any]) -> dict[str, Any]:
        messages = values.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="Messages are required.")

        safe_messages = [_safe_message(message) for message in messages if isinstance(message, dict)]
        if not safe_messages:
            raise HTTPException(status_code=400, detail="Messages are required.")
        if len(safe_messages) > 80:
            safe_messages = safe_messages[-80:]

        requested_id = str(values.get("id") or "").strip()
        now = _now()

        with self._lock, self._connect() as db:
            account = self._account_by_token(db, token)
            conversation_id = requested_id or f"chat_{secrets.token_hex(12)}"
            existing = (
                self._find_conversation(db, account["id"], conversation_id, required=False)
                if requested_id
                else None
            )
            title = str(values.get("title") or "").strip() or _title_from_messages(safe_messages)
            messages_json = json.dumps(safe_messages, ensure_ascii=False)

            if existing:
                db.execute(
                    """
                    UPDATE conversations
                       SET title = ?, messages_json = ?, updated_at = ?
                     WHERE id = ? AND account_id = ?
                    """,
                    (title, messages_json, now, conversation_id, account["id"]),
                )
            else:
                db.execute(
                    """
                    INSERT INTO conversations (
                        id, account_id, title, messages_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (conversation_id, account["id"], title, messages_json, now, now),
                )
            db.commit()
            return self._find_conversation(db, account["id"], conversation_id)

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA foreign_keys = ON")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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

    def _find_conversation(
        self,
        db: sqlite3.Connection,
        account_id: str,
        conversation_id: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT * FROM conversations WHERE id = ? AND account_id = ?",
            (conversation_id, account_id),
        ).fetchone()
        if not row:
            if required:
                raise HTTPException(status_code=404, detail="Conversation not found.")
            return None
        return _conversation_from_row(row)


def _conversation_from_row(row: sqlite3.Row, *, include_messages: bool = True) -> dict[str, Any]:
    conversation = {
        "id": row["id"],
        "accountId": row["account_id"],
        "title": row["title"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_messages:
        try:
            conversation["messages"] = json.loads(row["messages_json"])
        except json.JSONDecodeError:
            conversation["messages"] = []
    return conversation


def _safe_message(message: dict[str, Any]) -> dict[str, str]:
    role = str(message.get("role") or "").strip()
    if role not in {"user", "assistant"}:
        role = "user"
    content = _message_content_to_text(message.get("content"))
    return {"role": role, "content": content[:30000]}


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "image":
                parts.append("[imagem anexada]")
        return "\n".join(part for part in parts if part).strip()
    return str(content or "")


def _title_from_messages(messages: list[dict[str, str]]) -> str:
    first_user = next((message["content"] for message in messages if message["role"] == "user"), "")
    cleaned = " ".join(first_user.replace("\n", " ").split())
    cleaned = cleaned.split("Anexos:", 1)[0].strip()
    if not cleaned:
        return "Nova conversa"
    if len(cleaned) <= 54:
        return cleaned
    return f"{cleaned[:54].rstrip()}..."


def _now() -> str:
    return datetime.now(UTC).isoformat()
