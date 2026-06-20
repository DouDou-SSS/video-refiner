from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    provider_name: str
    base_url: str
    analysis_model: str
    merge_model: str
    supports_vision: bool
    supports_reasoning: bool
    max_tokens: int
    temperature: float


PROVIDER_PRESETS: list[ProviderPreset] = [
    ProviderPreset(
        key="xiaomi_mimo",
        provider_name="小米 MiMo",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        analysis_model="mimo-v2.5",
        merge_model="mimo-v2.5-pro",
        supports_vision=True,
        supports_reasoning=False,
        max_tokens=8192,
        temperature=0.2,
    ),
    ProviderPreset(
        key="volcengine_ark",
        provider_name="火山方舟",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        analysis_model="ark-code-latest",
        merge_model="ark-code-latest",
        supports_vision=True,
        supports_reasoning=False,
        max_tokens=8192,
        temperature=0.2,
    ),
    ProviderPreset(
        key="bailian",
        provider_name="阿里云百炼",
        base_url="https://coding.dashscope.aliyuncs.com/v1",
        analysis_model="qwen3.6-plus",
        merge_model="qwen3.6-plus",
        supports_vision=True,
        supports_reasoning=True,
        max_tokens=8192,
        temperature=0.2,
    ),
    ProviderPreset(
        key="openai",
        provider_name="OpenAI",
        base_url="https://api.openai.com/v1",
        analysis_model="gpt-4.1",
        merge_model="gpt-4.1",
        supports_vision=True,
        supports_reasoning=False,
        max_tokens=8192,
        temperature=0.2,
    ),
    ProviderPreset(
        key="deepseek",
        provider_name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        analysis_model="deepseek-chat",
        merge_model="deepseek-chat",
        supports_vision=False,
        supports_reasoning=False,
        max_tokens=8192,
        temperature=0.2,
    ),
    ProviderPreset(
        key="openrouter",
        provider_name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        analysis_model="openai/gpt-4.1",
        merge_model="openai/gpt-4.1",
        supports_vision=True,
        supports_reasoning=False,
        max_tokens=8192,
        temperature=0.2,
    ),
    ProviderPreset(
        key="custom",
        provider_name="自定义 OpenAI-compatible",
        base_url="",
        analysis_model="",
        merge_model="",
        supports_vision=True,
        supports_reasoning=False,
        max_tokens=8192,
        temperature=0.2,
    ),
]


VISION_CAPABLE_PROVIDER_KEYS = frozenset(item.key for item in PROVIDER_PRESETS if item.supports_vision)
