"""Task models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class VideoTaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


STATUS_LABELS = {
    VideoTaskStatus.QUEUED: "排队中",
    VideoTaskStatus.RUNNING: "运行中",
    VideoTaskStatus.SUCCEEDED: "已完成",
    VideoTaskStatus.FAILED: "失败",
    VideoTaskStatus.CANCELLING: "取消中",
    VideoTaskStatus.CANCELLED: "已取消",
}

ACTIVE_STATUSES = {
    VideoTaskStatus.QUEUED,
    VideoTaskStatus.RUNNING,
    VideoTaskStatus.CANCELLING,
}


@dataclass
class VideoTaskRecord:
    task_id: str
    unified_msg_origin: str
    prompt: str
    duration: int
    aspect_ratio: str
    resolution: str
    model: str
    reference_image_count: int = 0
    mode: str = "text"
    status: VideoTaskStatus = VideoTaskStatus.QUEUED
    message: str = "任务已提交"
    error: str = ""
    upstream_request_id: str = ""
    result_path: str = ""
    result_url: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.value)

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.finished_at or datetime.now()
        return max(0.0, (end - self.started_at).total_seconds())
