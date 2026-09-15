"""Shared video adapter base: fixed create → poll → download skeleton.

Each adapter walks ONLY its own confirmed protocol route: no cross-protocol
probing, no payload mutation retries. Subclasses implement `create()` and,
when needed, override the status/content URL builders.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger

from ..shared.constants import POLL_INTERVAL_SECONDS
from ..shared.logging import log_prefix, safe_log_text
from ..shared.types import ProviderConfig, VideoRequest, VideoResult
from ..generation.reference import image_to_data_url


class CreateError(Exception):
    """Create request failed. status=0 marks a non-HTTP local failure."""

    def __init__(self, body: str, status: int = 0):
        self.status = status
        self.body = body
        super().__init__(body if not status else f"API 错误 ({status}): {body}")


class VideoAdapterBase:
    finished_statuses = {"done", "succeeded", "completed", "success", "ready"}
    failed_statuses = {"failed", "error", "cancelled", "canceled"}

    def __init__(self, config: ProviderConfig):
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _root_base(self) -> str:
        raw = (self.config.base_url or "").strip()
        if not raw:
            raise ValueError("未配置 API 地址")
        raw = raw.rstrip("/")
        if "/v1" in raw:
            raw = raw.split("/v1", 1)[0]
        return raw

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _image_ref(self, image) -> str:
        """Remote http(s) URL as-is; anything else becomes a data URL."""
        if image.source_url and image.source_url.startswith(("http://", "https://")):
            return image.source_url
        return image_to_data_url(image)

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    async def create(self, request: VideoRequest) -> dict:
        """Submit one creation request and return the parsed response body."""
        raise NotImplementedError

    def status_urls(self, request_id: str) -> list[str]:
        root = self._root_base().rstrip("/")
        return [f"{root}/v1/videos/{request_id}"]

    def content_urls(self, request_id: str, status_url: str) -> list[str]:
        root = self._root_base().rstrip("/")
        urls = [f"{root}/v1/videos/{request_id}/content"]
        if status_url:
            urls.insert(0, status_url.rstrip("/") + "/content")
        return urls

    # ------------------------------------------------------------------
    # Shared flow
    # ------------------------------------------------------------------

    async def generate(
        self, request: VideoRequest, *, should_cancel=None
    ) -> VideoResult:
        prefix = log_prefix(self.__class__.__name__, request.task_id)
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=max(30, self.config.timeout))
        start = time.time()

        attempts = max(1, self.config.max_retry_attempts)
        create_data: dict | None = None
        for attempt in range(attempts):
            if should_cancel and should_cancel():
                return VideoResult(error="任务已取消")
            try:
                create_data = await self.create(request)
                break
            except CreateError as exc:
                logger.warning(
                    f"{prefix} 创建失败: {safe_log_text(str(exc), 200)}"
                )
                if exc.status < 500 or attempt == attempts - 1:
                    return VideoResult(error=str(exc))
                await asyncio.sleep(min(2**attempt, 8))
            except Exception as exc:
                msg = str(exc)
                if "Cannot connect to host" in msg or "Connect call failed" in msg:
                    return VideoResult(
                        error=(
                            f"无法连接视频网关（{safe_log_text(self.config.base_url or '', 80)}）。"
                            "请确认网关服务已启动，且 AstrBot 能访问该地址。"
                            f" 原始错误: {safe_log_text(msg, 180)}"
                        )
                    )
                logger.warning(f"{prefix} 创建异常: {safe_log_text(exc, 200)}")
                if attempt == attempts - 1:
                    return VideoResult(error=f"创建请求异常: {safe_log_text(exc, 200)}")
                await asyncio.sleep(min(2**attempt, 8))

        request_id = self._extract_request_id(create_data)
        if not request_id:
            direct_url = self._extract_video_url(create_data)
            if direct_url:
                video_bytes, content_type = await self._download_video(direct_url)
                if video_bytes is not None:
                    return VideoResult(
                        video_bytes=video_bytes,
                        content_type=content_type or "video/mp4",
                        video_url=direct_url,
                    )
            return VideoResult(error="上游未返回任务ID")

        deadline = start + max(30, self.config.timeout)
        status_list = self.status_urls(request_id)
        while True:
            if should_cancel and should_cancel():
                return VideoResult(
                    error="任务已取消", upstream_request_id=request_id
                )
            if time.time() >= deadline:
                return VideoResult(
                    error="等待视频生成超时", upstream_request_id=request_id
                )

            data: dict | None = None
            active_status_url = status_list[0]
            for status_url in status_list:
                try:
                    async with session.get(
                        status_url,
                        headers=self._headers(),
                        proxy=self.config.proxy,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            if resp.status in {404, 405}:
                                continue
                            return VideoResult(
                                error=self._format_error(resp.status, text),
                                upstream_request_id=request_id,
                            )
                        data = await self._safe_json(resp, text)
                        active_status_url = status_url
                        break
                except Exception as exc:
                    logger.warning(
                        f"{prefix} 状态查询异常: {safe_log_text(exc, 120)}"
                    )
                    continue
            if data is None:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            status = str(
                data.get("status")
                or data.get("task_status")
                or data.get("state")
                or ""
            ).lower()
            if status in self.failed_statuses:
                message = str(
                    data.get("error") or data.get("message") or "上游视频生成失败"
                )
                if isinstance(data.get("error"), dict):
                    message = str(
                        data["error"].get("message")
                        or data["error"].get("code")
                        or message
                    )
                return VideoResult(
                    error=message, upstream_request_id=request_id
                )
            if status in self.finished_statuses:
                return await self._collect_result(
                    prefix, request_id, active_status_url, data
                )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _collect_result(
        self, prefix: str, request_id: str, status_url: str, data: dict
    ) -> VideoResult:
        video_url = self._extract_video_url(data)
        if video_url and video_url.startswith("/"):
            video_url = f"{self._root_base().rstrip('/')}{video_url}"
        candidates: list[str] = []
        if video_url:
            candidates.append(video_url)
        candidates.extend(self.content_urls(request_id, status_url))
        seen: set[str] = set()
        video_bytes = None
        content_type = None
        for candidate in candidates:
            candidate = (candidate or "").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            video_bytes, content_type = await self._download_video(candidate)
            if video_bytes is not None:
                video_url = candidate
                logger.info(
                    f"{prefix} 视频内容下载成功: bytes={len(video_bytes)} "
                    f"url={safe_log_text(candidate, 120)}"
                )
                break
            logger.warning(
                f"{prefix} 视频内容下载失败，尝试下一个: {safe_log_text(candidate, 120)}"
            )
        if video_bytes is None and not video_url:
            return VideoResult(
                error="视频已完成但未获取到内容", upstream_request_id=request_id
            )
        if video_bytes is None:
            logger.warning(
                f"{prefix} 仅拿到视频URL，未能下载二进制，将降级发链接: "
                f"{safe_log_text(video_url or '', 160)}"
            )
        return VideoResult(
            video_bytes=video_bytes,
            content_type=content_type or "video/mp4",
            video_url=video_url,
            upstream_request_id=request_id,
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _download_video(self, url: str) -> tuple[bytes | None, str | None]:
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=min(300, max(30, self.config.timeout)))
        try:
            if url.startswith("/"):
                url = f"{self._root_base().rstrip('/')}{url}"
            async with session.get(
                url,
                headers=self._headers(json_body=False),
                proxy=self.config.proxy,
                timeout=timeout,
            ) as resp:
                content_type = (
                    resp.headers.get("Content-Type") or "video/mp4"
                ).split(";")[0].strip().lower()
                data = await resp.read()
                if resp.status >= 400:
                    logger.warning(
                        f"下载视频HTTP失败 status={resp.status} type={content_type} "
                        f"url={safe_log_text(url, 120)}"
                    )
                    return None, None
                if not data:
                    return None, content_type
                if content_type.startswith("application/json") or data[:1] in (b"{", b"["):
                    logger.warning(
                        f"下载结果不是视频二进制 type={content_type} url={safe_log_text(url, 120)}"
                    )
                    return None, content_type
                if content_type in {"application/octet-stream", "binary/octet-stream", ""}:
                    content_type = "video/mp4"
                return data, content_type
        except Exception as exc:
            logger.warning(f"下载视频失败: {safe_log_text(exc)}")
            return None, None

    def _extract_request_id(self, data: dict | None) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("request_id", "id", "task_id", "video_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nest_key in ("data", "result", "video"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                found = self._extract_request_id(nested)
                if found:
                    return found
        return ""

    def _extract_video_url(self, data: dict | None) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("url", "video_url", "content_url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nest_key in ("video", "videos", "result", "data", "output"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                found = self._extract_video_url(nested)
                if found:
                    return found
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
                    found = self._extract_video_url(
                        item if isinstance(item, dict) else None
                    )
                    if found:
                        return found
        return None

    async def _safe_json(self, resp: aiohttp.ClientResponse, text: str) -> dict | None:
        try:
            data = await resp.json(content_type=None)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _format_error(self, status: int, body: str) -> str:
        message = f"API 错误 ({status})"
        if self.config.show_user_error_details and body:
            import re

            detail = re.sub(r"\s+", " ", body).strip()
            if len(detail) > 300:
                detail = detail[:297] + "..."
            if detail:
                return f"{message}: {detail}"
        return message
