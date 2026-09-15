"""Config manager for the video generation plugin."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from astrbot.api import logger

from ..shared.constants import (
    ABSOLUTE_MAX_REFERENCE_IMAGES,
    DEFAULT_COMMON_PROMPT_ENHANCEMENT,
    DEFAULT_ENABLE_PROMPT_ENHANCEMENT,
    DEFAULT_IMAGE_PROMPT_ENHANCEMENT,
    DEFAULT_TEXT_PROMPT_ENHANCEMENT,
    DEFAULT_START_TEMPLATE,
    DEFAULT_MAX_REFERENCE_IMAGES,
    DEFAULT_NON_RETRYABLE_ERROR_KEYWORDS,
    DEFAULT_NON_RETRYABLE_STATUS_CODES,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_DURATION_SECONDS,
    MAX_TIMEOUT_SECONDS,
    UNSPECIFIED_TOKENS,
)
from ..shared.logging import log_prefix, safe_log_text
from ..shared.types import (
    ProviderConfig,
    VideoAdapterType,
    VideoCapability,
)
from .models import (
    GenerationSettings,
    PlatformSettings,
    PluginConfig,
    UsageSettings,
)

LOG = log_prefix("Config")

_CAPABILITY_LABELS = {
    "文生视频": VideoCapability.TEXT_TO_VIDEO,
    "图生视频": VideoCapability.IMAGE_TO_VIDEO,
    "宽高比": VideoCapability.ASPECT_RATIO,
    "分辨率": VideoCapability.RESOLUTION,
    "音频": VideoCapability.AUDIO,
}


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
    """Read one dict config section safely from AstrBotConfig or dict-like objects."""
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
    """Load video generation plugin settings from AstrBotConfig."""

    def __init__(self, raw_config: Any):
        self.raw = raw_config
        self._active: ProviderConfig | None = None
        self.config = self._parse(self.raw)

    def reload(self, raw_config: Any | None = None) -> None:
        if raw_config is not None:
            self.raw = raw_config
        self.config = self._parse(self.raw)

    @property
    def providers(self) -> list[ProviderConfig]:
        return self.config.providers

    @property
    def current_model_setting(self) -> str:
        return self.config.current_model

    @property
    def active_provider(self) -> ProviderConfig | None:
        return self._active

    def model_choices(self) -> list[str]:
        """Flat "供应商名称/模型名称" choices across all provider instances."""
        choices: list[str] = []
        for provider in self.config.providers:
            for model in provider.available_models:
                choices.append(f"{provider.name}/{model}")
        return choices

    def save_video_model(self, choice: str) -> None:
        """Persist the active 供应商/模型 selection."""
        try:
            self.raw["video_model"] = choice
            save = getattr(self.raw, "save_config", None)
            if callable(save):
                save()
        except Exception as exc:
            logger.error(f"{LOG} 保存视频线路失败: {safe_log_text(exc)}", exc_info=True)
        self.reload()

    @property
    def generation(self) -> GenerationSettings:
        return self.config.generation

    @property
    def usage(self) -> UsageSettings:
        return self.config.usage

    @property
    def platform(self) -> PlatformSettings:
        return self.config.platform

    @property
    def presets(self) -> dict:
        return self._presets

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self, raw: Any) -> PluginConfig:
        generation_raw = _section(raw, "generation")
        runtime_raw = _section(raw, "runtime")
        usage_raw = _section(raw, "usage")
        platform_raw = _section(raw, "platform")

        timeout = _as_int(
            _get(runtime_raw, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            DEFAULT_TIMEOUT_SECONDS,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        )
        retries = _as_int(
            _get(runtime_raw, "retry_attempts", DEFAULT_RETRY_ATTEMPTS),
            DEFAULT_RETRY_ATTEMPTS,
            minimum=0,
            maximum=10,
        )

        aspect = str(_get(generation_raw, "default_aspect_ratio") or "").strip()
        if aspect.lower() in UNSPECIFIED_TOKENS:
            aspect = ""

        resolution = str(
            _get(generation_raw, "default_resolution") or ""
        ).strip().lower()
        if resolution in UNSPECIFIED_TOKENS:
            resolution = ""

        # 0 = 不指定: the duration field is omitted from upstream requests.
        duration = _as_int(
            _get(generation_raw, "default_duration", 0),
            0,
            minimum=0,
            maximum=MAX_DURATION_SECONDS,
        )

        audio_raw = str(_get(generation_raw, "generate_audio", "")).strip().lower()
        if audio_raw in ("开启", "on", "true", "1"):
            audio_mode = "on"
        elif audio_raw in ("关闭", "off", "false", "0"):
            audio_mode = "off"
        else:
            audio_mode = ""

        max_refs = _as_int(
            _get(usage_raw, "max_reference_images", DEFAULT_MAX_REFERENCE_IMAGES),
            DEFAULT_MAX_REFERENCE_IMAGES,
            minimum=1,
            maximum=ABSOLUTE_MAX_REFERENCE_IMAGES,
        )

        keywords = _as_str_list(_get(runtime_raw, "non_retryable_error_keywords"))
        if not keywords:
            keywords = list(DEFAULT_NON_RETRYABLE_ERROR_KEYWORDS)

        generation = GenerationSettings(
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
            debug_request_logging=_as_bool(_get(runtime_raw, "debug_request_logging"), False),
            show_user_error_details=_as_bool(
                _get(runtime_raw, "show_user_error_details"), True
            ),
            non_retryable_status_codes=_as_int_list(
                _get(runtime_raw, "non_retryable_status_codes"),
                list(DEFAULT_NON_RETRYABLE_STATUS_CODES),
            ),
            non_retryable_error_keywords=keywords,
            result_info_items=(
                ["模型", "耗时"]
                if _get(generation_raw, "result_info_items", None) is None
                else _as_str_list(_get(generation_raw, "result_info_items"))
            ),
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
            generate_audio=audio_mode,
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

        providers = self._load_providers(_get(raw, "api_providers", []), generation)
        current = str(_get(raw, "video_model", "") or "").strip()
        self._active = self._select_provider(providers, current)

        return PluginConfig(
            providers=providers,
            current_model=current,
            usage=usage,
            generation=generation,
            platform=platform,
        )

    def _load_providers(
        self, raw_items: Any, generation: GenerationSettings
    ) -> list[ProviderConfig]:
        """Parse template_list entries into normalized provider configs."""
        if not isinstance(raw_items, list):
            if raw_items:
                logger.warning(f"{LOG} api_providers 配置格式错误，已按空列表处理")
            return []
        providers: list[ProviderConfig] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            parsed = self._parse_provider(item, generation)
            if parsed is not None:
                providers.append(parsed)
        return providers

    def _parse_provider(
        self, item: dict[str, Any], generation: GenerationSettings
    ) -> ProviderConfig | None:
        type_str = str(item.get("__template_key") or "").strip()
        try:
            provider_type = VideoAdapterType(type_str)
        except ValueError:
            logger.warning(f"{LOG} 忽略未知适配器类型: {safe_log_text(type_str)}")
            return None

        base_url = str(item.get("base_url") or "").strip()
        if "/v1" in base_url:
            base_url = base_url.split("/v1", 1)[0]

        capabilities = VideoCapability.NONE
        for label in _as_str_list(item.get("capability_options", [])):
            capabilities |= _CAPABILITY_LABELS.get(label.strip(), VideoCapability.NONE)
        if capabilities is VideoCapability.NONE:
            capabilities = (
                VideoCapability.TEXT_TO_VIDEO | VideoCapability.IMAGE_TO_VIDEO
            )

        # Per-provider overrides: 0/missing falls back to the global runtime value.
        timeout_raw = item.get("timeout")
        timeout = (
            generation.timeout_seconds
            if timeout_raw in (None, "", 0)
            else _as_int(timeout_raw, generation.timeout_seconds, 1, MAX_TIMEOUT_SECONDS)
        )
        retry_raw = item.get("max_retry_attempts")
        retries = (
            generation.retry_attempts
            if retry_raw in (None, "", 0)
            else _as_int(retry_raw, generation.retry_attempts, 0, 10)
        )

        name = str(item.get("name") or "").strip()
        return ProviderConfig(
            type=provider_type,
            name=name,
            base_url=base_url,
            api_key=str(item.get("api_key") or "").strip(),
            available_models=_as_str_list(item.get("available_models", [])),
            capabilities=capabilities,
            proxy=str(item.get("proxy") or "").strip() or None,
            timeout=timeout,
            max_retry_attempts=max(1, retries + 1),
            audio_mode=generation.generate_audio,
            show_user_error_details=generation.show_user_error_details,
        )

    def _select_provider(
        self, providers: list[ProviderConfig], current: str
    ) -> ProviderConfig | None:
        """Resolve the active provider and its selected model.

        `current` is "供应商名称/模型名称"; falls back to the first provider
        and its first available model.
        """
        if "/" in current:
            target_name, target_model = current.split("/", 1)
            for provider in providers:
                if provider.name == target_name:
                    return replace(provider, model=target_model)
        if not providers:
            logger.warning(f"{LOG} 未找到任何视频供应商配置")
            return None
        first = providers[0]
        model = first.available_models[0] if first.available_models else ""
        logger.info(
            f"{LOG} 未匹配到当前线路配置，默认使用: "
            f"{safe_log_text(first.name)}/{safe_log_text(model)}"
        )
        return replace(first, model=model)
