from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"sk-sp-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(api[_-]?key['\"\s:=]+)([A-Za-z0-9_\-]{12,})", re.IGNORECASE),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def redact(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1***", text)
        else:
            text = pattern.sub("***", text)
    return text


def run_command(
    args: list[str],
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

