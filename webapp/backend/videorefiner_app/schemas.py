from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class ProviderPresetOut(BaseModel):
    key: str
    provider_name: str
    base_url: str
    analysis_model: str
    merge_model: str
    supports_vision: bool
    supports_reasoning: bool
    max_tokens: int
    temperature: float


class ModelProfileIn(BaseModel):
    id: str | None = None
    provider_key: str = "custom"
    provider_name: str
    base_url: str
    api_key: str | None = None
    analysis_model: str
    merge_model: str
    supports_vision: bool = True
    supports_reasoning: bool = False
    max_tokens: int = Field(default=8192, ge=1, le=200000)
    temperature: float = Field(default=0.2, ge=0, le=2)


class ModelProfileOut(BaseModel):
    id: str
    provider_key: str
    provider_name: str
    base_url: str
    analysis_model: str
    merge_model: str
    supports_vision: bool
    supports_reasoning: bool
    max_tokens: int
    temperature: float
    key_storage: str | None = None
    is_tested: bool
    test_result: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class ModelTestOut(BaseModel):
    ok: bool
    text_ok: bool
    vision_ok: bool
    reasoning_ok: bool
    long_context_risk: str
    message: str
    errors: list[str] = []


class PreflightCheck(BaseModel):
    name: str
    ok: bool
    detail: str
    required: bool = True


class PreflightOut(BaseModel):
    ok: bool
    checks: list[PreflightCheck]


class JobCreateIn(BaseModel):
    input_type: Literal["single", "batch", "blogger"]
    inputs: list[str] = Field(min_length=1)
    model_profile_id: str
    output_dir: str | None = None
    max_videos: int = Field(default=50, ge=1, le=50)


class JobCleanupIn(BaseModel):
    categories: list[Literal["frames", "single_analysis", "kept_videos", "transcripts", "raw_data"]] = Field(min_length=1)


class JobOut(BaseModel):
    id: str
    status: str
    input_type: str
    output_dir: str
    model_profile_id: str
    max_videos: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class JobDetailOut(BaseModel):
    id: str
    status: str
    input_type: str
    output_dir: str
    model_profile_id: str
    max_videos: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    videos: list[dict[str, Any]]
    dimensions: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
