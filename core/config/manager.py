"""Config manager for Grok video plugin."""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..shared.constants import (
    ABSOLUTE_MAX_REFERENCE_IMAGES,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_COMMON_PROMPT_ENHANCEMENT,
    DEFAULT_ENABLE_PROMPT_ENHANCEMENT,
    DEFAULT_IMAGE_PROMPT_ENHANCEMENT,
    DEFAULT_TEXT_PROMPT_ENHANCEMENT,
    DEFAULT_START_TEMPLATE,
    DEFAULT_DURATION_SECONDS,
    DEFAULT_MAX_REFERENCE_IMAGES,
    DEFAULT_NON_RETRYABLE_ERROR_KEYWORDS,
    DEFAULT_NON_RETRYABLE_STATUS_CODES,
    DEFAULT_RESOLUTION,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_DURATION_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_DURATION_SECONDS,
    SUPPORTED_ASPECT_RATIOS,
    SUPPORTED_RESOLUTIONS,
)
from ..shared.logging import log_prefix, safe_log_text
from ..shared.types import AdapterConfig
from .models import GenerationSettings, PlatformSettings, PluginConfig, UsageSettings

LOG = log_prefix("Config")


def _as_int(
    value: Any,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("\n", ",").split(",")]
        return [item for item in items if item]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                # AstrBot list editors sometimes store objects; keep common text fields.
                text = str(
                    item.get("value")
                    or item.get("text")
                    or item.get("name")
                    or item.get("label")
                    or ""
                ).strip()
            else:
                text = str(item).strip()
            if text:
                result.append(text)
        return result
    item = str(value).strip()
    return [item] if item else []


def _as_int_list(value: Any, default: list[int]) -> list[int]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    result: list[int] = []
    for item in raw_items:
        try:
            if isinstance(item, dict):
                item = item.get("value", item.get("id"))
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result or list(default)


def _section(raw: Any, name: str) -> dict[str, Any]:
    """Read one config section safely from AstrBotConfig or dict-like objects."""
    value: Any = {}
    try:
        if raw is None:
            value = {}
        elif isinstance(raw, dict):
            value = raw.get(name, {})
        elif hasattr(raw, "get"):
            value = raw.get(name, {})
        else:
            value = getattr(raw, name, {})
    except Exception as exc:
        logger.warning(f"{LOG} 读取配置段 {name} 失败: {safe_log_text(exc)}")
        value = {}

    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    # Some builds may expose mapping-like objects.
    if hasattr(value, "items") and not isinstance(value, (str, bytes, list, tuple)):
        try:
            return dict(value.items())
        except Exception:
            pass
    logger.warning(f"{LOG} 配置项 {name} 格式错误（期望对象），已按空对象处理: {type(value).__name__}")
    return {}


def _get(cfg: dict[str, Any], key: str, default: Any = None) -> Any:
    if not isinstance(cfg, dict):
        return default
    return cfg.get(key, default)


class ConfigManager:
    """Load Grok video plugin settings from AstrBotConfig."""

    def __init__(self, raw_config: Any):
        self.raw = raw_config
        self.config = self._parse(self.raw)

    def reload(self, raw_config: Any | None = None) -> None:
        if raw_config is not None:
            self.raw = raw_config
        self.config = self._parse(self.raw)

    @property
    def adapter(self) -> AdapterConfig:
        return self.config.adapter

    @property
    def usage(self) -> UsageSettings:
        return self.config.usage

    @property
    def generation(self) -> GenerationSettings:
        return self.config.generation

    @property
    def platform(self) -> PlatformSettings:
        return self.config.platform

    def _parse(self, raw: Any) -> PluginConfig:
        provider = _section(raw, "provider")
        generation_raw = _section(raw, "generation")
        runtime_raw = _section(raw, "runtime")
        usage_raw = _section(raw, "usage")
        platform_raw = _section(raw, "platform")

        timeout = _as_int(
            _get(
                runtime_raw,
                "timeout_seconds",
                _get(provider, "timeout", DEFAULT_TIMEOUT_SECONDS),
            ),
            DEFAULT_TIMEOUT_SECONDS,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        )
        retries = _as_int(
            _get(
                runtime_raw,
                "retry_attempts",
                _get(provider, "retry_attempts", DEFAULT_RETRY_ATTEMPTS),
            ),
            DEFAULT_RETRY_ATTEMPTS,
            minimum=0,
            maximum=10,
        )

        model = str(
            _get(generation_raw, "model")
            or _get(provider, "model")
            or "grok-imagine-video"
        ).strip() or "grok-imagine-video"

        aspect = str(_get(generation_raw, "default_aspect_ratio") or DEFAULT_ASPECT_RATIO).strip()
        if aspect not in SUPPORTED_ASPECT_RATIOS:
            aspect = DEFAULT_ASPECT_RATIO

        resolution = str(
            _get(generation_raw, "default_resolution") or DEFAULT_RESOLUTION
        ).strip().lower()
        if resolution not in SUPPORTED_RESOLUTIONS:
            resolution = DEFAULT_RESOLUTION

        duration = _as_int(
            _get(generation_raw, "default_duration", DEFAULT_DURATION_SECONDS),
            DEFAULT_DURATION_SECONDS,
            minimum=MIN_DURATION_SECONDS,
            maximum=MAX_DURATION_SECONDS,
        )
        max_refs = _as_int(
            _get(usage_raw, "max_reference_images", DEFAULT_MAX_REFERENCE_IMAGES),
            DEFAULT_MAX_REFERENCE_IMAGES,
            minimum=1,
            maximum=ABSOLUTE_MAX_REFERENCE_IMAGES,
        )

        keywords = _as_str_list(_get(runtime_raw, "non_retryable_error_keywords"))
        if not keywords:
            keywords = list(DEFAULT_NON_RETRYABLE_ERROR_KEYWORDS)

        adapter = AdapterConfig(
            base_url=str(_get(provider, "base_url") or "").strip(),
            api_key=str(_get(provider, "api_key") or "").strip(),
            model=model,
            timeout=timeout,
            max_retry_attempts=max(1, retries + 1),
            debug_request_logging=_as_bool(_get(runtime_raw, "debug_request_logging"), False),
            show_user_error_details=_as_bool(
                _get(runtime_raw, "show_user_error_details"), True
            ),
            non_retryable_status_codes=_as_int_list(
                _get(runtime_raw, "non_retryable_status_codes"),
                list(DEFAULT_NON_RETRYABLE_STATUS_CODES),
            ),
            non_retryable_error_keywords=keywords,
            proxy=(str(_get(provider, "proxy") or "").strip() or None),
        )

        generation = GenerationSettings(
            model=model,
            default_duration=duration,
            default_aspect_ratio=aspect,
            default_resolution=resolution,
            timeout_seconds=timeout,
            retry_attempts=retries,
            max_concurrent_requests=_as_int(
                _get(runtime_raw, "max_concurrent_requests"), 3, 1, 20
            ),
            max_running_tasks=_as_int(_get(runtime_raw, "max_running_tasks"), 3, 1, 20),
            max_queued_tasks=_as_int(_get(runtime_raw, "max_queued_tasks"), 20, 0, 200),
            debug_request_logging=adapter.debug_request_logging,
            show_user_error_details=adapter.show_user_error_details,
            non_retryable_status_codes=list(adapter.non_retryable_status_codes),
            non_retryable_error_keywords=list(adapter.non_retryable_error_keywords),
            result_info_items=_as_str_list(_get(generation_raw, "result_info_items"))
            or ["模型", "耗时"],
            start_task_message_template=str(
                _get(generation_raw, "start_task_message_template")
                or DEFAULT_START_TEMPLATE
            ),
            auto_delete_after_send=_as_bool(
                _get(generation_raw, "auto_delete_after_send"), True
            ),
            enable_prompt_enhancement=_as_bool(
                _get(generation_raw, "enable_prompt_enhancement"),
                DEFAULT_ENABLE_PROMPT_ENHANCEMENT,
            ),
            common_prompt_enhancement=str(
                _get(generation_raw, "common_prompt_enhancement")
                or DEFAULT_COMMON_PROMPT_ENHANCEMENT
            ),
            image_prompt_enhancement=str(
                _get(generation_raw, "image_prompt_enhancement")
                or DEFAULT_IMAGE_PROMPT_ENHANCEMENT
            ),
            text_prompt_enhancement=str(
                _get(generation_raw, "text_prompt_enhancement")
                or DEFAULT_TEXT_PROMPT_ENHANCEMENT
            ),
        )

        usage = UsageSettings(
            rate_limit_seconds=_as_int(_get(usage_raw, "rate_limit_seconds"), 0, 0, 86400),
            enable_daily_limit=_as_bool(_get(usage_raw, "enable_daily_limit"), False),
            daily_limit_count=_as_int(_get(usage_raw, "daily_limit_count"), 10, 1, 100000),
            max_reference_size_mb=_as_int(
                _get(usage_raw, "max_reference_size_mb"), 10, 1, 100
            ),
            max_reference_images=max_refs,
            umo_blacklist=_as_str_list(_get(usage_raw, "umo_blacklist")),
            admin_bypass_limits=_as_bool(_get(usage_raw, "admin_bypass_limits"), True),
            umo_whitelist=_as_str_list(_get(usage_raw, "umo_whitelist")),
            blacklist_block_message=str(
                _get(usage_raw, "blacklist_block_message")
                or "❌ 当前会话已被加入黑名单，无法使用视频功能"
            ),
        )
        platform = PlatformSettings(
            qq_self_ids=_as_str_list(_get(platform_raw, "qq_self_ids")),
            wechat_self_ids=_as_str_list(_get(platform_raw, "wechat_self_ids")),
        )
        return PluginConfig(
            adapter=adapter,
            usage=usage,
            generation=generation,
            platform=platform,
        )


