from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapters import fetch_platform_metadata
from .db import Database
from .metadata import extract_duration_seconds, extract_published_at


LogFn = Callable[[str, str], None]
MetadataFetcher = Callable[[str, str, LogFn], dict[str, Any]]


def refresh_job_platform_metadata(
    db: Database,
    job_id: str,
    output_dir: Path,
    log: LogFn,
    fetcher: MetadataFetcher = fetch_platform_metadata,
) -> dict[str, Any]:
    """为缺少发布时间或时长的任务视频重新读取平台元数据。"""
    rows = db.query_all("SELECT * FROM videos WHERE job_id = ? ORDER BY created_at ASC", [job_id])
    updated: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    checked = 0

    for row in rows:
        source_meta = _source_meta(row.get("source_meta_json"))
        published_at = extract_published_at(row.get("published_at"), source_meta)
        duration = extract_duration_seconds(row.get("duration"), source_meta)
        if published_at and duration is not None:
            continue

        checked += 1
        try:
            fresh = fetcher(row["url"], row["platform"], log)
        except Exception as exc:
            errors.append(f"{row['video_id']}: {exc}")
            continue

        published_at = extract_published_at(published_at, fresh)
        duration = extract_duration_seconds(duration, fresh)
        changes: dict[str, Any] = {}
        if published_at and not row.get("published_at"):
            changes["published_at"] = published_at
        if duration is not None and not row.get("duration"):
            changes["duration"] = duration
        if not changes:
            continue

        merged_meta = dict(source_meta)
        merged_meta.update({key: value for key, value in fresh.items() if value not in (None, "")})
        if published_at:
            merged_meta["published_at"] = published_at
        if duration is not None:
            merged_meta["duration"] = duration
        changes["source_meta_json"] = json.dumps(merged_meta, ensure_ascii=False)
        db.update_video(row["id"], **changes)
        updated[str(row["video_id"])] = {
            "published_at": published_at,
            "duration_seconds": duration,
        }

    _patch_benchmark_metadata(output_dir, updated)
    remaining = len(rows) - sum(
        1
        for row in db.query_all("SELECT published_at, duration FROM videos WHERE job_id = ?", [job_id])
        if row.get("published_at") and row.get("duration") is not None
    )
    summary = {
        "checked": checked,
        "updated": len(updated),
        "remaining": remaining,
        "errors": errors,
    }
    if errors:
        log("warn", f"平台元数据补抓完成：更新 {len(updated)} 条，仍缺失 {remaining} 条；失败示例：{'；'.join(errors[:3])}")
    else:
        log("info", f"平台元数据补抓完成：检查 {checked} 条，更新 {len(updated)} 条，仍缺失 {remaining} 条")
    return summary


def _patch_benchmark_metadata(output_dir: Path, updated: dict[str, dict[str, Any]]) -> None:
    if not updated:
        return
    videos_dir = output_dir / "videos"
    for video_id, values in updated.items():
        card_path = videos_dir / f"{video_id}.card.json"
        if not card_path.exists():
            continue
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
            if not isinstance(card, dict):
                continue
            card.update({key: value for key, value in values.items() if value not in (None, "")})
            card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            continue

    index_path = output_dir / "retrieval_index.json"
    if not index_path.exists():
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        cards = index.get("cards") if isinstance(index, dict) else None
        if not isinstance(cards, list):
            return
        changed = False
        for card in cards:
            if not isinstance(card, dict):
                continue
            values = updated.get(str(card.get("video_id") or ""))
            if not values:
                continue
            card.update({key: value for key, value in values.items() if value not in (None, "")})
            changed = True
        if changed:
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return


def _source_meta(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
