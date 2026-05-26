from __future__ import annotations

from pathlib import Path

from videorefiner_app.adapters import detect_platform, extract_video_id, normalize_video_url, parse_inputs, safe_path_name
from videorefiner_app.cleanup import cleanup_outputs, collect_cleanup_targets, estimate_cleanup_outputs
from videorefiner_app.config import AppConfig
from videorefiner_app.db import Database
from videorefiner_app.llm import make_test_png_base64
from videorefiner_app.media_worker import _ocr_hotwords, _subtitle_text_from_file
from videorefiner_app.providers import PROVIDER_PRESETS
from videorefiner_app.security import SecretStore
from videorefiner_app.validation import validate_model_profile_for_5d


def test_provider_presets_cover_required_options() -> None:
    keys = {item.key for item in PROVIDER_PRESETS}
    assert {"bailian", "openai", "deepseek", "openrouter", "custom"} <= keys


def test_url_parsing_and_normalization() -> None:
    assert detect_platform("7635684541159460142") == "douyin"
    assert normalize_video_url("7635684541159460142").endswith("/7635684541159460142")
    assert extract_video_id("https://www.bilibili.com/video/BV1xx411c7mD") == "BV1xx411c7mD"
    rows = parse_inputs("batch", ["7635684541159460142", "BV1xx411c7mD"], 50, lambda *_: None)
    assert [row["platform"] for row in rows] == ["douyin", "bilibili"]


def test_safe_path_name_keeps_blogger_name_readable() -> None:
    assert safe_path_name('飞天闪客 / AI: Agent?') == "飞天闪客 _ AI_ Agent"
    assert safe_path_name("   ") == "未命名博主"


def test_text_only_profile_is_blocked_for_5d() -> None:
    errors = validate_model_profile_for_5d(
        {
            "is_tested": True,
            "supports_vision": False,
            "analysis_model": "deepseek-chat",
            "merge_model": "deepseek-chat",
        }
    )
    assert any("图片输入" in item for item in errors)


def test_model_test_image_meets_size_requirement() -> None:
    import base64
    import struct

    png = base64.b64decode(make_test_png_base64())
    width, height = struct.unpack(">II", png[16:24])
    assert width == 32
    assert height == 32
    assert width > 10 and height > 10


def test_subtitle_parser_removes_timing_lines(tmp_path: Path) -> None:
    subtitle = tmp_path / "demo.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n这是真正文案\n\n2\n00:00:01,000 --> 00:00:02,000\n不是时间轴\n",
        encoding="utf-8",
    )
    assert _subtitle_text_from_file(subtitle) == "这是真正文案 不是时间轴"


def test_ocr_hotwords_filters_obvious_noise() -> None:
    hotwords = _ocr_hotwords("@飞天闪客 2026新高考真题全刷基础2000题 大模型时代到来 数学选择秘杀三连领取")
    assert "飞天闪客" not in hotwords
    assert "三连领取" not in hotwords
    assert "大模型时代到来" in hotwords


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
