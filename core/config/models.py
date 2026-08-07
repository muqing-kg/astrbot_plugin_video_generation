"""Configuration models."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..shared.constants import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_BLACKLIST_MESSAGE,
    DEFAULT_COMMON_PROMPT_ENHANCEMENT,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_DURATION_SECONDS,
    DEFAULT_ENABLE_PROMPT_ENHANCEMENT,
    DEFAULT_IMAGE_PROMPT_ENHANCEMENT,
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    DEFAULT_MAX_QUEUED_TASKS,
    DEFAULT_MAX_REFERENCE_IMAGES,
    DEFAULT_MAX_REFERENCE_SIZE_MB,
    DEFAULT_MAX_RUNNING_TASKS,
    DEFAULT_RATE_LIMIT_SECONDS,
    DEFAULT_RESOLUTION,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_START_TEMPLATE,
    DEFAULT_TEXT_PROMPT_ENHANCEMENT,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_NON_RETRYABLE_ERROR_KEYWORDS,
    DEFAULT_NON_RETRYABLE_STATUS_CODES,
)
from ..shared.types import AdapterConfig


@dataclass
class UsageSettings:
    rate_limit_seconds: int = DEFAULT_RATE_LIMIT_SECONDS
    enable_daily_limit: bool = False
    daily_limit_count: int = DEFAULT_DAILY_LIMIT
    max_reference_size_mb: int = DEFAULT_MAX_REFERENCE_SIZE_MB
    max_reference_images: int = DEFAULT_MAX_REFERENCE_IMAGES
    umo_blacklist: list[str] = field(default_factory=list)
    admin_bypass_limits: bool = True
    umo_whitelist: list[str] = field(default_factory=list)
    blacklist_block_message: str = DEFAULT_BLACKLIST_MESSAGE


@dataclass
class GenerationSettings:
    model: str = "grok-imagine-video"
    default_duration: int = DEFAULT_DURATION_SECONDS
    default_aspect_ratio: str = DEFAULT_ASPECT_RATIO
    default_resolution: str = DEFAULT_RESOLUTION
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    max_running_tasks: int = DEFAULT_MAX_RUNNING_TASKS
    max_queued_tasks: int = DEFAULT_MAX_QUEUED_TASKS
    debug_request_logging: bool = False
    show_user_error_details: bool = True
    non_retryable_status_codes: list[int] = field(
        default_factory=lambda: list(DEFAULT_NON_RETRYABLE_STATUS_CODES)
    )
    non_retryable_error_keywords: list[str] = field(
        default_factory=lambda: list(DEFAULT_NON_RETRYABLE_ERROR_KEYWORDS)
    )
    result_info_items: list[str] = field(default_factory=lambda: ["模型", "耗时"])
    start_task_message_template: str = DEFAULT_START_TEMPLATE
    auto_delete_after_send: bool = True
    enable_prompt_enhancement: bool = DEFAULT_ENABLE_PROMPT_ENHANCEMENT
    common_prompt_enhancement: str = DEFAULT_COMMON_PROMPT_ENHANCEMENT
    image_prompt_enhancement: str = DEFAULT_IMAGE_PROMPT_ENHANCEMENT
    text_prompt_enhancement: str = DEFAULT_TEXT_PROMPT_ENHANCEMENT


@dataclass
class PlatformSettings:
    """Platform discrimination settings.

    QQ and WeChat may both be attached through the aiocqhttp adapter, and the
    AstrBot platform instance name is user-changeable. Pinning bot account ids
    (self_id) gives a stable discriminator that survives instance renames.
    """

    qq_self_ids: list[str] = field(default_factory=list)
    wechat_self_ids: list[str] = field(default_factory=list)


@dataclass
class PluginConfig:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    usage: UsageSettings = field(default_factory=UsageSettings)
    generation: GenerationSettings = field(default_factory=GenerationSettings)
    platform: PlatformSettings = field(default_factory=PlatformSettings)
