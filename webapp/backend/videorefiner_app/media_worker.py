from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


SUBTITLE_SUFFIXES = [".srt", ".vtt", ".ass", ".ssa", ".txt"]
BUNDLED_WHISPER_MODEL = Path(__file__).resolve().parents[3] / "models" / "whisper" / "faster-whisper-tiny"


def _normalize_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{[^}]+\}", "", text)
    text = text.replace("\\N", " ").replace("\\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _dedupe_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    last = ""
    for raw in lines:
        line = _normalize_text(raw)
        if not line or line == last:
            continue
        if len(last) > 10 and len(line) > 10:
            common = len(set(line) & set(last)) / max(len(line), len(last))
            if common > 0.88:
                continue
        result.append(line)
        last = line
    return result


def _subtitle_text_from_file(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    lines: list[str] = []

    if path.suffix.lower() in {".ass", ".ssa"}:
        for line in raw.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) == 10:
                lines.append(parts[-1])
    else:
        for line in raw.splitlines():
            text = line.strip()
            if not text or text.upper() == "WEBVTT":
                continue
            if text.isdigit() or "-->" in text:
                continue
            lines.append(text)

    return " ".join(_dedupe_lines(lines)).strip()


def _paired_tool(ffmpeg_bin: str, tool: str) -> str:
    ffmpeg_path = Path(ffmpeg_bin)
    if ffmpeg_path.parent != Path(".") and ffmpeg_path.name == "ffmpeg":
        return str(ffmpeg_path.with_name(tool))
    return tool


def _sidecar_subtitles(video_path: Path) -> str:
    candidates = []
    for suffix in SUBTITLE_SUFFIXES:
        candidates.append(video_path.with_suffix(suffix))
        candidates.append(video_path.parent / f"{video_path.stem}.zh{suffix}")
        candidates.append(video_path.parent / f"{video_path.stem}.zh-CN{suffix}")
    for path in candidates:
        if not path.exists():
            continue
        text = _subtitle_text_from_file(path)
        if text:
            return text
    return ""


def _embedded_subtitles(video_path: Path, work_dir: Path, ffmpeg_bin: str) -> str:
    ffprobe_bin = _paired_tool(ffmpeg_bin, "ffprobe")
    probe = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        return ""
    try:
        streams = json.loads(probe.stdout or "{}").get("streams") or []
    except json.JSONDecodeError:
        streams = []
    if not streams:
        return ""

    for suffix in [".srt", ".vtt", ".ass"]:
        out_path = work_dir / f"{video_path.stem}_embedded{suffix}"
        result = subprocess.run(
            [ffmpeg_bin, "-y", "-i", str(video_path), "-map", "0:s:0", str(out_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and out_path.exists():
            text = _subtitle_text_from_file(out_path)
            if text:
                return text
    return ""


def _soft_subtitles(video_path: Path, work_dir: Path, ffmpeg_bin: str) -> str:
    return _sidecar_subtitles(video_path) or _embedded_subtitles(video_path, work_dir, ffmpeg_bin)


def _subtitle_timeline_from_file(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() in {".ass", ".ssa"}:
        result = []
        for line in raw.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) != 10:
                continue
            start = _subtitle_stamp_seconds(parts[1])
            end = _subtitle_stamp_seconds(parts[2])
            text = _normalize_text(parts[-1])
            if text and start is not None and end is not None:
                result.append({"start_seconds": start, "end_seconds": end, "text": text, "source": "soft_subtitle", "timing": "timed"})
        return result

    result: list[dict[str, Any]] = []
    block: list[str] = []
    for line in [*raw.splitlines(), ""]:
        if line.strip():
            block.append(line.strip())
            continue
        timing_index = next((index for index, value in enumerate(block) if "-->" in value), None)
        if timing_index is not None:
            parts = block[timing_index].split("-->", 1)
            start = _subtitle_stamp_seconds(parts[0])
            end = _subtitle_stamp_seconds(parts[1])
            text = _normalize_text(" ".join(block[timing_index + 1 :]))
            if text and start is not None and end is not None:
                result.append({"start_seconds": start, "end_seconds": end, "text": text, "source": "soft_subtitle", "timing": "timed"})
        block = []
    return result


def _subtitle_stamp_seconds(raw: str) -> float | None:
    match = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[,.](\d{1,3}))?", raw.strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int((match.group(4) or "0").ljust(3, "0")[:3])
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _soft_subtitle_timeline(video_path: Path, work_dir: Path) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    for suffix in SUBTITLE_SUFFIXES:
        candidates.extend(
            [
                video_path.with_suffix(suffix),
                video_path.parent / f"{video_path.stem}.zh{suffix}",
                video_path.parent / f"{video_path.stem}.zh-CN{suffix}",
                work_dir / f"{video_path.stem}_embedded{suffix}",
            ]
        )
    for path in candidates:
        if path.exists():
            segments = _subtitle_timeline_from_file(path)
            if segments:
                return segments
    return []


def _frame_interval_seconds(frames_dir: Path) -> float:
    try:
        meta = json.loads((frames_dir / "frames_meta.json").read_text(encoding="utf-8"))
        return max(float(meta.get("frame_interval_seconds") or 1), 0.1)
    except (OSError, ValueError, json.JSONDecodeError):
        return 1.0


def _frame_timestamp(frame_path: Path, interval_seconds: float) -> float:
    match = re.search(r"(\d+)(?=\.[^.]+$)", frame_path.name)
    return max(0.0, (int(match.group(1)) - 1) * interval_seconds) if match else 0.0


def _ocr_bottom_frames(frames_dir: Path, bottom_ratio: float = 0.35) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    lines: list[str] = []
    timeline: list[dict[str, Any]] = []
    used_count = 0
    ratio = min(max(bottom_ratio, 0.1), 0.6)
    interval_seconds = _frame_interval_seconds(frames_dir)
    frames = sorted(path for path in frames_dir.glob("*.jpg") if path.is_file() and not path.name.startswith("."))
    total_frames = len(frames)
    max_frames = int(os.environ.get("VIDEO_REFINER_OCR_MAX_FRAMES", "240"))
    if max_frames > 0 and total_frames > max_frames:
        step = (total_frames - 1) / max(max_frames - 1, 1)
        frames = [frames[round(index * step)] for index in range(max_frames)]

    with tempfile.TemporaryDirectory(prefix="video-refiner-ocr-") as tmp:
        tmp_dir = Path(tmp)
        for frame_path in frames:
            crop_path = tmp_dir / frame_path.name
            with Image.open(frame_path) as image:
                width, height = image.size
                top = int(height * (1 - ratio))
                image.crop((0, top, width, height)).save(crop_path, quality=90)
            result, _ = engine(str(crop_path))
            texts = [line[1] for line in (result or []) if len(line) > 1 and line[1]]
            frame_text = _normalize_text(" ".join(texts))
            if frame_text:
                lines.append(frame_text)
                timestamp = _frame_timestamp(frame_path, interval_seconds)
                timeline.append(
                    {
                        "start_seconds": timestamp,
                        "end_seconds": timestamp + interval_seconds,
                        "text": frame_text,
                        "source": "ocr",
                        "timing": "timed",
                    }
                )
                used_count += 1

    deduped = _dedupe_lines(lines)
    return " ".join(deduped).strip(), {
        "ocr_region": f"bottom_{ratio:.2f}",
        "ocr_frames": total_frames,
        "ocr_frames_sampled": len(frames),
        "ocr_frames_with_text": used_count,
        "ocr_lines": len(deduped),
    }, timeline


def _ocr_hotwords(ocr_text: str, limit: int = 300) -> str:
    cleaned = re.sub(r"[@#][\w\u4e00-\u9fff-]+", " ", ocr_text)
    cleaned = re.sub(r"[^\w\u4e00-\u9fff，。！？；：、 ]+", " ", cleaned)
    chunks = re.split(r"[，。！？；：、\s]+", cleaned)
    counter: Counter[str] = Counter()
    order: list[str] = []
    for chunk in chunks:
        item = chunk.strip()
        if len(item) < 2 or len(item) > 18:
            continue
        if item.isdigit() or any(noise in item for noise in ["飞天闪客", "点赞关注", "三连领取"]):
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", item):
            continue
        if item not in counter:
            order.append(item)
        counter[item] += 1

    ranked = sorted(order, key=lambda value: (-counter[value], order.index(value)))
    text = " ".join(ranked[:80])
    return text[:limit]


def _ocr_is_primary_source(ocr_text: str, ocr_meta: dict[str, Any]) -> bool:
    min_chars = int(os.environ.get("VIDEO_REFINER_OCR_PRIMARY_MIN_CHARS", "1200"))
    min_text_frames = int(os.environ.get("VIDEO_REFINER_OCR_PRIMARY_MIN_TEXT_FRAMES", "12"))
    min_text_frame_ratio = float(os.environ.get("VIDEO_REFINER_OCR_PRIMARY_MIN_FRAME_RATIO", "0.12"))
    sampled = int(ocr_meta.get("ocr_frames_sampled") or 0)
    frames_with_text = int(ocr_meta.get("ocr_frames_with_text") or 0)
    frame_ratio = frames_with_text / max(sampled, 1)
    return len(ocr_text.strip()) >= min_chars and frames_with_text >= min_text_frames and frame_ratio >= min_text_frame_ratio


def _whisper(video_path: Path, ocr_reference: str, ffmpeg_bin: str, segments_dir: Path) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    from faster_whisper import WhisperModel

    duration_seconds = _media_duration(video_path, ffmpeg_bin)
    long_video_seconds = int(os.environ.get("VIDEO_REFINER_LONG_VIDEO_SECONDS", "600"))
    segment_seconds = int(os.environ.get("VIDEO_REFINER_WHISPER_SEGMENT_SECONDS", "300"))
    bundled_model = Path(os.environ.get("VIDEO_REFINER_WHISPER_MODEL_PATH", str(BUNDLED_WHISPER_MODEL))).expanduser()
    if bundled_model.exists():
        requested_model = str(bundled_model)
        models = [requested_model]
    else:
        requested_model = os.environ.get("VIDEO_REFINER_WHISPER_MODEL", "large-v3")
        models = [requested_model]
    if "VIDEO_REFINER_WHISPER_MODEL" not in os.environ and not Path(requested_model).exists() and requested_model != "medium":
        models.append("medium")

    last_error: Exception | None = None
    hotwords = _ocr_hotwords(ocr_reference)
    prompt = ""
    if hotwords:
        prompt = "以下是视频底部字幕中可能出现的专有名词和短语，仅用于纠正错别字，转写必须以音频为准：" + hotwords

    for model_size in models:
        try:
            model = WhisperModel(
                model_size,
                device=os.environ.get("VIDEO_REFINER_WHISPER_DEVICE", "cpu"),
                compute_type=os.environ.get("VIDEO_REFINER_WHISPER_COMPUTE_TYPE", "int8"),
                cpu_threads=int(os.environ.get("VIDEO_REFINER_WHISPER_CPU_THREADS", "0")),
                local_files_only=os.environ.get("VIDEO_REFINER_WHISPER_LOCAL_ONLY", "0") == "1",
            )
            model_label = Path(model_size).name if Path(model_size).exists() else model_size
            if duration_seconds and duration_seconds > long_video_seconds:
                text, count, timeline = _transcribe_segmented(
                    model,
                    video_path,
                    ffmpeg_bin,
                    segments_dir,
                    duration_seconds,
                    max(60, segment_seconds),
                    prompt,
                    hotwords,
                )
                return text, f"{model_label}-segmented", {
                    "whisper_long_video": True,
                    "whisper_duration_seconds": round(duration_seconds, 2),
                    "whisper_segment_seconds": max(60, segment_seconds),
                    "whisper_segments": count,
                    "whisper_segments_dir": str(segments_dir),
                }, timeline
            text, whisper_mode, timeline = _transcribe_timed_with_fallback(model, video_path, prompt, hotwords)
            audio_fallback = False
            if not text.strip():
                with tempfile.TemporaryDirectory(prefix="video-refiner-whisper-audio-") as tmp:
                    audio_path = Path(tmp) / f"{video_path.stem}.wav"
                    _extract_audio(video_path, audio_path, ffmpeg_bin, duration_seconds)
                    text, whisper_mode, timeline = _transcribe_timed_with_fallback(model, audio_path, prompt, hotwords)
                    audio_fallback = True
            return text, model_label, {
                "whisper_long_video": False,
                "whisper_duration_seconds": round(duration_seconds, 2) if duration_seconds else None,
                "whisper_audio_fallback": audio_fallback,
                "whisper_decode_mode": whisper_mode,
            }, timeline
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Whisper 转写失败：{last_error}")


def _extract_audio(video_path: Path, audio_path: Path, ffmpeg_bin: str, duration_seconds: float | None) -> None:
    result = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        timeout=max(180, int((duration_seconds or 60) * 2)),
    )
    if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size <= 1024:
        raise RuntimeError((result.stderr or result.stdout or "Whisper 音频兜底提取失败").strip()[-1000:])


def _transcribe_with_fallback(model: Any, media_path: Path, prompt: str, hotwords: str) -> tuple[str, str]:
    try:
        return _transcribe_with_whisper(model, media_path, prompt, hotwords, "high"), "high"
    except Exception as exc:
        high_error = exc
    try:
        return _transcribe_with_whisper(model, media_path, "", "", "simple"), "simple"
    except Exception as simple_exc:
        raise RuntimeError(f"高精度参数失败：{high_error}; 简单参数失败：{simple_exc}") from simple_exc


def _transcribe_timed_with_fallback(
    model: Any,
    media_path: Path,
    prompt: str,
    hotwords: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    try:
        text, timeline = _transcribe_timed_with_whisper(model, media_path, prompt, hotwords, "high")
        return text, "high", timeline
    except Exception as exc:
        high_error = exc
    try:
        text, timeline = _transcribe_timed_with_whisper(model, media_path, "", "", "simple")
        return text, "simple", timeline
    except Exception as simple_exc:
        raise RuntimeError(f"高精度参数失败：{high_error}; 简单参数失败：{simple_exc}") from simple_exc


def _transcribe_timed_with_whisper(
    model: Any,
    video_path: Path,
    prompt: str,
    hotwords: str,
    mode: str,
) -> tuple[str, list[dict[str, Any]]]:
    if mode == "simple":
        kwargs = {"language": "zh", "beam_size": int(os.environ.get("VIDEO_REFINER_WHISPER_SIMPLE_BEAM_SIZE", "5"))}
    else:
        kwargs = {
            "language": "zh",
            "beam_size": int(os.environ.get("VIDEO_REFINER_WHISPER_BEAM_SIZE", "8")),
            "best_of": int(os.environ.get("VIDEO_REFINER_WHISPER_BEST_OF", "8")),
            "patience": float(os.environ.get("VIDEO_REFINER_WHISPER_PATIENCE", "1.2")),
            "temperature": [0.0, 0.2, 0.4],
            "condition_on_previous_text": True,
            "vad_filter": False,
            "no_speech_threshold": 0.35,
            "compression_ratio_threshold": 2.4,
            "log_prob_threshold": -1.0,
            "initial_prompt": prompt or None,
            "hotwords": hotwords or None,
        }
    segments, _ = model.transcribe(str(video_path), **kwargs)
    timeline: list[dict[str, Any]] = []
    for segment in segments:
        text = str(getattr(segment, "text", "") or "").strip()
        start = float(getattr(segment, "start", 0.0) or 0.0)
        end = float(getattr(segment, "end", start) or start)
        if text and end >= start:
            timeline.append({"start_seconds": start, "end_seconds": end, "text": text, "source": "whisper", "timing": "timed"})
    return " ".join(item["text"] for item in timeline).strip(), timeline


def _transcribe_with_whisper(model: Any, video_path: Path, prompt: str, hotwords: str, mode: str) -> str:
    if mode == "simple":
        kwargs = {
            "language": "zh",
            "beam_size": int(os.environ.get("VIDEO_REFINER_WHISPER_SIMPLE_BEAM_SIZE", "5")),
        }
    else:
        kwargs = {
            "language": "zh",
            "beam_size": int(os.environ.get("VIDEO_REFINER_WHISPER_BEAM_SIZE", "8")),
            "best_of": int(os.environ.get("VIDEO_REFINER_WHISPER_BEST_OF", "8")),
            "patience": float(os.environ.get("VIDEO_REFINER_WHISPER_PATIENCE", "1.2")),
            "temperature": [0.0, 0.2, 0.4],
            "condition_on_previous_text": True,
            "vad_filter": False,
            "no_speech_threshold": 0.35,
            "compression_ratio_threshold": 2.4,
            "log_prob_threshold": -1.0,
            "initial_prompt": prompt or None,
            "hotwords": hotwords or None,
        }
    segments, _ = model.transcribe(str(video_path), **kwargs)
    return " ".join(segment.text for segment in segments).strip()


def _transcribe_segmented(
    model: Any,
    video_path: Path,
    ffmpeg_bin: str,
    segments_dir: Path,
    duration_seconds: float,
    segment_seconds: int,
    prompt: str,
    hotwords: str,
) -> tuple[str, int, list[dict[str, Any]]]:
    segments_dir.mkdir(parents=True, exist_ok=True)
    segment_count = int((duration_seconds + segment_seconds - 1) // segment_seconds)
    parts: list[str] = []
    timeline: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="video-refiner-whisper-segments-") as tmp:
        tmp_dir = Path(tmp)
        for index in range(segment_count):
            start = index * segment_seconds
            length = min(segment_seconds, max(1, duration_seconds - start))
            text_path = segments_dir / f"segment_{index:03d}.txt"
            if text_path.exists():
                segment_text = text_path.read_text(encoding="utf-8").strip()
            else:
                audio_path = tmp_dir / f"segment_{index:03d}.wav"
                result = subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-ss",
                        str(start),
                        "-t",
                        str(length),
                        "-i",
                        str(video_path),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        str(audio_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=max(180, int(length * 2)),
                )
                if result.returncode != 0 or not audio_path.exists():
                    raise RuntimeError((result.stderr or result.stdout or f"分段音频提取失败：{index}").strip()[-1000:])
                segment_text, _ = _transcribe_with_fallback(model, audio_path, prompt, hotwords)
                text_path.write_text(segment_text, encoding="utf-8")
            parts.append(f"[{_format_stamp(start)}-{_format_stamp(start + length)}]\n{segment_text}")
            if segment_text.strip():
                timeline.append(
                    {
                        "start_seconds": start,
                        "end_seconds": start + length,
                        "text": segment_text.strip(),
                        "source": "whisper",
                        "timing": "timed",
                    }
                )
    return "\n\n".join(parts).strip(), segment_count, timeline


def _media_duration(video_path: Path, ffmpeg_bin: str) -> float | None:
    ffprobe_bin = _paired_tool(ffmpeg_bin, "ffprobe")
    try:
        result = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return float(json.loads(result.stdout or "{}").get("format", {}).get("duration") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _format_stamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _punctuate(text: str) -> str:
    if len(text) > 50000:
        return text
    try:
        from funasr import AutoModel

        model_path = os.environ.get(
            "FUNASR_PUNC_MODEL_PATH",
            os.path.expanduser("~/.cache/modelscope/hub/models/damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"),
        )
        model = AutoModel(model=model_path)
        result = model.generate(input=text)
        if isinstance(result, list):
            return result[0].get("text", text)
        return result.get("text", text)
    except Exception:
        return text


def _is_low_quality_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return True
    if "�" in compact:
        return True
    if len(compact) >= 20:
        counts = Counter(compact)
        most_common_ratio = counts.most_common(1)[0][1] / len(compact)
        unique_ratio = len(counts) / len(compact)
        if most_common_ratio > 0.45 or unique_ratio < 0.03:
            return True
    return bool(re.search(r"(.{1,3})\1{8,}", compact))


def _write_sidecar(output: Path, suffix: str, text: str) -> str:
    path = output.with_name(f"{output.stem}_{suffix}.txt")
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_timeline_sidecar(output: Path, segments: list[dict[str, Any]], source: str, duration_seconds: float | None) -> str:
    path = output.with_name(f"{output.stem}_timeline.json")
    path.write_text(
        json.dumps(
            {
                "schema_version": "video-refiner.transcript_timeline.v1",
                "source": source,
                "duration_seconds": duration_seconds,
                "segments": segments,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(path)


def _coarse_timeline(text: str, source: str, duration_seconds: float | None) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    return [
        {
            "start_seconds": 0,
            "end_seconds": float(duration_seconds or 0),
            "text": text.strip(),
            "source": source,
            "timing": "coarse",
        }
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="video-refiner media worker")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--video-path", required=True)
    parser.add_argument("--frames-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    args = parser.parse_args()

    video_path = Path(args.video_path)
    frames_dir = Path(args.frames_dir)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {"video_id": args.video_id, "extraction_version": 2}
    duration_seconds = _media_duration(video_path, args.ffmpeg_bin)
    soft_text = _soft_subtitles(video_path, output.parent, args.ffmpeg_bin)
    timeline_segments: list[dict[str, Any]] = []
    if soft_text:
        source = "软字幕"
        raw_text = soft_text
        timeline_segments = _soft_subtitle_timeline(video_path, output.parent)
        meta["soft_subtitle_chars"] = len(soft_text)
        meta["soft_subtitle_path"] = _write_sidecar(output, "soft_subtitle", soft_text)
    else:
        ocr_text = ""
        ocr_segments: list[dict[str, Any]] = []
        try:
            ocr_text, ocr_meta, ocr_segments = _ocr_bottom_frames(frames_dir)
            meta.update(ocr_meta)
        except Exception as exc:
            meta["ocr_error"] = str(exc)[:300]
        if ocr_text:
            meta["ocr_reference_chars"] = len(ocr_text)
            meta["ocr_reference_path"] = _write_sidecar(output, "ocr_reference", ocr_text)
        if ocr_text and _ocr_is_primary_source(ocr_text, meta):
            raw_text = ocr_text
            source = "底部硬字幕OCR主文案"
            timeline_segments = ocr_segments
            meta["ocr_primary_source"] = True
        else:
            segments_dir = output.parent / f"{args.video_id}_segments"
            try:
                raw_text, model_size, whisper_meta, timeline_segments = _whisper(video_path, ocr_text, args.ffmpeg_bin, segments_dir)
                source = "Whisper高精度"
                if ocr_text:
                    source += "+底部硬字幕OCR校对"
                meta["whisper_model"] = model_size
                meta.update(whisper_meta)
                meta["whisper_raw_chars"] = len(raw_text)
                meta["whisper_raw_path"] = _write_sidecar(output, "whisper_raw", raw_text)
                if ocr_text and _is_low_quality_text(raw_text):
                    raw_text = ocr_text
                    source = "底部硬字幕OCR兜底"
                    timeline_segments = ocr_segments
                    meta["whisper_low_quality_fallback"] = True
                    meta["fallback_reason"] = "Whisper returned low quality text; using bottom OCR text"
            except Exception as exc:
                if not ocr_text:
                    raise
                raw_text = ocr_text
                source = "底部硬字幕OCR兜底"
                timeline_segments = ocr_segments
                meta["whisper_error"] = str(exc)[-500:]
                meta["fallback_reason"] = "Whisper failed; using bottom OCR text"

    if not raw_text.strip():
        raise SystemExit("无任何文案来源")

    if _is_low_quality_text(raw_text):
        corrected = raw_text
        meta["low_quality_transcript"] = True
        meta["low_quality_reason"] = "repeated_or_garbled_text"
    else:
        corrected = _punctuate(raw_text)
    output.write_text(corrected, encoding="utf-8")
    if not timeline_segments:
        timeline_segments = _coarse_timeline(corrected, source, duration_seconds)
    meta.update(
        {
            "source": source,
            "chars": len(corrected),
            "transcript_timeline_path": _write_timeline_sidecar(output, timeline_segments, source, duration_seconds),
        }
    )
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
