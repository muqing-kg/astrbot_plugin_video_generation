"""Simple in-memory task manager with optional persistence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..shared.logging import log_prefix, safe_log_text
from .models import ACTIVE_STATUSES, VideoTaskRecord, VideoTaskStatus

LOG = log_prefix("TaskManager")


class TaskManager:
    def __init__(
        self,
        *,
        max_running_tasks: int = 3,
        max_queued_tasks: int = 20,
        enable_history: bool = False,
        history_limit: int = 1000,
        history_retention_days: int = 0,
        persistence_file: Path | None = None,
    ) -> None:
        self.max_running_tasks = max(1, max_running_tasks)
        self.max_queued_tasks = max(0, max_queued_tasks)
        self.enable_history = enable_history
        self.history_limit = max(1, history_limit)
        self.history_retention_days = max(0, history_retention_days)
        self.persistence_file = persistence_file
        self._records: dict[str, VideoTaskRecord] = {}
        self._queue: asyncio.Queue[tuple[str, Callable[[], Awaitable[None]]]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._running_count = 0

    def configure(
        self,
        *,
        max_running_tasks: int | None = None,
        max_queued_tasks: int | None = None,
        enable_history: bool | None = None,
        history_limit: int | None = None,
        history_retention_days: int | None = None,
    ) -> None:
        if max_running_tasks is not None:
            self.max_running_tasks = max(1, max_running_tasks)
        if max_queued_tasks is not None:
            self.max_queued_tasks = max(0, max_queued_tasks)
        if enable_history is not None:
            self.enable_history = enable_history
        if history_limit is not None:
            self.history_limit = max(1, history_limit)
        if history_retention_days is not None:
            self.history_retention_days = max(0, history_retention_days)

    def load_history(self) -> None:
        if not self.enable_history or not self.persistence_file or not self.persistence_file.exists():
            return
        try:
            raw = json.loads(self.persistence_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"{LOG} 读取任务历史失败: {safe_log_text(exc)}")
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                record = VideoTaskRecord(
                    task_id=str(item.get("task_id") or ""),
                    unified_msg_origin=str(item.get("unified_msg_origin") or ""),
                    prompt=str(item.get("prompt") or ""),
                    duration=int(item.get("duration") or 0),
                    aspect_ratio=str(item.get("aspect_ratio") or ""),
                    resolution=str(item.get("resolution") or ""),
                    model=str(item.get("model") or ""),
                    reference_image_count=int(item.get("reference_image_count") or 0),
                    mode=str(item.get("mode") or "text"),
                    status=VideoTaskStatus(str(item.get("status") or "failed")),
                    message=str(item.get("message") or ""),
                    error=str(item.get("error") or ""),
                    upstream_request_id=str(item.get("upstream_request_id") or ""),
                    result_path=str(item.get("result_path") or ""),
                    result_url=str(item.get("result_url") or ""),
                )
                if record.task_id:
                    self._records[record.task_id] = record
            except Exception:
                continue
        self._cleanup_history()

    def _save_history(self) -> None:
        if not self.enable_history or not self.persistence_file:
            return
        payload = []
        for record in self._records.values():
            payload.append(
                {
                    "task_id": record.task_id,
                    "unified_msg_origin": record.unified_msg_origin,
                    "prompt": record.prompt,
                    "duration": record.duration,
                    "aspect_ratio": record.aspect_ratio,
                    "resolution": record.resolution,
                    "model": record.model,
                    "reference_image_count": record.reference_image_count,
                    "mode": record.mode,
                    "status": record.status.value,
                    "message": record.message,
                    "error": record.error,
                    "upstream_request_id": record.upstream_request_id,
                    "result_path": record.result_path,
                    "result_url": record.result_url,
                    "created_at": record.created_at.isoformat(),
                    "started_at": record.started_at.isoformat() if record.started_at else None,
                    "finished_at": record.finished_at.isoformat() if record.finished_at else None,
                }
            )
        try:
            self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
            self.persistence_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"{LOG} 保存任务历史失败: {safe_log_text(exc)}")

    def _cleanup_history(self) -> None:
        records = list(self._records.values())
        if self.history_retention_days > 0:
            cutoff = datetime.now() - timedelta(days=self.history_retention_days)
            records = [
                item
                for item in records
                if item.is_active or (item.finished_at or item.created_at) >= cutoff
            ]
        records.sort(key=lambda item: item.created_at, reverse=True)
        kept: dict[str, VideoTaskRecord] = {}
        active = [item for item in records if item.is_active]
        finished = [item for item in records if not item.is_active][: self.history_limit]
        for item in active + finished:
            kept[item.task_id] = item
        self._records = kept

    def start_workers(self) -> None:
        while len(self._workers) < self.max_running_tasks:
            worker = asyncio.create_task(self._worker_loop(), name=f"video-worker-{len(self._workers)}")
            self._workers.append(worker)

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def get_queue_rejection(self) -> str | None:
        queued = sum(1 for item in self._records.values() if item.status == VideoTaskStatus.QUEUED)
        if queued >= self.max_queued_tasks:
            return f"❌ 视频任务队列已满（上限 {self.max_queued_tasks}），请稍后再试"
        return None

    def create_record(self, record: VideoTaskRecord) -> VideoTaskRecord:
        self._records[record.task_id] = record
        self._cleanup_history()
        self._save_history()
        return record

    def get(self, task_id: str) -> VideoTaskRecord | None:
        return self._records.get(task_id)

    def list_active(self, unified_msg_origin: str) -> list[VideoTaskRecord]:
        items = [
            item
            for item in self._records.values()
            if item.unified_msg_origin == unified_msg_origin and item.is_active
        ]
        items.sort(key=lambda item: item.created_at)
        return items

    def list_for_umo(self, unified_msg_origin: str) -> list[VideoTaskRecord]:
        items = [
            item for item in self._records.values() if item.unified_msg_origin == unified_msg_origin
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items

    def update(self, task_id: str, **fields: Any) -> VideoTaskRecord | None:
        record = self._records.get(task_id)
        if not record:
            return None
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        self._save_history()
        return record

    def request_cancel(self, task_id: str) -> VideoTaskRecord | None:
        record = self._records.get(task_id)
        if not record or not record.is_active:
            return None
        record.cancel_requested = True
        if record.status == VideoTaskStatus.QUEUED:
            record.status = VideoTaskStatus.CANCELLED
            record.finished_at = datetime.now()
            record.message = "任务已取消"
        else:
            record.status = VideoTaskStatus.CANCELLING
            record.message = "正在取消"
        self._save_history()
        return record

    async def enqueue(self, task_id: str, worker: Callable[[], Awaitable[None]]) -> None:
        await self._queue.put((task_id, worker))

    async def _worker_loop(self) -> None:
        while True:
            task_id, worker = await self._queue.get()
            record = self._records.get(task_id)
            try:
                if not record or record.status == VideoTaskStatus.CANCELLED or record.cancel_requested:
                    if record and record.status != VideoTaskStatus.CANCELLED:
                        record.status = VideoTaskStatus.CANCELLED
                        record.finished_at = datetime.now()
                        record.message = "任务已取消"
                        self._save_history()
                    continue
                self._running_count += 1
                record.status = VideoTaskStatus.RUNNING
                record.started_at = datetime.now()
                record.message = "任务运行中"
                self._save_history()
                await worker()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"{LOG} 任务执行异常 {task_id}: {safe_log_text(exc)}", exc_info=True)
                if record:
                    record.status = VideoTaskStatus.FAILED
                    record.error = str(exc)
                    record.message = "任务失败"
                    record.finished_at = datetime.now()
                    self._save_history()
            finally:
                self._running_count = max(0, self._running_count - 1)
                self._queue.task_done()
