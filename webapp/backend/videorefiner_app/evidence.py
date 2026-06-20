from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .utils import list_visible_files, run_command, utc_now


TIMELINE_SCHEMA_VERSION = "video-refiner.visual_timeline.v1"
MAX_SCENE_SERIES_POINTS = 360
VISUAL_BATCH_SIZE = 6
FRAME_INDEX_RE = re.compile(r"(\d+)(?=\.[^.]+$)")
DETECTED_CUT_THRESHOLD = 0.45
DETECTED_CUT_HIGH_THRESHOLD = 0.7
MIN_PRECISE_TIMING_SEGMENTS = 2


def transcript_timeline_path(raw_transcript_path: Path) -> Path:
    return raw_transcript_path.with_name(f"{raw_transcript_path.stem}_timeline.json")


def visual_timeline_path(evidence_dir: Path, video_id: str) -> Path:
    return evidence_dir / f"{video_id}.visual_timeline.json"


def build_visual_timeline(
    video_id: str,
    video_path: Path,
    frames_dir: Path,
    transcript_path: Path,
    evidence_dir: Path,
    ffmpeg_bin: str,
    *,
    duration_seconds: float | None = None,
    scene_points: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Create an auditable timeline from local media and timed text only."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    frame_interval = _frame_interval_seconds(frames_dir)
    frames = list_visible_files(frames_dir, "*.jpg")
    if not frames:
        raise ValueError("证据时间线缺少帧图")

    duration = duration_seconds or _media_duration(video_path, ffmpeg_bin)
    if duration is None or duration <= 0:
        duration = max(frame_interval * max(len(frames) - 1, 1), float(len(frames)))

    if scene_points is None:
        try:
            scene_points = collect_scene_points(video_path, ffmpeg_bin)
        except Exception:
            scene_points = []
    scene_points = _normalize_scene_points(scene_points)
    transcript = read_transcript_timeline(transcript_path)
    frame_rows = [
        {
            "path": _relative_frame_path(path, evidence_dir.parent),
            "timestamp_seconds": round(_frame_timestamp(path, frame_interval), 3),
        }
        for path in frames
    ]
    selected = _select_evidence_frames(frame_rows, scene_points, duration)
    detected_cuts = select_detected_cut_points(scene_points)
    shots = _build_shots(video_id, selected, transcript, duration, detected_cuts)
    if not shots:
        raise ValueError("证据时间线未生成可用镜头")

    alignment_status = "timed" if any(item.get("timing") == "timed" for item in transcript) else "coarse"
    timeline = {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "video_id": video_id,
        "generated_at": utc_now(),
        "duration_seconds": round(duration, 3),
        "frame_interval_seconds": frame_interval,
        "scene_curve": _downsample_scene_points(scene_points),
        "shots": shots,
        "quality": {
            "frame_count": len(frames),
            "shot_count": len(shots),
            "scene_detection": "available" if scene_points else "unavailable",
            "transcript_alignment": alignment_status,
            "visual_observations": "pending",
            "observation_coverage": "partial",
            "visual_confidence_summary": {"high": 0, "medium": 0, "low": 0},
            "alignment_status": alignment_status,
            "eligible_for_precise_timing": False,
        },
    }
    validate_visual_timeline(timeline, require_visual_observations=False)
    path = visual_timeline_path(evidence_dir, video_id)
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    return timeline


def read_visual_timeline(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"证据时间线不可读取：{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError("证据时间线必须是 JSON 对象")
    return _upgrade_legacy_timeline(value)


def read_transcript_timeline(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segments, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        start = _as_seconds(item.get("start_seconds"))
        end = _as_seconds(item.get("end_seconds"))
        if not text or start is None or end is None or end < start:
            continue
        normalized.append(
            {
                "start_seconds": start,
                "end_seconds": end,
                "text": text,
                "source": str(item.get("source") or "transcript"),
                "timing": str(item.get("timing") or "timed"),
            }
        )
    return normalized


def collect_scene_points(video_path: Path, ffmpeg_bin: str) -> list[dict[str, float]]:
    result = run_command(
        [
            ffmpeg_bin,
            "-i",
            str(video_path),
            "-vf",
            "select='gte(scene,0)',metadata=print:file=-",
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=900,
    )
    output = "\n".join([result.stdout or "", result.stderr or ""])
    if result.returncode != 0 and "lavfi.scene_score" not in output:
        raise RuntimeError((result.stderr or result.stdout or "场景变化检测失败").strip()[-500:])
    points: list[dict[str, float]] = []
    current_time: float | None = None
    for line in output.splitlines():
        time_match = re.search(r"pts_time:([\d.]+)", line)
        if time_match:
            current_time = float(time_match.group(1))
            continue
        score_match = re.search(r"lavfi\.scene_score=([\d.eE+-]+)", line)
        if score_match and current_time is not None:
            try:
                score = float(score_match.group(1))
            except ValueError:
                continue
            points.append({"timestamp_seconds": current_time, "score": score})
            current_time = None
    return points


def select_scene_peaks(points: list[dict[str, float]], threshold: float = 0.18, gap_seconds: float = 0.75) -> list[dict[str, float]]:
    meaningful = [item for item in _normalize_scene_points(points) if item["score"] >= threshold]
    if not meaningful:
        return []
    groups: list[list[dict[str, float]]] = [[meaningful[0]]]
    for point in meaningful[1:]:
        previous = groups[-1][-1]
        if point["timestamp_seconds"] - previous["timestamp_seconds"] <= gap_seconds:
            groups[-1].append(point)
        else:
            groups.append([point])
    return [max(group, key=lambda item: item["score"]) for group in groups]


def select_detected_cut_points(points: list[dict[str, float]]) -> list[dict[str, float | str]]:
    """Return only high-enough scene peaks that can be used as segment boundaries."""
    cuts: list[dict[str, float | str]] = []
    for point in select_scene_peaks(points, threshold=DETECTED_CUT_THRESHOLD):
        score = float(point["score"])
        cuts.append(
            {
                **point,
                "boundary_confidence": "high" if score >= DETECTED_CUT_HIGH_THRESHOLD else "medium",
            }
        )
    return cuts


def visual_batches(timeline: dict[str, Any], max_batch_size: int = VISUAL_BATCH_SIZE) -> list[list[dict[str, Any]]]:
    shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
    return [shots[index : index + max_batch_size] for index in range(0, len(shots), max_batch_size)]


def build_visual_observation_prompt(video_id: str, shots: list[dict[str, Any]]) -> str:
    manifest = [
        {
            "evidence_id": shot["evidence_id"],
            "time_range": shot["time_range"],
            "segment_type": shot.get("segment_type"),
            "boundary_source": shot.get("boundary_source"),
            "boundary_confidence": shot.get("boundary_confidence"),
            "transcript_excerpt": shot.get("transcript_excerpt", ""),
            "ocr_excerpt": shot.get("ocr_excerpt", ""),
        }
        for shot in shots
    ]
    return "\n".join(
        [
            "你是视频视觉证据标注员。按图片顺序分析已编号的镜头，严格返回 JSON 对象。",
            "只能描述图片中可见的主体、动作、构图、色调、屏幕文字和剪辑切换痕迹。",
            "不得从静态图推断声音、背景音乐、完整台词或连续运镜；若证据不足，写‘未确认’。",
            "segment_type=evidence_window 是抽样分析窗口，不等于真实镜头时长或精确切点。"
            "只有 segment_type=detected_cut_segment 才表示由场景检测得到的切段边界。",
            "台词事实只能来自 transcript_excerpt，屏幕文字事实只能来自 ocr_excerpt 或图中清晰可见文字。",
            "输出格式：{\"shots\":[{\"evidence_id\":\"...\",\"visual_description\":\"...\",\"shot_type\":\"...\",\"composition\":\"...\",\"on_screen_text_observation\":\"...\",\"transition_observation\":\"...\",\"confidence\":\"high|medium|low\",\"uncertainty\":\"...\"}]}。",
            f"video_id: {video_id}",
            "镜头清单：",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        ]
    )


def parse_visual_observations(raw: str, expected_ids: set[str]) -> dict[str, dict[str, str]]:
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("视觉证据模型未返回 JSON")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"视觉证据 JSON 解析失败：{exc}") from exc
    rows = data.get("shots") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("视觉证据 JSON 缺少 shots")
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        evidence_id = str(row.get("evidence_id") or "")
        if evidence_id not in expected_ids:
            continue
        description = str(row.get("visual_description") or "").strip()
        if len(description) < 12:
            raise ValueError(f"{evidence_id} 缺少可信画面描述")
        confidence = str(row.get("confidence") or "").lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        result[evidence_id] = {
            "visual_description": description,
            "shot_type": str(row.get("shot_type") or "未确认").strip(),
            "composition": str(row.get("composition") or "未确认").strip(),
            "on_screen_text_observation": str(row.get("on_screen_text_observation") or "未确认").strip(),
            "transition_observation": str(row.get("transition_observation") or "未确认").strip(),
            "confidence": confidence,
            "uncertainty": str(row.get("uncertainty") or "").strip(),
        }
    if set(result) != expected_ids:
        missing = sorted(expected_ids - set(result))
        raise ValueError("视觉证据结果缺少镜头：" + "、".join(missing))
    return result


def apply_visual_observations(timeline: dict[str, Any], observations: dict[str, dict[str, str]]) -> dict[str, Any]:
    shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
    for shot in shots:
        evidence_id = str(shot.get("evidence_id") or "")
        if evidence_id not in observations:
            raise ValueError(f"视觉证据缺少 {evidence_id}")
        shot["visual_observation"] = observations[evidence_id]
    quality = timeline.setdefault("quality", {})
    quality["visual_observations"] = "complete"
    quality["observation_coverage"] = "complete"
    quality["visual_confidence_summary"] = _visual_confidence_summary(shots)
    quality["alignment_status"] = str(quality.get("alignment_status") or quality.get("transcript_alignment") or "coarse")
    quality["transcript_alignment"] = quality["alignment_status"]
    quality["eligible_for_precise_timing"] = _eligible_for_precise_timing(timeline)
    validate_visual_timeline(timeline, require_visual_observations=True)
    return timeline


def write_visual_timeline(path: Path, timeline: dict[str, Any]) -> None:
    validate_visual_timeline(timeline, require_visual_observations=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_visual_timeline(timeline: dict[str, Any], require_visual_observations: bool = True) -> None:
    _upgrade_legacy_timeline(timeline)
    if timeline.get("schema_version") != TIMELINE_SCHEMA_VERSION:
        raise ValueError("证据时间线 schema 不匹配")
    video_id = str(timeline.get("video_id") or "")
    shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
    if not video_id or not shots:
        raise ValueError("证据时间线缺少视频或镜头")
    quality = timeline.get("quality") if isinstance(timeline.get("quality"), dict) else {}
    observation_coverage = str(quality.get("observation_coverage") or "")
    if observation_coverage not in {"complete", "partial"}:
        raise ValueError("证据时间线缺少有效视觉覆盖状态")
    alignment_status = str(quality.get("alignment_status") or "")
    if alignment_status not in {"timed", "coarse"}:
        raise ValueError("证据时间线缺少有效文案对齐状态")
    confidence_summary = quality.get("visual_confidence_summary")
    if not isinstance(confidence_summary, dict) or any(
        not isinstance(confidence_summary.get(level), int) or confidence_summary.get(level, 0) < 0
        for level in ("high", "medium", "low")
    ):
        raise ValueError("证据时间线缺少有效视觉置信度汇总")
    if not isinstance(quality.get("eligible_for_precise_timing"), bool):
        raise ValueError("证据时间线缺少精确时序资格标记")

    ids: set[str] = set()
    for shot in shots:
        evidence_id = str(shot.get("evidence_id") or "")
        if not evidence_id.startswith(f"video:{video_id}:shot:") or evidence_id in ids:
            raise ValueError("证据时间线存在无效或重复 evidence_id")
        ids.add(evidence_id)
        if not str(shot.get("keyframe") or "").strip():
            raise ValueError(f"{evidence_id} 缺少关键帧")
        if _as_seconds(shot.get("start_seconds")) is None or _as_seconds(shot.get("end_seconds")) is None:
            raise ValueError(f"{evidence_id} 缺少时间范围")
        segment_type = str(shot.get("segment_type") or "")
        boundary_source = str(shot.get("boundary_source") or "")
        boundary_confidence = str(shot.get("boundary_confidence") or "")
        if segment_type == "evidence_window":
            if boundary_source not in {"uniform_coverage", "scene_peak"} or boundary_confidence != "low":
                raise ValueError(f"{evidence_id} 证据窗口边界语义无效")
        elif segment_type == "detected_cut_segment":
            if boundary_source != "detected_cut" or boundary_confidence not in {"high", "medium"}:
                raise ValueError(f"{evidence_id} 检测切段边界语义无效")
        else:
            raise ValueError(f"{evidence_id} 缺少有效分段类型")
        if require_visual_observations and not str((shot.get("visual_observation") or {}).get("visual_description") or "").strip():
            raise ValueError(f"{evidence_id} 缺少视觉证据")
    if require_visual_observations and observation_coverage != "complete":
        raise ValueError("证据时间线视觉覆盖未完成")
    if quality.get("eligible_for_precise_timing") and not _eligible_for_precise_timing(timeline):
        raise ValueError("证据时间线不满足精确时序资格")


def timeline_prompt_summary(timeline: dict[str, Any], max_shots: int = 24) -> str:
    rows = []
    shots = _evenly_selected_shots(timeline.get("shots") or [], max_shots)
    for shot in shots:
        visual = shot.get("visual_observation") if isinstance(shot.get("visual_observation"), dict) else {}
        rows.append(
            {
                "evidence_id": shot.get("evidence_id"),
                "time_range": shot.get("time_range"),
                "segment_type": shot.get("segment_type"),
                "boundary_source": shot.get("boundary_source"),
                "boundary_confidence": shot.get("boundary_confidence"),
                "visual": visual.get("visual_description") or "未标注",
                "screen_text": visual.get("on_screen_text_observation") or shot.get("ocr_excerpt") or "",
                "transcript_excerpt": shot.get("transcript_excerpt") or "",
                "transition": visual.get("transition_observation") or "未确认",
                "confidence": visual.get("confidence") or "low",
            }
        )
    quality = timeline.get("quality") if isinstance(timeline.get("quality"), dict) else {}
    return json.dumps(
        {
            "quality": {
                "alignment_status": quality.get("alignment_status") or quality.get("transcript_alignment") or "coarse",
                "eligible_for_precise_timing": bool(quality.get("eligible_for_precise_timing")),
            },
            "segments": rows,
        },
        ensure_ascii=False,
        indent=2,
    )


def evenly_selected_keyframes(timeline: dict[str, Any], max_frames: int) -> list[str]:
    return [str(shot.get("keyframe")) for shot in _evenly_selected_shots(timeline.get("shots") or [], max_frames) if shot.get("keyframe")]


def timeline_evidence_summary(timeline: dict[str, Any]) -> dict[str, Any]:
    """Small, stable metadata for retrieval indexes; never includes media or transcript content."""
    _upgrade_legacy_timeline(timeline)
    shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
    quality = timeline.get("quality") if isinstance(timeline.get("quality"), dict) else {}
    confidence = quality.get("visual_confidence_summary") if isinstance(quality.get("visual_confidence_summary"), dict) else {}
    return {
        "segmentCount": len(shots),
        "detectedCutSegmentCount": sum(1 for shot in shots if shot.get("segment_type") == "detected_cut_segment"),
        "observationCoverage": str(quality.get("observation_coverage") or "partial"),
        "alignmentStatus": str(quality.get("alignment_status") or "coarse"),
        "eligibleForPreciseTiming": bool(quality.get("eligible_for_precise_timing")),
        "confidence": {level: int(confidence.get(level) or 0) for level in ("high", "medium", "low")},
    }


def _upgrade_legacy_timeline(timeline: dict[str, Any]) -> dict[str, Any]:
    """Keep old evidence files readable while giving their midpoint windows honest semantics."""
    shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        if not shot.get("segment_type"):
            shot["segment_type"] = "evidence_window"
        if not shot.get("boundary_source"):
            shot["boundary_source"] = "scene_peak" if float(shot.get("scene_score") or 0) > 0 else "uniform_coverage"
        if not shot.get("boundary_confidence"):
            shot["boundary_confidence"] = "low"

    quality = timeline.setdefault("quality", {})
    if not isinstance(quality, dict):
        quality = {}
        timeline["quality"] = quality
    alignment_status = str(quality.get("alignment_status") or quality.get("transcript_alignment") or "coarse")
    if alignment_status not in {"timed", "coarse"}:
        alignment_status = "coarse"
    quality["alignment_status"] = alignment_status
    quality["transcript_alignment"] = alignment_status
    visual_observations = str(quality.get("visual_observations") or "")
    has_complete_observations = bool(shots) and all(
        str((shot.get("visual_observation") or {}).get("visual_description") or "").strip()
        for shot in shots
        if isinstance(shot, dict)
    )
    if visual_observations not in {"complete", "pending"}:
        visual_observations = "complete" if has_complete_observations else "pending"
    quality["visual_observations"] = visual_observations
    quality["observation_coverage"] = "complete" if has_complete_observations else "partial"
    quality["visual_confidence_summary"] = _visual_confidence_summary(shots)
    if not isinstance(quality.get("eligible_for_precise_timing"), bool):
        quality["eligible_for_precise_timing"] = _eligible_for_precise_timing(timeline)
    return timeline


def _visual_confidence_summary(shots: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"high": 0, "medium": 0, "low": 0}
    for shot in shots:
        observation = shot.get("visual_observation") if isinstance(shot, dict) else None
        if not isinstance(observation, dict) or not str(observation.get("visual_description") or "").strip():
            continue
        confidence = str(observation.get("confidence") or "low").lower()
        summary[confidence if confidence in summary else "low"] += 1
    return summary


def _eligible_for_precise_timing(timeline: dict[str, Any]) -> bool:
    quality = timeline.get("quality") if isinstance(timeline.get("quality"), dict) else {}
    if str(quality.get("alignment_status") or quality.get("transcript_alignment") or "coarse") != "timed":
        return False
    shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
    detected_count = sum(
        1
        for shot in shots
        if isinstance(shot, dict)
        and shot.get("segment_type") == "detected_cut_segment"
        and shot.get("boundary_confidence") in {"high", "medium"}
    )
    return detected_count >= MIN_PRECISE_TIMING_SEGMENTS


def _build_shots(
    video_id: str,
    selected: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    duration: float,
    detected_cuts: list[dict[str, float | str]],
) -> list[dict[str, Any]]:
    selected = sorted(selected, key=lambda item: item["timestamp_seconds"])
    shots: list[dict[str, Any]] = []
    for index, item in enumerate(selected, start=1):
        previous = selected[index - 2]["timestamp_seconds"] if index > 1 else 0.0
        following = selected[index]["timestamp_seconds"] if index < len(selected) else duration
        start = 0.0 if index == 1 else (previous + item["timestamp_seconds"]) / 2
        end = duration if index == len(selected) else (item["timestamp_seconds"] + following) / 2
        shots.append(
            _shot_record(
                start,
                end,
                item,
                transcript,
                segment_type="evidence_window",
                boundary_source=str(item.get("selection_source") or "uniform_coverage"),
                boundary_confidence="low",
            )
        )

    segment_limit = max(2, _shot_budget(duration) // 2)
    candidates = [
        (left, right)
        for left, right in zip(detected_cuts, detected_cuts[1:])
        if float(right["timestamp_seconds"]) - float(left["timestamp_seconds"]) >= 0.5
    ]
    candidates = sorted(
        candidates,
        key=lambda pair: min(float(pair[0]["score"]), float(pair[1]["score"])),
        reverse=True,
    )[:segment_limit]
    for left, right in candidates:
        start = float(left["timestamp_seconds"])
        end = float(right["timestamp_seconds"])
        midpoint = (start + end) / 2
        keyframe = _closest_frame(selected, midpoint)
        boundary_confidence = "high" if left["boundary_confidence"] == right["boundary_confidence"] == "high" else "medium"
        shots.append(
            _shot_record(
                start,
                end,
                keyframe,
                transcript,
                segment_type="detected_cut_segment",
                boundary_source="detected_cut",
                boundary_confidence=boundary_confidence,
                scene_score=max(float(left["score"]), float(right["score"])),
            )
        )

    ordered = sorted(shots, key=lambda shot: (shot["start_seconds"], shot["end_seconds"], shot["segment_type"]))
    for index, shot in enumerate(ordered, start=1):
        shot["evidence_id"] = f"video:{video_id}:shot:{index:03d}"
    return ordered


def _shot_record(
    start: float,
    end: float,
    keyframe: dict[str, Any],
    transcript: list[dict[str, Any]],
    *,
    segment_type: str,
    boundary_source: str,
    boundary_confidence: str,
    scene_score: float | None = None,
) -> dict[str, Any]:
    related = _related_text(transcript, start, end)
    transcript_text = " ".join(row["text"] for row in related if row["source"] != "ocr")
    ocr_text = " ".join(row["text"] for row in related if row["source"] == "ocr")
    return {
        "evidence_id": "",
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "time_range": f"{_format_stamp(start)}-{_format_stamp(end)}",
        "keyframe": keyframe["path"],
        "scene_score": round(float(scene_score if scene_score is not None else keyframe.get("scene_score") or 0), 5),
        "segment_type": segment_type,
        "boundary_source": boundary_source,
        "boundary_confidence": boundary_confidence,
        "transcript_excerpt": _trim(transcript_text, 420),
        "ocr_excerpt": _trim(ocr_text, 240),
        "text_alignment": "timed" if any(row["timing"] == "timed" for row in related) else "coarse",
        "visual_observation": {},
    }


def _select_evidence_frames(frame_rows: list[dict[str, Any]], scene_points: list[dict[str, float]], duration: float) -> list[dict[str, Any]]:
    budget = _shot_budget(duration)
    coverage_times = [duration * index / max(1, budget - 1) for index in range(budget)]
    selected = [{**_closest_frame(frame_rows, timestamp), "selection_source": "uniform_coverage"} for timestamp in coverage_times]
    peaks = select_scene_peaks(scene_points)
    peak_limit = max(2, budget // 2)
    for peak in sorted(peaks, key=lambda item: item["score"], reverse=True)[:peak_limit]:
        frame = _closest_frame(frame_rows, peak["timestamp_seconds"])
        frame["scene_score"] = peak["score"]
        frame["selection_source"] = "scene_peak"
        selected.append(frame)

    deduped: dict[str, dict[str, Any]] = {}
    for row in selected:
        existing = deduped.get(row["path"])
        if existing is None or row.get("selection_source") == "scene_peak":
            deduped[row["path"]] = row
    values = sorted(deduped.values(), key=lambda item: item["timestamp_seconds"])
    if len(values) <= budget:
        return values
    return _downsample_selected(values, budget)


def _downsample_selected(values: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(budget)]
    first = values[0]["timestamp_seconds"]
    last = values[-1]["timestamp_seconds"]
    span = max(last - first, 1.0)
    for item in values:
        bucket = min(budget - 1, int((item["timestamp_seconds"] - first) / span * budget))
        buckets[bucket].append(item)
    selected = [max(bucket, key=lambda item: item.get("scene_score", 0)) for bucket in buckets if bucket]
    return sorted(selected, key=lambda item: item["timestamp_seconds"])


def _evenly_selected_shots(shots: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(shots) <= limit:
        return shots
    indexes = {round(index * (len(shots) - 1) / max(1, limit - 1)) for index in range(limit)}
    return [shot for index, shot in enumerate(shots) if index in indexes]


def _related_text(items: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    related = [item for item in items if item["end_seconds"] >= start and item["start_seconds"] <= end]
    return related[:8]


def _closest_frame(frames: list[dict[str, Any]], timestamp: float) -> dict[str, Any]:
    best = min(frames, key=lambda item: abs(item["timestamp_seconds"] - timestamp))
    return {**best, "scene_score": float(best.get("scene_score") or 0)}


def _shot_budget(duration: float) -> int:
    if duration <= 60:
        return 10
    if duration <= 300:
        return 18
    if duration <= 600:
        return 30
    return 36


def _frame_interval_seconds(frames_dir: Path) -> float:
    meta_path = frames_dir / "frames_meta.json"
    try:
        value = json.loads(meta_path.read_text(encoding="utf-8"))
        interval = float(value.get("frame_interval_seconds") or 1)
        return max(interval, 0.1)
    except (OSError, ValueError, json.JSONDecodeError):
        return 1.0


def _frame_timestamp(path: Path, interval: float) -> float:
    match = FRAME_INDEX_RE.search(path.name)
    if not match:
        return 0.0
    return max(0.0, (int(match.group(1)) - 1) * interval)


def _relative_frame_path(path: Path, output_dir: Path) -> str:
    try:
        return path.relative_to(output_dir).as_posix()
    except ValueError:
        return path.name


def _media_duration(video_path: Path, ffmpeg_bin: str) -> float | None:
    ffmpeg_path = Path(ffmpeg_bin)
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


def _normalize_scene_points(points: list[dict[str, float]]) -> list[dict[str, float]]:
    normalized = []
    for item in points:
        timestamp = _as_seconds(item.get("timestamp_seconds", item.get("t")))
        score = _as_seconds(item.get("score"))
        if timestamp is None or score is None or timestamp < 0 or score < 0:
            continue
        normalized.append({"timestamp_seconds": round(timestamp, 3), "score": float(score)})
    return sorted(normalized, key=lambda item: item["timestamp_seconds"])


def _downsample_scene_points(points: list[dict[str, float]]) -> list[dict[str, float]]:
    if len(points) <= MAX_SCENE_SERIES_POINTS:
        return points
    size = math.ceil(len(points) / MAX_SCENE_SERIES_POINTS)
    result = []
    for start in range(0, len(points), size):
        result.append(max(points[start : start + size], key=lambda item: item["score"]))
    return result


def _as_seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _trim(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip() + "…"


def _format_stamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
