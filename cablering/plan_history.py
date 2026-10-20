"""SQLite persistence for shared, completed plan generations."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PlanHistoryStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_plans (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    building_ref TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT UNIQUE,
                    updated_at TEXT
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(saved_plans)")
            }
            if "updated_at" not in columns:
                connection.execute("ALTER TABLE saved_plans ADD COLUMN updated_at TEXT")
            connection.execute(
                "UPDATE saved_plans SET updated_at = created_at WHERE updated_at IS NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS saved_plans_created_at_idx "
                "ON saved_plans(created_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_plan_generations (
                    idempotency_key TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES saved_plans(id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "building_ref": row["building_ref"],
            "metadata": json.loads(row["metadata_json"]),
            "metrics": json.loads(row["metrics_json"]),
            "last_accessed_at": row["last_accessed_at"],
            "access_count": row["access_count"],
        }

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        return {
            **PlanHistoryStore._metadata(row),
            "input": json.loads(row["input_json"]),
            "result": json.loads(row["result_json"]),
        }

    def find_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT saved_plans.* FROM saved_plans
                LEFT JOIN saved_plan_generations
                    ON saved_plan_generations.plan_id = saved_plans.id
                WHERE saved_plans.idempotency_key = ?
                    OR saved_plan_generations.idempotency_key = ?
                ORDER BY saved_plans.updated_at DESC, saved_plans.created_at DESC LIMIT 1
                """,
                (idempotency_key, idempotency_key),
            ).fetchone()
        return self._record(row) if row else None

    def latest_for_building_ref(self, building_ref: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM saved_plans
                WHERE building_ref = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (building_ref,),
            ).fetchone()
        return self._metadata(row) if row else None

    def save(
        self,
        *,
        name: str,
        building_ref: str,
        input_data: dict,
        result: dict,
        metadata: dict,
        metrics: dict,
        idempotency_key: str,
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved_at = datetime.now(timezone.utc).isoformat()
            existing = connection.execute(
                """
                SELECT * FROM saved_plans WHERE building_ref = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT 1
                """,
                (building_ref,),
            ).fetchone()
            if existing:
                first_created = connection.execute(
                    "SELECT MIN(created_at) FROM saved_plans WHERE building_ref = ?",
                    (building_ref,),
                ).fetchone()[0]
                connection.execute(
                    """
                    UPDATE saved_plans
                    SET created_at = ?, input_json = ?, result_json = ?,
                        metadata_json = ?, metrics_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        first_created,
                        self._json(input_data),
                        self._json(result),
                        self._json(metadata),
                        self._json(metrics),
                        saved_at,
                        existing["id"],
                    ),
                )
                record_id = existing["id"]
            else:
                record_id = uuid.uuid4().hex
                created_at = saved_at
                connection.execute(
                    """
                    INSERT INTO saved_plans (
                        id, name, created_at, building_ref, input_json, result_json,
                        metadata_json, metrics_json, idempotency_key, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        name,
                        created_at,
                        building_ref,
                        self._json(input_data),
                        self._json(result),
                        self._json(metadata),
                        self._json(metrics),
                        idempotency_key,
                        saved_at,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM saved_plans WHERE id = ?", (record_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT OR IGNORE INTO saved_plan_generations (idempotency_key, plan_id)
                VALUES (?, ?)
                """,
                (idempotency_key, record_id),
            )
            generation = connection.execute(
                "SELECT plan_id FROM saved_plan_generations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if generation["plan_id"] != record_id:
                raise sqlite3.IntegrityError("Generation key is already assigned to another plan")
        return self._record(row)

    def list(self, query: str = "", sort: str = "newest") -> list[dict]:
        if sort not in {"newest", "oldest", "recently_accessed"}:
            raise ValueError(f"Unsupported history sort: {sort}")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM saved_plans ORDER BY updated_at DESC, created_at DESC, id DESC"
            ).fetchall()

        unique: dict[str, dict] = {}
        for row in rows:
            item = self._metadata(row)
            item["metadata_source"] = row["metadata_json"]
            if row["last_accessed_at"]:
                item["accessed_at_values"] = [row["last_accessed_at"]]
            else:
                item["accessed_at_values"] = []
            item["access_count_total"] = row["access_count"]
            existing = unique.get(row["building_ref"])
            if existing is None:
                unique[row["building_ref"]] = item
            else:
                existing["created_at"] = min(existing["created_at"], row["created_at"])
                existing["access_count_total"] += row["access_count"]
                existing["accessed_at_values"].extend(item["accessed_at_values"])
                existing["last_accessed_at"] = max(
                    existing["accessed_at_values"], default=None
                )
        plans = list(unique.values())
        if query:
            needle = query.casefold()
            plans = [
                item for item in plans
                if any(
                    needle in str(value).casefold()
                    for value in (
                        item["name"],
                        item["id"],
                        item["building_ref"],
                        item["metadata_source"],
                    )
                )
            ]
        for item in plans:
            item["access_count"] = item.pop("access_count_total")
            item.pop("metadata_source")
            item.pop("accessed_at_values")
        if sort == "newest":
            plans.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        elif sort == "oldest":
            plans.sort(key=lambda item: (item["created_at"], item["id"]))
        else:
            plans.sort(
                key=lambda item: (
                    item["last_accessed_at"] is not None,
                    item["last_accessed_at"] or "",
                    item["created_at"],
                ),
                reverse=True,
            )
        return plans

    def get(self, record_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM saved_plans WHERE id = ?", (record_id,)
            ).fetchone()
        return self._record(row) if row else None

    def record_access(self, record_id: str) -> dict | None:
        accessed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE saved_plans
                SET last_accessed_at = ?, access_count = access_count + 1
                WHERE id = ?
                """,
                (accessed_at, record_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM saved_plans WHERE id = ?", (record_id,)
            ).fetchone()
        return self._metadata(row)

    def rename(self, record_id: str, name: str) -> dict | None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE saved_plans SET name = ? WHERE id = ?", (name, record_id)
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM saved_plans WHERE id = ?", (record_id,)
            ).fetchone()
        return self._metadata(row)

    def delete(self, record_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT building_ref FROM saved_plans WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                DELETE FROM saved_plan_generations
                WHERE plan_id IN (
                    SELECT id FROM saved_plans WHERE building_ref = ?
                )
                """,
                (row["building_ref"],),
            )
            cursor = connection.execute(
                "DELETE FROM saved_plans WHERE building_ref = ?", (row["building_ref"],)
            )
        return cursor.rowcount > 0
