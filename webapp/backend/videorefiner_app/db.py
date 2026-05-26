from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

from .utils import redact, utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS model_profiles (
  id TEXT PRIMARY KEY,
  provider_key TEXT NOT NULL,
  provider_name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  analysis_model TEXT NOT NULL,
  merge_model TEXT NOT NULL,
  supports_vision INTEGER NOT NULL DEFAULT 1,
  supports_reasoning INTEGER NOT NULL DEFAULT 0,
  max_tokens INTEGER NOT NULL DEFAULT 8192,
  temperature REAL NOT NULL DEFAULT 0.2,
  key_storage TEXT,
  is_tested INTEGER NOT NULL DEFAULT 0,
  test_result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  input_type TEXT NOT NULL,
  output_dir TEXT NOT NULL,
  model_profile_id TEXT NOT NULL,
  model_profile_snapshot TEXT NOT NULL,
  config_snapshot TEXT NOT NULL,
  max_videos INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  video_id TEXT NOT NULL,
  url TEXT NOT NULL,
  platform TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT,
  author TEXT,
  duration REAL,
  method TEXT,
  error TEXT,
  skip_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  video_db_id TEXT,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS dimensions (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  video_db_id TEXT NOT NULL,
  dimension TEXT NOT NULL,
  status TEXT NOT NULL,
  output_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_id, video_db_id, dimension)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  video_db_id TEXT,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  meta_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL
);
"""

_UNSET = object()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_schema()
            self._conn.commit()

    def _migrate_schema(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(videos)").fetchall()}
        if "retry_count" not in columns:
            self._conn.execute("ALTER TABLE videos ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "last_error_at" not in columns:
            self._conn.execute("ALTER TABLE videos ADD COLUMN last_error_at TEXT")
        if "next_retry_at" not in columns:
            self._conn.execute("ALTER TABLE videos ADD COLUMN next_retry_at TEXT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def upsert_model_profile(self, payload: dict[str, Any], key_storage: str | None) -> dict[str, Any]:
        now = utc_now()
        profile_id = payload.get("id") or str(uuid.uuid4())
        existing = self.query_one("SELECT created_at FROM model_profiles WHERE id = ?", [profile_id])
        created_at = existing["created_at"] if existing else now
        self.execute(
            """
            INSERT INTO model_profiles (
              id, provider_key, provider_name, base_url, analysis_model, merge_model,
              supports_vision, supports_reasoning, max_tokens, temperature, key_storage,
              is_tested, test_result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              provider_key=excluded.provider_key,
              provider_name=excluded.provider_name,
              base_url=excluded.base_url,
              analysis_model=excluded.analysis_model,
              merge_model=excluded.merge_model,
              supports_vision=excluded.supports_vision,
              supports_reasoning=excluded.supports_reasoning,
              max_tokens=excluded.max_tokens,
              temperature=excluded.temperature,
              key_storage=COALESCE(excluded.key_storage, model_profiles.key_storage),
              is_tested=0,
              test_result_json=NULL,
              updated_at=excluded.updated_at
            """,
            [
                profile_id,
                payload["provider_key"],
                payload["provider_name"],
                payload["base_url"].rstrip("/"),
                payload["analysis_model"],
                payload["merge_model"],
                int(payload["supports_vision"]),
                int(payload["supports_reasoning"]),
                int(payload["max_tokens"]),
                float(payload["temperature"]),
                key_storage,
                0,
                None,
                created_at,
                now,
            ],
        )
        return self.get_model_profile(profile_id)

    def get_model_profile(self, profile_id: str) -> dict[str, Any]:
        row = self.query_one("SELECT * FROM model_profiles WHERE id = ?", [profile_id])
        if not row:
            raise KeyError(profile_id)
        return row

    def delete_model_profile(self, profile_id: str) -> None:
        self.execute("DELETE FROM model_profiles WHERE id = ?", [profile_id])

    def count_active_jobs_for_profile(self, profile_id: str) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS count FROM jobs WHERE model_profile_id = ? AND status IN ('queued', 'running')",
            [profile_id],
        )
        return int(row["count"] if row else 0)

    def list_model_profiles(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM model_profiles ORDER BY updated_at DESC")

    def set_model_test_result(self, profile_id: str, result: dict[str, Any]) -> None:
        self.execute(
            """
            UPDATE model_profiles
            SET is_tested = ?, test_result_json = ?, supports_vision = ?, supports_reasoning = ?, updated_at = ?
            WHERE id = ?
            """,
            [
                int(result.get("ok", False)),
                json.dumps(result, ensure_ascii=False),
                int(result.get("vision_ok", False)),
                int(result.get("reasoning_ok", False)),
                utc_now(),
                profile_id,
            ],
        )

    def create_job(
        self,
        input_type: str,
        output_dir: str,
        model_profile_id: str,
        model_profile_snapshot: dict[str, Any],
        config_snapshot: dict[str, Any],
        max_videos: int,
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = utc_now()
        self.execute(
            """
            INSERT INTO jobs (
              id, status, input_type, output_dir, model_profile_id, model_profile_snapshot,
              config_snapshot, max_videos, created_at
            ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                job_id,
                input_type,
                output_dir,
                model_profile_id,
                json.dumps(model_profile_snapshot, ensure_ascii=False),
                json.dumps(config_snapshot, ensure_ascii=False),
                max_videos,
                now,
            ],
        )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.query_one("SELECT * FROM jobs WHERE id = ?", [job_id])
        if not row:
            raise KeyError(job_id)
        return row

    def list_jobs(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50")

    def update_job(self, job_id: str, status: str | None = None, error: Any = _UNSET, **fields: Any) -> None:
        pairs = []
        values: list[Any] = []
        if status is not None:
            pairs.append("status = ?")
            values.append(status)
        if error is not _UNSET:
            pairs.append("error = ?")
            values.append(error)
        for key, value in fields.items():
            pairs.append(f"{key} = ?")
            values.append(value)
        if not pairs:
            return
        values.append(job_id)
        self.execute(f"UPDATE jobs SET {', '.join(pairs)} WHERE id = ?", values)

    def create_video(self, job_id: str, video_id: str, url: str, platform: str) -> str:
        video_db_id = str(uuid.uuid4())
        now = utc_now()
        self.execute(
            """
            INSERT INTO videos (id, job_id, video_id, url, platform, status, retry_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            [video_db_id, job_id, video_id, url, platform, now, now],
        )
        return video_db_id

    def update_video(self, video_db_id: str, status: str | None = None, **fields: Any) -> None:
        pairs = []
        values: list[Any] = []
        if status is not None:
            pairs.append("status = ?")
            values.append(status)
        for key, value in fields.items():
            pairs.append(f"{key} = ?")
            values.append(value)
        pairs.append("updated_at = ?")
        values.append(utc_now())
        values.append(video_db_id)
        self.execute(f"UPDATE videos SET {', '.join(pairs)} WHERE id = ?", values)

    def add_dimension(self, job_id: str, video_db_id: str, dimension: str) -> None:
        now = utc_now()
        self.execute(
            """
            INSERT OR IGNORE INTO dimensions (id, job_id, video_db_id, dimension, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            [str(uuid.uuid4()), job_id, video_db_id, dimension, now, now],
        )

    def update_dimension(self, job_id: str, video_db_id: str, dimension: str, status: str, **fields: Any) -> None:
        pairs = ["status = ?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            pairs.append(f"{key} = ?")
            values.append(value)
        pairs.append("updated_at = ?")
        values.extend([utc_now(), job_id, video_db_id, dimension])
        self.execute(
            f"UPDATE dimensions SET {', '.join(pairs)} WHERE job_id = ? AND video_db_id = ? AND dimension = ?",
            values,
        )

    def add_artifact(self, job_id: str, kind: str, path: str, video_db_id: str | None = None, meta: dict[str, Any] | None = None) -> None:
        self.execute(
            """
            INSERT INTO artifacts (id, job_id, video_db_id, kind, path, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [str(uuid.uuid4()), job_id, video_db_id, kind, path, json.dumps(meta or {}, ensure_ascii=False), utc_now()],
        )

    def add_log(self, job_id: str, level: str, message: str) -> None:
        self.execute(
            "INSERT INTO logs (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
            [job_id, utc_now(), level, redact(message)],
        )

    def get_job_detail(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        job["videos"] = self.query_all("SELECT * FROM videos WHERE job_id = ? ORDER BY created_at ASC", [job_id])
        job["dimensions"] = self.query_all("SELECT * FROM dimensions WHERE job_id = ? ORDER BY created_at ASC", [job_id])
        job["artifacts"] = self.query_all("SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at ASC", [job_id])
        return job
