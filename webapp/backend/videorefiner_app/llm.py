from __future__ import annotations

import base64
import json
import struct
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import OpenAI


CAPABILITY_TEST_MAX_TOKENS = 256


def make_test_png_base64(size: int = 32) -> str:
    """Build a small valid PNG whose width and height pass multimodal API limits."""
    raw_rows = b"".join(b"\x00" + (b"\x2f\x91\xc7" * size) for _ in range(size))

    def chunk(name: bytes, data: bytes) -> bytes:
        body = name + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw_rows))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


class LLMClient:
    def __init__(
        self,
        profile: dict[str, Any],
        api_key: str,
        timeout_seconds: float = 600.0,
        rate_limit_retries: int = 2,
        rate_limit_retry_delay_seconds: int = 120,
        log: Callable[[str], None] | None = None,
    ):
        self.profile = profile
        self.client = OpenAI(api_key=api_key, base_url=profile["base_url"], timeout=timeout_seconds)
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_retry_delay_seconds = rate_limit_retry_delay_seconds
        self.log = log

    def chat_text(self, model: str, text: str, max_tokens: int | None = None, reasoning: bool = False) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens or int(self.profile.get("max_tokens") or 8192),
            "temperature": float(self.profile.get("temperature") or 0.2),
        }
        if reasoning:
            kwargs["extra_body"] = {"thinking": {"type": "enabled", "budget_tokens": 1024}}
        resp = self._create_with_rate_limit_retry(**kwargs)
        return resp.choices[0].message.content or ""

    def chat_multimodal(
        self,
        model: str,
        text_blocks: list[str],
        image_paths: list[Path],
        max_tokens: int | None = None,
    ) -> str:
        content: list[dict[str, Any]] = []
        for text in text_blocks:
            content.append({"type": "text", "text": text})
        for image_path in image_paths:
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        resp = self._create_with_rate_limit_retry(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens or int(self.profile.get("max_tokens") or 8192),
            temperature=float(self.profile.get("temperature") or 0.2),
        )
        return resp.choices[0].message.content or ""

    def _create_with_rate_limit_retry(self, **kwargs: Any) -> Any:
        for attempt in range(self.rate_limit_retries + 1):
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                if not _is_rate_limit_error(exc) or attempt >= self.rate_limit_retries:
                    raise
                delay = self.rate_limit_retry_delay_seconds * (attempt + 1)
                if self.log:
                    self.log(f"模型触发限流/额度保护，等待 {delay} 秒后自动重试（{attempt + 1}/{self.rate_limit_retries}）")
                time.sleep(delay)


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        getattr(exc, "status_code", None) == 429
        or "error code: 429" in text
        or "throttling" in text
        or "quota exceeded" in text
        or "rate limit" in text
    )


def _long_context_risk(profile: dict[str, Any]) -> str:
    max_tokens = int(profile.get("max_tokens") or 0)
    if max_tokens < 4096:
        return "high"
    if max_tokens < 8192:
        return "medium"
    return "low"


def test_model_profile(profile: dict[str, Any], api_key: str) -> dict[str, Any]:
    errors: list[str] = []
    text_ok = False
    vision_ok = False
    reasoning_ok = False
    client = LLMClient(profile, api_key, timeout_seconds=30.0, rate_limit_retries=0)

    try:
        out = client.chat_text(
            profile["analysis_model"],
            "回复 OK，用于测试连接。",
            max_tokens=CAPABILITY_TEST_MAX_TOKENS,
        )
        text_ok = bool(out.strip())
    except Exception as exc:
        errors.append(f"文本调用失败：{exc}")

    if text_ok and bool(profile.get("supports_vision")):
        try:
            content = [
                {"type": "text", "text": "这是一张 32x32 测试图。请回复 OK。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{make_test_png_base64()}"}},
            ]
            resp = client.client.chat.completions.create(
                model=profile["analysis_model"],
                messages=[{"role": "user", "content": content}],
                max_tokens=CAPABILITY_TEST_MAX_TOKENS,
                temperature=0,
            )
            vision_ok = bool(resp.choices[0].message.content)
        except Exception as exc:
            errors.append(f"图片输入失败：{exc}")
    elif not profile.get("supports_vision"):
        errors.append("该配置声明不支持图片输入，不能运行 6 维炼化中的单视频帧图分析。")

    if text_ok and bool(profile.get("supports_reasoning")):
        try:
            out = client.chat_text(
                profile["merge_model"],
                "回复 OK，用于测试 reasoning 参数。",
                max_tokens=CAPABILITY_TEST_MAX_TOKENS,
                reasoning=True,
            )
            reasoning_ok = bool(out.strip())
        except Exception as exc:
            errors.append(f"reasoning 参数不可用，运行时会自动关闭：{exc}")
    else:
        reasoning_ok = False

    ok = text_ok and vision_ok
    return {
        "ok": ok,
        "text_ok": text_ok,
        "vision_ok": vision_ok,
        "reasoning_ok": reasoning_ok,
        "long_context_risk": _long_context_risk(profile),
        "message": "模型配置可用于 6 维炼化" if ok else "模型配置未通过必需能力检测",
        "errors": errors,
    }


def parse_test_result(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
