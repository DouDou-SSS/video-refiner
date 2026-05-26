from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable


CleanupCategory = str

CLEANUP_CATEGORY_ORDER = ("frames", "single_analysis", "kept_videos", "transcripts", "raw_data")
ALLOWED_CLEANUP_CATEGORIES = set(CLEANUP_CATEGORY_ORDER)


def artifact_kinds_for_cleanup(categories: Iterable[CleanupCategory]) -> list[str]:
    mapping = {
        "frames": "frames",
        "single_analysis": "single_analysis",
        "kept_videos": "kept_video",
        "transcripts": "transcript",
        "raw_data": "video",
    }
    return sorted({mapping[item] for item in categories if item in mapping})


def collect_cleanup_targets(output_dir: Path, categories: Iterable[CleanupCategory]) -> list[Path]:
    selected = set(categories)
    targets: list[Path] = []
    raw_dir = output_dir / "原始数据"

    if "single_analysis" in selected:
        targets.append(output_dir / "单视频分析")
    if "kept_videos" in selected:
        targets.append(output_dir / "视频保留")
    if "transcripts" in selected:
        targets.append(output_dir / "文案")

    if raw_dir.exists():
        if {"raw_data", "frames"} <= selected:
            targets.append(raw_dir)
        elif "raw_data" in selected:
            targets.extend(_raw_data_children_without_frames(raw_dir))
        elif "frames" in selected:
            targets.extend(path for path in raw_dir.glob("*_frames") if path.is_dir())

    return _dedupe_paths(targets)


def cleanup_outputs(output_dir: Path, categories: Iterable[CleanupCategory]) -> dict:
    selected = set(categories)
    unknown = selected - ALLOWED_CLEANUP_CATEGORIES
    if unknown:
        raise ValueError("未知清理类别：" + "、".join(sorted(unknown)))

    root = output_dir.expanduser().resolve()
    targets = collect_cleanup_targets(root, selected)
    deleted_paths: list[str] = []
    freed_bytes = 0

    for target in targets:
        resolved = target.expanduser().resolve(strict=False)
        _ensure_inside_output_dir(root, resolved)
        if not resolved.exists() and not resolved.is_symlink():
            continue
        freed_bytes += _path_size(resolved)
        _delete_path(resolved)
        deleted_paths.append(str(resolved))

    return {"deleted_count": len(deleted_paths), "freed_bytes": freed_bytes, "deleted_paths": deleted_paths}


def estimate_cleanup_outputs(output_dir: Path) -> dict[str, dict[str, int]]:
    root = output_dir.expanduser().resolve(strict=False)
    summary: dict[str, dict[str, int]] = {}

    for category in CLEANUP_CATEGORY_ORDER:
        targets = collect_cleanup_targets(root, [category])
        total_bytes = 0
        count = 0
        for target in targets:
            resolved = target.expanduser().resolve(strict=False)
            _ensure_inside_output_dir(root, resolved)
            if not resolved.exists() and not resolved.is_symlink():
                continue
            total_bytes += _path_size(resolved)
            count += 1
        summary[category] = {"bytes": total_bytes, "count": count}

    return summary


def _raw_data_children_without_frames(raw_dir: Path) -> list[Path]:
    targets = []
    for child in raw_dir.iterdir():
        if child.is_dir() and child.name.endswith("_frames"):
            continue
        targets.append(child)
    return targets


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _ensure_inside_output_dir(root: Path, target: Path) -> None:
    if target == root:
        raise ValueError("拒绝删除整个任务输出目录")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"拒绝删除输出目录外的路径：{target}") from exc


def _path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_symlink() or child.is_file():
                total += child.lstat().st_size
        except FileNotFoundError:
            continue
    return total


def _delete_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)
