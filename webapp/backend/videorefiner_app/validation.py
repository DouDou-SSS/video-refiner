from __future__ import annotations

from typing import Any


def validate_model_profile_for_5d(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not profile.get("is_tested"):
        errors.append("请先测试并通过模型配置")
    if not profile.get("supports_vision"):
        errors.append("5 维炼化必须选择支持图片输入的模型")
    if not profile.get("analysis_model"):
        errors.append("缺少单视频蒸馏模型")
    if not profile.get("merge_model"):
        errors.append("缺少跨视频合并模型")
    return errors

