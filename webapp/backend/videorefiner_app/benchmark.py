from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import local_timestamp


BENCHMARK_PROMPT = "Benchmark Intelligence 汇总.md"
RETRIEVAL_PACK_MAX_CHARS = 12000

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
    duration_seconds: float | None
    transcript_path: Path
    raw_transcript_path: Path
    video_path: Path
    kept_video_path: Path
    analysis_paths: dict[str, Path]
    transcript_excerpt: str
    analysis_excerpts: dict[str, str]


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
) -> list[VideoMaterial]:
    materials: list[VideoMaterial] = []
    for row in rows:
        video_id = str(row["video_id"])
        transcript_path = transcript_dir / f"video_{video_id}.md"
        raw_transcript_path = tmp_dir / f"{video_id}_transcript.txt"
        analysis_paths = {dim["name"]: single_dir / f"{video_id}_{dim['name']}.md" for dim in dimensions}
        materials.append(
            VideoMaterial(
                video_id=video_id,
                title=str(row.get("title") or video_id),
                platform=str(row.get("platform") or "unknown"),
                source_url=str(row.get("url") or ""),
                duration_seconds=_optional_float(row.get("duration")),
                transcript_path=transcript_path,
                raw_transcript_path=raw_transcript_path,
                video_path=tmp_dir / f"{video_id}.mp4",
                kept_video_path=keep_dir / f"{video_id}.mp4",
                analysis_paths=analysis_paths,
                transcript_excerpt=_read_excerpt(transcript_path, max_excerpt_chars),
                analysis_excerpts={name: _read_excerpt(path, max_excerpt_chars) for name, path in analysis_paths.items() if path.exists()},
            )
        )
    return materials


def build_benchmark_prompt(
    prompt_template: str,
    creator: str,
    platform: str,
    materials: list[VideoMaterial],
    legacy_outputs: dict[str, Path],
    max_legacy_chars: int,
) -> str:
    legacy_text = []
    for name, path in legacy_outputs.items():
        if path.exists():
            legacy_text.append(f"## 旧版{name}\n{_read_excerpt(path, max_legacy_chars)}")

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
                    "",
                    "### 文案摘录",
                    material.transcript_excerpt or "无",
                    "",
                    analyses or "### 单视频分析\n无",
                ]
            )
        )

    return "\n\n".join(
        [
            prompt_template,
            f"# 输入上下文\n- creator: {creator}\n- platform: {platform}\n- video_count: {len(materials)}",
            "# 旧版 5 维合并结果",
            "\n\n".join(legacy_text) or "无",
            "# 单视频资料摘录",
            "\n\n---\n\n".join(video_text),
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
    fallback = build_fallback_benchmark_data(creator, platform, materials)
    normalized = {
        "creator_profile_md": _text_or(data.get("creator_profile_md"), fallback["creator_profile_md"]),
        "pattern_library_md": _text_or(data.get("pattern_library_md"), fallback["pattern_library_md"]),
        "qa_checklist_md": _text_or(data.get("qa_checklist_md"), fallback["qa_checklist_md"]),
        "retrieval_pack_md": _text_or(data.get("retrieval_pack_md"), fallback["retrieval_pack_md"]),
        "video_cards": [],
        "video_notes": {},
    }

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
        normalized["video_cards"].append(_normalize_card(cards_by_id.get(material.video_id) or {}, creator, platform, material))
        normalized["video_notes"][material.video_id] = _text_or(
            notes_by_id.get(material.video_id),
            _default_video_notes(material, creator),
        )
    return normalized


def build_fallback_benchmark_data(creator: str, platform: str, materials: list[VideoMaterial]) -> dict[str, Any]:
    ids = "、".join(material.video_id for material in materials[:8]) or "无"
    profile = f"""# Creator Profile - {creator}

## 基本定位
- 平台：{platform}
- 样本数量：{len(materials)}
- 代表样本：{ids}
- 内容气质：待结合单视频分析继续精炼。

## 选题策略
基于已完成样本生成，后续应从 `videos/*.card.json` 追溯到具体视频证据。

## 开场钩子模式
优先查看每张视频卡片中的 `hook_type`、`topic` 和 `evidence_refs`。

## 叙事结构
优先查看 `pattern_library.md` 与代表视频卡片，避免直接套用原文。

## 文案语言
只抽象句式、节奏和表达策略，不直接搬运博主原文。

## 情绪曲线
以好奇、冲突、反转、认同等结构化标签为主。

## 视觉包装
参考视频卡片中的 visual_patterns，不把单个平台包装方式视为通用规律。

## 剪辑节奏
参考视频卡片中的 editing_patterns，并按目标平台重新验证。

## 标题封面
只复用方法论，不复用具体标题措辞。

## 互动与运营
以评论引导、系列化、账号定位等模式为主。

## 可借鉴 Pattern
见 `pattern_library.md`。

## 不可照搬内容
不要洗稿，不要复制原文句子，不要复制具体事实编排。

## 与抖音适配注意
B站样本只能作为创作先验，抖音真实数据优先级更高。
"""
    pattern = f"""# Pattern Library - {creator}

## Hook Patterns
### 待精炼钩子模式
- 适用场景：从 `{ids}` 等样本继续提炼。
- 结构：问题/冲突/反常识/数字等入口。
- 示例来源：{ids}
- 可复用方式：复用结构，不复用原文。
- 风险：平台差异、事实失真、低级洗稿。

## Story Patterns
以视频卡片中的 `structure` 和 `emotion_curve` 为准。

## Script Patterns
以方法论标签为准，不保留大段原文。

## Visual Patterns
以视觉元素类别和密度为准。

## Editing Patterns
以镜头节奏、转场密度、素材组织方式为准。

## Operation Patterns
以标题、封面、评论引导等可迁移策略为准。

## Failure / Risk Patterns
避免照搬平台语境、情绪过载、事实核查不足和原文改写。
"""
    checklist = f"""# QA Checklist - {creator}

## 选题检查
- 是否只借鉴模式，而不是照搬具体选题结论？
- 是否结合目标平台真实数据重新判断？

## 大纲检查
- 是否有清晰钩子、冲突、递进和结论？
- 是否能追溯到视频卡片证据？

## 文案检查
- 是否避免直接改写博主原文？
- 是否控制风险表达和事实依据？

## 视觉检查
- 是否只复用视觉方法论？

## 剪辑检查
- 是否匹配目标平台节奏？

## 标题封面检查
- 是否有冲突或悬念，但不夸大事实？

## 事实与风险检查
- 是否有事实核查入口？
- 是否标注不可迁移的平台特征？

## 不应模仿的内容
- 原句、独特梗、私域话术、未经核实的事实链。
"""
    retrieval_pack = f"""# Retrieval Pack - {creator}

## 使用边界
本文件只提供少量代表样本和模式入口，不包含完整 raw transcript 或完整单视频分析。

## Creator Profile 摘要
查看 `creator_profile.md`。

## Relevant Patterns
查看 `pattern_library.md`。

## Relevant Cards
{chr(10).join(f"- {material.video_id}: videos/{material.video_id}.card.json" for material in materials[:8])}

## Task Suggestions
- 先读本 pack，再按需读取 3-8 张 video card。
- 不要读取全量 `raw/`。
- 不要直接洗稿或复刻博主原文。
"""
    return {
        "creator_profile_md": profile,
        "pattern_library_md": pattern,
        "qa_checklist_md": checklist,
        "retrieval_pack_md": retrieval_pack,
        "video_cards": [_normalize_card({}, creator, platform, material) for material in materials],
        "video_notes": {material.video_id: _default_video_notes(material, creator) for material in materials},
    }


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


def _normalize_card(card: dict[str, Any], creator: str, platform: str, material: VideoMaterial) -> dict[str, Any]:
    normalized = _default_card(material, creator, platform)
    for field in CARD_FIELDS:
        if field in card and card[field] not in (None, ""):
            normalized[field] = card[field]
    normalized["video_id"] = material.video_id
    normalized["platform"] = str(normalized.get("platform") or material.platform or platform or "unknown")
    normalized["creator"] = str(normalized.get("creator") or creator)
    normalized["source_url"] = str(normalized.get("source_url") or material.source_url)
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
    normalized["editing_density"] = str(card.get("editing_density") or "unknown")
    normalized["visual_density"] = str(card.get("visual_density") or "unknown")
    normalized["platform_fit"] = _platform_fit(card.get("platform_fit"))
    return normalized


def _default_card(material: VideoMaterial, creator: str, platform: str) -> dict[str, Any]:
    evidence = [str(path) for path in [material.transcript_path, *material.analysis_paths.values()] if path.exists()]
    return {
        "video_id": material.video_id,
        "platform": material.platform or platform or "unknown",
        "creator": creator,
        "source_url": material.source_url,
        "topic": material.title,
        "published_at": None,
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
        "evidence_refs": evidence,
        "tags": [],
    }


def _default_video_notes(material: VideoMaterial, creator: str) -> str:
    evidence = "\n".join(f"- {path}" for path in [material.transcript_path, *material.analysis_paths.values()] if path.exists()) or "- 无"
    return f"""# Video Notes - {material.video_id}

- 博主：{creator}
- 标题：{material.title}
- 平台：{material.platform}
- 来源：{material.source_url}

## 摘要
该 notes 由 Benchmark Intelligence 阶段生成，供按需检索，不包含完整 raw transcript。

## 可追溯证据
{evidence}

## 使用边界
- 只借鉴模式，不直接改写原文。
- 平台表现需要结合目标平台数据重新判断。
"""


def _index_card(card: dict[str, Any], card_path: Path, notes_path: Path, output_dir: Path) -> dict[str, Any]:
    return {
        "video_id": card["video_id"],
        "card_path": _relative_posix(card_path, output_dir),
        "notes_path": _relative_posix(notes_path, output_dir),
        "tags": _as_list(card.get("tags")),
        "topic": str(card.get("topic") or ""),
        "hook_type": str(card.get("hook_type") or ""),
        "structure_type": str(card.get("structure_type") or ""),
        "emotion_curve": _as_list(card.get("emotion_curve")),
        "editing_density": str(card.get("editing_density") or "unknown"),
        "visual_density": str(card.get("visual_density") or "unknown"),
        "platform_fit": _platform_fit(card.get("platform_fit")),
    }


def _raw_refs(materials: list[VideoMaterial], legacy_outputs: dict[str, Path], output_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy": "references_only",
        "updated_at": local_timestamp(),
        "note": "raw/ 只保存引用，不复制视频、文案和单视频分析大文件。",
        "transcripts": [
            {"video_id": item.video_id, "path": str(item.transcript_path), "exists": item.transcript_path.exists()} for item in materials
        ],
        "raw_transcripts": [
            {"video_id": item.video_id, "path": str(item.raw_transcript_path), "exists": item.raw_transcript_path.exists()} for item in materials
        ],
        "videos": [
            {
                "video_id": item.video_id,
                "download_path": str(item.video_path),
                "kept_path": str(item.kept_video_path),
                "download_exists": item.video_path.exists(),
                "kept_exists": item.kept_video_path.exists(),
            }
            for item in materials
        ],
        "single_analysis": [
            {
                "video_id": item.video_id,
                "paths": {name: str(path) for name, path in item.analysis_paths.items()},
            }
            for item in materials
        ],
        "legacy_outputs": [
            {
                "name": name,
                "path": str(path),
                "legacy_copy_path": str(output_dir / "legacy" / path.name),
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


def _text_or(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


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
