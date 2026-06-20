from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


DURATION_KEYS = (
    "duration_seconds",
    "duration",
    "video_duration",
    "length",
    "length_seconds",
)
DURATION_MS_KEYS = ("duration_ms", "video_duration_ms", "length_ms")
PUBLISHED_AT_KEYS = (
    "published_at",
    "publish_time",
    "published_time",
    "publish_timestamp",
    "create_time",
    "created_at",
    "timestamp",
    "release_timestamp",
    "upload_date",
    "pubdate",
    "date",
)


def extract_duration_seconds(*sources: Any) -> float | None:
    for source in sources:
        if isinstance(source, dict):
            for key in DURATION_KEYS:
                duration = _parse_duration(source.get(key))
                if duration is not None:
                    return duration
            for key in DURATION_MS_KEYS:
                duration = _parse_duration(source.get(key), milliseconds=True)
                if duration is not None:
                    return duration
        else:
            duration = _parse_duration(source)
            if duration is not None:
                return duration
    return None


def extract_published_at(*sources: Any) -> str | None:
    for source in sources:
        if isinstance(source, dict):
            for key in PUBLISHED_AT_KEYS:
                published_at = _parse_published_at(source.get(key))
                if published_at:
                    return published_at
        else:
            published_at = _parse_published_at(source)
            if published_at:
                return published_at
    return None


def _parse_duration(value: Any, milliseconds: bool = False) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if milliseconds else float(value)
        return round(seconds, 3) if seconds > 0 else None

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        seconds = float(text) / 1000 if milliseconds else float(text)
        return round(seconds, 3) if seconds > 0 else None
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
        parts = [float(part) for part in text.split(":")]
        if len(parts) == 2:
            seconds = parts[0] * 60 + parts[1]
        else:
            seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        return round(seconds, 3) if seconds > 0 else None
    return None


def _parse_published_at(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _epoch_to_iso(float(value))

    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "unknown"}:
        return None
    if re.fullmatch(r"\d{13}", text):
        return _epoch_to_iso(float(text) / 1000)
    if re.fullmatch(r"\d{10}", text):
        return _epoch_to_iso(float(text))
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", text):
        return text
    return text


def _epoch_to_iso(seconds: float) -> str | None:
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None
