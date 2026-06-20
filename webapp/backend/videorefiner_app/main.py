from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .adapters import resolve_blogger_name
from .cleanup import artifact_kinds_for_cleanup, cleanup_outputs, estimate_cleanup_outputs
from .config import REPO_ROOT, load_config
from .db import Database
from .evidence import read_visual_timeline, validate_visual_timeline
from .export_package import export_videoautomation_package
from .llm import parse_test_result, test_model_profile
from .metadata_refresh import refresh_job_platform_metadata
from .pipeline import TaskManager, config_snapshot
from .preflight import run_preflight
from .providers import PROVIDER_PRESETS, VISION_CAPABLE_PROVIDER_KEYS
from .schemas import JobCleanupIn, JobCreateIn, ModelProfileIn
from .security import SecretStore
from .utils import local_timestamp, utc_now
from .validation import validate_model_profile_for_refinement


config = load_config()
db = Database(config.database_path)
secrets = SecretStore(config)


def _mark_interrupted_jobs_retryable() -> None:
    rows = db.query_all("SELECT id FROM jobs WHERE status IN ('queued', 'running')")
    for row in rows:
        now = utc_now()
        db.execute(
            """
            UPDATE videos
            SET status = 'failed', error = COALESCE(error, '服务重启时该视频步骤中断，可点击重试继续。'),
                next_retry_at = NULL, updated_at = ?
            WHERE job_id = ? AND status NOT IN ('done', 'skipped', 'failed')
            """,
            [now, row["id"]],
        )
        db.execute(
            """
            UPDATE dimensions
            SET status = 'failed', error = COALESCE(error, '服务重启时该维度中断，可点击重试继续。'), updated_at = ?
            WHERE job_id = ? AND status = 'running'
            """,
            [now, row["id"]],
        )
        db.update_job(
            row["id"],
            status="failed",
            error="服务重启后任务中断，请点击重试从已完成产物继续。",
            finished_at=now,
            cancel_requested=0,
        )
        db.add_log(row["id"], "warn", "服务重启后任务中断，可点击重试继续")


_mark_interrupted_jobs_retryable()
tasks = TaskManager(db, config, secrets)

app = FastAPI(title="视频炼化 Web", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_frontend(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _profile_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider_key": row["provider_key"],
        "provider_name": row["provider_name"],
        "base_url": row["base_url"],
        "analysis_model": row["analysis_model"],
        "merge_model": row["merge_model"],
        "supports_vision": bool(row["supports_vision"]),
        "supports_reasoning": bool(row["supports_reasoning"]),
        "max_tokens": row["max_tokens"],
        "temperature": row["temperature"],
        "key_storage": row.get("key_storage"),
        "is_tested": bool(row["is_tested"]),
        "test_result": parse_test_result(row.get("test_result_json")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _job_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "input_type": row["input_type"],
        "output_dir": row["output_dir"],
        "model_profile_id": row["model_profile_id"],
        "max_videos": row["max_videos"],
        "created_at": row["created_at"],
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "error": row.get("error"),
    }


def _resolve_output_dir(payload: JobCreateIn) -> tuple[str, dict[str, Any]]:
    if payload.output_dir:
        return payload.output_dir, {}
    if payload.input_type == "blogger" and payload.inputs:
        blogger_name = resolve_blogger_name(payload.inputs[0])
        return str(config.output_root / blogger_name), {"blogger_name": blogger_name}
    return str(config.output_root / f"视频炼化_{local_timestamp()}"), {}


@app.get("/api/health")
def health() -> dict[str, Any]:
    bundled_whisper_model = REPO_ROOT / "models" / "whisper" / "faster-whisper-tiny"
    return {
        "ok": True,
        "version": __version__,
        "repo_root": str(REPO_ROOT),
        "bundled_whisper_model": all(
            (bundled_whisper_model / name).exists() for name in ["model.bin", "config.json", "tokenizer.json"]
        ),
    }


@app.get("/api/provider-presets")
def provider_presets() -> list[dict[str, Any]]:
    return [asdict(item) for item in PROVIDER_PRESETS]


@app.post("/api/preflight")
def preflight() -> dict[str, Any]:
    return run_preflight(config)


@app.get("/api/model-profiles")
def list_model_profiles() -> list[dict[str, Any]]:
    return [_profile_out(row) for row in db.list_model_profiles()]


@app.post("/api/model-profiles")
def save_model_profile(payload: ModelProfileIn) -> dict[str, Any]:
    key_storage = None
    if payload.api_key:
        temp_id = payload.id or "pending"
        if payload.id:
            key_storage = secrets.set_api_key(payload.id, payload.api_key)
        else:
            # Store after DB assigns an id below.
            key_storage = "__defer__"

    data = payload.model_dump(exclude={"api_key"})
    if key_storage == "__defer__":
        row = db.upsert_model_profile(data, None)
        real_storage = secrets.set_api_key(row["id"], payload.api_key or "")
        row = db.upsert_model_profile({**data, "id": row["id"]}, real_storage)
    else:
        row = db.upsert_model_profile(data, key_storage)
    return _profile_out(row)


@app.post("/api/model-profiles/{profile_id}/test")
def test_profile(profile_id: str) -> dict[str, Any]:
    try:
        profile = db.get_model_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模型配置不存在") from exc
    api_key = secrets.get_api_key(profile_id)
    if not api_key:
        raise HTTPException(status_code=400, detail="模型配置没有 API Key")
    profile_for_test = _profile_out(profile)
    if profile_for_test["provider_key"] in VISION_CAPABLE_PROVIDER_KEYS:
        profile_for_test["supports_vision"] = True
    result = test_model_profile(profile_for_test, api_key)
    db.set_model_test_result(profile_id, result)
    return result


@app.delete("/api/model-profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, Any]:
    try:
        db.get_model_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模型配置不存在") from exc
    active_jobs = db.count_active_jobs_for_profile(profile_id)
    if active_jobs:
        raise HTTPException(status_code=400, detail="该配置仍有运行中或排队中的任务，不能删除")
    secrets.delete_api_key(profile_id)
    db.delete_model_profile(profile_id)
    return {"ok": True}


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [_job_out(row) for row in db.list_jobs()]


@app.post("/api/jobs")
def create_job(payload: JobCreateIn) -> dict[str, Any]:
    try:
        profile = db.get_model_profile(payload.model_profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模型配置不存在") from exc
    profile_out = _profile_out(profile)
    validation_errors = validate_model_profile_for_refinement(profile_out)
    if validation_errors:
        raise HTTPException(status_code=400, detail="；".join(validation_errors))
    output_dir, job_meta = _resolve_output_dir(payload)
    job = db.create_job(
        payload.input_type,
        output_dir,
        payload.model_profile_id,
        profile_out,
        config_snapshot(config, payload.inputs, {**job_meta, "frame_interval_seconds": payload.frame_interval_seconds}),
        min(payload.max_videos, config.daily_limit),
    )
    db.add_log(job["id"], "info", "任务已创建")
    tasks.start_job(job["id"])
    return _job_out(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        detail = db.get_job_detail(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return {
        **_job_out(detail),
        "videos": detail["videos"],
        "dimensions": detail["dimensions"],
        "artifacts": detail["artifacts"],
        "cleanup_sizes": estimate_cleanup_outputs(Path(detail["output_dir"])),
    }


@app.get("/api/jobs/{job_id}/artifacts")
def get_artifacts(job_id: str) -> list[dict[str, Any]]:
    try:
        db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return db.query_all("SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at ASC", [job_id])


@app.get("/api/jobs/{job_id}/evidence/{video_id}")
def get_video_evidence_timeline(job_id: str, video_id: str) -> dict[str, Any]:
    try:
        job = db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    row = db.query_one("SELECT id FROM videos WHERE job_id = ? AND video_id = ?", [job_id, video_id])
    if not row:
        raise HTTPException(status_code=404, detail="视频不属于该任务")

    output_dir = Path(job["output_dir"]).expanduser().resolve()
    timeline_path = output_dir / "evidence" / f"{video_id}.visual_timeline.json"
    if not timeline_path.is_file():
        raise HTTPException(status_code=404, detail="该视频尚未生成证据时间线")
    try:
        timeline = read_visual_timeline(timeline_path)
        validate_visual_timeline(timeline, require_visual_observations=True)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    for shot in timeline.get("shots", []):
        if not isinstance(shot, dict):
            continue
        keyframe_ref = Path(str(shot.get("keyframe") or ""))
        if keyframe_ref.is_absolute() or ".." in keyframe_ref.parts:
            shot["keyframe_url"] = ""
            continue
        keyframe_path = (output_dir / keyframe_ref).resolve()
        try:
            keyframe_path.relative_to(output_dir)
        except ValueError:
            shot["keyframe_url"] = ""
            continue
        shot["keyframe_url"] = f"/api/files?path={quote(str(keyframe_path))}" if keyframe_path.is_file() else ""
    return timeline


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    db.update_job(job_id, cancel_requested=1)
    db.add_log(job_id, "warn", "收到取消请求")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict[str, Any]:
    try:
        job = db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if job["status"] in {"running", "queued"}:
        raise HTTPException(status_code=400, detail="任务正在运行，不能重试")
    db.execute(
        """
        UPDATE videos
        SET status = 'pending', error = NULL, skip_reason = NULL, retry_count = 0,
            last_error_at = NULL, next_retry_at = NULL, updated_at = ?
        WHERE job_id = ? AND status IN ('failed', 'retry_wait')
        """,
        [utc_now(), job_id],
    )
    db.execute(
        """
        UPDATE dimensions
        SET status = 'pending', error = NULL, updated_at = ?
        WHERE job_id = ? AND status = 'failed'
        """,
        [utc_now(), job_id],
    )
    db.update_job(job_id, status="queued", error=None, finished_at=None, cancel_requested=0)
    db.add_log(job_id, "info", "收到重试请求，将复用已完成产物")
    tasks.start_job(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/refresh-platform-metadata")
def refresh_platform_metadata(job_id: str) -> dict[str, Any]:
    try:
        job = db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if job["status"] in {"queued", "running"}:
        raise HTTPException(status_code=400, detail="任务运行中，不能同时补抓平台元数据")

    result = refresh_job_platform_metadata(
        db,
        job_id,
        Path(job["output_dir"]),
        lambda level, message: db.add_log(job_id, level, message),
    )
    return {"ok": True, **result}


@app.post("/api/jobs/{job_id}/open-output-dir")
def open_output_dir(job_id: str) -> dict[str, Any]:
    try:
        job = db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    output_dir = Path(job["output_dir"]).expanduser().resolve()
    if not output_dir.exists() or not output_dir.is_dir():
        raise HTTPException(status_code=404, detail="输出目录不存在")
    try:
        subprocess.run(["open", str(output_dir)], check=True, timeout=10)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="当前系统不支持 open 命令") from exc
    except subprocess.SubprocessError as exc:
        raise HTTPException(status_code=500, detail=f"打开目录失败：{exc}") from exc
    return {"ok": True, "path": str(output_dir)}


@app.post("/api/jobs/{job_id}/export-videoautomation")
def export_for_videoautomation(job_id: str) -> dict[str, Any]:
    try:
        job = db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if job["status"] in {"queued", "running"}:
        raise HTTPException(status_code=400, detail="任务运行中，不能导出")
    if job["status"] not in {"done", "partial_done"}:
        raise HTTPException(status_code=400, detail="任务未完成，不能导出。请先点击重试，等 Benchmark Intelligence 生成完成后再导出。")

    try:
        result = export_videoautomation_package(
            Path(job["output_dir"]),
            (config.output_root,),
            requested_video_count=int(job.get("max_videos") or 0) or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add_log(job_id, "info", f"已导出 VideoAutomation 起号基底包：{result['path']}")
    return result


@app.post("/api/jobs/{job_id}/cleanup")
def cleanup_job_outputs(job_id: str, payload: JobCleanupIn) -> dict[str, Any]:
    try:
        job = db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if job["status"] in {"queued", "running"}:
        raise HTTPException(status_code=400, detail="任务运行中，不能清理产物")

    try:
        result = cleanup_outputs(Path(job["output_dir"]), payload.categories)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    artifact_kinds = artifact_kinds_for_cleanup(payload.categories)
    if artifact_kinds:
        placeholders = ", ".join("?" for _ in artifact_kinds)
        db.execute(
            f"DELETE FROM artifacts WHERE job_id = ? AND kind IN ({placeholders})",
            [job_id, *artifact_kinds],
        )
    if "single_analysis" in payload.categories:
        db.execute("UPDATE dimensions SET output_path = NULL WHERE job_id = ?", [job_id])
    db.add_log(
        job_id,
        "warn",
        f"已清理产物：{', '.join(payload.categories)}；删除 {result['deleted_count']} 项，释放 {result['freed_bytes']} 字节",
    )
    return {"ok": True, **result}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    try:
        db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc

    async def stream():
        last_id = 0
        while True:
            rows = db.query_all(
                "SELECT * FROM logs WHERE job_id = ? AND id > ? ORDER BY id ASC",
                [job_id, last_id],
            )
            for row in rows:
                last_id = row["id"]
                yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
            job = db.get_job(job_id)
            if job["status"] in {"done", "partial_done", "failed", "cancelled"} and not rows:
                yield f"data: {json.dumps({'id': last_id, 'level': 'system', 'message': '[stream-end]'}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/files")
def open_file(path: str) -> FileResponse:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path)


frontend_dist = REPO_ROOT / "webapp" / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
