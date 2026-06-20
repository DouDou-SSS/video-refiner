from __future__ import annotations

import hashlib
import json
import queue
import random
import re
import shutil
import threading
import time
from collections.abc import Callable
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import detect_platform, download_video, extract_frames, extract_video_id, parse_inputs
from .benchmark import (
    BENCHMARK_BATCH_SIZE,
    BENCHMARK_PROMPT,
    CREATOR_MARKDOWN_OUTPUTS,
    build_creator_markdown_prompt,
    build_video_batch_prompt,
    build_video_note_prompt,
    collect_video_materials,
    infer_creator,
    infer_platform,
    model_output_failure,
    normalize_creator_summary_data,
    normalize_video_cards_data,
    parse_benchmark_json,
    remove_benchmark_outputs,
    validate_benchmark_data,
    validate_video_batch_data,
    write_benchmark_outputs,
)
from .config import AppConfig
from .db import Database
from .evidence import (
    apply_visual_observations,
    build_visual_observation_prompt,
    build_visual_timeline,
    evenly_selected_keyframes,
    parse_visual_observations,
    read_visual_timeline,
    timeline_prompt_summary,
    transcript_timeline_path,
    validate_visual_timeline,
    visual_batches,
    visual_timeline_path,
    write_visual_timeline,
)
from .llm import LLMClient
from .metadata import extract_duration_seconds, extract_published_at
from .metadata_refresh import refresh_job_platform_metadata
from .security import SecretStore
from .utils import list_visible_files, local_timestamp, run_command, utc_now
from .validation import validate_model_profile_for_refinement


SINGLE_VIDEO_DIMENSIONS = [
    {"name": "文案风格", "prompt": "文案风格蒸馏.md", "output": "文案风格.md"},
    {"name": "视频脚本", "prompt": "视频脚本蒸馏.md", "output": "视频脚本.md"},
    {"name": "剪辑逻辑", "prompt": "剪辑逻辑蒸馏.md", "output": "剪辑逻辑.md"},
    {"name": "选题策略", "prompt": "选题策略蒸馏.md", "output": "选题策略.md"},
    {"name": "运营策略", "prompt": "运营策略蒸馏.md", "output": "运营策略.md"},
]
BENCHMARK_DIMENSION = {
    "name": "Benchmark Intelligence",
    "prompt": BENCHMARK_PROMPT,
    "outputs": [
        "creator_profile.md",
        "pattern_library.md",
        "qa_checklist.md",
        "retrieval_index.json",
        "retrieval_pack.md",
    ],
}
DIMENSIONS = SINGLE_VIDEO_DIMENSIONS


class JobCancelled(RuntimeError):
    pass


class BenchmarkModelRetryable(RuntimeError):
    pass


class TaskManager:
    def __init__(self, db: Database, config: AppConfig, secrets: SecretStore):
        self.db = db
        self.config = config
        self.secrets = secrets
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def start_job(self, job_id: str) -> None:
        with self._lock:
            existing = self._threads.get(job_id)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
            self._threads[job_id] = thread
            thread.start()

    def _run_job(self, job_id: str) -> None:
        runner = PipelineRunner(self.db, self.config, self.secrets, job_id)
        try:
            runner.run()
        except JobCancelled:
            self.db.update_job(job_id, status="cancelled", finished_at=utc_now(), error="用户取消")
            self.db.add_log(job_id, "warn", "任务已取消")
        except Exception as exc:
            self.db.update_job(job_id, status="failed", finished_at=utc_now(), error=str(exc))
            self.db.add_log(job_id, "error", f"任务失败：{exc}")


class PipelineRunner:
    def __init__(self, db: Database, config: AppConfig, secrets: SecretStore, job_id: str):
        self.db = db
        self.config = config
        self.secrets = secrets
        self.job_id = job_id
        self.job = db.get_job(job_id)
        self.config_snapshot = json.loads(self.job["config_snapshot"])
        self.profile = json.loads(self.job["model_profile_snapshot"])
        self.api_key = self.secrets.get_api_key(self.job["model_profile_id"])
        if not self.api_key:
            raise RuntimeError("模型配置没有可用 API Key。")
        self.llm = LLMClient(self.profile, self.api_key, log=lambda message: self._log("warn", message))
        self.output_dir = Path(self.job["output_dir"]).expanduser()
        self.tmp_dir = self.output_dir / "原始数据"
        self.single_dir = self.output_dir / "单视频分析"
        self.transcript_dir = self.output_dir / "文案"
        self.keep_dir = self.output_dir / "视频保留"
        self.evidence_dir = self.output_dir / "evidence"

    def run(self) -> None:
        self.db.update_job(self.job_id, status="running", started_at=utc_now(), error=None, cancel_requested=0)
        self._log("info", "固定流程启动：预检 → 解析输入 → 下载 → 抽帧 → 文案 → 证据时间线 → 资料检查 → 5个单视频维度蒸馏 → 跨视频合并 → 第6维 Benchmark Intelligence 汇总")
        for directory in [self.output_dir, self.tmp_dir, self.single_dir, self.transcript_dir, self.keep_dir, self.evidence_dir]:
            directory.mkdir(parents=True, exist_ok=True)

        self._check_model_capability()
        rows = self._load_or_parse_videos()
        if not rows:
            raise RuntimeError("未解析到任何视频。")

        self._reactivate_invalid_completed_videos()
        self._process_videos_with_auto_retry()

        final_rows = self._get_video_rows()
        completed_video_rows = [row for row in final_rows if row["status"] == "done"]
        failed_rows = [row for row in final_rows if row["status"] == "failed"]
        if completed_video_rows:
            if failed_rows:
                self._log("warn", f"{len(failed_rows)} 个视频达到自动重试上限或不可重试，将基于已完成视频先生成阶段性结果。")
            self._merge_outputs(completed_video_rows)
            self._write_benchmark_intelligence(completed_video_rows)
        else:
            self._log("warn", "没有资料完整的视频，跳过合并精炼。")
            raise RuntimeError("所有视频都未处理成功，请查看视频失败原因。")
        self._write_progress()
        self._write_manifest()
        if failed_rows:
            error = f"{len(failed_rows)} 个视频自动重试后仍失败，请查看视频失败原因。"
            self.db.update_job(self.job_id, status="partial_done", finished_at=utc_now(), error=error)
            self._log("error", f"任务结束但存在失败视频：{error}")
        else:
            self.db.update_job(self.job_id, status="done", finished_at=utc_now())
            self._log("info", "任务完成")

    def _check_model_capability(self) -> None:
        validation_errors = validate_model_profile_for_refinement(self.profile)
        if validation_errors:
            raise RuntimeError("；".join(validation_errors))

    def _load_or_parse_videos(self) -> list[dict[str, Any]]:
        existing = self.db.query_all("SELECT * FROM videos WHERE job_id = ? ORDER BY created_at ASC", [self.job_id])
        max_videos = int(self.job["max_videos"])
        if existing and len(existing) >= max_videos:
            return existing
        if existing:
            self._log("warn", f"当前任务已有 {len(existing)}/{max_videos} 个视频，将重新解析主页并补充缺失视频")
        inputs = self.config_snapshot["inputs"]
        parsed = parse_inputs(self.job["input_type"], inputs, max_videos, self._log)
        existing_video_ids = {str(row["video_id"]) for row in existing}
        rows: list[dict[str, Any]] = []
        for item in parsed:
            if str(item["video_id"]) in existing_video_ids:
                continue
            video_db_id = self.db.create_video(self.job_id, item["video_id"], item["url"], item["platform"], item)
            for dim in DIMENSIONS:
                self.db.add_dimension(self.job_id, video_db_id, dim["name"])
            row = self.db.query_one("SELECT * FROM videos WHERE id = ?", [video_db_id])
            if row:
                rows.append(row)
        if existing:
            self._log("info", f"补充解析新增 {len(rows)} 个视频，当前共 {len(existing) + len(rows)} 个")
            return self._get_video_rows()
        self._log("info", f"解析输入得到 {len(rows)} 个视频")
        return rows

    def _get_video_rows(self) -> list[dict[str, Any]]:
        return self.db.query_all("SELECT * FROM videos WHERE job_id = ? ORDER BY created_at ASC", [self.job_id])

    def _reactivate_invalid_completed_videos(self) -> None:
        reactivated = 0
        requires_evidence = self._requires_visual_evidence()
        for row in self._get_video_rows():
            if row["status"] != "done":
                continue
            invalid_dimensions: list[tuple[str, str]] = []
            if requires_evidence:
                timeline_path = visual_timeline_path(self.evidence_dir, str(row["video_id"]))
                try:
                    validate_visual_timeline(read_visual_timeline(timeline_path), require_visual_observations=True)
                except Exception as exc:
                    invalid_dimensions.extend((dim["name"], f"视觉证据时间线不可用：{exc}") for dim in DIMENSIONS)
            for dim in DIMENSIONS:
                path = self.single_dir / f"{row['video_id']}_{dim['name']}.md"
                if not path.is_file() or path.stat().st_size == 0:
                    invalid_dimensions.append((dim["name"], "分析文件缺失或为空"))
                    continue
                failure = model_output_failure(path.read_text(encoding="utf-8", errors="ignore"))
                if failure:
                    invalid_dimensions.append((dim["name"], f"模型返回无效分析：{failure}"))
                elif requires_evidence and not self._analysis_has_evidence_refs(path.read_text(encoding="utf-8", errors="ignore"), str(row["video_id"])):
                    invalid_dimensions.append((dim["name"], "分析缺少可追溯 evidence_id"))
            if not invalid_dimensions:
                continue
            deduped_dimensions = {dimension: error for dimension, error in invalid_dimensions}
            for dimension, error in deduped_dimensions.items():
                self.db.update_dimension(self.job_id, row["id"], dimension, "failed", error=error)
            names = "、".join(deduped_dimensions)
            self.db.update_video(row["id"], status="pending", error=f"检测到无效维度：{names}", retry_count=0, next_retry_at=None)
            self._log("warn", f"已完成视频存在无效分析，重新进入补跑：{row['video_id']} / {names}")
            reactivated += 1
        if reactivated:
            self._log("warn", f"共 {reactivated} 个历史完成视频缺少可信证据或维度引用，将补建证据并重跑相关维度")

    def _process_videos_with_auto_retry(self) -> None:
        while True:
            rows = self._get_video_rows()
            processable = [row for row in rows if self._should_process_video(row)]
            if not processable:
                return
            for row in rows:
                self._check_cancelled()
                if not self._should_process_video(row):
                    continue
                if row["status"] in {"failed", "retry_wait"}:
                    self._log(
                        "warn",
                        f"自动重试视频：{row['video_id']}（第 {int(row.get('retry_count') or 0) + 1}/{self.config.auto_retry_max_attempts} 次尝试）",
                    )
                processed = self._process_video(row)
                self._delay_between_videos(row != rows[-1])
            retryable_rows = self._retryable_failed_rows()
            if not retryable_rows:
                return
            delay = self._auto_retry_delay_seconds()
            next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            ids = "、".join(row["video_id"] for row in retryable_rows[:5])
            suffix = "..." if len(retryable_rows) > 5 else ""
            for row in retryable_rows:
                self.db.update_video(row["id"], status="retry_wait", next_retry_at=next_retry_at)
            self._log("warn", f"{len(retryable_rows)} 个失败视频将自动重试：{ids}{suffix}；等待 {delay:.1f} 秒")
            self._sleep_interruptible(delay)

    def _should_process_video(self, row: dict[str, Any]) -> bool:
        if row["status"] in {"done", "skipped"}:
            return False
        if row["status"] in {"failed", "retry_wait"}:
            return self._can_auto_retry(row)
        return True

    def _retryable_failed_rows(self) -> list[dict[str, Any]]:
        return [row for row in self._get_video_rows() if row["status"] == "failed" and self._can_auto_retry(row)]

    def _can_auto_retry(self, row: dict[str, Any]) -> bool:
        if int(row.get("retry_count") or 0) >= self.config.auto_retry_max_attempts:
            return False
        return self._is_retryable_error(row.get("error") or "")

    def _is_retryable_error(self, error: str) -> bool:
        text = error.lower()
        return any(
            marker in text
            for marker in [
                "429",
                "throttling",
                "quota exceeded",
                "rate limit",
                "timed out",
                "timeout",
                "temporarily",
                "try again later",
                "connection",
                "curl",
                "download",
                "yt-dlp",
                "下载",
                "固定阶梯",
                "服务重启",
                "中断",
                "mcporter",
                "whisper",
                "文案",
                "无任何文案来源",
                "视觉证据批次",
                "视觉证据模型",
                "模型分析缺少可追溯 evidence_id",
                "模型返回无效分析",
                "json 解析",
            ]
        )

    def _auto_retry_delay_seconds(self) -> float:
        min_ms = max(0, self.config.auto_retry_delay_min_ms)
        max_ms = max(min_ms, self.config.auto_retry_delay_max_ms)
        return random.randint(min_ms, max_ms) / 1000

    def _process_video(self, row: dict[str, Any]) -> dict[str, Any] | None:
        video_db_id = row["id"]
        video_id = row["video_id"]
        url = row["url"]
        if row["status"] == "done":
            self._log("info", f"跳过已完成视频：{video_id}")
            return row

        video_path = self.tmp_dir / f"{video_id}.mp4"
        frames_dir = self.tmp_dir / f"{video_id}_frames"
        transcript_path = self.transcript_dir / f"video_{video_id}.md"
        raw_transcript_path = self.tmp_dir / f"{video_id}_transcript.txt"

        try:
            self.db.update_video(video_db_id, status="downloading", error=None, skip_reason=None, next_retry_at=None)
            source_meta = self._source_meta_from_row(row)
            if not video_path.exists() or video_path.stat().st_size <= 1024:
                self._log("info", f"下载视频：{video_id}")
                download = download_video(
                    url,
                    video_path,
                    self._log,
                    self.api_key,
                    source_urls=self.config_snapshot.get("inputs") or [],
                    max_videos=int(self.job["max_videos"]),
                    source_meta=source_meta,
                )
            else:
                download = {
                    "platform": detect_platform(url),
                    "method": "existing",
                    "title": source_meta.get("title") or source_meta.get("desc") or row.get("title") or video_id,
                    "published_at": extract_published_at(source_meta, row.get("published_at")),
                    "duration": extract_duration_seconds(source_meta, row.get("duration")),
                    "size_mb": round(video_path.stat().st_size / 1024 / 1024, 2),
                }
                self._log("info", f"复用已下载视频：{video_path}")
            duration = extract_duration_seconds(download, source_meta, row.get("duration"))
            if duration is None:
                duration = self._video_duration_seconds(video_path)
            published_at = extract_published_at(download, source_meta, row.get("published_at"))
            merged_meta = self._merge_source_meta(source_meta, download, duration, published_at)
            self.db.update_video(
                video_db_id,
                platform=download.get("platform") or detect_platform(url),
                method=download.get("method"),
                title=download.get("title") or row.get("title") or video_id,
                duration=duration,
                published_at=published_at,
                source_meta_json=json.dumps(merged_meta, ensure_ascii=False),
            )
            self.db.add_artifact(self.job_id, "video", str(video_path), video_db_id, download)

            self.db.update_video(video_db_id, status="framing")
            frame_interval_seconds, frame_interval_mode = self._frame_interval_config(video_path)
            frames = list_visible_files(frames_dir, "*.jpg")
            if frames and not self._frames_match_task_config(frames_dir, frame_interval_seconds):
                self._log("warn", f"{video_id} 现有帧图间隔与当前任务不一致，将重新抽帧")
                shutil.rmtree(frames_dir)
                frames = []
            if not frames:
                self._log("info", f"抽帧：{video_id}（{frame_interval_mode}，每 {frame_interval_seconds} 秒 1 帧）")
                frames = extract_frames(self.config, video_path, frames_dir, frame_interval_seconds)
                self._write_frames_meta(frames_dir, frame_interval_seconds, frame_interval_mode, len(frames))
            self.db.add_artifact(
                self.job_id,
                "frames",
                str(frames_dir),
                video_db_id,
                {"count": len(frames), "frame_interval_seconds": frame_interval_seconds, "frame_interval_mode": frame_interval_mode},
            )

            self.db.update_video(video_db_id, status="transcribing")
            transcript = self._load_or_extract_transcript(video_id, video_path, frames_dir, raw_transcript_path, transcript_path, row)

            missing = []
            if not frames:
                missing.append("帧图")
            if not transcript:
                missing.append("文案")
            if missing:
                reason = "缺少 " + "、".join(missing)
                self.db.update_video(video_db_id, status="skipped", skip_reason=reason, next_retry_at=None)
                self._log("warn", f"{video_id} 资料不全，跳过蒸馏：{reason}")
                return self.db.query_one("SELECT * FROM videos WHERE id = ?", [video_db_id])

            self.db.update_video(video_db_id, status="evidencing")
            evidence = self._ensure_visual_evidence(video_db_id, video_id, video_path, frames_dir, raw_transcript_path)
            self.db.update_video(video_db_id, status="distilling")
            self._distill_video(video_db_id, video_id, frames_dir, frames, row, transcript, evidence)
            keep_path = self.keep_dir / f"{video_id}.mp4"
            if not keep_path.exists():
                shutil.copy2(video_path, keep_path)
            self.db.add_artifact(self.job_id, "kept_video", str(keep_path), video_db_id)
            self.db.update_video(video_db_id, status="done", error=None, skip_reason=None, next_retry_at=None)
            self._write_progress()
            return self.db.query_one("SELECT * FROM videos WHERE id = ?", [video_db_id])
        except Exception as exc:
            retry_count = self._next_retry_count(video_db_id)
            failed_at = utc_now()
            self.db.update_video(video_db_id, status="failed", error=str(exc), retry_count=retry_count, last_error_at=failed_at, next_retry_at=None)
            if self._is_retryable_error(str(exc)) and retry_count < self.config.auto_retry_max_attempts:
                self._log("warn", f"{video_id} 失败后会自动重试（已失败 {retry_count}/{self.config.auto_retry_max_attempts} 次）")
            elif self._is_retryable_error(str(exc)):
                self._log("error", f"{video_id} 已达到自动重试上限：{self.config.auto_retry_max_attempts} 次")
            self._log("error", f"{video_id} 处理失败：{exc}")
            self._write_progress()
            return self.db.query_one("SELECT * FROM videos WHERE id = ?", [video_db_id])

    def _next_retry_count(self, video_db_id: str) -> int:
        row = self.db.query_one("SELECT retry_count FROM videos WHERE id = ?", [video_db_id])
        return int((row or {}).get("retry_count") or 0) + 1

    def _frame_interval_config(self, video_path: Path) -> tuple[int, str]:
        value = self.config_snapshot.get("frame_interval_seconds")
        if value is not None:
            return max(1, int(value)), "自定义配置"
        duration = self._video_duration_seconds(video_path)
        if duration is not None and duration > 600:
            return 5, "默认配置：10 分钟外视频"
        return 1, "默认配置：10 分钟内视频"

    def _video_duration_seconds(self, video_path: Path) -> float | None:
        ffmpeg_path = Path(self.config.ffmpeg_bin)
        ffprobe_bin = str(ffmpeg_path.with_name("ffprobe")) if ffmpeg_path.name == "ffmpeg" else "ffprobe"
        result = run_command(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video_path)],
            timeout=30,
        )
        if result.returncode != 0:
            return None
        try:
            duration = float(json.loads(result.stdout or "{}").get("format", {}).get("duration") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return duration if duration > 0 else None

    def _source_meta_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        source_meta: dict[str, Any] = {}
        if row.get("source_meta_json"):
            try:
                parsed = json.loads(row["source_meta_json"] or "{}")
                if isinstance(parsed, dict):
                    source_meta = parsed
            except json.JSONDecodeError:
                source_meta = {}
        if row.get("title") and not source_meta.get("title"):
            source_meta["title"] = row["title"]
        if row.get("duration") and not extract_duration_seconds(source_meta):
            source_meta["duration"] = row["duration"]
        if row.get("published_at") and not extract_published_at(source_meta):
            source_meta["published_at"] = row["published_at"]
        return source_meta

    def _merge_source_meta(
        self,
        source_meta: dict[str, Any],
        download: dict[str, Any],
        duration: float | None,
        published_at: str | None,
    ) -> dict[str, Any]:
        merged = dict(source_meta)
        for key in ("title", "desc", "method", "play_url", "download_url"):
            if download.get(key) not in (None, ""):
                merged[key] = download[key]
        if duration is not None:
            merged["duration"] = duration
        if published_at:
            merged["published_at"] = published_at
        return merged

    def _frames_match_task_config(self, frames_dir: Path, frame_interval_seconds: int) -> bool:
        meta_path = frames_dir / "frames_meta.json"
        if not meta_path.exists():
            return frame_interval_seconds == 1
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return int(meta.get("frame_interval_seconds") or 1) == frame_interval_seconds

    def _write_frames_meta(self, frames_dir: Path, frame_interval_seconds: int, frame_interval_mode: str, frame_count: int) -> None:
        meta = {
            "frame_interval_seconds": frame_interval_seconds,
            "frame_interval_mode": frame_interval_mode,
            "frame_count": frame_count,
            "created_at": utc_now(),
        }
        (frames_dir / "frames_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_or_extract_transcript(
        self,
        video_id: str,
        video_path: Path,
        frames_dir: Path,
        raw_output: Path,
        transcript_path: Path,
        row: dict[str, Any],
    ) -> str:
        if transcript_path.exists():
            text = transcript_path.read_text(encoding="utf-8")
            if "提取方式：字幕/OCR" not in text:
                marker = "## 完整文案"
                transcript = text.split(marker, 1)[-1].strip() if marker in text else text.strip()
                if not self._is_low_quality_transcript(transcript):
                    return transcript
                self._log("warn", f"{video_id} 检测到低质量旧文案，将重新提取或使用标题描述兜底")
            self._log("warn", f"{video_id} 检测到旧版整图 OCR 文案，将按新版 Whisper 主轨重新提取")
        worker_path = Path(__file__).resolve().parent / "media_worker.py"
        result = run_command(
            [
                self.config.system_python,
                str(worker_path),
                "--video-id",
                video_id,
                "--video-path",
                str(video_path),
                "--frames-dir",
                str(frames_dir),
                "--output",
                str(raw_output),
                "--ffmpeg-bin",
                self.config.ffmpeg_bin,
            ],
            timeout=3600,
        )
        meta: dict[str, Any]
        if result.returncode != 0:
            worker_error = (result.stderr or result.stdout or "文案提取失败").strip()
            raise RuntimeError(worker_error[-1000:])
        else:
            transcript = raw_output.read_text(encoding="utf-8").strip()
            meta = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
            if self._is_low_quality_transcript(transcript) and meta.get("source") not in {"底部硬字幕OCR兜底", "底部硬字幕OCR主文案"}:
                raise RuntimeError("文案质量过低，未进入 6 维炼化")
        md = (
            f"# 视频文案 - {video_id}\n\n"
            f"> 标题：{row.get('title') or video_id}\n"
            f"> 提取时间：{local_timestamp()}\n"
            f"> 提取方式：{meta.get('source', '字幕/OCR/Whisper')} + FunASR标点分段\n\n"
            "---\n\n"
            "## 完整文案\n\n"
            f"{transcript}\n"
        )
        transcript_path.write_text(md, encoding="utf-8")
        self.db.add_artifact(self.job_id, "transcript", str(transcript_path), row["id"], meta)
        self._log("info", f"文案完成：{video_id}，{len(transcript)} 字")
        return transcript

    def _is_low_quality_transcript(self, text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        if not compact:
            return True
        if "�" in compact:
            return True
        if len(compact) >= 20:
            counts = Counter(compact)
            most_common_ratio = counts.most_common(1)[0][1] / len(compact)
            unique_ratio = len(counts) / len(compact)
            if most_common_ratio > 0.45 or unique_ratio < 0.03:
                return True
        return bool(re.search(r"(.{1,3})\1{8,}", compact))

    def _distill_video(
        self,
        video_db_id: str,
        video_id: str,
        frames_dir: Path,
        frames: list[Path],
        row: dict[str, Any],
        transcript: str,
        evidence: dict[str, Any],
    ) -> None:
        selected = self._evidence_frame_paths(evidence, frames)
        visual_summary = timeline_prompt_summary(evidence)
        for dim in DIMENSIONS:
            self._check_cancelled()
            output_path = self.single_dir / f"{video_id}_{dim['name']}.md"
            if output_path.exists() and output_path.stat().st_size > 0:
                existing = output_path.read_text(encoding="utf-8", errors="ignore")
                existing_error = model_output_failure(existing)
                if not existing_error and self._analysis_has_evidence_refs(existing, video_id):
                    self.db.update_dimension(self.job_id, video_db_id, dim["name"], "done", output_path=str(output_path), error=None)
                    self._log("info", f"复用已完成维度：{video_id} / {dim['name']}")
                    continue
                reason = existing_error or "缺少证据引用"
                self._log("warn", f"检测到无效旧分析，重新生成：{video_id} / {dim['name']}（{reason}）")
                output_path.unlink()
            try:
                self.db.update_dimension(self.job_id, video_db_id, dim["name"], "running", error=None)
                prompt = (self.config.prompts_dir / dim["prompt"]).read_text(encoding="utf-8")
                info = (
                    f"---\n视频信息：\n"
                    f"- 标题：{row.get('title') or video_id}\n"
                    f"- 平台：{row.get('platform') or detect_platform(row['url'])}\n"
                    f"- 视频ID：{video_id}\n"
                )
                analysis_transcript = self._analysis_transcript(video_id, transcript)
                text_blocks = [
                    prompt,
                    info,
                    f"---\n完整文案（已验证+标点分段，必要时已按头中尾采样）：\n{analysis_transcript}\n",
                    "---\n已验证视觉证据时间线：以下条目中的 evidence_id、时间范围、画面描述和文案摘录可作为事实依据。"
                    "不得从静态图推断声音、完整台词或连续运镜；没有足够证据时必须写明未确认。"
                    "每个关键视觉、剪辑或脚本结论请在句末标注至少一个 evidence_id。\n"
                    + visual_summary,
                ]
                self._log("info", f"蒸馏维度：{video_id} / {dim['name']}")
                analysis = self.llm.chat_multimodal(
                    self.profile["analysis_model"],
                    text_blocks,
                    selected,
                    max_tokens=int(self.profile.get("max_tokens") or 8192),
                )
                analysis_error = model_output_failure(analysis)
                if analysis_error:
                    raise RuntimeError(f"模型返回无效分析：{analysis_error}")
                if not self._analysis_has_evidence_refs(analysis, video_id):
                    raise RuntimeError("模型分析缺少可追溯 evidence_id")
                output_path.write_text(analysis, encoding="utf-8")
                self.db.update_dimension(self.job_id, video_db_id, dim["name"], "done", output_path=str(output_path), error=None)
                self.db.add_artifact(self.job_id, "single_analysis", str(output_path), video_db_id, {"dimension": dim["name"]})
                self._delay_between_dimensions(dim != DIMENSIONS[-1])
            except Exception as exc:
                self.db.update_dimension(self.job_id, video_db_id, dim["name"], "failed", error=str(exc))
                raise RuntimeError(f"{dim['name']} 蒸馏失败：{exc}") from exc

    def _ensure_visual_evidence(
        self,
        video_db_id: str,
        video_id: str,
        video_path: Path,
        frames_dir: Path,
        raw_transcript_path: Path,
    ) -> dict[str, Any]:
        path = visual_timeline_path(self.evidence_dir, video_id)
        if path.exists():
            try:
                timeline = read_visual_timeline(path)
                validate_visual_timeline(timeline, require_visual_observations=True)
                self._log("info", f"复用已完成证据时间线：{video_id}")
                return timeline
            except Exception as exc:
                self._log("warn", f"{video_id} 旧证据时间线不可用，将重新生成：{exc}")

        self._log("info", f"构建证据时间线：{video_id}")
        timeline = build_visual_timeline(
            video_id,
            video_path,
            frames_dir,
            transcript_timeline_path(raw_transcript_path),
            self.evidence_dir,
            self.config.ffmpeg_bin,
            duration_seconds=self._video_duration_seconds(video_path),
        )
        existing: dict[str, dict[str, str]] = {}
        for shot in timeline.get("shots", []):
            observation = shot.get("visual_observation")
            if isinstance(observation, dict) and str(observation.get("visual_description") or "").strip():
                existing[str(shot["evidence_id"])] = {key: str(value) for key, value in observation.items()}

        pending = [shot for shot in timeline["shots"] if str(shot["evidence_id"]) not in existing]
        for batch_index, batch in enumerate(visual_batches({"shots": pending}), start=1):
            self._check_cancelled()
            expected_ids = {str(shot["evidence_id"]) for shot in batch}
            image_paths = [self.output_dir / str(shot["keyframe"]) for shot in batch]
            if any(not image.exists() for image in image_paths):
                missing = [str(image) for image in image_paths if not image.exists()]
                raise RuntimeError("证据关键帧不存在：" + "、".join(missing[:3]))
            prompt = build_visual_observation_prompt(video_id, batch)
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    self._log("info", f"视觉证据标注：{video_id}，批次 {batch_index}，第 {attempt + 1} 次")
                    raw = self.llm.chat_multimodal(
                        self.profile["analysis_model"],
                        [prompt],
                        image_paths,
                        max_tokens=min(4096, int(self.profile.get("max_tokens") or 8192)),
                    )
                    failure = model_output_failure(raw)
                    if failure:
                        raise ValueError(f"模型返回失败响应：{failure}")
                    existing.update(parse_visual_observations(raw, expected_ids))
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        self._log("warn", f"视觉证据批次 {batch_index} 未通过校验，自动重试：{exc}")
            else:
                raise RuntimeError(f"视觉证据批次 {batch_index} 连续两次未通过校验：{last_error}")

        timeline = apply_visual_observations(timeline, existing)
        write_visual_timeline(path, timeline)
        self.db.add_artifact(
            self.job_id,
            "visual_timeline",
            str(path),
            video_db_id,
            {
                "video_id": video_id,
                "shot_count": len(timeline.get("shots") or []),
                "transcript_alignment": timeline.get("quality", {}).get("transcript_alignment"),
                "detected_cut_segment_count": sum(
                    1 for shot in timeline.get("shots", []) if shot.get("segment_type") == "detected_cut_segment"
                ),
                "eligible_for_precise_timing": timeline.get("quality", {}).get("eligible_for_precise_timing"),
            },
        )
        self._log("info", f"证据时间线完成：{video_id}，{len(timeline.get('shots') or [])} 个镜头")
        return timeline

    def _evidence_frame_paths(self, evidence: dict[str, Any], fallback_frames: list[Path]) -> list[Path]:
        paths = [self.output_dir / path for path in evenly_selected_keyframes(evidence, self.config.max_dimension_frames)]
        paths = [path for path in paths if path.exists()]
        return paths or self._select_frames(fallback_frames)

    @staticmethod
    def _analysis_has_evidence_refs(text: str, video_id: str) -> bool:
        return f"video:{video_id}:shot:" in text

    def _requires_visual_evidence(self) -> bool:
        snapshot = getattr(self, "config_snapshot", {})
        return int(snapshot.get("evidence_timeline_version") or 0) >= 1

    def _merge_outputs(self, video_rows: list[dict[str, Any]]) -> None:
        self._log("info", "开始合并精炼")
        for dim in DIMENSIONS:
            self._check_cancelled()
            prompt = (self.config.prompts_dir / dim["prompt"]).read_text(encoding="utf-8")
            input_text = prompt + "\n\n## 输入数据\n\n"
            usable_count = 0
            for row in video_rows:
                analysis_path = self.single_dir / f"{row['video_id']}_{dim['name']}.md"
                transcript_path = self.transcript_dir / f"video_{row['video_id']}.md"
                if not analysis_path.exists():
                    continue
                usable_count += 1
                transcript = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
                input_text += (
                    f"\n### 视频 {usable_count}: {row.get('title') or row['video_id']}\n"
                    f"文案摘录: {transcript[:self.config.max_merge_chars_per_video]}...\n\n"
                    f"## {dim['name']} 分析\n{analysis_path.read_text(encoding='utf-8')}\n\n"
                )
            if usable_count == 0:
                continue
            merge_prompt = (
                f"你是资深内容策略师。请将多个视频的{dim['name']}分析合并为一份精炼的最终输出文件。\n\n"
                "要求：不是逐个拼接；提炼共性规律、公式、模板；去重；结构化；可直接执行；"
                f"标注基于 {usable_count} 个视频、更新时间和版本。\n\n"
                "请直接输出 Markdown 文件内容，不要解释。"
            )
            self._log("info", f"合并输出：{dim['output']}")
            try:
                merged = self.llm.chat_text(
                    self.profile["merge_model"],
                    merge_prompt + "\n\n" + input_text,
                    max_tokens=max(4096, int(self.profile.get("max_tokens") or 8192)),
                    reasoning=bool(self.profile.get("supports_reasoning")),
                )
            except Exception as exc:
                if self.profile.get("supports_reasoning"):
                    self._log("warn", f"reasoning 合并失败，关闭 reasoning 重试：{exc}")
                    merged = self.llm.chat_text(
                        self.profile["merge_model"],
                        merge_prompt + "\n\n" + input_text,
                        max_tokens=max(4096, int(self.profile.get("max_tokens") or 8192)),
                        reasoning=False,
                    )
                else:
                    raise
            final_path = self.output_dir / dim["output"]
            final_path.write_text(merged, encoding="utf-8")
            self.db.add_artifact(self.job_id, "final_output", str(final_path), None, {"dimension": dim["name"], "video_count": usable_count})

    def _write_benchmark_intelligence(self, video_rows: list[dict[str, Any]]) -> None:
        prompt_path = self.config.prompts_dir / BENCHMARK_PROMPT
        prompt_template = prompt_path.read_text(encoding="utf-8")
        video_rows = self._ensure_video_metadata(video_rows)
        creator = infer_creator(self.output_dir, self.config_snapshot)
        platform = infer_platform(video_rows)
        materials = collect_video_materials(
            video_rows,
            self.single_dir,
            self.transcript_dir,
            self.tmp_dir,
            self.keep_dir,
            DIMENSIONS,
            min(max(self.config.max_merge_chars_per_video, 1200), 2000),
            self.evidence_dir,
            require_visual_evidence=self._requires_visual_evidence(),
        )
        if not materials:
            self._log("warn", "没有可用于 Benchmark Intelligence 的完成视频，跳过新版结构化产物。")
            return

        legacy_outputs = self._legacy_output_paths()
        attempt = 1
        while True:
            self._check_cancelled()
            try:
                self._log("info", f"蒸馏维度：Benchmark Intelligence 汇总（第 {attempt} 次）")
                self._write_benchmark_intelligence_once(prompt_template, creator, platform, materials, legacy_outputs)
                self.db.update_job(self.job_id, error=None)
                return
            except BenchmarkModelRetryable as exc:
                delay = self._auto_retry_delay_seconds()
                message = (
                    "Benchmark Intelligence 云端模型输出未通过质量校验，"
                    f"已停止写入结构化产物，等待 {delay:.1f} 秒后自动重试第 {attempt + 1} 次：{exc}"
                )
                self.db.update_job(self.job_id, status="running", error=message)
                self._log("warn", message)
                self._sleep_interruptible(delay)
                attempt += 1

    def _ensure_video_metadata(self, video_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        needs_published_at = any(
            not extract_published_at(row.get("published_at"), self._source_meta_from_row(row)) for row in video_rows
        )
        if needs_published_at:
            refresh_job_platform_metadata(self.db, self.job_id, self.output_dir, self._log)
            video_rows = self.db.query_all("SELECT * FROM videos WHERE job_id = ? ORDER BY created_at ASC", [self.job_id])
        updated_rows: list[dict[str, Any]] = []
        duration_updates = 0
        published_updates = 0
        for row in video_rows:
            row = dict(row)
            source_meta = self._source_meta_from_row(row)
            duration = extract_duration_seconds(row.get("duration"), source_meta)
            if duration is None:
                video_path = self.tmp_dir / f"{row['video_id']}.mp4"
                if video_path.exists() and video_path.stat().st_size > 1024:
                    duration = self._video_duration_seconds(video_path)
            published_at = extract_published_at(row.get("published_at"), source_meta)

            changes: dict[str, Any] = {}
            if duration is not None and not row.get("duration"):
                changes["duration"] = duration
                row["duration"] = duration
                duration_updates += 1
            if published_at and not row.get("published_at"):
                changes["published_at"] = published_at
                row["published_at"] = published_at
                published_updates += 1
            if changes:
                merged_meta = self._merge_source_meta(source_meta, {}, duration, published_at)
                changes["source_meta_json"] = json.dumps(merged_meta, ensure_ascii=False)
                self.db.update_video(row["id"], **changes)
                row["source_meta_json"] = changes["source_meta_json"]
            updated_rows.append(row)

        if duration_updates or published_updates:
            self._log("info", f"已补充视频元数据：时长 {duration_updates} 条，发布时间 {published_updates} 条")
        return updated_rows

    def _write_benchmark_intelligence_once(
        self,
        prompt_template: str,
        creator: str,
        platform: str,
        materials: list[Any],
        legacy_outputs: dict[str, Path],
    ) -> None:
        remove_benchmark_outputs(self.output_dir)
        self.db.execute(
            """
            DELETE FROM artifacts
            WHERE job_id = ? AND kind IN (
                'benchmark_profile', 'benchmark_pattern_library', 'benchmark_qa_checklist',
                'retrieval_pack', 'video_card', 'video_notes', 'retrieval_index',
                'raw_refs', 'legacy_output'
            )
            """,
            [self.job_id],
        )
        self._log("info", f"生成 Benchmark Intelligence 结构化产物：每批 {BENCHMARK_BATCH_SIZE} 个视频")
        cards: list[dict[str, Any]] = []
        notes: dict[str, str] = {}
        batches = [materials[index : index + BENCHMARK_BATCH_SIZE] for index in range(0, len(materials), BENCHMARK_BATCH_SIZE)]
        for batch_index, batch in enumerate(batches, start=1):
            self._check_cancelled()
            self._log("info", f"Benchmark 视频卡片批次 {batch_index}/{len(batches)}")
            prompt = build_video_batch_prompt(prompt_template, creator, platform, batch)
            try:
                batch_data = self._benchmark_json_call(
                    prompt,
                    f"视频卡片批次 {batch_index}",
                    lambda parsed, current_batch=batch: normalize_video_cards_data(parsed, creator, platform, current_batch),
                )
            except Exception as exc:
                raise BenchmarkModelRetryable(f"视频卡片批次 {batch_index} 连续失败：{exc}") from exc
            batch_cards = batch_data["video_cards"]
            cards.extend(batch_cards)
            cards_by_id = {str(card["video_id"]): card for card in batch_cards}
            for material in batch:
                card = cards_by_id[material.video_id]
                try:
                    notes[material.video_id] = self._benchmark_video_note_call(
                        prompt_template,
                        creator,
                        platform,
                        material,
                        card,
                    )
                except Exception as exc:
                    raise BenchmarkModelRetryable(f"视频笔记 {material.video_id} 连续失败：{exc}") from exc

        try:
            creator_data = self._benchmark_creator_markdown_calls(
                prompt_template, creator, platform, cards, notes, materials, legacy_outputs
            )
        except Exception as exc:
            raise BenchmarkModelRetryable(f"账号级文档连续失败：{exc}") from exc
        data = {**creator_data, "video_cards": cards, "video_notes": notes}
        try:
            validate_benchmark_data(data, materials)
        except Exception as exc:
            raise BenchmarkModelRetryable(f"最终结构化产物质量校验失败：{exc}") from exc

        for artifact in write_benchmark_outputs(self.output_dir, creator, platform, data, materials, legacy_outputs):
            self.db.add_artifact(self.job_id, artifact["kind"], str(artifact["path"]), None, artifact.get("meta"))
        self._log("info", "Benchmark Intelligence 结构化产物已生成")

    def _benchmark_video_note_call(
        self,
        prompt_template: str,
        creator: str,
        platform: str,
        material: Any,
        card: dict[str, Any],
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._check_cancelled()
                self._log("info", f"Benchmark 视频笔记：{material.video_id}")
                prompt = build_video_note_prompt(prompt_template, creator, platform, material, card)
                if attempt:
                    prompt += "\n\n上次 Notes 未通过内容核验。请补足脚本、视觉、运营、证据方法段落，并引用稳定 evidence_id。"
                note = self._benchmark_text_call(prompt, f"视频笔记 {material.video_id}")
                validate_video_batch_data({"video_cards": [card], "video_notes": {material.video_id: note}}, [material])
                return note
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    self._log("warn", f"Benchmark 视频笔记 {material.video_id} 未通过校验，自动重试：{exc}")
        raise RuntimeError(f"Benchmark 视频笔记 {material.video_id} 连续两次未通过校验：{last_error}")

    def _benchmark_creator_markdown_calls(
        self,
        prompt_template: str,
        creator: str,
        platform: str,
        cards: list[dict[str, Any]],
        notes: dict[str, str],
        materials: list[Any],
        legacy_outputs: dict[str, Path],
    ) -> dict[str, str]:
        last_error: Exception | None = None
        for attempt in range(2):
            data: dict[str, str] = {}
            try:
                for output_key, spec in CREATOR_MARKDOWN_OUTPUTS.items():
                    self._check_cancelled()
                    self._log("info", f"Benchmark 账号级文档：{spec['title']}")
                    prompt = build_creator_markdown_prompt(
                        prompt_template,
                        creator,
                        platform,
                        cards,
                        legacy_outputs,
                        max(self.config.max_merge_chars_per_video, 4000),
                        output_key,
                    )
                    if attempt:
                        prompt += "\n\n上次账号级文档未通过内容核验。请补足真实 video_id、证据引用、方法总结和风险边界。"
                    data[output_key] = self._benchmark_text_call(prompt, spec["title"], timeout_seconds=180)
                creator_result = normalize_creator_summary_data(data)
                validate_benchmark_data({**creator_result, "video_cards": cards, "video_notes": notes}, materials)
                return creator_result
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    self._log("warn", f"Benchmark 账号级文档未通过校验，自动重试：{exc}")
        raise RuntimeError(f"Benchmark 账号级文档连续两次未通过校验：{last_error}")

    def _benchmark_text_call(self, prompt: str, label: str, timeout_seconds: int | None = None) -> str:
        def call_model() -> str:
            return self.llm.chat_text(
                self.profile["merge_model"],
                prompt,
                max_tokens=max(4096, int(self.profile.get("max_tokens") or 8192)),
                reasoning=False,
            ).strip()

        raw = self._call_with_timeout(call_model, timeout_seconds, label) if timeout_seconds else call_model()
        failure = model_output_failure(raw)
        if failure:
            raise ValueError(f"{label} 模型返回失败响应：{failure}")
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
        return raw

    def _call_with_timeout(self, func: Callable[[], str], timeout_seconds: int | None, label: str) -> str:
        if not timeout_seconds:
            return func()
        result_queue: queue.Queue[tuple[str, str | BaseException]] = queue.Queue(maxsize=1)

        def runner() -> None:
            try:
                result_queue.put(("ok", func()))
            except BaseException as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        try:
            status, payload = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError(f"{label} 模型调用超过 {timeout_seconds} 秒未返回") from exc
        if status == "error":
            raise payload
        return str(payload)

    def _benchmark_json_call(
        self,
        prompt: str,
        label: str,
        normalize: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            retry_prompt = prompt if attempt == 0 else prompt + "\n\n上次结果未通过 schema/内容校验。请重新完整返回合法 JSON，不要解释。"
            try:
                raw = self.llm.chat_text(
                    self.profile["merge_model"],
                    retry_prompt,
                    max_tokens=max(8192, int(self.profile.get("max_tokens") or 8192)),
                    reasoning=bool(self.profile.get("supports_reasoning")) and attempt == 0,
                )
                failure = model_output_failure(raw)
                if failure:
                    raise ValueError(f"模型返回失败响应：{failure}")
                return normalize(parse_benchmark_json(raw))
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    self._log("warn", f"Benchmark {label} 未通过校验，自动重试：{exc}")
        raise RuntimeError(f"Benchmark {label} 连续两次未通过校验：{last_error}")

    def _legacy_output_paths(self) -> dict[str, Path]:
        return {dim["name"]: self.output_dir / dim["output"] for dim in DIMENSIONS}

    def _select_frames(self, frames: list[Path]) -> list[Path]:
        if len(frames) <= self.config.max_dimension_frames:
            return frames
        step = max(1, len(frames) // self.config.max_dimension_frames)
        return frames[::step][: self.config.max_dimension_frames]

    def _analysis_transcript(self, video_id: str, transcript: str) -> str:
        limit = self.config.max_analysis_chars_per_video
        if limit <= 0 or len(transcript) <= limit:
            return transcript
        head_len = int(limit * 0.45)
        middle_len = int(limit * 0.25)
        tail_len = limit - head_len - middle_len
        middle_start = max(0, (len(transcript) - middle_len) // 2)
        self._log("warn", f"{video_id} 文案 {len(transcript)} 字，送模型前采样到 {limit} 字以降低限流风险")
        return (
            transcript[:head_len]
            + "\n\n...[中段采样]...\n\n"
            + transcript[middle_start : middle_start + middle_len]
            + "\n\n...[末段采样]...\n\n"
            + transcript[-tail_len:]
        )

    def _write_progress(self) -> None:
        videos = self.db.query_all("SELECT * FROM videos WHERE job_id = ? ORDER BY created_at ASC", [self.job_id])
        summary = [
            {
                "videoId": row["video_id"],
                "desc": row.get("title") or "",
                "status": row["status"],
                "error": row.get("error"),
                "skipReason": row.get("skip_reason"),
                "retryCount": row.get("retry_count"),
                "nextRetryAt": row.get("next_retry_at"),
            }
            for row in videos
        ]
        (self.output_dir / "进度.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.db.add_artifact(self.job_id, "progress", str(self.output_dir / "进度.json"))

    def _write_manifest(self) -> None:
        prompt_hashes = {}
        for dim in DIMENSIONS:
            prompt_path = self.config.prompts_dir / dim["prompt"]
            prompt_hashes[dim["prompt"]] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        benchmark_prompt_path = self.config.prompts_dir / BENCHMARK_PROMPT
        if benchmark_prompt_path.exists():
            prompt_hashes[BENCHMARK_PROMPT] = hashlib.sha256(benchmark_prompt_path.read_bytes()).hexdigest()
        artifacts = self.db.query_all("SELECT kind, path, meta_json FROM artifacts WHERE job_id = ? ORDER BY created_at ASC", [self.job_id])
        manifest = {
            "schema_version": 1,
            "software_version": __version__,
            "job_id": self.job_id,
            "created_at": self.job["created_at"],
            "finished_at": utc_now(),
            "state_machine": [
                "preflight",
                "parse",
                "download",
                "frames",
                "transcript",
                "visual_evidence",
                "material_check",
                "distill",
                "merge",
                "benchmark_intelligence",
                "done",
            ],
            "pipeline_dimensions": {
                "single_video": SINGLE_VIDEO_DIMENSIONS,
                "creator_level": BENCHMARK_DIMENSION,
            },
            "model_profile": self.profile,
            "prompt_hashes": prompt_hashes,
            "config": self.config_snapshot,
            "artifacts": artifacts,
        }
        path = self.output_dir / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.db.add_artifact(self.job_id, "manifest", str(path))

    def _delay_between_videos(self, should_delay: bool) -> None:
        if not should_delay:
            return
        delay = random.randint(self.config.video_delay_min_ms, self.config.video_delay_max_ms) / 1000
        self._log("info", f"视频间隔 {delay:.1f} 秒")
        self._sleep_interruptible(delay)

    def _delay_between_dimensions(self, should_delay: bool) -> None:
        if not should_delay:
            return
        delay = random.randint(self.config.dimension_delay_min_ms, self.config.dimension_delay_max_ms) / 1000
        self._log("info", f"维度间隔 {delay:.1f} 秒")
        self._sleep_interruptible(delay)

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self._check_cancelled()
            time.sleep(min(1, end - time.time()))

    def _check_cancelled(self) -> None:
        job = self.db.get_job(self.job_id)
        if job.get("cancel_requested"):
            raise JobCancelled()

    def _log(self, level: str, message: str) -> None:
        self.db.add_log(self.job_id, level, message)


def config_snapshot(config: AppConfig, inputs: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = asdict(config)
    data = {key: str(value) if isinstance(value, Path) else value for key, value in data.items()}
    data["inputs"] = inputs
    data["evidence_timeline_version"] = 1
    if extra:
        data.update(extra)
    return data
