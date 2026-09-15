"""Grok 线路适配器：POST /v1/videos/generations（JSON，image 对象形态）。"""

from __future__ import annotations

from typing import Any

import aiohttp

from ..shared.types import VideoCapability, VideoRequest
from .base import CreateError, VideoAdapterBase


class GrokAdapter(VideoAdapterBase):
    async def create(self, request: VideoRequest) -> dict:
        caps = self.config.capabilities
        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt,
        }
        if request.duration and request.duration > 0:
            payload["duration"] = int(request.duration)
        if request.aspect_ratio and (caps & VideoCapability.ASPECT_RATIO):
            payload["aspect_ratio"] = request.aspect_ratio
        if request.resolution and (caps & VideoCapability.RESOLUTION):
            payload["resolution"] = request.resolution
        if request.images:
            if not (caps & VideoCapability.IMAGE_TO_VIDEO):
                raise CreateError("该线路未启用图生视频能力，请在供应商配置中勾选")
            payload["image"] = {"url": self._image_ref(request.images[0])}

        root = self._root_base().rstrip("/")
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=max(30, self.config.timeout))
        async with session.post(
            f"{root}/v1/videos/generations",
            json=payload,
            headers=self._headers(),
            proxy=self.config.proxy,
            timeout=timeout,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise CreateError(text, status=resp.status)
            data = await self._safe_json(resp, text)
            return data or {}

    def status_urls(self, request_id: str) -> list[str]:
        root = self._root_base().rstrip("/")
        return [
            f"{root}/v1/videos/{request_id}",
            f"{root}/v1/video/generations/{request_id}",
        ]
