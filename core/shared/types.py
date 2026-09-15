"""Shared types."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImageData:
    data: bytes
    mime_type: str
    source_url: str | None = None


class VideoAdapterType(str, enum.Enum):
    """Supported video gateway adapter types (one fixed protocol each)."""

    UNIFIED_MEDIA = "unified_media"
    SORA = "sora"
    GROK = "grok"


class VideoCapability(enum.Flag):
    """Capabilities switchable per provider instance."""

    NONE = 0
    TEXT_TO_VIDEO = enum.auto()
    IMAGE_TO_VIDEO = enum.auto()
    ASPECT_RATIO = enum.auto()
    RESOLUTION = enum.auto()
    AUDIO = enum.auto()


@dataclass
class ProviderConfig:
    """One configured video provider instance (from a template_list entry)."""

    type: VideoAdapterType = VideoAdapterType.UNIFIED_MEDIA
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    available_models: list[str] = field(default_factory=list)
    capabilities: VideoCapability = (
        VideoCapability.TEXT_TO_VIDEO
        | VideoCapability.IMAGE_TO_VIDEO
        | VideoCapability.ASPECT_RATIO
        | VideoCapability.RESOLUTION
        | VideoCapability.AUDIO
    )
    proxy: str | None = None
    timeout: int = 600
    max_retry_attempts: int = 2
    # ""/on/off: controls the optional audio field (off = muted generation).
    audio_mode: str = ""
    show_user_error_details: bool = True


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
