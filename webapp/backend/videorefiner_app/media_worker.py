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


def _ocr_bottom_frames(frames_dir: Path, bottom_ratio: float = 0.35) -> tuple[str, dict[str, Any]]:
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    lines: list[str] = []
    frame_count = 0
    used_count = 0
    ratio = min(max(bottom_ratio, 0.1), 0.6)

    with tempfile.TemporaryDirectory(prefix="video-refiner-ocr-") as tmp:
        tmp_dir = Path(tmp)
        for frame_path in sorted(frames_dir.glob("*.jpg")):
            frame_count += 1
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
                used_count += 1

    deduped = _dedupe_lines(lines)
    return " ".join(deduped).strip(), {
        "ocr_region": f"bottom_{ratio:.2f}",
        "ocr_frames": frame_count,
        "ocr_frames_with_text": used_count,
        "ocr_lines": len(deduped),
    }


def _ocr_hotwords(ocr_text: str, limit: int = 900) -> str:
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


def _whisper(video_path: Path, ocr_reference: str) -> tuple[str, str]:
    from faster_whisper import WhisperModel

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
            segments, _ = model.transcribe(
                str(video_path),
                language="zh",
                beam_size=int(os.environ.get("VIDEO_REFINER_WHISPER_BEAM_SIZE", "8")),
                best_of=int(os.environ.get("VIDEO_REFINER_WHISPER_BEST_OF", "8")),
                patience=float(os.environ.get("VIDEO_REFINER_WHISPER_PATIENCE", "1.2")),
                temperature=[0.0, 0.2, 0.4],
                condition_on_previous_text=True,
                vad_filter=False,
                no_speech_threshold=0.35,
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                initial_prompt=prompt or None,
                hotwords=hotwords or None,
            )
            model_label = Path(model_size).name if Path(model_size).exists() else model_size
            return " ".join(segment.text for segment in segments).strip(), model_label
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Whisper 转写失败：{last_error}")


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


def _write_sidecar(output: Path, suffix: str, text: str) -> str:
    path = output.with_name(f"{output.stem}_{suffix}.txt")
    path.write_text(text, encoding="utf-8")
    return str(path)


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
    soft_text = _soft_subtitles(video_path, output.parent, args.ffmpeg_bin)
    if soft_text:
        source = "软字幕"
        raw_text = soft_text
        meta["soft_subtitle_chars"] = len(soft_text)
        meta["soft_subtitle_path"] = _write_sidecar(output, "soft_subtitle", soft_text)
    else:
        ocr_text = ""
        try:
            ocr_text, ocr_meta = _ocr_bottom_frames(frames_dir)
            meta.update(ocr_meta)
        except Exception as exc:
            meta["ocr_error"] = str(exc)[:300]
        if ocr_text:
            meta["ocr_reference_chars"] = len(ocr_text)
            meta["ocr_reference_path"] = _write_sidecar(output, "ocr_reference", ocr_text)

        raw_text, model_size = _whisper(video_path, ocr_text)
        source = "Whisper高精度"
        if ocr_text:
            source += "+底部硬字幕OCR校对"
        meta["whisper_model"] = model_size
        meta["whisper_raw_chars"] = len(raw_text)
        meta["whisper_raw_path"] = _write_sidecar(output, "whisper_raw", raw_text)

    if not raw_text.strip():
        raise SystemExit("无任何文案来源")

    corrected = _punctuate(raw_text)
    output.write_text(corrected, encoding="utf-8")
    meta.update({"source": source, "chars": len(corrected)})
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
