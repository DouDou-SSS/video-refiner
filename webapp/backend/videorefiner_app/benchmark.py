from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .metadata import extract_duration_seconds, extract_published_at
from .evidence import (
    read_visual_timeline,
    timeline_evidence_summary,
    timeline_prompt_summary,
    validate_visual_timeline,
    visual_timeline_path,
)
from .utils import local_timestamp


BENCHMARK_PROMPT = "Benchmark Intelligence 汇总.md"
RETRIEVAL_PACK_MAX_CHARS = 12000
BENCHMARK_BATCH_SIZE = 3
CREATOR_MARKDOWN_OUTPUTS = {
    "creator_profile_md": {
        "title": "creator_profile.md",
        "goal": "提炼该博主的账号定位、稳定受众、内容气质、可信证据使用方式和可复用边界。",
        "requirements": "必须引用至少 3 个真实 video_id；围绕账号级规律写，不要逐条复述视频。",
    },
    "pattern_library_md": {
        "title": "pattern_library.md",
        "goal": "沉淀可复用的脚本、视觉、剪辑、选题、运营模式库。",
        "requirements": "每个 pattern 尽量绑定 creator、video_id、evidence、risk；禁止直接改写原文。",
    },
    "qa_checklist_md": {
        "title": "qa_checklist.md",
        "goal": "生成给 VideoAutomation 使用的起号基底核验清单。",
        "requirements": "按脚本、视觉、剪辑、选题、运营、事实核验、风险边界组织；每项要可执行。",
    },
    "retrieval_pack_md": {
        "title": "retrieval_pack.md",
        "goal": "生成轻量检索包，帮助 VideoAutomation 优先读取代表样本。",
        "requirements": "必须列出 3-8 个代表样本，路径格式必须是 videos/{video_id}.card.json；不要包含完整 raw transcript。",
    },
}

MODEL_FAILURE_MARKERS = (
    "the request was rejected because it was considered high risk",
    "request was rejected",
    "content policy",
    "无法协助完成该请求",
    "不能协助完成该请求",
    "分析返回为空",
)

PLACEHOLDER_MARKERS = (
    "待结合单视频分析继续精炼",
    "待精炼钩子模式",
    "该 notes 由 benchmark intelligence 阶段生成",
    "优先查看每张视频卡片",
)

REQUIRED_CARD_TEXT_FIELDS = ("topic", "hook_type", "structure_type", "editing_density", "visual_density")
REQUIRED_CARD_LIST_FIELDS = (
    "structure",
    "emotion_curve",
    "script_patterns",
    "visual_patterns",
    "editing_patterns",
    "operation_patterns",
    "risk_notes",
    "tags",
)

CARD_FIELDS = [
    "video_id",
    "platform",
    "creator",
    "source_url",
    "topic",
    "published_at",
    "duration_seconds",
    "hook_type",
    "structure",
    "emotion_curve",
    "script_patterns",
    "visual_patterns",
    "editing_patterns",
    "operation_patterns",
    "best_quotes",
    "risk_notes",
    "evidence_refs",
    "tags",
]


@dataclass
class VideoMaterial:
    video_id: str
    title: str
    platform: str
    source_url: str
    published_at: str | None
    duration_seconds: float | None
    transcript_path: Path
    raw_transcript_path: Path
    video_path: Path
    kept_video_path: Path
    analysis_paths: dict[str, Path]
    transcript_excerpt: str
    analysis_excerpts: dict[str, str]
    visual_timeline_path: Path | None = None
    visual_timeline_ref: str = ""
    visual_timeline_excerpt: str = ""
    visual_evidence_refs: list[str] = field(default_factory=list)
    evidence_coverage: dict[str, Any] = field(default_factory=dict)
    evidence_required: bool = False


def infer_creator(output_dir: Path, config_snapshot: dict[str, Any]) -> str:
    return str(config_snapshot.get("blogger_name") or output_dir.name or "未知博主")


def infer_platform(rows: list[dict[str, Any]]) -> str:
    platforms = {str(row.get("platform") or "").strip() for row in rows if row.get("platform")}
    platforms.discard("")
    if len(platforms) == 1:
        return next(iter(platforms))
    if len(platforms) > 1:
        return "mixed"
    return "unknown"


def collect_video_materials(
    rows: list[dict[str, Any]],
    single_dir: Path,
    transcript_dir: Path,
    tmp_dir: Path,
    keep_dir: Path,
    dimensions: list[dict[str, str]],
    max_excerpt_chars: int,
    evidence_dir: Path | None = None,
    require_visual_evidence: bool = False,
) -> list[VideoMaterial]:
    materials: list[VideoMaterial] = []
    for row in rows:
        video_id = str(row["video_id"])
        source_meta = _source_meta(row)
        transcript_path = transcript_dir / f"video_{video_id}.md"
        raw_transcript_path = tmp_dir / f"{video_id}_transcript.txt"
        analysis_paths = {dim["name"]: single_dir / f"{video_id}_{dim['name']}.md" for dim in dimensions}
        timeline_path = visual_timeline_path(evidence_dir, video_id) if evidence_dir else None
        timeline: dict[str, Any] = {}
        if timeline_path and timeline_path.exists():
            timeline = read_visual_timeline(timeline_path)
            validate_visual_timeline(timeline, require_visual_observations=True)
        elif require_visual_evidence:
            raise ValueError(f"{video_id} 缺少通过校验的视觉证据时间线")
        visual_shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
        visual_refs = [str(shot.get("evidence_id")) for shot in visual_shots if shot.get("evidence_id")]
        coverage = timeline.get("quality") if isinstance(timeline.get("quality"), dict) else {}
        evidence_summary = timeline_evidence_summary(timeline) if timeline else {}
        materials.append(
            VideoMaterial(
                video_id=video_id,
                title=str(row.get("title") or video_id),
                platform=str(row.get("platform") or "unknown"),
                source_url=str(row.get("url") or ""),
                published_at=extract_published_at(row.get("published_at"), source_meta),
                duration_seconds=extract_duration_seconds(row.get("duration"), source_meta),
                transcript_path=transcript_path,
                raw_transcript_path=raw_transcript_path,
                video_path=tmp_dir / f"{video_id}.mp4",
                kept_video_path=keep_dir / f"{video_id}.mp4",
                analysis_paths=analysis_paths,
                transcript_excerpt=_read_excerpt(transcript_path, max_excerpt_chars),
                analysis_excerpts={name: _read_excerpt(path, max_excerpt_chars) for name, path in analysis_paths.items() if path.exists()},
                visual_timeline_path=timeline_path if timeline else None,
                visual_timeline_ref=f"evidence/{video_id}.visual_timeline.json" if timeline else "",
                visual_timeline_excerpt=timeline_prompt_summary(timeline) if timeline else "",
                visual_evidence_refs=visual_refs,
                evidence_coverage={
                    "shot_count": int(coverage.get("shot_count") or 0),
                    "detected_cut_segment_count": int(evidence_summary.get("detectedCutSegmentCount") or 0),
                    "transcript_alignment": str(coverage.get("transcript_alignment") or "unavailable"),
                    "visual_observations": str(coverage.get("visual_observations") or "unavailable"),
                    "observation_coverage": str(evidence_summary.get("observationCoverage") or "partial"),
                    "visual_confidence_summary": evidence_summary.get("confidence") or {"high": 0, "medium": 0, "low": 0},
                    "alignment_status": str(evidence_summary.get("alignmentStatus") or "coarse"),
                    "eligible_for_precise_timing": bool(evidence_summary.get("eligibleForPreciseTiming")),
                },
                evidence_required=require_visual_evidence,
            )
        )
    return materials


def build_video_batch_prompt(
    prompt_template: str,
    creator: str,
    platform: str,
    materials: list[VideoMaterial],
) -> str:
    video_text = []
    for index, material in enumerate(materials, start=1):
        analyses = "\n\n".join(
            f"### {dimension}\n{text}" for dimension, text in material.analysis_excerpts.items() if text.strip()
        )
        video_text.append(
            "\n".join(
                [
                    f"## 视频 {index}: {material.title}",
                    f"- video_id: {material.video_id}",
                    f"- platform: {material.platform}",
                    f"- source_url: {material.source_url}",
                    f"- published_at: {material.published_at}",
                    f"- duration_seconds: {material.duration_seconds}",
                    "",
                    "### 文案摘录",
                    material.transcript_excerpt or "无",
                    "",
                    "### 已验证视觉证据时间线",
                    material.visual_timeline_excerpt or "无",
                    "",
                    analyses or "### 五维分析\n无",
                ]
            )
        )
    return "\n\n".join(
        [
            prompt_template,
            "# 本次输出范围：video_batch",
            "只返回 video_cards 一个字段。必须覆盖本批全部 video_id，不得返回占位值或 unknown。",
            "不要返回 video_notes，不要把 Markdown 放进 JSON。Card 的方法字段必须从对应五维分析提炼。",
            f"- creator: {creator}\n- platform: {platform}\n- batch_video_count: {len(materials)}",
            "# 本批视频资料",
            "\n\n---\n\n".join(video_text),
        ]
    )


def build_video_note_prompt(
    prompt_template: str,
    creator: str,
    platform: str,
    material: VideoMaterial,
    card: dict[str, Any],
) -> str:
    analyses = "\n\n".join(
        f"### {dimension}\n{text}" for dimension, text in material.analysis_excerpts.items() if text.strip()
    )
    return "\n\n".join(
        [
            prompt_template,
            "# 本次输出范围：video_note_markdown",
            f"- creator: {creator}",
            f"- platform: {platform}",
            f"- video_id: {material.video_id}",
            f"- title: {material.title}",
            "",
            "只输出这一条视频的 Markdown Notes 正文，不要 JSON，不要代码块，不要解释。",
            "必须包含 `## 核心方法`、`## 脚本与叙事`、`## 视觉与剪辑`、`## 运营与风险`、`## 证据`。",
            "Notes 必须让人脱离原始文件也能读懂；不要写本机路径；证据只写稳定 evidence_id。",
            "# 已验证 Card",
            json.dumps(card, ensure_ascii=False, indent=2),
            "# 文案摘录",
            material.transcript_excerpt or "无",
            "# 已验证视觉证据时间线",
            material.visual_timeline_excerpt or "无",
            "# 五维分析摘录",
            analyses or "无",
        ]
    )


def build_creator_markdown_prompt(
    prompt_template: str,
    creator: str,
    platform: str,
    cards: list[dict[str, Any]],
    legacy_outputs: dict[str, Path],
    max_legacy_chars: int,
    output_key: str,
) -> str:
    spec = CREATOR_MARKDOWN_OUTPUTS[output_key]
    legacy_text = []
    for name, path in legacy_outputs.items():
        if path.exists():
            legacy_text.append(f"## 旧版{name}\n{_read_excerpt(path, max_legacy_chars)}")
    return "\n\n".join(
        [
            prompt_template,
            "# 本次输出范围：creator_markdown",
            f"- output_key: {output_key}",
            f"- output_file: {spec['title']}",
            f"- creator: {creator}",
            f"- platform: {platform}",
            f"- video_count: {len(cards)}",
            "",
            "只输出 Markdown 正文，不要 JSON，不要代码块，不要解释。",
            spec["goal"],
            spec["requirements"],
            "必须是方法论、模式、证据引用和风险边界；不得洗稿、不得改写博主原文。",
            "# 已验证 Video Cards 摘要",
            json.dumps(_compact_cards(cards), ensure_ascii=False, indent=2),
            "# 旧版五维账号级汇总摘录",
            "\n\n".join(legacy_text) or "无",
        ]
    )


def parse_benchmark_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("模型未返回 JSON 对象")
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        end = text.rfind("}")
        if end <= start:
            raise ValueError(f"JSON 解析失败：{exc}") from exc
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型返回的 JSON 顶层不是对象")
    return data


def normalize_benchmark_data(
    data: dict[str, Any],
    creator: str,
    platform: str,
    materials: list[VideoMaterial],
) -> dict[str, Any]:
    video_data = normalize_video_batch_data(data, creator, platform, materials)
    normalized = {
        "creator_profile_md": _required_text(data, "creator_profile_md"),
        "pattern_library_md": _required_text(data, "pattern_library_md"),
        "qa_checklist_md": _required_text(data, "qa_checklist_md"),
        "retrieval_pack_md": _required_text(data, "retrieval_pack_md"),
        **video_data,
    }
    validate_benchmark_data(normalized, materials)
    return normalized


def normalize_creator_summary_data(data: dict[str, Any]) -> dict[str, str]:
    return {
        "creator_profile_md": _required_text(data, "creator_profile_md"),
        "pattern_library_md": _required_text(data, "pattern_library_md"),
        "qa_checklist_md": _required_text(data, "qa_checklist_md"),
        "retrieval_pack_md": _required_text(data, "retrieval_pack_md"),
    }


def normalize_video_batch_data(
    data: dict[str, Any],
    creator: str,
    platform: str,
    materials: list[VideoMaterial],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {"video_cards": [], "video_notes": {}}

    input_cards = data.get("video_cards") if isinstance(data.get("video_cards"), list) else []
    cards_by_id = {str(card.get("video_id")): card for card in input_cards if isinstance(card, dict) and card.get("video_id")}

    input_notes = data.get("video_notes")
    if isinstance(input_notes, dict):
        notes_by_id = {str(key): str(value) for key, value in input_notes.items()}
    elif isinstance(input_notes, list):
        notes_by_id = {
            str(item.get("video_id")): str(item.get("markdown") or item.get("notes") or "")
            for item in input_notes
            if isinstance(item, dict) and item.get("video_id")
        }
    else:
        notes_by_id = {}

    for material in materials:
        if material.video_id not in cards_by_id:
            raise ValueError(f"模型结果缺少 video card：{material.video_id}")
        if not notes_by_id.get(material.video_id, "").strip():
            raise ValueError(f"模型结果缺少 video notes：{material.video_id}")
        normalized["video_cards"].append(_normalize_card(cards_by_id[material.video_id], creator, platform, material))
        normalized["video_notes"][material.video_id] = notes_by_id[material.video_id].strip()
    validate_video_batch_data(normalized, materials)
    return normalized


def normalize_video_cards_data(
    data: dict[str, Any],
    creator: str,
    platform: str,
    materials: list[VideoMaterial],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {"video_cards": []}

    input_cards = data.get("video_cards") if isinstance(data.get("video_cards"), list) else []
    cards_by_id = {str(card.get("video_id")): card for card in input_cards if isinstance(card, dict) and card.get("video_id")}

    for material in materials:
        if material.video_id not in cards_by_id:
            raise ValueError(f"模型结果缺少 video card：{material.video_id}")
        normalized["video_cards"].append(_normalize_card(cards_by_id[material.video_id], creator, platform, material))
    validate_video_cards_data(normalized, materials)
    return normalized


def validate_video_cards_data(data: dict[str, Any], materials: list[VideoMaterial]) -> None:
    material_ids = {item.video_id for item in materials}
    cards = data.get("video_cards") if isinstance(data.get("video_cards"), list) else []
    card_ids = {str(card.get("video_id")) for card in cards if isinstance(card, dict)}
    issues: list[str] = []
    if card_ids != material_ids:
        issues.append(f"Card ID 不一致：期望 {sorted(material_ids)}，实际 {sorted(card_ids)}")
    for card in cards:
        video_id = str(card.get("video_id") or "unknown")
        for field in REQUIRED_CARD_TEXT_FIELDS:
            value = str(card.get(field) or "").strip()
            if not value or value.lower() == "unknown":
                issues.append(f"{video_id} 缺少 {field}")
        for field in REQUIRED_CARD_LIST_FIELDS:
            if not _as_list(card.get(field)):
                issues.append(f"{video_id} 缺少 {field}")
        platform_fit = card.get("platform_fit")
        if not isinstance(platform_fit, dict) or all(str(value).strip().lower() in {"", "unknown"} for value in platform_fit.values()):
            issues.append(f"{video_id} 缺少 platform_fit")
        failure = model_output_failure(str(card))
        if failure:
            issues.append(f"{video_id} Card 包含模型失败响应：{failure}")
        material = next((item for item in materials if item.video_id == video_id), None)
        if material and material.evidence_required:
            if card.get("visual_timeline_ref") != material.visual_timeline_ref:
                issues.append(f"{video_id} Card 缺少视觉时间线引用")
            coverage = card.get("evidence_coverage")
            if not isinstance(coverage, dict) or coverage.get("observation_coverage") != "complete":
                issues.append(f"{video_id} Card 视觉证据覆盖未完成")
            if not set(material.visual_evidence_refs).issubset(set(_as_list(card.get("evidence_refs")))):
                issues.append(f"{video_id} Card 缺少镜头 evidence_id")
    if issues:
        raise ValueError("；".join(issues[:12]))


def validate_video_batch_data(data: dict[str, Any], materials: list[VideoMaterial]) -> None:
    validate_video_cards_data(data, materials)
    cards = data.get("video_cards") if isinstance(data.get("video_cards"), list) else []
    issues: list[str] = []
    for card in cards:
        video_id = str(card.get("video_id") or "unknown")
        notes = str((data.get("video_notes") or {}).get(video_id) or "").strip()
        if len(notes) < 180:
            issues.append(f"{video_id} Notes 过短，不能独立表达方法")
        if not all(heading in notes for heading in ("脚本", "视觉", "运营", "证据")):
            issues.append(f"{video_id} Notes 缺少脚本/视觉/运营/证据方法段落")
        failure = model_output_failure(notes)
        if failure:
            issues.append(f"{video_id} Notes 包含模型失败响应：{failure}")
        if _contains_absolute_path(notes):
            issues.append(f"{video_id} Notes 含绝对路径")
    if issues:
        raise ValueError("；".join(issues[:12]))


def validate_benchmark_data(data: dict[str, Any], materials: list[VideoMaterial]) -> None:
    validate_video_batch_data(data, materials)
    video_ids = [item.video_id for item in materials]
    issues: list[str] = []
    for key in ("creator_profile_md", "pattern_library_md", "qa_checklist_md", "retrieval_pack_md"):
        text = str(data.get(key) or "").strip()
        if len(text) < 240:
            issues.append(f"{key} 内容过短")
        if model_output_failure(text):
            issues.append(f"{key} 包含模型失败响应")
        if any(marker in text.lower() for marker in PLACEHOLDER_MARKERS):
            issues.append(f"{key} 包含占位内容")
        if _contains_absolute_path(text):
            issues.append(f"{key} 含绝对路径")
    for key in ("creator_profile_md", "pattern_library_md", "retrieval_pack_md"):
        if video_ids and not any(video_id in str(data.get(key) or "") for video_id in video_ids):
            issues.append(f"{key} 缺少代表 video_id")
    pack = str(data.get("retrieval_pack_md") or "")
    pack_ids = set(re.findall(r"videos/([^/\s]+)\.card\.json", pack))
    expected_min = min(3, len(video_ids))
    if len(pack_ids) < expected_min or len(pack_ids) > min(8, len(video_ids)):
        issues.append(f"retrieval_pack 代表 Card 数量应为 {expected_min}-{min(8, len(video_ids))}")
    if issues:
        raise ValueError("；".join(issues[:12]))


def model_output_failure(text: str) -> str | None:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return "模型返回为空"
    for marker in MODEL_FAILURE_MARKERS:
        if marker in lowered:
            return marker
    return None


def write_benchmark_outputs(
    output_dir: Path,
    creator: str,
    platform: str,
    data: dict[str, Any],
    materials: list[VideoMaterial],
    legacy_outputs: dict[str, Path],
) -> list[dict[str, Any]]:
    videos_dir = output_dir / "videos"
    raw_dir = output_dir / "raw"
    legacy_dir = output_dir / "legacy"
    videos_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    legacy_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    top_level = [
        ("benchmark_profile", output_dir / "creator_profile.md", data["creator_profile_md"]),
        ("benchmark_pattern_library", output_dir / "pattern_library.md", data["pattern_library_md"]),
        ("benchmark_qa_checklist", output_dir / "qa_checklist.md", data["qa_checklist_md"]),
        ("retrieval_pack", output_dir / "retrieval_pack.md", _sanitize_retrieval_pack(data["retrieval_pack_md"])),
    ]
    for kind, path, text in top_level:
        path.write_text(str(text).strip() + "\n", encoding="utf-8")
        artifacts.append({"kind": kind, "path": path, "meta": {"creator": creator, "platform": platform}})

    cards = data["video_cards"]
    notes = data["video_notes"]
    index_cards = []
    for card in cards:
        video_id = str(card["video_id"])
        card_path = videos_dir / f"{video_id}.card.json"
        notes_path = videos_dir / f"{video_id}.notes.md"
        card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        notes_path.write_text(str(notes.get(video_id) or "").strip() + "\n", encoding="utf-8")
        artifacts.append({"kind": "video_card", "path": card_path, "meta": {"video_id": video_id}})
        artifacts.append({"kind": "video_notes", "path": notes_path, "meta": {"video_id": video_id}})
        index_cards.append(_index_card(card, card_path, notes_path, output_dir))

    index = {
        "creator": creator,
        "platform": platform,
        "updated_at": local_timestamp(),
        "cards": index_cards,
    }
    index_path = output_dir / "retrieval_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append({"kind": "retrieval_index", "path": index_path, "meta": {"creator": creator, "platform": platform}})

    refs_path = raw_dir / "refs.json"
    refs_path.write_text(json.dumps(_raw_refs(materials, legacy_outputs, output_dir), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append({"kind": "raw_refs", "path": refs_path, "meta": {"policy": "references_only"}})

    for name, path in legacy_outputs.items():
        if not path.exists():
            continue
        legacy_path = legacy_dir / path.name
        shutil.copy2(path, legacy_path)
        artifacts.append({"kind": "legacy_output", "path": legacy_path, "meta": {"source_path": str(path), "name": name}})

    return artifacts


def remove_benchmark_outputs(output_dir: Path) -> None:
    for name in [
        "creator_profile.md",
        "pattern_library.md",
        "qa_checklist.md",
        "retrieval_index.json",
        "retrieval_pack.md",
    ]:
        path = output_dir / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for directory in [output_dir / "videos", output_dir / "raw", output_dir / "legacy"]:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass


def _normalize_card(card: dict[str, Any], creator: str, platform: str, material: VideoMaterial) -> dict[str, Any]:
    normalized = _default_card(material, creator, platform)
    for field in CARD_FIELDS:
        if field in card and card[field] not in (None, ""):
            normalized[field] = card[field]
    normalized["video_id"] = material.video_id
    normalized["platform"] = str(normalized.get("platform") or material.platform or platform or "unknown")
    normalized["creator"] = str(normalized.get("creator") or creator)
    normalized["source_url"] = str(normalized.get("source_url") or material.source_url)
    normalized["published_at"] = normalized.get("published_at") or material.published_at
    normalized["duration_seconds"] = _optional_float(normalized.get("duration_seconds"))
    for field in [
        "structure",
        "emotion_curve",
        "script_patterns",
        "visual_patterns",
        "editing_patterns",
        "operation_patterns",
        "best_quotes",
        "risk_notes",
        "evidence_refs",
        "tags",
    ]:
        normalized[field] = _as_list(normalized.get(field))
    normalized["best_quotes"] = [_truncate(item, 120) for item in normalized["best_quotes"][:6]]
    normalized["title"] = str(card.get("title") or material.title)
    normalized["structure_type"] = str(card.get("structure_type") or _structure_type(normalized["structure"]))
    normalized["editing_density"] = str(card.get("editing_density") or "")
    normalized["visual_density"] = str(card.get("visual_density") or "")
    normalized["platform_fit"] = _platform_fit(card.get("platform_fit"))
    normalized["evidence_refs"] = _evidence_ids(material)
    normalized["visual_timeline_ref"] = material.visual_timeline_ref
    normalized["evidence_coverage"] = material.evidence_coverage
    return normalized


def _default_card(material: VideoMaterial, creator: str, platform: str) -> dict[str, Any]:
    return {
        "video_id": material.video_id,
        "platform": material.platform or platform or "unknown",
        "creator": creator,
        "source_url": material.source_url,
        "topic": material.title,
        "published_at": material.published_at,
        "duration_seconds": material.duration_seconds,
        "hook_type": "",
        "structure": [],
        "emotion_curve": [],
        "script_patterns": [],
        "visual_patterns": [],
        "editing_patterns": [],
        "operation_patterns": [],
        "best_quotes": [],
        "risk_notes": ["待人工或后续模型复核，避免直接洗稿和平台误判。"],
        "evidence_refs": _evidence_ids(material),
        "visual_timeline_ref": material.visual_timeline_ref,
        "evidence_coverage": material.evidence_coverage,
        "tags": [],
    }


def _index_card(card: dict[str, Any], card_path: Path, notes_path: Path, output_dir: Path) -> dict[str, Any]:
    coverage = card.get("evidence_coverage") if isinstance(card.get("evidence_coverage"), dict) else {}
    return {
        "video_id": card["video_id"],
        "card_path": _relative_posix(card_path, output_dir),
        "notes_path": _relative_posix(notes_path, output_dir),
        "tags": _as_list(card.get("tags")),
        "topic": str(card.get("topic") or ""),
        "published_at": card.get("published_at"),
        "duration_seconds": _optional_float(card.get("duration_seconds")),
        "hook_type": str(card.get("hook_type") or ""),
        "structure_type": str(card.get("structure_type") or ""),
        "emotion_curve": _as_list(card.get("emotion_curve")),
        "editing_density": str(card.get("editing_density") or "unknown"),
        "visual_density": str(card.get("visual_density") or "unknown"),
        "platform_fit": _platform_fit(card.get("platform_fit")),
        "visual_timeline_ref": str(card.get("visual_timeline_ref") or ""),
        "evidence_coverage": coverage,
        "evidence_summary": {
            "segmentCount": int(coverage.get("shot_count") or 0),
            "detectedCutSegmentCount": int(coverage.get("detected_cut_segment_count") or 0),
            "observationCoverage": str(coverage.get("observation_coverage") or "partial"),
            "alignmentStatus": str(coverage.get("alignment_status") or coverage.get("transcript_alignment") or "coarse"),
            "eligibleForPreciseTiming": bool(coverage.get("eligible_for_precise_timing")),
            "confidence": coverage.get("visual_confidence_summary")
            if isinstance(coverage.get("visual_confidence_summary"), dict)
            else {"high": 0, "medium": 0, "low": 0},
        },
    }


def _raw_refs(materials: list[VideoMaterial], legacy_outputs: dict[str, Path], output_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "policy": "references_only",
        "updated_at": local_timestamp(),
        "note": "仅保存稳定 evidence_id，不复制视频、文案、单视频分析，也不暴露本机绝对路径。",
        "transcripts": [
            {
                "video_id": item.video_id,
                "evidence_id": f"video:{item.video_id}:transcript",
                "kind": "transcript",
                "included": False,
            }
            for item in materials
        ],
        "videos": [
            {
                "video_id": item.video_id,
                "evidence_id": f"video:{item.video_id}:source",
                "kind": "source_video",
                "included": False,
            }
            for item in materials
        ],
        "single_analysis": [
            {
                "video_id": item.video_id,
                "dimensions": [
                    {
                        "name": name,
                        "evidence_id": f"video:{item.video_id}:analysis:{_dimension_key(name)}",
                        "included": False,
                    }
                    for name in item.analysis_paths
                ],
            }
            for item in materials
        ],
        "visual_timelines": [
            {
                "video_id": item.video_id,
                "timeline_ref": item.visual_timeline_ref,
                "evidence_ids": item.visual_evidence_refs,
                "included": False,
            }
            for item in materials
            if item.visual_timeline_ref
        ],
        "legacy_outputs": [
            {
                "name": name,
                "package_path": f"legacy/{path.name}",
                "exists": path.exists(),
            }
            for name, path in legacy_outputs.items()
        ],
    }


def _read_excerpt(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    marker = "## 完整文案"
    if marker in text:
        text = text.split(marker, 1)[-1].strip()
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return text[:head] + "\n\n...[中段省略，仅供模式提炼]...\n\n" + text[-tail:]


def _source_meta(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("source_meta_json"):
        return {}
    try:
        parsed = json.loads(row["source_meta_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize_retrieval_pack(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(
        r"\n##\s*完整文案\b.*?(?=\n##\s|\Z)",
        "\n## 完整文案\n[已删除：retrieval_pack 不允许包含完整 raw transcript。]\n",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    if len(text) <= RETRIEVAL_PACK_MAX_CHARS:
        return text
    return (
        text[:RETRIEVAL_PACK_MAX_CHARS].rstrip()
        + "\n\n> 已截断：retrieval_pack 超过长度上限，完整资料请按需读取 video card 或原始引用。"
    )


def _required_text(data: dict[str, Any], key: str) -> str:
    text = str(data.get(key) or "").strip()
    if not text:
        raise ValueError(f"模型结果缺少 {key}")
    return text


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _platform_fit(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            "douyin": str(value.get("douyin") or "unknown"),
            "bilibili": str(value.get("bilibili") or "unknown"),
        }
    return {"douyin": "unknown", "bilibili": "unknown"}


def _dimension_key(name: str) -> str:
    return {
        "文案风格": "script_style",
        "视频脚本": "script_structure",
        "剪辑逻辑": "editing_logic",
        "选题策略": "topic_strategy",
        "运营策略": "operation_strategy",
    }.get(name, re.sub(r"\W+", "_", name).strip("_") or "analysis")


def _evidence_ids(material: VideoMaterial) -> list[str]:
    evidence = [f"video:{material.video_id}:transcript"]
    evidence.extend(
        f"video:{material.video_id}:analysis:{_dimension_key(name)}"
        for name, path in material.analysis_paths.items()
        if path.exists()
    )
    evidence.extend(material.visual_evidence_refs)
    return list(dict.fromkeys(evidence))


def _contains_absolute_path(text: str) -> bool:
    return bool(re.search(r"(?:^|[\s\"'`])/(?:Users|Volumes|private|tmp)/", str(text or "")))


def _structure_type(structure: list[str]) -> str:
    if not structure:
        return "unknown"
    return "-".join(structure[:5])


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _compact_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for card in cards:
        compact.append(
            {
                "video_id": card.get("video_id"),
                "topic": card.get("topic"),
                "hook_type": card.get("hook_type"),
                "structure_type": card.get("structure_type"),
                "structure": _as_list(card.get("structure"))[:5],
                "emotion_curve": _as_list(card.get("emotion_curve"))[:5],
                "script_patterns": _as_list(card.get("script_patterns"))[:4],
                "visual_patterns": _as_list(card.get("visual_patterns"))[:4],
                "editing_patterns": _as_list(card.get("editing_patterns"))[:4],
                "operation_patterns": _as_list(card.get("operation_patterns"))[:4],
                "risk_notes": _as_list(card.get("risk_notes"))[:3],
                "tags": _as_list(card.get("tags"))[:8],
                "card_path": f"videos/{card.get('video_id')}.card.json",
            }
        )
    return compact
