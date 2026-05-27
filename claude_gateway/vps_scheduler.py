from __future__ import annotations

import asyncio
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from fastapi import HTTPException

from .config import Settings


class VPSScheduleStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.account_data_file)
        self._lock = Lock()
        self._init_db()

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM vps_schedules ORDER BY created_at DESC").fetchall()
        return [_schedule_from_row(row) for row in rows]

    def create_schedule(self, values: dict[str, Any]) -> dict[str, Any]:
        schedule = _schedule_from_values(values)
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO vps_schedules (
                    id, name, start_at, days, on_hours, off_hours, active,
                    last_desired_state, last_action_at, last_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _schedule_values(schedule),
            )
            db.commit()
        return schedule

    def update_schedule(self, schedule_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM vps_schedules WHERE id = ?", (schedule_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="VPS schedule not found.")
            schedule = _schedule_from_row(row)
            if "active" in values:
                schedule["active"] = bool(values["active"])
            for key in ("name", "startAt", "start_at", "days", "onHours", "on_hours", "offHours", "off_hours"):
                if key in values:
                    schedule = _schedule_from_values({**schedule, **values, "id": schedule_id})
                    break
            db.execute(
                """
                UPDATE vps_schedules
                   SET name = ?, start_at = ?, days = ?, on_hours = ?, off_hours = ?,
                       active = ?, last_desired_state = ''
                 WHERE id = ?
                """,
                (
                    schedule["name"],
                    schedule["startAt"],
                    schedule["days"],
                    schedule["onHours"],
                    schedule["offHours"],
                    int(schedule["active"]),
                    schedule_id,
                ),
            )
            db.commit()
        return schedule

    def delete_schedule(self, schedule_id: str) -> dict[str, str]:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM vps_schedules WHERE id = ?", (schedule_id,))
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="VPS schedule not found.")
        return {"status": "deleted"}

    def status(self) -> dict[str, Any]:
        schedules = self.list_schedules()
        active = [item for item in schedules if item["active"]]
        desired = self._desired_state(active[0]) if active else {"desiredState": "off", "nextTransitionAt": ""}
        return {
            "configured": bool(self.settings.runpod_api_key and self.settings.runpod_pod_id),
            "runpodApiConfigured": bool(self.settings.runpod_api_key),
            "podId": self.settings.runpod_pod_id,
            "vllmBaseUrl": self.settings.vps_model_base_url,
            "activeSchedules": len(active),
            **desired,
        }

    async def tick(self) -> dict[str, Any]:
        schedules = [item for item in self.list_schedules() if item["active"]]
        if not schedules:
            return {"action": "none", "reason": "no_active_schedule", **self.status()}
        schedule = schedules[0]
        desired = self._desired_state(schedule)
        state = desired["desiredState"]
        if not self.settings.runpod_api_key or not self.settings.runpod_pod_id:
            self._record(schedule["id"], state, "RunPod credentials are not configured.")
            return {"action": "none", "reason": "runpod_not_configured", **desired}
        if state == schedule.get("lastDesiredState"):
            return {"action": "none", "reason": "already_requested", **desired}
        action = "start" if state == "on" else "stop"
        try:
            await self._runpod(action)
        except Exception as exc:
            self._record(schedule["id"], state, str(exc)[:500])
            return {"action": action, "status": "error", "error": str(exc)[:500], **desired}
        self._record(schedule["id"], state, "")
        return {"action": action, "status": "success", **desired}

    async def manual_action(self, action: str) -> dict[str, Any]:
        normalized = str(action or "").strip().lower()
        if normalized not in {"start", "stop"}:
            raise HTTPException(status_code=400, detail="Action must be start or stop.")
        if not self.settings.runpod_api_key or not self.settings.runpod_pod_id:
            raise HTTPException(status_code=400, detail="RunPod credentials are not configured.")
        await self._runpod(normalized)
        desired_state = "on" if normalized == "start" else "off"
        return {
            "action": normalized,
            "status": "success",
            **self.status(),
            "desiredState": desired_state,
            "message": "RunPod command sent. vLLM can still take a few minutes to finish loading the model.",
        }

    def _desired_state(self, schedule: dict[str, Any]) -> dict[str, str]:
        start_at = _parse_datetime(schedule.get("startAt"))
        if not start_at:
            return {"desiredState": "off", "nextTransitionAt": ""}
        now = datetime.now(UTC)
        days = max(1, int(schedule.get("days") or 1))
        on_hours = max(1, int(schedule.get("onHours") or 12))
        off_hours = max(1, int(schedule.get("offHours") or 12))
        end_at = start_at + timedelta(days=days)
        if now < start_at:
            return {"desiredState": "off", "nextTransitionAt": start_at.isoformat()}
        if now >= end_at:
            return {"desiredState": "off", "nextTransitionAt": ""}

        elapsed_hours = (now - start_at).total_seconds() / 3600
        cycle = on_hours + off_hours
        position = elapsed_hours % cycle
        cycle_start = now - timedelta(hours=position)
        if position < on_hours:
            next_at = min(cycle_start + timedelta(hours=on_hours), end_at)
            return {"desiredState": "on", "nextTransitionAt": next_at.isoformat()}
        next_at = min(cycle_start + timedelta(hours=cycle), end_at)
        return {"desiredState": "off", "nextTransitionAt": next_at.isoformat()}

    async def _runpod(self, action: str) -> None:
        url = f"https://rest.runpod.io/v1/pods/{self.settings.runpod_pod_id}/{action}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {self.settings.runpod_api_key}"})
        if response.status_code >= 400:
            raise RuntimeError(f"RunPod {action} failed with HTTP {response.status_code}: {response.text[:200]}")

    def _record(self, schedule_id: str, desired_state: str, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE vps_schedules
                   SET last_desired_state = ?, last_action_at = ?, last_error = ?
                 WHERE id = ?
                """,
                (desired_state, datetime.now(UTC).isoformat(), error, schedule_id),
            )
            db.commit()

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS vps_schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    on_hours INTEGER NOT NULL,
                    off_hours INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    last_desired_state TEXT NOT NULL DEFAULT '',
                    last_action_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db


async def vps_scheduler_loop(store: VPSScheduleStore) -> None:
    interval = max(30, int(store.settings.vps_scheduler_interval_seconds or 60))
    while True:
        await store.tick()
        await asyncio.sleep(interval)


def _schedule_from_values(values: dict[str, Any]) -> dict[str, Any]:
    start_at = _parse_datetime(values.get("startAt") or values.get("start_at"))
    if not start_at:
        raise HTTPException(status_code=400, detail="Informe startAt em ISO ou datetime-local.")
    days = max(1, min(365, int(float(values.get("days") or 1))))
    on_hours = max(1, min(24 * 30, int(float(values.get("onHours") or values.get("on_hours") or 12))))
    off_hours = max(1, min(24 * 30, int(float(values.get("offHours") or values.get("off_hours") or 12))))
    return {
        "id": str(values.get("id") or f"vps_{secrets.token_hex(12)}"),
        "name": str(values.get("name") or "Ciclo VPS").strip() or "Ciclo VPS",
        "startAt": start_at.isoformat(),
        "days": days,
        "onHours": on_hours,
        "offHours": off_hours,
        "active": bool(values.get("active", True)),
        "lastDesiredState": str(values.get("lastDesiredState") or ""),
        "lastActionAt": str(values.get("lastActionAt") or ""),
        "lastError": str(values.get("lastError") or ""),
        "createdAt": str(values.get("createdAt") or datetime.now(UTC).isoformat()),
    }


def _schedule_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "startAt": row["start_at"],
        "days": row["days"],
        "onHours": row["on_hours"],
        "offHours": row["off_hours"],
        "active": bool(row["active"]),
        "lastDesiredState": row["last_desired_state"],
        "lastActionAt": row["last_action_at"],
        "lastError": row["last_error"],
        "createdAt": row["created_at"],
    }


def _schedule_values(schedule: dict[str, Any]) -> tuple[Any, ...]:
    return (
        schedule["id"],
        schedule["name"],
        schedule["startAt"],
        schedule["days"],
        schedule["onHours"],
        schedule["offHours"],
        int(schedule["active"]),
        schedule["lastDesiredState"],
        schedule["lastActionAt"],
        schedule["lastError"],
        schedule["createdAt"],
    )


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
