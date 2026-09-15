"""统一媒体协议线路适配器。

POST /v1/videos（JSON，mode 声明输入模式）；图生参考媒体先经
POST /v1/videos/uploads 上传换取受保护 URL，再以 images 数组提交。
"""

from __future__ import annotations

from typing import Any

import aiohttp

from astrbot.api import logger

from ..shared.logging import safe_log_text
from ..shared.types import VideoCapability, VideoRequest
from .base import CreateError, VideoAdapterBase


class UnifiedMediaAdapter(VideoAdapterBase):
    async def create(self, request: VideoRequest) -> dict:
        caps = self.config.capabilities
        root = self._root_base().rstrip("/")

        urls: list[str] | None = None
        if request.images:
            if not (caps & VideoCapability.IMAGE_TO_VIDEO):
                raise CreateError("该线路未启用图生视频能力，请在供应商配置中勾选")
            urls = await self._upload_images(request.images)
            if urls is None:
                raise CreateError("参考媒体上传失败，请查看插件日志")

        payload: dict[str, Any] = {
            "model": request.model,
            "mode": "image-to-video" if request.images else "text-to-video",
            "prompt": request.prompt,
        }
        if request.duration and request.duration > 0:
            payload["duration"] = int(request.duration)
        if request.aspect_ratio and (caps & VideoCapability.ASPECT_RATIO):
            payload["aspect_ratio"] = request.aspect_ratio
        if request.resolution and (caps & VideoCapability.RESOLUTION):
            payload["resolution"] = request.resolution
        if (
            self.config.audio_mode in ("on", "off")
            and (caps & VideoCapability.AUDIO)
        ):
            payload["audio"] = self.config.audio_mode == "on"
        if urls:
            payload["images"] = urls

        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=max(30, self.config.timeout))
        async with session.post(
            f"{root}/v1/videos",
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

    async def _upload_images(self, images) -> list[str] | None:
        """Upload each reference image; return protected URLs, or None on failure."""
        root = self._root_base().rstrip("/")
        upload_url = f"{root}/v1/videos/uploads"
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=max(60, self.config.timeout))
        urls: list[str] = []
        for index, image in enumerate(images):
            mime = (image.mime_type or "").split(";")[0].strip().lower() or "image/png"
            ext = ".png"
            if mime in ("image/jpeg", "image/jpg"):
                ext = ".jpg"
            elif mime == "image/gif":
                ext = ".gif"
            elif mime == "image/webp":
                ext = ".webp"
            form = aiohttp.FormData()
            form.add_field(
                "file",
                image.data,
                filename=f"reference{index}{ext}",
                content_type=mime,
            )
            try:
                async with session.post(
                    upload_url,
                    data=form,
                    headers=self._headers(json_body=False),
                    proxy=self.config.proxy,
                    timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        logger.warning(
                            f"参考媒体上传失败 status={resp.status} "
                            f"body={safe_log_text(text, 160)}"
                        )
                        return None
                    data = await self._safe_json(resp, text)
                    url = self._extract_video_url(data)
                    if not url and text.strip().startswith(("http://", "https://")):
                        url = text.strip()
                    if not url:
                        logger.warning(
                            f"上传响应缺少 URL: {safe_log_text(text, 160)}"
                        )
                        return None
                    urls.append(url)
            except Exception as exc:
                logger.warning(f"参考媒体上传异常: {safe_log_text(exc)}")
                return None
        return urls
