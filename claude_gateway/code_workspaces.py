from __future__ import annotations

import base64
import io
import json
import secrets
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .config import Settings


MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_FILE_BYTES = 800 * 1024
MAX_FILES = 1500


class CodeWorkspaceStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = Path(settings.account_data_file)
        self.root = self.db_path.parent / "code_workspaces"
        self.root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def list_for_customer(self, token: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            account = self._account_by_token(db, token)
            rows = db.execute(
                """
                SELECT * FROM code_workspaces
                 WHERE account_id = ?
                 ORDER BY updated_at DESC
                """,
                (account["id"],),
            ).fetchall()
        return [_workspace_from_row(row) for row in rows]

    def create_from_zip(
        self,
        token: str,
        *,
        name: str,
        zip_bytes: bytes,
        source: str = "upload",
        repo_url: str = "",
        ref: str = "",
    ) -> dict[str, Any]:
        if len(zip_bytes) > MAX_ARCHIVE_BYTES:
            raise HTTPException(status_code=413, detail="ZIP maior que 25 MB.")

        workspace_id = f"code_{secrets.token_hex(12)}"
        now = _now()
        with self._connect() as db:
            account = self._account_by_token(db, token)
            workspace_dir = self._workspace_dir(account["id"], workspace_id)
            workspace_dir.mkdir(parents=True, exist_ok=True)
            file_count = _extract_zip(zip_bytes, workspace_dir)
            display_name = (name or _name_from_repo(repo_url) or "Projeto de código").strip()[:120]
            db.execute(
                """
                INSERT INTO code_workspaces (
                    id, account_id, name, source, repo_url, ref, file_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    account["id"],
                    display_name,
                    source,
                    repo_url,
                    ref,
                    file_count,
                    now,
                    now,
                ),
            )
            db.commit()
            return _workspace_from_row(
                db.execute("SELECT * FROM code_workspaces WHERE id = ?", (workspace_id,)).fetchone()
            )

    def create_from_base64_zip(self, token: str, values: dict[str, Any]) -> dict[str, Any]:
        raw = str(values.get("zipBase64") or values.get("zip_base64") or "")
        if "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            zip_bytes = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="ZIP inválido.") from exc
        return self.create_from_zip(
            token,
            name=str(values.get("name") or ""),
            zip_bytes=zip_bytes,
            source="upload",
        )

    def list_files(self, token: str, workspace_id: str) -> dict[str, Any]:
        workspace, workspace_dir = self._workspace_for_token(token, workspace_id)
        files: list[dict[str, Any]] = []
        for path in sorted(workspace_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace_dir).as_posix()
            if _ignored_path(relative):
                continue
            files.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "editable": path.stat().st_size <= MAX_FILE_BYTES and _looks_text(path),
                }
            )
            if len(files) >= MAX_FILES:
                break
        return {"workspace": workspace, "files": files}

    def read_file(self, token: str, workspace_id: str, file_path: str) -> dict[str, Any]:
        workspace, workspace_dir = self._workspace_for_token(token, workspace_id)
        target = _safe_child(workspace_dir, file_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Arquivo grande demais para editar no navegador.")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="Arquivo binário não editável.") from exc
        return {"workspace": workspace, "path": file_path, "content": content}

    def write_file(self, token: str, workspace_id: str, values: dict[str, Any]) -> dict[str, Any]:
        workspace, workspace_dir = self._workspace_for_token(token, workspace_id)
        file_path = str(values.get("path") or "").strip()
        content = str(values.get("content") or "")
        if not file_path:
            raise HTTPException(status_code=400, detail="Caminho do arquivo é obrigatório.")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Arquivo grande demais para salvar.")
        target = _safe_child(workspace_dir, file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        now = _now()
        with self._connect() as db:
            db.execute(
                "UPDATE code_workspaces SET updated_at = ? WHERE id = ?",
                (now, workspace_id),
            )
            db.commit()
        return {"workspace": {**workspace, "updatedAt": now}, "path": file_path, "saved": True}

    def zip_bytes_for(self, token: str, workspace_id: str) -> tuple[str, bytes]:
        workspace, workspace_dir = self._workspace_for_token(token, workspace_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(workspace_dir.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(workspace_dir).as_posix()
                    if not _ignored_path(relative):
                        archive.write(path, relative)
        filename = f"{_slug(workspace['name']) or 'projeto'}-{workspace_id}.zip"
        return filename, buffer.getvalue()

    def _workspace_for_token(self, token: str, workspace_id: str) -> tuple[dict[str, Any], Path]:
        with self._connect() as db:
            account = self._account_by_token(db, token)
            row = db.execute(
                "SELECT * FROM code_workspaces WHERE id = ? AND account_id = ?",
                (workspace_id, account["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Workspace não encontrado.")
            workspace = _workspace_from_row(row)
        workspace_dir = self._workspace_dir(account["id"], workspace_id)
        if not workspace_dir.exists():
            raise HTTPException(status_code=404, detail="Arquivos do workspace não encontrados.")
        return workspace, workspace_dir

    def _workspace_dir(self, account_id: str, workspace_id: str) -> Path:
        return self.root / _slug(account_id) / workspace_id

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS code_workspaces (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    repo_url TEXT NOT NULL DEFAULT '',
                    ref TEXT NOT NULL DEFAULT '',
                    file_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _account_by_token(self, db: sqlite3.Connection, token: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
        if not row:
            raise HTTPException(status_code=403, detail="Token de cliente inválido.")
        if not row["active"]:
            raise HTTPException(status_code=403, detail="Conta pausada.")
        return row


def github_repo_parts(repo_url: str) -> tuple[str, str]:
    value = repo_url.strip().rstrip("/")
    marker = "github.com/"
    if marker not in value:
        raise HTTPException(status_code=400, detail="Informe uma URL do GitHub.")
    tail = value.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in tail.split("/") if part]
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="URL do GitHub inválida.")
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    return owner, repo


def _extract_zip(zip_bytes: bytes, target_dir: Path) -> int:
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="ZIP inválido.") from exc

    file_infos = [info for info in archive.infolist() if not info.is_dir()]
    if len(file_infos) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"ZIP excede {MAX_FILES} arquivos.")
    if sum(info.file_size for info in file_infos) > MAX_ARCHIVE_BYTES * 4:
        raise HTTPException(status_code=413, detail="ZIP descompactado grande demais.")

    root_prefix = _common_root_prefix([info.filename for info in file_infos])
    count = 0
    try:
        for info in file_infos:
            relative = info.filename
            if root_prefix and relative.startswith(root_prefix):
                relative = relative[len(root_prefix) :]
            relative = relative.strip("/")
            if not relative or _ignored_path(relative):
                continue
            output = _safe_child(target_dir, relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            count += 1
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    return count


def _safe_child(root: Path, relative_path: str) -> Path:
    if not relative_path or "\x00" in relative_path:
        raise HTTPException(status_code=400, detail="Caminho inválido.")
    target = (root / relative_path).resolve()
    root_resolved = root.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise HTTPException(status_code=400, detail="Caminho fora do workspace.")
    return target


def _common_root_prefix(paths: list[str]) -> str:
    first_parts = paths[0].split("/") if paths else []
    if len(first_parts) < 2:
        return ""
    root = first_parts[0]
    if root and all(path.startswith(f"{root}/") for path in paths):
        return f"{root}/"
    return ""


def _looks_text(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\x00" not in sample


def _ignored_path(path: str) -> bool:
    parts = path.split("/")
    return any(part in {".git", "__pycache__", "node_modules", ".venv"} for part in parts)


def _workspace_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "accountId": row["account_id"],
        "name": row["name"],
        "source": row["source"],
        "repoUrl": row["repo_url"],
        "ref": row["ref"],
        "fileCount": row["file_count"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _name_from_repo(repo_url: str) -> str:
    try:
        owner, repo = github_repo_parts(repo_url)
    except HTTPException:
        return ""
    return f"{owner}/{repo}"


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value).strip("-")


def _now() -> str:
    return datetime.now(UTC).isoformat()
