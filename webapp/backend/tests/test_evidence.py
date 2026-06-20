from __future__ import annotations

import json
from pathlib import Path

import pytest

from videorefiner_app.evidence import (
    TIMELINE_SCHEMA_VERSION,
    apply_visual_observations,
    build_visual_timeline,
    evenly_selected_keyframes,
    parse_visual_observations,
    select_scene_peaks,
    validate_visual_timeline,
)
from videorefiner_app.pipeline import PipelineRunner


def _frame_dir(tmp_path: Path) -> Path:
    frames = tmp_path / "原始数据" / "123_frames"
    frames.mkdir(parents=True)
    for index in range(1, 61):
        (frames / f"frame_{index:04d}.jpg").write_bytes(b"jpeg")
    (frames / "frames_meta.json").write_text(json.dumps({"frame_interval_seconds": 1}), encoding="utf-8")
    return frames


def test_select_scene_peaks_keeps_one_peak_per_consecutive_cut() -> None:
    peaks = select_scene_peaks(
        [
            {"timestamp_seconds": 1.0, "score": 0.2},
            {"timestamp_seconds": 1.3, "score": 0.8},
            {"timestamp_seconds": 3.0, "score": 0.6},
        ]
    )
    assert [(item["timestamp_seconds"], item["score"]) for item in peaks] == [(1.3, 0.8), (3.0, 0.6)]


def test_visual_timeline_aligns_timed_transcript_and_requires_visual_observations(tmp_path: Path) -> None:
    frames = _frame_dir(tmp_path)
    transcript = tmp_path / "原始数据" / "123_transcript_timeline.json"
    transcript.write_text(
        json.dumps(
            {
                "segments": [
                    {"start_seconds": 0, "end_seconds": 6, "text": "开头的事实文案", "source": "whisper", "timing": "timed"},
                    {"start_seconds": 20, "end_seconds": 28, "text": "中段的事实文案", "source": "whisper", "timing": "timed"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    timeline = build_visual_timeline(
        "123",
        tmp_path / "123.mp4",
        frames,
        transcript,
        tmp_path / "evidence",
        "ffmpeg",
        duration_seconds=59,
        scene_points=[{"timestamp_seconds": 20, "score": 0.9}],
    )
    assert timeline["schema_version"] == TIMELINE_SCHEMA_VERSION
    assert timeline["quality"]["transcript_alignment"] == "timed"
    assert any("中段的事实文案" in shot["transcript_excerpt"] for shot in timeline["shots"])
    with pytest.raises(ValueError, match="缺少视觉证据"):
        validate_visual_timeline(timeline)


def test_visual_timeline_distinguishes_evidence_windows_from_detected_cut_segments(tmp_path: Path) -> None:
    frames = _frame_dir(tmp_path)
    transcript = tmp_path / "原始数据" / "123_transcript_timeline.json"
    transcript.write_text(
        json.dumps(
            {
                "segments": [
                    {"start_seconds": 0, "end_seconds": 40, "text": "带有可靠时间的文案。", "source": "whisper", "timing": "timed"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    timeline = build_visual_timeline(
        "123",
        tmp_path / "123.mp4",
        frames,
        transcript,
        tmp_path / "evidence",
        "ffmpeg",
        duration_seconds=59,
        scene_points=[
            {"timestamp_seconds": 8, "score": 0.82},
            {"timestamp_seconds": 18, "score": 0.78},
            {"timestamp_seconds": 32, "score": 0.86},
        ],
    )

    windows = [shot for shot in timeline["shots"] if shot["segment_type"] == "evidence_window"]
    cuts = [shot for shot in timeline["shots"] if shot["segment_type"] == "detected_cut_segment"]

    assert windows
    assert all(shot["boundary_source"] in {"uniform_coverage", "scene_peak"} for shot in windows)
    assert all(shot["boundary_confidence"] == "low" for shot in windows)
    assert len(cuts) >= 2
    assert all(shot["boundary_source"] == "detected_cut" for shot in cuts)
    assert all(shot["boundary_confidence"] in {"high", "medium"} for shot in cuts)
    assert timeline["quality"]["alignment_status"] == "timed"


def test_visual_observation_parser_rejects_missing_shots() -> None:
    with pytest.raises(ValueError, match="缺少镜头"):
        parse_visual_observations('{"shots": []}', {"video:123:shot:001"})


def test_evenly_selected_keyframes_keeps_head_middle_and_tail() -> None:
    timeline = {"shots": [{"keyframe": f"frame_{index}.jpg"} for index in range(10)]}
    assert evenly_selected_keyframes(timeline, 3) == ["frame_0.jpg", "frame_4.jpg", "frame_9.jpg"]


def test_apply_visual_observations_completes_timeline(tmp_path: Path) -> None:
    frames = _frame_dir(tmp_path)
    transcript = tmp_path / "原始数据" / "123_transcript_timeline.json"
    transcript.write_text(json.dumps({"segments": []}), encoding="utf-8")
    timeline = build_visual_timeline(
        "123",
        tmp_path / "123.mp4",
        frames,
        transcript,
        tmp_path / "evidence",
        "ffmpeg",
        duration_seconds=30,
        scene_points=[],
    )
    observations = {
        shot["evidence_id"]: {
            "visual_description": "画面中可见人物与室内场景。",
            "shot_type": "中景",
            "composition": "主体居中",
            "on_screen_text_observation": "未确认",
            "transition_observation": "未确认",
            "confidence": "medium",
            "uncertainty": "单帧不能判断连续运镜。",
        }
        for shot in timeline["shots"]
    }
    completed = apply_visual_observations(timeline, observations)
    assert completed["quality"]["visual_observations"] == "complete"
    assert completed["quality"]["observation_coverage"] == "complete"
    assert completed["quality"]["visual_confidence_summary"] == {
        "high": 0,
        "medium": len(timeline["shots"]),
        "low": 0,
    }
    assert completed["quality"]["alignment_status"] == "coarse"
    assert completed["quality"]["eligible_for_precise_timing"] is False
    validate_visual_timeline(completed)


def test_visual_evidence_cloud_failure_is_retryable_but_missing_frame_is_not() -> None:
    runner = object.__new__(PipelineRunner)
    assert runner._is_retryable_error("视觉证据批次 1 连续两次未通过校验：JSON 解析失败")
    assert not runner._is_retryable_error("证据关键帧不存在：frame_0001.jpg")
