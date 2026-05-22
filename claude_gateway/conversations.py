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
    if _is_literal_title(cleaned):
        return cleaned
    semantic = _semantic_title(cleaned)
    if semantic:
        return semantic
    if len(cleaned) <= 42:
        return cleaned
    return f"{cleaned[:42].rstrip()}..."


def _is_literal_title(text: str) -> bool:
    normalized = " ".join(text.lower().replace("?", "").replace("!", "").split())
    words = normalized.split()
    if len(words) <= 3:
        return True
    greetings = {
        "oi",
        "ola",
        "olá",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
        "oi tudo bem",
        "olá tudo bem",
        "ola tudo bem",
    }
    return normalized in greetings


def _semantic_title(text: str) -> str:
    value = text.strip()
    lowered = value.lower()
    greetings = ("bom dia", "boa tarde", "boa noite", "oi", "olá", "ola")
    for greeting in greetings:
        if lowered.startswith(f"{greeting},"):
            value = value[len(greeting) + 1 :].strip()
            lowered = value.lower()
            break

    patterns: list[tuple[tuple[str, ...], str]] = [
        (("crie ", "criar ", "faça ", "faca ", "monte ", "desenvolva "), "Criação de"),
        (("corrija ", "corrigir ", "arrume ", "conserte "), "Correção de"),
        (("analise ", "analisar ", "revise ", "revisar "), "Análise de"),
        (("planeje ", "planejar "), "Planejamento de"),
        (("explique ", "explica ", "me explique "), "Explicação sobre"),
        (("preciso de ", "preciso criar ", "quero criar ", "queria criar "), "Criação de"),
        (("quero entender ", "preciso entender ", "me ensine "), "Guia de"),
    ]
    for prefixes, label in patterns:
        for prefix in prefixes:
            if lowered.startswith(prefix):
                subject = value[len(prefix) :].strip(" .,:;!?")
                subject = _strip_leading_articles(subject)
                if subject:
                    return _clamp_title(f"{label} {_title_case_pt(subject)}")

    if any(word in lowered for word in ("bug", "erro", "falha", "quebr")):
        return "Correção de Bug"
    if any(word in lowered for word in ("app", "aplicativo", "site", "landing page", "sistema")):
        return "Planejamento do Projeto"
    return ""


def _strip_leading_articles(value: str) -> str:
    lowered = value.lower()
    for prefix in ("um ", "uma ", "o ", "a ", "os ", "as "):
        if lowered.startswith(prefix):
            return value[len(prefix) :]
    return value


def _title_case_pt(value: str) -> str:
    small = {"a", "o", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "e", "em", "para", "por", "com"}
    words = value.split()
    titled = []
    for index, word in enumerate(words):
        lowered = word.lower()
        if index > 0 and lowered in small:
            titled.append(lowered)
        else:
            titled.append(lowered[:1].upper() + lowered[1:])
    return " ".join(titled)


def _clamp_title(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= 54 else f"{cleaned[:54].rstrip()}..."


def _now() -> str:
    return datetime.now(UTC).isoformat()
