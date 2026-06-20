from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .benchmark import (
    MODEL_FAILURE_MARKERS,
    PLACEHOLDER_MARKERS,
    REQUIRED_CARD_LIST_FIELDS,
    REQUIRED_CARD_TEXT_FIELDS,
)
from .evidence import read_visual_timeline, validate_visual_timeline
from .utils import local_timestamp, utc_now


EXPORT_SCHEMA_VERSION = "videoautomation.creator_base.v1"
VALIDATION_SCHEMA_VERSION = "videoautomation.export_validation.v1"
EXPORT_PRODUCT_NAME = "VideoAutomation 起号基底包"

REQUIRED_TOP_LEVEL_FILES = [
    "retrieval_pack.md",
    "creator_profile.md",
    "pattern_library.md",
    "qa_checklist.md",
    "retrieval_index.json",
]

DIMENSION_NAMES = ("文案风格", "视频脚本", "剪辑逻辑", "选题策略", "运营策略")
SENSITIVE_KEY_PARTS = ("api_key", "apikey", "authorization", "cookie", "secret", "token", "password")
OMITTED_PATH_KEYS = {"path", "download_path", "kept_path", "legacy_copy_path"}
ABSOLUTE_PATH_PATTERN = re.compile(r"(?:^|[\s\"'`])/(?:Users|Volumes|private|tmp)/")


class ExportValidationError(ValueError):
    def __init__(self, report_path: Path, report: dict[str, Any]):
        issues = "；".join(report["blockingIssues"][:5])
        super().__init__(f"导出质量核验未通过：{issues}。核验报告：{report_path}")
        self.report_path = report_path
        self.report = report


def export_videoautomation_package(
    output_dir: Path,
    fallback_roots: tuple[Path, ...] = (),
    requested_video_count: int | None = None,
) -> dict[str, Any]:
    source_dir = _resolve_source_dir(output_dir, fallback_roots)
    if not source_dir.exists() or not source_dir.is_dir():
        raise ValueError("输出目录不存在")

    report = validate_videoautomation_export(source_dir, requested_video_count)
    timestamp = local_timestamp()
    if report["status"] != "passed":
        failed_dir = source_dir / f"videoautomation_export_FAILED_{timestamp}"
        failed_dir.mkdir(parents=True, exist_ok=False)
        report_path = failed_dir / "validation_report.json"
        _write_json(report_path, report)
        raise ExportValidationError(report_path, report)

    index = _read_json(source_dir / "retrieval_index.json")
    creator = str(index.get("creator") or source_dir.name)
    platform = str(index.get("platform") or "unknown")
    video_cards = _visible_files(source_dir / "videos", "*.card.json")
    video_notes = _visible_files(source_dir / "videos", "*.notes.md")

    export_dir = source_dir / f"videoautomation_export_{timestamp}"
    export_dir.mkdir(parents=True, exist_ok=False)
    (export_dir / "videos").mkdir()
    (export_dir / "raw").mkdir()

    for name in REQUIRED_TOP_LEVEL_FILES:
        shutil.copyfile(source_dir / name, export_dir / name)
    for path in video_cards + video_notes:
        shutil.copyfile(path, export_dir / "videos" / path.name)

    timeline_paths = _timeline_paths_from_cards(source_dir, video_cards)
    if timeline_paths:
        evidence_target = export_dir / "evidence"
        evidence_target.mkdir()
        for path in timeline_paths:
            # Read first so legacy time lines are exported with explicit window semantics.
            _write_json(evidence_target / path.name, read_visual_timeline(path))

    legacy_source = source_dir / "legacy"
    if legacy_source.exists():
        legacy_files = _visible_files(legacy_source, "*.md")
        if legacy_files:
            legacy_target = export_dir / "legacy"
            legacy_target.mkdir()
            for path in legacy_files:
                shutil.copyfile(path, legacy_target / path.name)

    raw_refs = _sanitize_refs(_read_json(source_dir / "raw" / "refs.json"))
    _write_json(export_dir / "raw" / "refs.json", raw_refs)

    manifest = {
        "productName": EXPORT_PRODUCT_NAME,
        "creator": creator,
        "platform": platform,
        "videoCount": len(video_cards),
        "generatedAt": utc_now(),
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "evidenceTimelineCount": len(timeline_paths),
    }
    _write_json(export_dir / "manifest.json", manifest)
    _write_json(export_dir / "validation_report.json", report)
    _remove_appledouble_files(export_dir)

    return {
        "ok": True,
        "path": str(export_dir),
        "manifest": manifest,
        "validation": report,
        "file_count": sum(1 for path in export_dir.rglob("*") if path.is_file()),
    }


def validate_videoautomation_export(source_dir: Path, requested_video_count: int | None = None) -> dict[str, Any]:
    blocking: list[str] = []
    warnings: list[str] = []
    failed_dimensions: list[dict[str, str]] = []
    invalid_video_ids: set[str] = set()
    absolute_path_count = 0
    placeholder_count = 0

    missing = _missing_required_files(source_dir)
    if missing:
        blocking.append("缺少必需产物：" + "、".join(missing))

    cards: dict[str, dict[str, Any]] = {}
    notes: dict[str, str] = {}
    index: dict[str, Any] = {}
    if not missing:
        try:
            index = _read_json(source_dir / "retrieval_index.json")
        except ValueError as exc:
            blocking.append(str(exc))
        for path in _visible_files(source_dir / "videos", "*.card.json"):
            try:
                card = _read_json(path)
            except ValueError as exc:
                blocking.append(str(exc))
                continue
            video_id = str(card.get("video_id") or "")
            if not video_id:
                blocking.append(f"{path.name} 缺少 video_id")
                continue
            if video_id in cards:
                blocking.append(f"重复 Video Card：{video_id}")
            cards[video_id] = card
        notes = {
            path.name.removesuffix(".notes.md"): path.read_text(encoding="utf-8", errors="ignore")
            for path in _visible_files(source_dir / "videos", "*.notes.md")
        }

    total = requested_video_count if requested_video_count and requested_video_count > 0 else len(cards)
    coverage: dict[str, dict[str, float | int]] = {}
    coverage_fields = (*REQUIRED_CARD_TEXT_FIELDS, *REQUIRED_CARD_LIST_FIELDS, "platform_fit", "published_at", "duration_seconds")
    for field in coverage_fields:
        present = sum(1 for card in cards.values() if _field_present(card.get(field)))
        coverage[field] = {"present": present, "total": len(cards), "ratio": round(present / len(cards), 4) if cards else 0.0}

    expected_ids = set(cards)
    if total != len(cards):
        blocking.append(f"请求 {total} 个视频，但只有 {len(cards)} 张 Video Card")
    if set(notes) != expected_ids:
        blocking.append(f"Card/Notes ID 不一致：Card {len(cards)}，Notes {len(notes)}")

    index_ids = {
        str(item.get("video_id") or "")
        for item in index.get("cards", [])
        if isinstance(item, dict) and item.get("video_id")
    }
    if index_ids != expected_ids:
        blocking.append(f"Retrieval Index 与 Card ID 不一致：Index {len(index_ids)}，Card {len(cards)}")

    for video_id, card in cards.items():
        card_issues: list[str] = []
        for field in REQUIRED_CARD_TEXT_FIELDS:
            value = str(card.get(field) or "").strip()
            if not value or value.lower() == "unknown":
                card_issues.append(f"缺少 {field}")
                placeholder_count += 1
        for field in REQUIRED_CARD_LIST_FIELDS:
            if not _field_present(card.get(field)):
                card_issues.append(f"缺少 {field}")
                placeholder_count += 1
        platform_fit = card.get("platform_fit")
        if not isinstance(platform_fit, dict) or all(str(value).strip().lower() in {"", "unknown"} for value in platform_fit.values()):
            card_issues.append("缺少 platform_fit")
            placeholder_count += 1
        evidence_refs = card.get("evidence_refs") if isinstance(card.get("evidence_refs"), list) else []
        if not evidence_refs or any(not str(value).startswith(f"video:{video_id}:") for value in evidence_refs):
            card_issues.append("evidence_refs 不是稳定证据 ID")
        serialized = json.dumps(card, ensure_ascii=False)
        absolute_path_count += len(ABSOLUTE_PATH_PATTERN.findall(serialized))
        if _failure_marker(serialized):
            card_issues.append("包含模型拒答或错误响应")
        if card.get("published_at") in (None, ""):
            warnings.append(f"{video_id} 缺少 published_at")
        if card.get("duration_seconds") in (None, ""):
            warnings.append(f"{video_id} 缺少 duration_seconds")

        timeline_ref = str(card.get("visual_timeline_ref") or "").strip()
        if timeline_ref:
            timeline_path = _safe_relative_source_path(source_dir, timeline_ref)
            if timeline_path is None or not timeline_path.is_file():
                card_issues.append("visual_timeline_ref 不存在或不是相对路径")
            else:
                try:
                    timeline = read_visual_timeline(timeline_path)
                    validate_visual_timeline(timeline, require_visual_observations=True)
                    timeline_ids = {
                        str(shot.get("evidence_id") or "")
                        for shot in timeline.get("shots", [])
                        if isinstance(shot, dict)
                    }
                    if not timeline_ids.issubset(set(evidence_refs)):
                        card_issues.append("visual timeline 的 evidence_id 未写入 Card")
                    coverage = card.get("evidence_coverage")
                    if not isinstance(coverage, dict) or coverage.get("observation_coverage") != "complete":
                        card_issues.append("evidence_coverage 未标记视觉证据完成")
                    quality = timeline.get("quality") if isinstance(timeline.get("quality"), dict) else {}
                    if not bool(quality.get("eligible_for_precise_timing")):
                        warnings.append(f"{video_id} 视觉证据时间线不适用于精确时序，仅可用于宏观视觉组织与风格学习")
                except ValueError as exc:
                    card_issues.append(f"visual_timeline 无效：{exc}")

        note = notes.get(video_id, "").strip()
        if len(note) < 180 or not all(heading in note for heading in ("脚本", "视觉", "运营", "证据")):
            card_issues.append("Notes 不能独立表达脚本、视觉、运营和证据方法")
        if _failure_marker(note):
            card_issues.append("Notes 包含模型拒答或错误响应")
        absolute_path_count += len(ABSOLUTE_PATH_PATTERN.findall(note))
        placeholder_count += _placeholder_hits(note)
        if card_issues:
            invalid_video_ids.add(video_id)
            blocking.append(f"{video_id}：" + "、".join(card_issues))

        for dimension in DIMENSION_NAMES:
            path = source_dir / "单视频分析" / f"{video_id}_{dimension}.md"
            if not path.is_file():
                failed_dimensions.append({"video_id": video_id, "dimension": dimension, "reason": "missing"})
                invalid_video_ids.add(video_id)
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            marker = _failure_marker(text)
            if marker:
                failed_dimensions.append({"video_id": video_id, "dimension": dimension, "reason": marker})
                invalid_video_ids.add(video_id)

    if failed_dimensions:
        blocking.append(f"{len(failed_dimensions)} 个单视频维度缺失或包含模型拒答")

    creator_ids = set(cards)
    for name in ("creator_profile.md", "pattern_library.md", "qa_checklist.md", "retrieval_pack.md"):
        path = source_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        absolute_path_count += len(ABSOLUTE_PATH_PATTERN.findall(text))
        hits = _placeholder_hits(text)
        placeholder_count += hits
        if len(text.strip()) < 240:
            blocking.append(f"{name} 内容过短")
        if hits:
            blocking.append(f"{name} 含 {hits} 处占位内容")
        if _failure_marker(text):
            blocking.append(f"{name} 包含模型拒答或错误响应")
        if name != "qa_checklist.md" and creator_ids and not any(video_id in text for video_id in creator_ids):
            blocking.append(f"{name} 未绑定代表 video_id")

    pack_path = source_dir / "retrieval_pack.md"
    if pack_path.is_file():
        pack_ids = set(re.findall(r"videos/([^/\s]+)\.card\.json", pack_path.read_text(encoding="utf-8", errors="ignore")))
        minimum = min(3, len(cards))
        if len(pack_ids) < minimum or len(pack_ids) > min(8, len(cards)) or not pack_ids.issubset(expected_ids):
            blocking.append(f"retrieval_pack 代表 Card 必须为 {minimum}-{min(8, len(cards))} 个有效 ID")

    try:
        sanitized_refs = _sanitize_refs(_read_json(source_dir / "raw" / "refs.json"))
        absolute_path_count += len(ABSOLUTE_PATH_PATTERN.findall(json.dumps(sanitized_refs, ensure_ascii=False)))
    except ValueError as exc:
        blocking.append(str(exc))

    if absolute_path_count:
        blocking.append(f"导出内容仍含 {absolute_path_count} 处本机绝对路径")

    blocking = list(dict.fromkeys(blocking))
    warnings = list(dict.fromkeys(warnings))
    valid_count = max(0, len(cards) - len(invalid_video_ids))
    return {
        "schemaVersion": VALIDATION_SCHEMA_VERSION,
        "status": "failed" if blocking else "passed",
        "requestedVideoCount": total,
        "validVideoCount": valid_count,
        "failedVideoCount": max(total - valid_count, len(invalid_video_ids)),
        "fieldCoverage": coverage,
        "failedDimensions": failed_dimensions,
        "absolutePathCount": absolute_path_count,
        "placeholderCount": placeholder_count,
        "blockingIssues": blocking,
        "warnings": warnings,
    }


def _resolve_source_dir(output_dir: Path, fallback_roots: tuple[Path, ...]) -> Path:
    source_dir = output_dir.expanduser().resolve()
    if source_dir.is_dir():
        return source_dir
    for root in fallback_roots:
        candidate = root.expanduser().resolve() / source_dir.name
        if candidate.is_dir():
            return candidate
    return source_dir


def _visible_files(directory: Path, pattern: str) -> list[Path]:
    return sorted(path for path in directory.glob(pattern) if path.is_file() and not path.name.startswith("."))


def _timeline_paths_from_cards(source_dir: Path, cards: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for card_path in cards:
        try:
            card = _read_json(card_path)
        except ValueError:
            continue
        ref = str(card.get("visual_timeline_ref") or "").strip()
        if not ref:
            continue
        path = _safe_relative_source_path(source_dir, ref)
        if path and path.is_file() and path.suffix == ".json":
            paths.append(path)
    return sorted(set(paths))


def _safe_relative_source_path(source_dir: Path, reference: str) -> Path | None:
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    path = (source_dir / candidate).resolve()
    try:
        path.relative_to(source_dir.resolve())
    except ValueError:
        return None
    return path


def _remove_appledouble_files(directory: Path) -> None:
    for path in directory.rglob("._*"):
        if path.is_file():
            path.unlink()


def _missing_required_files(source_dir: Path) -> list[str]:
    expected = [*REQUIRED_TOP_LEVEL_FILES, "raw/refs.json"]
    return [name for name in expected if not (source_dir / name).is_file()]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return data


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _field_present(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and any(str(item).strip().lower() not in {"", "unknown"} for item in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value)
    return value not in (None, "") and str(value).strip().lower() != "unknown"


def _failure_marker(text: str) -> str | None:
    lowered = str(text or "").lower()
    return next((marker for marker in MODEL_FAILURE_MARKERS if marker in lowered), None)


def _placeholder_hits(text: str) -> int:
    lowered = str(text or "").lower()
    return sum(lowered.count(marker) for marker in PLACEHOLDER_MARKERS)


def _sanitize_refs(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
                cleaned[key] = "[已移除：敏感字段]"
            elif normalized_key in OMITTED_PATH_KEYS or normalized_key.endswith("_path"):
                cleaned[key] = "[已省略：导出包不携带原始文件路径]"
            elif normalized_key == "paths" and isinstance(item, dict):
                cleaned[key] = {name: "[已省略：导出包不携带单视频分析路径]" for name in item}
            else:
                cleaned[key] = _sanitize_refs(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_refs(item) for item in value]
    return value
