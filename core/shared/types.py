"""Shared types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImageData:
    data: bytes
    mime_type: str
    source_url: str | None = None


@dataclass
class AdapterConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = "grok-imagine-video"
    timeout: int = 600
    max_retry_attempts: int = 1
    debug_request_logging: bool = False
    show_user_error_details: bool = False
    non_retryable_status_codes: list[int] = field(default_factory=list)
    non_retryable_error_keywords: list[str] = field(default_factory=list)
    proxy: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoRequest:
    prompt: str
    duration: int
    aspect_ratio: str
    resolution: str
    model: str
    images: list[ImageData] = field(default_factory=list)
    task_id: str | None = None


@dataclass
class VideoResult:
    video_bytes: bytes | None = None
    content_type: str = "video/mp4"
    video_url: str | None = None
    upstream_request_id: str | None = None
    error: str | None = None
