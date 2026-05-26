from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from .config import AppConfig, REPO_ROOT


def _command_check(name: str, command: str, required: bool = True) -> dict:
    path = shutil.which(command)
    if not path:
        return {"name": name, "ok": False, "detail": f"未找到命令：{command}", "required": required}
    return {"name": name, "ok": True, "detail": path, "required": required}


def _python_import_check(name: str, module: str, required: bool = True) -> dict:
    spec = importlib.util.find_spec(module)
    if spec:
        return {"name": name, "ok": True, "detail": module, "required": required}
    return {"name": name, "ok": False, "detail": f"当前后端 Python 未安装 {module}", "required": required}


def _external_python_import_check(name: str, python_bin: str, module: str, required: bool = True) -> dict:
    try:
        result = subprocess.run(
            [python_bin, "-c", f"import {module}; print('OK')"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        ok = result.returncode == 0
        detail = "OK" if ok else (result.stderr or result.stdout).strip()[:300]
        return {"name": name, "ok": ok, "detail": detail, "required": required}
    except Exception as exc:
        return {"name": name, "ok": False, "detail": str(exc), "required": required}


def _opencli_check() -> dict:
    if not shutil.which("opencli"):
        return {"name": "OpenCLI", "ok": False, "detail": "未安装 opencli", "required": True}
    try:
        result = subprocess.run(["opencli", "doctor"], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        ok = ("Extension: connected" in output or "[OK] Extension:" in output) and (
            "Connectivity: connected" in output or "[OK] Connectivity:" in output
        )
        detail = "Chrome 扩展已连接" if ok else "OpenCLI 已安装，但 Chrome 扩展未连接；请按 OpenCLI 提示安装并连接 Chrome 扩展"
        return {"name": "OpenCLI", "ok": ok, "detail": detail, "required": True}
    except Exception as exc:
        return {"name": "OpenCLI", "ok": False, "detail": str(exc), "required": True}


def _whisper_check(config: AppConfig) -> dict:
    model_path = Path(
        os.environ.get(
            "VIDEO_REFINER_WHISPER_MODEL_PATH",
            str(REPO_ROOT / "models" / "whisper" / "faster-whisper-tiny"),
        )
    ).expanduser()
    if model_path.exists():
        missing = [name for name in ["model.bin", "config.json", "tokenizer.json"] if not (model_path / name).exists()]
        if missing:
            return {
                "name": "Whisper",
                "ok": False,
                "detail": f"本地模型不完整：{model_path}，缺少 {', '.join(missing)}",
                "required": True,
            }
        code = """
from faster_whisper import WhisperModel
import os
model_path = os.environ["VIDEO_REFINER_PREFLIGHT_WHISPER_MODEL_PATH"]
WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=1)
print("OK")
"""
        env = {**os.environ, "VIDEO_REFINER_PREFLIGHT_WHISPER_MODEL_PATH": str(model_path)}
        try:
            result = subprocess.run([config.system_python, "-c", code], capture_output=True, text=True, timeout=60, env=env)
            ok = result.returncode == 0
            detail = f"本地模型可加载：{model_path}" if ok else (result.stderr or result.stdout).strip()[:300]
            return {"name": "Whisper", "ok": ok, "detail": detail, "required": True}
        except Exception as exc:
            return {"name": "Whisper", "ok": False, "detail": str(exc), "required": True}

    code = """
from faster_whisper import WhisperModel
import os
model = os.environ.get("VIDEO_REFINER_WHISPER_MODEL", "large-v3")
WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=1, local_files_only=True)
print("OK")
"""
    try:
        result = subprocess.run([config.system_python, "-c", code], capture_output=True, text=True, timeout=60, env=os.environ.copy())
        ok = result.returncode == 0
        detail = "本机 HuggingFace 缓存可加载" if ok else "Whisper 模型未缓存，首次运行会联网下载；portable 包应包含本地模型"
        return {"name": "Whisper", "ok": ok, "detail": detail, "required": True}
    except Exception as exc:
        return {"name": "Whisper", "ok": False, "detail": str(exc), "required": True}


def run_preflight(config: AppConfig) -> dict:
    checks = [
        _command_check("ffmpeg", config.ffmpeg_bin, True),
        _command_check("curl", "curl", True),
        _command_check("mcporter", "mcporter", True),
        _command_check("yt-dlp", "yt-dlp", False),
        _opencli_check(),
        _python_import_check("openai Python SDK", "openai", True),
        _whisper_check(config),
        _external_python_import_check("FunASR", config.system_python, "funasr", True),
        _external_python_import_check("RapidOCR", config.system_python, "rapidocr_onnxruntime", True),
        _external_python_import_check("Camoufox", config.camoufox_python, "camoufox", True),
    ]
    ok = all(item["ok"] for item in checks if item["required"])
    return {"ok": ok, "checks": checks, "config": asdict(config)}
