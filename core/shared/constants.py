"""Shared constants for video generation plugin."""

from __future__ import annotations

DEFAULT_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 1200
DEFAULT_RETRY_ATTEMPTS = 1
DEFAULT_DURATION_SECONDS = 6
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 15
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "720p"
DEFAULT_MAX_REFERENCE_IMAGES = 1
ABSOLUTE_MAX_REFERENCE_IMAGES = 8
DEFAULT_MAX_REFERENCE_SIZE_MB = 10
DEFAULT_MAX_CONCURRENT_REQUESTS = 3
DEFAULT_MAX_RUNNING_TASKS = 3
DEFAULT_MAX_QUEUED_TASKS = 20
DEFAULT_RATE_LIMIT_SECONDS = 0
DEFAULT_DAILY_LIMIT = 10
DEFAULT_HISTORY_LIMIT = 1000
DEFAULT_HISTORY_RETENTION_DAYS = 0
POLL_INTERVAL_SECONDS = 2.0

SUPPORTED_ASPECT_RATIOS = (
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
)
SUPPORTED_RESOLUTIONS = ("480p", "720p", "1080p")

DEFAULT_NON_RETRYABLE_STATUS_CODES = (400, 401, 403, 404, 405, 422)
DEFAULT_NON_RETRYABLE_ERROR_KEYWORDS = (
    "参数",
    "无效",
    "不支持",
    "未配置",
    "invalid",
    "bad request",
    "unauthorized",
    "forbidden",
    "permission",
    "not found",
    "unsupported",
    "safety",
    "content policy",
)

DEFAULT_START_TEMPLATE = "已开始视频生成任务{reference_images_block} [{duration}s] [{aspect_ratio}] [任务ID: {task_id}]"
DEFAULT_BLACKLIST_MESSAGE = "❌ 当前会话已被加入黑名单，无法使用视频功能"

DEFAULT_ENABLE_PROMPT_ENHANCEMENT = True
DEFAULT_COMMON_PROMPT_ENHANCEMENT = (
    "画面要求：高细节、清晰边缘、低噪点、运动稳定、时序一致。"
    "输出风格自然，不要过度锐化。"
)
DEFAULT_IMAGE_PROMPT_ENHANCEMENT = "保持参考图主体身份、构图和色调风格一致。"
DEFAULT_TEXT_PROMPT_ENHANCEMENT = "主体动作连贯，镜头转场平滑。"
