from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from videorefiner_app.adapters import detect_platform, download_video, extract_video_id, normalize_video_url, parse_inputs, safe_path_name
from videorefiner_app.benchmark import (
    VideoMaterial,
    collect_video_materials,
    normalize_benchmark_data,
    normalize_video_cards_data,
    parse_benchmark_json,
    remove_benchmark_outputs,
    validate_video_batch_data,
    write_benchmark_outputs,
)
from videorefiner_app.cleanup import cleanup_outputs, collect_cleanup_targets, estimate_cleanup_outputs
from videorefiner_app.config import AppConfig
from videorefiner_app.db import Database
from videorefiner_app.export_package import export_videoautomation_package
from videorefiner_app.llm import make_test_png_base64
from videorefiner_app.media_worker import (
    _ocr_hotwords,
    _ocr_is_primary_source,
    _subtitle_text_from_file,
    _subtitle_timeline_from_file,
    _transcribe_with_fallback,
)
from videorefiner_app.metadata import extract_duration_seconds, extract_published_at
from videorefiner_app.metadata_refresh import refresh_job_platform_metadata
from videorefiner_app.pipeline import PipelineRunner
from videorefiner_app.providers import PROVIDER_PRESETS, VISION_CAPABLE_PROVIDER_KEYS
from videorefiner_app.schemas import JobCreateIn
from videorefiner_app.security import SecretStore
from videorefiner_app.utils import list_visible_files
from videorefiner_app.validation import validate_model_profile_for_refinement


def _valid_card(video_id: str = "123") -> dict[str, object]:
    return {
        "video_id": video_id,
        "platform": "douyin",
        "creator": "飞天闪客",
        "source_url": f"https://www.douyin.com/video/{video_id}",
        "topic": "用反常识问题解释复杂技术",
        "published_at": "2026-06-19T08:30:00Z",
        "duration_seconds": 90,
        "hook_type": "反常识问题",
        "structure": ["问题钩子", "案例拆解", "结论升华"],
        "emotion_curve": ["好奇", "紧张", "释然"],
        "script_patterns": ["短句提问后立即给出判断"],
        "visual_patterns": ["关键词字幕配合案例画面"],
        "editing_patterns": ["观点切换时更换画面"],
        "operation_patterns": ["结尾用问题引导评论"],
        "best_quotes": ["用问题打开认知缺口"],
        "risk_notes": ["事实结论需要二次核验"],
        "evidence_refs": [f"video:{video_id}:transcript", f"video:{video_id}:analysis:script_structure"],
        "tags": ["技术科普", "反常识"],
        "title": "测试标题",
        "structure_type": "问题-案例-结论",
        "editing_density": "中高：约 3-5 秒切换信息画面",
        "visual_density": "中：字幕与案例画面并行",
        "platform_fit": {"douyin": "适合 60-120 秒高密度表达", "bilibili": "需扩充案例与事实来源"},
    }


def _valid_notes(video_id: str = "123") -> str:
    return f"""# Video Notes - {video_id}

## 核心方法
用反常识问题制造认知缺口，再通过具体案例逐层解释，最后给出能够复述的结论。

## 脚本与叙事
开场直接提问，中段按问题、案例、判断三步推进，避免先铺设过长背景。

## 视觉与剪辑
关键词字幕只强化当前观点，案例画面随论点切换，保持中高信息密度。

## 运营与风险
结尾用开放问题引导讨论；涉及事实、数字和因果关系时必须二次核验。

## 证据
- video:{video_id}:transcript
- video:{video_id}:analysis:script_structure
"""


def _write_valid_export_source(output_dir: Path, video_id: str = "123") -> None:
    videos_dir = output_dir / "videos"
    raw_dir = output_dir / "raw"
    single_dir = output_dir / "单视频分析"
    for directory in (videos_dir, raw_dir, single_dir):
        directory.mkdir(parents=True, exist_ok=True)

    profile = (f"# Creator Profile - 飞天闪客\n\n代表视频 {video_id}。" + "定位、受众和内容气质均由真实样本归纳。" * 20)
    pattern = (f"# Pattern Library - 飞天闪客\n\n代表视频 {video_id}。" + "模式包含适用场景、结构、证据和风险边界。" * 20)
    checklist = "# QA Checklist - 飞天闪客\n\n" + "逐项检查选题、脚本、视觉、剪辑、事实和风险。" * 20
    pack = (f"# Retrieval Pack - 飞天闪客\n\n代表样本：videos/{video_id}.card.json\n\n" + "先读模式，再按证据 ID 回查，不复制原文。" * 20)
    for name, text in {
        "creator_profile.md": profile,
        "pattern_library.md": pattern,
        "qa_checklist.md": checklist,
        "retrieval_pack.md": pack,
    }.items():
        (output_dir / name).write_text(text, encoding="utf-8")

    card = _valid_card(video_id)
    (videos_dir / f"{video_id}.card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    (videos_dir / f"{video_id}.notes.md").write_text(_valid_notes(video_id), encoding="utf-8")
    (output_dir / "retrieval_index.json").write_text(
        json.dumps(
            {
                "creator": "飞天闪客",
                "platform": "douyin",
                "cards": [
                    {
                        "video_id": video_id,
                        "card_path": f"videos/{video_id}.card.json",
                        "notes_path": f"videos/{video_id}.notes.md",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for dimension in ["文案风格", "视频脚本", "剪辑逻辑", "选题策略", "运营策略"]:
        (single_dir / f"{video_id}_{dimension}.md").write_text(f"# {dimension}\n\n有效的具体分析内容。", encoding="utf-8")
    (raw_dir / "refs.json").write_text(
        json.dumps(
            {
                "policy": "references_only",
                "api_key": "secret-value",
                "videos": [{"video_id": video_id, "download_path": str(output_dir / "原始数据" / f"{video_id}.mp4")}],
                "single_analysis": [{"video_id": video_id, "paths": {"文案风格": str(single_dir / f"{video_id}_文案风格.md")}}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_provider_presets_cover_required_options() -> None:
    keys = {item.key for item in PROVIDER_PRESETS}
    assert {"xiaomi_mimo", "volcengine_ark", "bailian", "openai", "deepseek", "openrouter", "custom"} <= keys

    xiaomi = next(item for item in PROVIDER_PRESETS if item.key == "xiaomi_mimo")
    assert xiaomi.base_url == "https://token-plan-cn.xiaomimimo.com/v1"
    assert xiaomi.analysis_model == "mimo-v2.5"
    assert xiaomi.merge_model == "mimo-v2.5-pro"
    assert xiaomi.supports_vision is True
    assert xiaomi.supports_reasoning is False
    assert "xiaomi_mimo" in VISION_CAPABLE_PROVIDER_KEYS

    volcengine = next(item for item in PROVIDER_PRESETS if item.key == "volcengine_ark")
    assert volcengine.provider_name == "火山方舟"
    assert volcengine.base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert volcengine.analysis_model == "ark-code-latest"
    assert volcengine.merge_model == "ark-code-latest"
    assert volcengine.supports_vision is True
    assert volcengine.supports_reasoning is False
    assert "volcengine_ark" in VISION_CAPABLE_PROVIDER_KEYS


def test_url_parsing_and_normalization() -> None:
    assert detect_platform("7635684541159460142") == "douyin"
    assert normalize_video_url("7635684541159460142").endswith("/7635684541159460142")
    assert extract_video_id("https://www.bilibili.com/video/BV1xx411c7mD") == "BV1xx411c7mD"
    rows = parse_inputs("batch", ["7635684541159460142", "BV1xx411c7mD"], 50, lambda *_: None)
    assert [row["platform"] for row in rows] == ["douyin", "bilibili"]


def test_safe_path_name_keeps_blogger_name_readable() -> None:
    assert safe_path_name('飞天闪客 / AI: Agent?') == "飞天闪客 _ AI_ Agent"
    assert safe_path_name("   ") == "未命名博主"


def test_text_only_profile_is_blocked_for_refinement() -> None:
    errors = validate_model_profile_for_refinement(
        {
            "is_tested": True,
            "supports_vision": False,
            "analysis_model": "deepseek-chat",
            "merge_model": "deepseek-chat",
        }
    )
    assert any("图片输入" in item for item in errors)


def test_completed_video_with_model_refusal_is_reactivated(tmp_path: Path) -> None:
    class FakeDb:
        def __init__(self) -> None:
            self.rows = [{"id": "db-1", "video_id": "123", "status": "done"}]
            self.dimension_updates: list[tuple[str, str]] = []

        def query_all(self, _sql: str, _params: list[str]) -> list[dict[str, str]]:
            return self.rows

        def update_dimension(self, _job_id: str, _video_db_id: str, dimension: str, status: str, **_kwargs: object) -> None:
            self.dimension_updates.append((dimension, status))

        def update_video(self, _video_db_id: str, **values: object) -> None:
            self.rows[0].update(values)

    single_dir = tmp_path / "单视频分析"
    single_dir.mkdir()
    for dimension in ["文案风格", "视频脚本", "剪辑逻辑", "选题策略", "运营策略"]:
        text = "有效分析"
        if dimension == "视频脚本":
            text = "The request was rejected because it was considered high risk"
        (single_dir / f"123_{dimension}.md").write_text(text, encoding="utf-8")

    runner = object.__new__(PipelineRunner)
    runner.db = FakeDb()
    runner.job_id = "job-1"
    runner.single_dir = single_dir
    runner._log = lambda *_args: None

    runner._reactivate_invalid_completed_videos()

    assert runner.db.rows[0]["status"] == "pending"
    assert runner.db.rows[0]["retry_count"] == 0
    assert runner.db.dimension_updates == [("视频脚本", "failed")]


def test_benchmark_metadata_repair_fills_duration_from_existing_video(tmp_path: Path) -> None:
    class FakeDb:
        def __init__(self) -> None:
            self.updated: dict[str, object] = {}

        def update_video(self, _video_db_id: str, **values: object) -> None:
            self.updated.update(values)

    tmp_dir = tmp_path / "原始数据"
    tmp_dir.mkdir()
    (tmp_dir / "123.mp4").write_bytes(b"0" * 2048)
    row = {
        "id": "db-1",
        "video_id": "123",
        "duration": None,
        "published_at": None,
        "source_meta_json": json.dumps({"create_time": 1_719_014_400}),
    }
    runner = object.__new__(PipelineRunner)
    runner.db = FakeDb()
    runner.tmp_dir = tmp_dir
    runner._video_duration_seconds = lambda _path: 123.456
    runner._log = lambda *_args: None

    repaired = runner._ensure_video_metadata([row])

    assert repaired[0]["duration"] == 123.456
    assert repaired[0]["published_at"] == "2024-06-22T00:00:00Z"
    assert runner.db.updated["duration"] == 123.456
    assert runner.db.updated["published_at"] == "2024-06-22T00:00:00Z"


def test_model_test_image_meets_size_requirement() -> None:
    import base64
    import struct

    png = base64.b64decode(make_test_png_base64())
    width, height = struct.unpack(">II", png[16:24])
    assert width == 32
    assert height == 32
    assert width > 10 and height > 10


def test_metadata_extractors_parse_common_provider_fields() -> None:
    assert extract_duration_seconds({"duration_ms": 90500}) == 90.5
    assert extract_duration_seconds("01:02") == 62
    assert extract_published_at({"create_time": 1_719_014_400}) == "2024-06-22T00:00:00Z"
    assert extract_published_at({"timestamp": 1_719_014_400}) == "2024-06-22T00:00:00Z"
    assert extract_published_at({"upload_date": "20260619"}) == "2026-06-19"


def test_refresh_platform_metadata_updates_cards_and_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.sqlite3")
    job = db.create_job("batch", str(tmp_path / "output"), "profile-1", {}, {"inputs": []}, 1)
    video_db_id = db.create_video(
        job["id"],
        "123",
        "https://www.douyin.com/video/123",
        "douyin",
        {"title": "原始标题"},
    )
    output_dir = tmp_path / "output"
    videos_dir = output_dir / "videos"
    videos_dir.mkdir(parents=True)
    (videos_dir / "123.card.json").write_text(json.dumps({"video_id": "123", "published_at": None, "duration_seconds": None}), encoding="utf-8")
    (output_dir / "retrieval_index.json").write_text(
        json.dumps({"cards": [{"video_id": "123", "published_at": None, "duration_seconds": None}]}),
        encoding="utf-8",
    )

    result = refresh_job_platform_metadata(
        db,
        job["id"],
        output_dir,
        lambda *_args: None,
        fetcher=lambda *_args: {"published_at": "2026-06-19", "duration": 123.4, "metadata_source": "test"},
    )

    row = db.query_one("SELECT published_at, duration, source_meta_json FROM videos WHERE id = ?", [video_db_id])
    card = json.loads((videos_dir / "123.card.json").read_text(encoding="utf-8"))
    index = json.loads((output_dir / "retrieval_index.json").read_text(encoding="utf-8"))
    assert result["updated"] == 1
    assert result["remaining"] == 0
    assert row["published_at"] == "2026-06-19"
    assert row["duration"] == 123.4
    assert json.loads(row["source_meta_json"])["metadata_source"] == "test"
    assert card["published_at"] == "2026-06-19"
    assert card["duration_seconds"] == 123.4
    assert index["cards"][0]["published_at"] == "2026-06-19"


def test_list_visible_files_excludes_appledouble_metadata(tmp_path: Path) -> None:
    real_frame = tmp_path / "frame_0001.jpg"
    real_frame.write_bytes(b"\xff\xd8\xff\xd9")
    (tmp_path / "._frame_0001.jpg").write_bytes(b"AppleDouble")

    assert list_visible_files(tmp_path, "*.jpg") == [real_frame]


def test_media_worker_direct_script_entrypoint_starts() -> None:
    worker_path = Path(__file__).parents[1] / "videorefiner_app" / "media_worker.py"
    result = subprocess.run([sys.executable, str(worker_path), "--help"], capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stderr
    assert "--frames-dir" in result.stdout


def test_job_create_frame_interval_defaults_to_auto_policy() -> None:
    payload = JobCreateIn(input_type="batch", inputs=["7635684541159460142"], model_profile_id="profile-1")
    assert payload.frame_interval_seconds is None


def test_frame_interval_auto_policy_by_video_duration(tmp_path: Path, monkeypatch) -> None:
    runner = object.__new__(PipelineRunner)
    runner.config_snapshot = {"frame_interval_seconds": None}
    video_path = tmp_path / "demo.mp4"

    monkeypatch.setattr(runner, "_video_duration_seconds", lambda _: 600)
    assert runner._frame_interval_config(video_path)[0] == 1

    monkeypatch.setattr(runner, "_video_duration_seconds", lambda _: 601)
    assert runner._frame_interval_config(video_path)[0] == 5

    runner.config_snapshot = {"frame_interval_seconds": 12}
    assert runner._frame_interval_config(video_path)[0] == 12


def test_subtitle_parser_removes_timing_lines(tmp_path: Path) -> None:
    subtitle = tmp_path / "demo.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n这是真正文案\n\n2\n00:00:01,000 --> 00:00:02,000\n不是时间轴\n",
        encoding="utf-8",
    )
    assert _subtitle_text_from_file(subtitle) == "这是真正文案 不是时间轴"


def test_subtitle_timeline_keeps_real_time_ranges(tmp_path: Path) -> None:
    subtitle = tmp_path / "demo.srt"
    subtitle.write_text("1\n00:00:01,250 --> 00:00:03,500\n这是真正文案\n", encoding="utf-8")
    assert _subtitle_timeline_from_file(subtitle) == [
        {
            "start_seconds": 1.25,
            "end_seconds": 3.5,
            "text": "这是真正文案",
            "source": "soft_subtitle",
            "timing": "timed",
        }
    ]


def test_ocr_hotwords_filters_obvious_noise() -> None:
    hotwords = _ocr_hotwords("@飞天闪客 2026新高考真题全刷基础2000题 大模型时代到来 数学选择秘杀三连领取")
    assert "飞天闪客" not in hotwords
    assert "三连领取" not in hotwords
    assert "大模型时代到来" in hotwords


def test_ocr_primary_source_requires_enough_bottom_subtitle_signal() -> None:
    text = "这是一段底部硬字幕。" * 120
    assert _ocr_is_primary_source(text, {"ocr_frames_sampled": 100, "ocr_frames_with_text": 20})
    assert not _ocr_is_primary_source(text, {"ocr_frames_sampled": 100, "ocr_frames_with_text": 5})
    assert not _ocr_is_primary_source("太短", {"ocr_frames_sampled": 100, "ocr_frames_with_text": 20})


def test_whisper_falls_back_to_simple_decode_params(tmp_path: Path) -> None:
    class Segment:
        text = " 简单参数成功"

    class Model:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def transcribe(self, _path: str, **kwargs: object):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("high failed")
            return [Segment()], object()

    model = Model()
    text, mode = _transcribe_with_fallback(model, tmp_path / "demo.wav", "提示词", "热词")
    assert text == "简单参数成功"
    assert mode == "simple"
    assert "initial_prompt" in model.calls[0]
    assert "initial_prompt" not in model.calls[1]
    assert model.calls[1]["beam_size"] == 5


def test_cleanup_raw_data_keeps_frame_dirs_when_frames_not_selected(tmp_path: Path) -> None:
    output_dir = tmp_path / "输出"
    frames_dir = output_dir / "原始数据" / "123_frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / "frame_001.jpg").write_text("image", encoding="utf-8")
    (output_dir / "原始数据" / "123.mp4").write_text("video", encoding="utf-8")
    (output_dir / "原始数据" / "123_transcript.txt").write_text("text", encoding="utf-8")

    sizes = estimate_cleanup_outputs(output_dir)
    assert sizes["frames"] == {"bytes": 5, "count": 1}
    assert sizes["raw_data"] == {"bytes": 9, "count": 2}

    targets = collect_cleanup_targets(output_dir, ["raw_data"])
    assert frames_dir not in targets

    result = cleanup_outputs(output_dir, ["raw_data"])
    assert result["deleted_count"] == 2
    assert frames_dir.exists()
    assert not (output_dir / "原始数据" / "123.mp4").exists()
    assert not (output_dir / "原始数据" / "123_transcript.txt").exists()


def test_cleanup_raw_data_and_frames_deletes_raw_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "输出"
    raw_dir = output_dir / "原始数据"
    frames_dir = raw_dir / "123_frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / "frame_001.jpg").write_text("image", encoding="utf-8")
    (raw_dir / "123.mp4").write_text("video", encoding="utf-8")
    (output_dir / "视频脚本.md").write_text("final", encoding="utf-8")

    result = cleanup_outputs(output_dir, ["raw_data", "frames"])
    assert result["deleted_count"] == 1
    assert not raw_dir.exists()
    assert (output_dir / "视频脚本.md").exists()


def test_cleanup_rejects_unknown_category(tmp_path: Path) -> None:
    output_dir = tmp_path / "输出"
    output_dir.mkdir()

    try:
        cleanup_outputs(output_dir, ["all"])
    except ValueError as exc:
        assert "未知清理类别" in str(exc)
    else:
        raise AssertionError("unknown cleanup category should fail")


def test_database_does_not_store_api_key(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.sqlite3")
    payload = {
        "provider_key": "custom",
        "provider_name": "Mock",
        "base_url": "https://example.test/v1",
        "analysis_model": "vision-model",
        "merge_model": "merge-model",
        "supports_vision": True,
        "supports_reasoning": False,
        "max_tokens": 8192,
        "temperature": 0.2,
    }
    row = db.upsert_model_profile(payload, "encrypted_file")
    assert "api_key" not in row
    assert "secret-token" not in str(row)
    db.delete_model_profile(row["id"])
    assert db.query_one("SELECT * FROM model_profiles WHERE id = ?", [row["id"]]) is None


def test_video_source_metadata_is_persisted_for_download_reuse(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.sqlite3")
    job = db.create_job("blogger", str(tmp_path / "out"), "profile-1", {}, {"inputs": []}, 1)

    video_db_id = db.create_video(
        job["id"],
        "123",
        "https://www.douyin.com/video/123",
        "douyin",
        {
            "title": "缓存标题",
            "play_url": "https://video.example/123.mp4",
            "duration_ms": 90500,
            "create_time": 1_719_014_400,
        },
    )
    row = db.query_one("SELECT title, duration, published_at, source_meta_json FROM videos WHERE id = ?", [video_db_id])

    assert row is not None
    assert row["title"] == "缓存标题"
    assert row["duration"] == 90.5
    assert row["published_at"] == "2024-06-22T00:00:00Z"
    assert json.loads(row["source_meta_json"])["play_url"] == "https://video.example/123.mp4"


def test_add_artifact_replaces_same_job_kind_path(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.sqlite3")
    job = db.create_job("batch", str(tmp_path / "out"), "profile-1", {}, {"inputs": []}, 1)
    path = str(tmp_path / "out" / "文案风格.md")

    db.add_artifact(job["id"], "final_output", path, None, {"version": 1})
    db.add_artifact(job["id"], "final_output", path, None, {"version": 2})
    rows = db.query_all("SELECT kind, path, meta_json FROM artifacts WHERE job_id = ?", [job["id"]])

    assert len(rows) == 1
    assert json.loads(rows[0]["meta_json"])["version"] == 2


def test_douyin_download_reuses_cached_play_url(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "123.mp4"
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        return None if name == "mcporter" else f"/mock/{name}"

    def fake_curl(url: str, path: Path) -> None:
        calls.append(url)
        path.write_bytes(b"0" * 2048)

    monkeypatch.setattr("videorefiner_app.adapters.shutil.which", fake_which)
    monkeypatch.setattr("videorefiner_app.adapters._curl_download", fake_curl)

    result = download_video(
        "https://www.douyin.com/video/123",
        output_path,
        lambda *_args: None,
        source_meta={
            "title": "缓存标题",
            "play_url": "https://video.example/123.mp4",
            "duration": 88,
            "published_at": "2026-06-19T08:30:00Z",
        },
    )

    assert result["method"] == "cached-play-url"
    assert result["title"] == "缓存标题"
    assert result["duration"] == 88
    assert result["published_at"] == "2026-06-19T08:30:00Z"
    assert calls == ["https://video.example/123.mp4"]


def test_secret_store_fallback_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("videorefiner_app.security.platform.system", lambda: "Linux")
    config = AppConfig(
        app_home=tmp_path,
        database_path=tmp_path / "app.sqlite3",
        output_root=tmp_path / "out",
        prompts_dir=tmp_path / "prompts",
        system_python="python3",
        camoufox_python="python3",
        ffmpeg_bin="ffmpeg",
        daily_limit=50,
        video_delay_min_ms=1,
        video_delay_max_ms=1,
        dimension_delay_min_ms=1,
        dimension_delay_max_ms=1,
        auto_retry_max_attempts=3,
        auto_retry_delay_min_ms=1,
        auto_retry_delay_max_ms=1,
        frame_fps=1,
        max_dimension_frames=20,
        max_analysis_chars_per_video=60000,
        max_merge_chars_per_video=500,
    )
    store = SecretStore(config)
    store.set_api_key("profile-1", "secret-token")
    assert store.get_api_key("profile-1") == "secret-token"
    assert "secret-token" not in (tmp_path / "secure" / "api-keys.json.enc").read_text(encoding="utf-8", errors="ignore")


def test_benchmark_outputs_write_structured_files_and_refs(tmp_path: Path) -> None:
    output_dir = tmp_path / "飞天闪客"
    single_dir = output_dir / "单视频分析"
    transcript_dir = output_dir / "文案"
    tmp_dir = output_dir / "原始数据"
    keep_dir = output_dir / "视频保留"
    for directory in [single_dir, transcript_dir, tmp_dir, keep_dir]:
        directory.mkdir(parents=True)

    (transcript_dir / "video_BV123.md").write_text("# 视频文案\n\n## 完整文案\n\n这是一段测试文案。", encoding="utf-8")
    (single_dir / "BV123_文案风格.md").write_text("短句、反问、冲突开场。", encoding="utf-8")
    (tmp_dir / "BV123.mp4").write_text("video", encoding="utf-8")
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "BV123.visual_timeline.json").write_text(
        json.dumps(
            {
                "schema_version": "video-refiner.visual_timeline.v1",
                "video_id": "BV123",
                "duration_seconds": 12,
                "shots": [
                    {
                        "evidence_id": "video:BV123:shot:001",
                        "start_seconds": 0,
                        "end_seconds": 6,
                        "time_range": "00:00:00-00:00:06",
                        "keyframe": "原始数据/BV123_frames/frame_0001.jpg",
                        "segment_type": "detected_cut_segment",
                        "boundary_source": "detected_cut",
                        "boundary_confidence": "medium",
                        "visual_observation": {"visual_description": "人物讲解配合大标题。", "confidence": "medium"},
                    },
                    {
                        "evidence_id": "video:BV123:shot:002",
                        "start_seconds": 6,
                        "end_seconds": 12,
                        "time_range": "00:00:06-00:00:12",
                        "keyframe": "原始数据/BV123_frames/frame_0006.jpg",
                        "segment_type": "evidence_window",
                        "boundary_source": "uniform_coverage",
                        "boundary_confidence": "low",
                        "visual_observation": {"visual_description": "画面切换为案例素材。", "confidence": "high"},
                    },
                ],
                "quality": {
                    "shot_count": 2,
                    "visual_observations": "complete",
                    "observation_coverage": "complete",
                    "visual_confidence_summary": {"high": 1, "medium": 1, "low": 0},
                    "transcript_alignment": "timed",
                    "alignment_status": "timed",
                    "eligible_for_precise_timing": False,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    legacy_path = output_dir / "文案风格.md"
    legacy_path.write_text("旧版文案风格总结", encoding="utf-8")

    rows = [
        {
            "video_id": "BV123",
            "title": "测试标题",
            "platform": "bilibili",
            "url": "https://www.bilibili.com/video/BV123",
            "duration": 12,
            "published_at": "2026-06-19T08:30:00Z",
        }
    ]
    dimensions = [{"name": "文案风格", "prompt": "文案风格蒸馏.md", "output": "文案风格.md"}]
    materials = collect_video_materials(
        rows,
        single_dir,
        transcript_dir,
        tmp_dir,
        keep_dir,
        dimensions,
        500,
        evidence_dir=evidence_dir,
        require_visual_evidence=True,
    )
    data = normalize_benchmark_data(
        {
            "creator_profile_md": "# Creator Profile - 飞天闪客\n\n代表视频 BV123。" + ("具体定位与方法。" * 40),
            "pattern_library_md": "# Pattern Library - 飞天闪客\n\n代表视频 BV123。" + ("适用场景、结构和风险。" * 40),
            "qa_checklist_md": "# QA Checklist - 飞天闪客\n\n" + ("检查事实、脚本、视觉和风险。" * 40),
            "retrieval_pack_md": "# Retrieval Pack - 飞天闪客\n\n代表样本：videos/BV123.card.json\n\n## 完整文案\n\n" + ("原文" * 7000),
            "video_cards": [_valid_card("BV123")],
            "video_notes": {"BV123": _valid_notes("BV123")},
        },
        "飞天闪客",
        "bilibili",
        materials,
    )

    artifacts = write_benchmark_outputs(output_dir, "飞天闪客", "bilibili", data, materials, {"文案风格": legacy_path})

    card = json.loads((output_dir / "videos" / "BV123.card.json").read_text(encoding="utf-8"))
    for field in [
        "video_id",
        "platform",
        "creator",
        "source_url",
        "topic",
        "hook_type",
        "structure",
        "emotion_curve",
        "evidence_refs",
        "tags",
    ]:
        assert field in card
    assert card["topic"] == "用反常识问题解释复杂技术"
    assert card["published_at"] == "2026-06-19T08:30:00Z"
    assert card["duration_seconds"] == 90

    index = json.loads((output_dir / "retrieval_index.json").read_text(encoding="utf-8"))
    assert index["cards"][0]["card_path"] == "videos/BV123.card.json"
    assert index["cards"][0]["notes_path"] == "videos/BV123.notes.md"
    assert index["cards"][0]["published_at"] == "2026-06-19T08:30:00Z"
    assert index["cards"][0]["duration_seconds"] == 90
    assert index["cards"][0]["evidence_summary"] == {
        "segmentCount": 2,
        "detectedCutSegmentCount": 1,
        "observationCoverage": "complete",
        "alignmentStatus": "timed",
        "eligibleForPreciseTiming": False,
        "confidence": {"high": 1, "medium": 1, "low": 0},
    }

    refs = json.loads((output_dir / "raw" / "refs.json").read_text(encoding="utf-8"))
    retrieval_pack = (output_dir / "retrieval_pack.md").read_text(encoding="utf-8")
    assert "原文" * 100 not in retrieval_pack
    assert "不允许包含完整 raw transcript" in retrieval_pack
    assert len(retrieval_pack) < 13000
    assert refs["policy"] == "references_only"
    assert refs["videos"][0]["evidence_id"] == "video:BV123:source"
    assert refs["single_analysis"][0]["dimensions"][0]["evidence_id"].startswith("video:BV123:analysis:")
    assert "/Users/" not in json.dumps(refs, ensure_ascii=False)
    assert not (output_dir / "raw" / "videos").exists()
    assert (output_dir / "legacy" / "文案风格.md").exists()
    assert {artifact["kind"] for artifact in artifacts} >= {"benchmark_profile", "video_card", "retrieval_index", "raw_refs", "legacy_output"}


def test_parse_benchmark_json_accepts_code_fence() -> None:
    assert parse_benchmark_json('```json\n{"creator_profile_md":"ok"}\n```')["creator_profile_md"] == "ok"


def test_benchmark_video_cards_do_not_require_markdown_notes() -> None:
    material = VideoMaterial(
        video_id="123",
        title="测试标题",
        platform="douyin",
        source_url="https://www.douyin.com/video/123",
        published_at="2026-06-19T08:30:00Z",
        duration_seconds=90,
        transcript_path=Path("文案/video_123.md"),
        raw_transcript_path=Path("原始数据/123_raw.txt"),
        video_path=Path("原始数据/123.mp4"),
        kept_video_path=Path("视频保留/123.mp4"),
        analysis_paths={},
        transcript_excerpt="这是一段文案摘录。",
        analysis_excerpts={},
    )

    data = normalize_video_cards_data({"video_cards": [_valid_card("123")]}, "飞天闪客", "douyin", [material])

    assert data["video_cards"][0]["video_id"] == "123"
    with pytest.raises(ValueError, match="Notes 过短"):
        validate_video_batch_data({**data, "video_notes": {"123": ""}}, [material])


def test_benchmark_model_failure_is_not_converted_to_fallback() -> None:
    class FakeLlm:
        def chat_text(self, *_args: object, **_kwargs: object) -> str:
            return "The request was rejected because it was considered high risk"

    runner = object.__new__(PipelineRunner)
    runner.llm = FakeLlm()
    runner.profile = {"merge_model": "mock-model", "max_tokens": 8192, "supports_reasoning": False}
    runner._log = lambda *_args: None

    with pytest.raises(RuntimeError, match="连续两次未通过校验"):
        runner._benchmark_json_call("prompt", "视频卡片批次 1", lambda parsed: parsed)


def test_export_videoautomation_package_copies_only_lightweight_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "飞天闪客"
    legacy_dir = output_dir / "legacy"
    _write_valid_export_source(output_dir)
    for directory in [legacy_dir, output_dir / "文案", output_dir / "视频保留"]:
        directory.mkdir(parents=True)
    (output_dir / "videos" / "._123.card.json").write_text("AppleDouble", encoding="utf-8")
    (legacy_dir / "文案风格.md").write_text("旧版文档", encoding="utf-8")
    (output_dir / "文案" / "video_123.md").write_text("完整文案", encoding="utf-8")
    (output_dir / "视频保留" / "123.mp4").write_text("video", encoding="utf-8")

    result = export_videoautomation_package(output_dir, requested_video_count=1)
    export_dir = Path(result["path"])

    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["productName"] == "VideoAutomation 起号基底包"
    assert manifest["creator"] == "飞天闪客"
    assert manifest["platform"] == "douyin"
    assert manifest["videoCount"] == 1
    assert manifest["schemaVersion"] == "videoautomation.creator_base.v1"
    validation = json.loads((export_dir / "validation_report.json").read_text(encoding="utf-8"))
    assert validation["status"] == "passed"
    assert validation["validVideoCount"] == 1
    assert (export_dir / "retrieval_pack.md").exists()
    assert (export_dir / "videos" / "123.card.json").exists()
    assert (export_dir / "videos" / "123.notes.md").exists()
    assert (export_dir / "legacy" / "文案风格.md").exists()
    assert not (export_dir / "文案").exists()
    assert not (export_dir / "单视频分析").exists()
    assert not list(export_dir.rglob("*.mp4"))
    assert not list(export_dir.rglob("._*"))

    refs = json.loads((export_dir / "raw" / "refs.json").read_text(encoding="utf-8"))
    assert refs["api_key"] == "[已移除：敏感字段]"
    assert refs["videos"][0]["download_path"] == "[已省略：导出包不携带原始文件路径]"
    assert refs["single_analysis"][0]["paths"]["文案风格"] == "[已省略：导出包不携带单视频分析路径]"


def test_export_videoautomation_package_includes_lightweight_visual_timeline_only(tmp_path: Path) -> None:
    output_dir = tmp_path / "飞天闪客"
    _write_valid_export_source(output_dir)
    video_id = "123"
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir()
    timeline = {
        "schema_version": "video-refiner.visual_timeline.v1",
        "video_id": video_id,
        "duration_seconds": 90,
        "shots": [
            {
                "evidence_id": f"video:{video_id}:shot:001",
                "start_seconds": 0,
                "end_seconds": 10,
                "time_range": "00:00:00-00:00:10",
                "keyframe": "原始数据/123_frames/frame_0001.jpg",
                "segment_type": "evidence_window",
                "boundary_source": "uniform_coverage",
                "boundary_confidence": "low",
                "visual_observation": {"visual_description": "画面中可见人物与字幕。"},
            }
        ],
        "quality": {
            "visual_observations": "complete",
            "observation_coverage": "complete",
            "visual_confidence_summary": {"high": 0, "medium": 0, "low": 1},
            "transcript_alignment": "timed",
            "alignment_status": "timed",
            "eligible_for_precise_timing": False,
        },
    }
    (evidence_dir / f"{video_id}.visual_timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    card = _valid_card(video_id)
    card["evidence_refs"] = [*card["evidence_refs"], f"video:{video_id}:shot:001"]
    card["visual_timeline_ref"] = f"evidence/{video_id}.visual_timeline.json"
    card["evidence_coverage"] = {
        "shot_count": 1,
        "detected_cut_segment_count": 0,
        "transcript_alignment": "timed",
        "visual_observations": "complete",
        "observation_coverage": "complete",
        "visual_confidence_summary": {"high": 0, "medium": 0, "low": 1},
        "alignment_status": "timed",
        "eligible_for_precise_timing": False,
    }
    (output_dir / "videos" / f"{video_id}.card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    result = export_videoautomation_package(output_dir, requested_video_count=1)
    export_dir = Path(result["path"])

    assert (export_dir / "evidence" / f"{video_id}.visual_timeline.json").is_file()
    assert not list(export_dir.rglob("*.jpg"))
    assert result["manifest"]["evidenceTimelineCount"] == 1


def test_export_warns_when_visual_timeline_is_not_eligible_for_precise_timing(tmp_path: Path) -> None:
    output_dir = tmp_path / "飞天闪客"
    _write_valid_export_source(output_dir)
    video_id = "123"
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir()
    timeline = {
        "schema_version": "video-refiner.visual_timeline.v1",
        "video_id": video_id,
        "duration_seconds": 90,
        "shots": [
            {
                "evidence_id": f"video:{video_id}:shot:001",
                "start_seconds": 0,
                "end_seconds": 10,
                "time_range": "00:00:00-00:00:10",
                "keyframe": "原始数据/123_frames/frame_0001.jpg",
                "segment_type": "evidence_window",
                "boundary_source": "uniform_coverage",
                "boundary_confidence": "low",
                "visual_observation": {"visual_description": "画面中可见人物与字幕。", "confidence": "low"},
            }
        ],
        "quality": {
            "visual_observations": "complete",
            "observation_coverage": "complete",
            "visual_confidence_summary": {"high": 0, "medium": 0, "low": 1},
            "transcript_alignment": "coarse",
            "alignment_status": "coarse",
            "eligible_for_precise_timing": False,
        },
    }
    (evidence_dir / f"{video_id}.visual_timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    card = _valid_card(video_id)
    card["evidence_refs"] = [*card["evidence_refs"], f"video:{video_id}:shot:001"]
    card["visual_timeline_ref"] = f"evidence/{video_id}.visual_timeline.json"
    card["evidence_coverage"] = {
        "shot_count": 1,
        "visual_observations": "complete",
        "observation_coverage": "complete",
        "alignment_status": "coarse",
        "eligible_for_precise_timing": False,
    }
    (output_dir / "videos" / f"{video_id}.card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    result = export_videoautomation_package(output_dir, requested_video_count=1)

    assert result["validation"]["status"] == "passed"
    assert any("不适用于精确时序" in warning for warning in result["validation"]["warnings"])


def test_export_videoautomation_package_recovers_moved_output_dir(tmp_path: Path) -> None:
    old_output_dir = tmp_path / "old" / "飞天闪客"
    current_root = tmp_path / "current"
    output_dir = current_root / "飞天闪客"
    _write_valid_export_source(output_dir)

    result = export_videoautomation_package(old_output_dir, (current_root,), requested_video_count=1)

    assert Path(result["path"]).parent == output_dir


def test_export_videoautomation_package_blocks_placeholder_cards(tmp_path: Path) -> None:
    output_dir = tmp_path / "全球档案馆"
    _write_valid_export_source(output_dir)
    bad_card = _valid_card("123")
    bad_card["hook_type"] = ""
    bad_card["structure"] = []
    bad_card["evidence_refs"] = [str(output_dir / "单视频分析" / "123_视频脚本.md")]
    (output_dir / "videos" / "123.card.json").write_text(json.dumps(bad_card, ensure_ascii=False), encoding="utf-8")
    (output_dir / "单视频分析" / "123_视频脚本.md").write_text(
        "The request was rejected because it was considered high risk",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="导出质量核验未通过"):
        export_videoautomation_package(output_dir, requested_video_count=1)

    failed_dirs = list(output_dir.glob("videoautomation_export_FAILED_*"))
    assert len(failed_dirs) == 1
    report = json.loads((failed_dirs[0] / "validation_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["failedDimensions"][0]["dimension"] == "视频脚本"
    assert report["absolutePathCount"] >= 1
    assert report["placeholderCount"] >= 2


def test_remove_benchmark_outputs_clears_stale_structured_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "全球档案馆"
    _write_valid_export_source(output_dir)
    legacy_dir = output_dir / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "文案风格.md").write_text("旧版文档", encoding="utf-8")

    remove_benchmark_outputs(output_dir)

    for name in [
        "creator_profile.md",
        "pattern_library.md",
        "qa_checklist.md",
        "retrieval_index.json",
        "retrieval_pack.md",
    ]:
        assert not (output_dir / name).exists()
    assert not (output_dir / "videos").exists()
    assert not (output_dir / "raw").exists()
    assert not legacy_dir.exists()
    assert (output_dir / "单视频分析").exists()


def test_remove_benchmark_outputs_ignores_disappearing_directory(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "全球档案馆"
    _write_valid_export_source(output_dir)
    original_rmtree = shutil.rmtree

    def flaky_rmtree(path: Path) -> None:
        if Path(path).name == "videos":
            raise FileNotFoundError(path)
        original_rmtree(path)

    monkeypatch.setattr("videorefiner_app.benchmark.shutil.rmtree", flaky_rmtree)

    remove_benchmark_outputs(output_dir)

    assert not (output_dir / "raw").exists()
    assert not (output_dir / "legacy").exists()
