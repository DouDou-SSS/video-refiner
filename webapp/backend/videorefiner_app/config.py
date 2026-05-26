from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
APP_HOME = Path(os.environ.get("VIDEO_REFINER_HOME", Path.home() / ".video-refiner"))
DEFAULT_CONFIG_PATH = Path(os.environ.get("VIDEO_REFINER_CONFIG", APP_HOME / "config.yaml"))


@dataclass(frozen=True)
class AppConfig:
    app_home: Path
    database_path: Path
    output_root: Path
    prompts_dir: Path
    system_python: str
    camoufox_python: str
    ffmpeg_bin: str
    daily_limit: int
    video_delay_min_ms: int
    video_delay_max_ms: int
    dimension_delay_min_ms: int
    dimension_delay_max_ms: int
    auto_retry_max_attempts: int
    auto_retry_delay_min_ms: int
    auto_retry_delay_max_ms: int
    frame_fps: int
    max_dimension_frames: int
    max_analysis_chars_per_video: int
    max_merge_chars_per_video: int


DEFAULTS: dict[str, Any] = {
    "database_path": str(APP_HOME / "video-refiner.sqlite3"),
    "output_root": str(Path.home() / "Desktop" / "视频炼化输出"),
    "prompts_dir": str(REPO_ROOT / "prompts"),
    "system_python": sys.executable,
    "camoufox_python": sys.executable,
    "ffmpeg_bin": "ffmpeg",
    "daily_limit": 50,
    "video_delay_min_ms": 3000,
    "video_delay_max_ms": 8000,
    "dimension_delay_min_ms": 10000,
    "dimension_delay_max_ms": 20000,
    "auto_retry_max_attempts": 3,
    "auto_retry_delay_min_ms": 60000,
    "auto_retry_delay_max_ms": 180000,
    "frame_fps": 1,
    "max_dimension_frames": 20,
    "max_analysis_chars_per_video": 60000,
    "max_merge_chars_per_video": 500,
}


def ensure_default_config(path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(DEFAULTS, f, allow_unicode=True, sort_keys=False)
    os.chmod(path, 0o600)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    ensure_default_config(path)
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    data = {**DEFAULTS, **loaded}
    env_overrides = {
        "prompts_dir": os.environ.get("VIDEO_REFINER_PROMPTS_DIR"),
        "system_python": os.environ.get("VIDEO_REFINER_SYSTEM_PYTHON"),
        "camoufox_python": os.environ.get("VIDEO_REFINER_CAMOUFOX_PYTHON"),
        "ffmpeg_bin": os.environ.get("VIDEO_REFINER_FFMPEG_BIN"),
    }
    data.update({key: value for key, value in env_overrides.items() if value})
    app_home = path.parent
    app_home.mkdir(parents=True, exist_ok=True)
    Path(data["output_root"]).expanduser().mkdir(parents=True, exist_ok=True)
    return AppConfig(
        app_home=app_home,
        database_path=Path(data["database_path"]).expanduser(),
        output_root=Path(data["output_root"]).expanduser(),
        prompts_dir=Path(data["prompts_dir"]).expanduser(),
        system_python=str(data["system_python"]),
        camoufox_python=str(data["camoufox_python"]),
        ffmpeg_bin=str(data["ffmpeg_bin"]),
        daily_limit=int(data["daily_limit"]),
        video_delay_min_ms=int(data["video_delay_min_ms"]),
        video_delay_max_ms=int(data["video_delay_max_ms"]),
        dimension_delay_min_ms=int(data["dimension_delay_min_ms"]),
        dimension_delay_max_ms=int(data["dimension_delay_max_ms"]),
        auto_retry_max_attempts=int(data["auto_retry_max_attempts"]),
        auto_retry_delay_min_ms=int(data["auto_retry_delay_min_ms"]),
        auto_retry_delay_max_ms=int(data["auto_retry_delay_max_ms"]),
        frame_fps=int(data["frame_fps"]),
        max_dimension_frames=int(data["max_dimension_frames"]),
        max_analysis_chars_per_video=int(data["max_analysis_chars_per_video"]),
        max_merge_chars_per_video=int(data["max_merge_chars_per_video"]),
    )
