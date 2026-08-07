"""Grok2API video adapter."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from astrbot.api import logger

from ..generation.reference import image_to_data_url
from ..shared.constants import POLL_INTERVAL_SECONDS
from ..shared.logging import log_prefix, safe_log_text
from ..shared.types import AdapterConfig, VideoRequest, VideoResult

LOG = log_prefix("Adapter")
API_STATUS_RE = re.compile(r"API 错误\s*\((\d{3})\)")


class Grok2APIVideoAdapter:
    """Talk to grok2api video endpoints, with limited new-api fallbacks."""

    def __init__(self, config: AdapterConfig):
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
        lower = raw.lower()
        # If user pasted a full create path, strip back to service root-ish base.
        for suffix in (
            "/v1/videos/generations",
            "/v1/video/generations",
            "/v1/videos",
            "/videos/generations",
            "/video/generations",
        ):
            if lower.endswith(suffix):
                return raw[: -len(suffix)].rstrip("/") or raw
        if lower.endswith("/v1"):
            return raw[: -len("/v1")].rstrip("/") or raw
        return raw

    def _candidate_create_urls(self) -> list[str]:
        """Prefer grok2api path, then common gateway aliases."""
        raw = (self.config.base_url or "").strip().rstrip("/")
        lower = raw.lower()
        if lower.endswith("/videos/generations") or lower.endswith("/video/generations") or lower.endswith("/videos"):
            return [raw]

        root = self._root_base()
        candidates = [
            f"{root}/v1/videos/generations",  # grok2api
            f"{root}/v1/videos",              # OpenAI videos style / some gateways
            f"{root}/v1/video/generations",   # new-api style
        ]
        # de-dup keep order
        seen: set[str] = set()
        ordered: list[str] = []
        for item in candidates:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _status_urls(self, create_url: str, request_id: str) -> list[str]:
        rid = request_id.strip()
        parsed = urlparse(create_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        root = origin
        for suffix in (
            "/v1/videos/generations",
            "/v1/video/generations",
            "/v1/videos",
            "/videos/generations",
            "/video/generations",
        ):
            if path.endswith(suffix):
                root = origin + path[: -len(suffix)]
                break
        urls = [
            f"{root}/v1/videos/{rid}",
            f"{root}/v1/video/generations/{rid}",
        ]
        if create_url.endswith("/videos/generations"):
            urls.insert(0, create_url[: -len("/generations")] + f"/{rid}")
        elif create_url.endswith("/videos"):
            urls.insert(0, f"{create_url}/{rid}")
        elif create_url.endswith("/video/generations"):
            urls.insert(0, f"{create_url}/{rid}")
        # de-dup
        seen: set[str] = set()
        ordered: list[str] = []
        for item in urls:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _content_urls(self, status_url: str, request_id: str) -> list[str]:
        rid = request_id.strip()
        parsed = urlparse(status_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        root = self._root_base()
        urls: list[str] = []
        # Prefer grok2api official content endpoint first.
        if root:
            urls.append(f"{root.rstrip('/')}/v1/videos/{rid}/content")
        if origin:
            urls.append(f"{origin}/v1/videos/{rid}/content")
        if status_url:
            if status_url.startswith("http://") or status_url.startswith("https://"):
                urls.append(status_url.rstrip("/") + "/content")
            elif origin:
                urls.append(origin.rstrip("/") + "/" + status_url.lstrip("/"))
                if not status_url.rstrip("/").endswith("/content"):
                    urls.append(origin.rstrip("/") + "/" + status_url.lstrip("/").rstrip("/") + "/content")
        # de-dup
        seen: set[str] = set()
        ordered: list[str] = []
        for item in urls:
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _is_retryable(self, error: str) -> bool:
        match = API_STATUS_RE.search(error or "")
        if match:
            code = int(match.group(1))
            return code not in set(self.config.non_retryable_status_codes)
        lowered = (error or "").lower()
        return not any(k.lower() in lowered for k in self.config.non_retryable_error_keywords)

    def _build_payload(self, request: VideoRequest) -> dict[str, Any]:
        """Build grok2api-compatible video create body.

        grok2api rejects unknown fields (DisallowUnknownFields), so do NOT send
        OpenAI-only aliases like seconds / input_reference.
        """
        payload: dict[str, Any] = {
            "model": request.model or self.config.model,
            "prompt": request.prompt,
            "duration": int(request.duration),
            "aspect_ratio": request.aspect_ratio,
            "resolution": request.resolution,
        }
        if request.images:
            image = request.images[0]
            if image.source_url and image.source_url.startswith(("http://", "https://")):
                image_url = image.source_url
            else:
                image_url = image_to_data_url(image)
            payload["image"] = {"url": image_url}
        return payload

    async def generate(self, request: VideoRequest, *, should_cancel=None) -> VideoResult:
        if not self.config.api_key:
            return VideoResult(error="未配置 API Key")
        last_error = "生成失败"
        attempts = max(1, self.config.max_retry_attempts)
        for attempt in range(attempts):
            if should_cancel and should_cancel():
                return VideoResult(error="任务已取消")
            result = await self._generate_once(request, should_cancel=should_cancel)
            if result.error is None:
                return result
            last_error = result.error
            if not self._is_retryable(last_error):
                return result
            if attempt < attempts - 1:
                await asyncio.sleep(min(2 ** attempt, 8))
        return VideoResult(error=f"重试失败: {last_error}")

    async def _generate_once(self, request: VideoRequest, *, should_cancel=None) -> VideoResult:
        payload = self._build_payload(request)
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        prefix = log_prefix("Adapter", request.task_id)
        start = time.time()

        create_urls = self._candidate_create_urls()
        last_create_error = "创建视频任务失败"
        create_data: dict[str, Any] | None = None
        used_create_url = ""

        try:
            for create_url in create_urls:
                if should_cancel and should_cancel():
                    return VideoResult(error="任务已取消")
                if self.config.debug_request_logging:
                    logger.debug(
                        f"{prefix} 尝试创建视频 url={create_url} model={payload.get('model')} "
                        f"duration={payload.get('duration')} images={len(request.images)}"
                    )
                async with session.post(
                    create_url,
                    json=payload,
                    headers=self._headers(),
                    proxy=self.config.proxy,
                    timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        last_create_error = self._format_error(resp.status, text)
                        # Path not found on this gateway: try next candidate.
                        if resp.status in {404, 405}:
                            logger.warning(
                                f"{prefix} 创建路径不可用 ({resp.status}): {create_url}"
                            )
                            continue
                        return VideoResult(error=last_create_error)
                    create_data = await self._safe_json(resp, text)
                    used_create_url = create_url
                    break
            else:
                hint = (
                    "当前地址没有可用的视频创建接口。"
                    "请确认 API 地址指向 grok2api（支持 /v1/videos/generations），"
                    "而不是只支持 chat 的中转。"
                )
                return VideoResult(error=f"{last_create_error}；{hint}")

            request_id = self._extract_request_id(create_data)
            if not request_id:
                # Some gateways may return final result directly.
                direct_url = self._extract_video_url(create_data)
                if direct_url:
                    video_bytes, content_type = await self._download_video(direct_url)
                    return VideoResult(
                        video_bytes=video_bytes,
                        content_type=content_type or "video/mp4",
                        video_url=direct_url,
                        upstream_request_id="",
                    )
                return VideoResult(error="上游未返回 request_id")

            status_urls = self._status_urls(used_create_url, request_id)
            deadline = start + max(1, self.config.timeout)
            active_status_url = status_urls[0]
            while True:
                if should_cancel and should_cancel():
                    return VideoResult(error="任务已取消", upstream_request_id=request_id)
                if time.time() >= deadline:
                    return VideoResult(error="等待视频生成超时", upstream_request_id=request_id)

                data = None
                last_status_error = ""
                for status_url in status_urls:
                    async with session.get(
                        status_url,
                        headers=self._headers(),
                        proxy=self.config.proxy,
                        timeout=timeout,
                    ) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            last_status_error = self._format_error(resp.status, text)
                            if resp.status in {404, 405}:
                                continue
                            return VideoResult(
                                error=last_status_error,
                                upstream_request_id=request_id,
                            )
                        data = await self._safe_json(resp, text)
                        active_status_url = status_url
                        break
                if data is None:
                    # all status urls failed with 404; keep polling a bit in case job not visible yet
                    if last_status_error:
                        logger.debug(f"{prefix} 状态查询暂不可用: {last_status_error}")
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                status = str((data or {}).get("status") or "").lower()
                progress = (data or {}).get("progress")
                try:
                    progress_num = int(progress) if progress is not None else None
                except (TypeError, ValueError):
                    progress_num = None
                has_video = bool(self._extract_video_url(data))
                finished = status in {"done", "succeeded", "completed", "success", "ready"} or (
                    progress_num == 100 and has_video
                )
                if finished:
                    video_url = self._extract_video_url(data)
                    video_bytes = None
                    content_type = None
                    # Normalize relative content paths to absolute grok2api URLs.
                    if video_url and video_url.startswith("/"):
                        root = self._root_base().rstrip("/")
                        video_url = f"{root}{video_url}"
                    download_candidates: list[str] = []
                    if video_url:
                        download_candidates.append(video_url)
                    download_candidates.extend(self._content_urls(active_status_url, request_id))
                    seen_dl: set[str] = set()
                    for candidate in download_candidates:
                        candidate = (candidate or "").strip()
                        if not candidate or candidate in seen_dl:
                            continue
                        seen_dl.add(candidate)
                        video_bytes, content_type = await self._download_video(candidate)
                        if video_bytes is not None:
                            video_url = candidate
                            logger.info(
                                f"{prefix} 视频内容下载成功: bytes={len(video_bytes)} url={safe_log_text(candidate, 120)}"
                            )
                            break
                        logger.warning(
                            f"{prefix} 视频内容下载失败，尝试下一个: {safe_log_text(candidate, 120)}"
                        )
                    if video_bytes is None and not video_url:
                        return VideoResult(
                            error="视频已完成但未获取到内容",
                            upstream_request_id=request_id,
                        )
                    if video_bytes is None:
                        logger.warning(
                            f"{prefix} 仅拿到视频URL，未能下载二进制，将降级发链接: {safe_log_text(video_url, 160)}"
                        )
                    return VideoResult(
                        video_bytes=video_bytes,
                        content_type=content_type or "video/mp4",
                        video_url=video_url,
                        upstream_request_id=request_id,
                    )
                if status in {"failed", "error", "cancelled", "canceled"}:
                    message = str(
                        (data or {}).get("error")
                        or (data or {}).get("message")
                        or "上游视频生成失败"
                    )
                    if isinstance((data or {}).get("error"), dict):
                        message = str(
                            data["error"].get("message")
                            or data["error"].get("code")
                            or message
                        )
                    return VideoResult(error=message, upstream_request_id=request_id)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:
            logger.error(f"{prefix} 视频请求异常: {safe_log_text(exc)}", exc_info=True)
            msg = str(exc)
            if "Cannot connect to host" in msg or "Connect call failed" in msg:
                return VideoResult(
                    error=(
                        f"无法连接 grok2api（{safe_log_text(self.config.base_url or '', 80)}）。"
                        "请确认 grok2api 容器/服务已启动，且 AstrBot 能访问该地址。"
                        f" 原始错误: {safe_log_text(msg, 180)}"
                    )
                )
            return VideoResult(error=f"请求异常: {exc}")

    def _extract_request_id(self, data: dict[str, Any] | None) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("request_id", "id", "task_id", "video_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # nested
        for nest_key in ("data", "result", "video"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                found = self._extract_request_id(nested)
                if found:
                    return found
        return ""

    async def _download_video(self, url: str) -> tuple[bytes | None, str | None]:
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=min(300, max(30, self.config.timeout)))
        try:
            # Relative path cannot be downloaded directly.
            if url.startswith("/"):
                root = self._root_base().rstrip("/")
                url = f"{root}{url}"
            async with session.get(
                url,
                headers=self._headers(),
                proxy=self.config.proxy,
                timeout=timeout,
            ) as resp:
                content_type = (resp.headers.get("Content-Type") or "video/mp4").split(";")[0].strip().lower()
                data = await resp.read()
                if resp.status >= 400:
                    logger.warning(
                        f"{LOG} 下载视频HTTP失败 status={resp.status} type={content_type} url={safe_log_text(url, 120)}"
                    )
                    return None, None
                if not data:
                    return None, content_type
                # Avoid treating JSON error body as video.
                if content_type.startswith("application/json") or data[:1] in (b"{", b"["):
                    logger.warning(
                        f"{LOG} 下载结果不是视频二进制 type={content_type} url={safe_log_text(url, 120)}"
                    )
                    return None, content_type
                if content_type in {"application/octet-stream", "binary/octet-stream", ""}:
                    content_type = "video/mp4"
                return data, content_type
        except Exception as exc:
            logger.warning(f"{LOG} 下载视频失败: {safe_log_text(exc)}")
            return None, None

    def _extract_video_url(self, data: dict[str, Any] | None) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("url", "video_url", "content_url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nest_key in ("video", "result", "data", "output"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                found = self._extract_video_url(nested)
                if found:
                    return found
            if isinstance(nested, list):
                for item in nested:
                    found = self._extract_video_url(item if isinstance(item, dict) else None)
                    if found:
                        return found
        return None

    async def _safe_json(self, resp: aiohttp.ClientResponse, text: str) -> dict[str, Any] | None:
        try:
            data = await resp.json(content_type=None)
            return data if isinstance(data, dict) else None
        except Exception:
            if self.config.debug_request_logging:
                logger.debug(f"{LOG} 非 JSON 响应: {safe_log_text(text, 300)}")
            return None

    def _format_error(self, status: int, body: str) -> str:
        message = f"API 错误 ({status})"
        if self.config.show_user_error_details and body:
            detail = re.sub(r"\s+", " ", body).strip()
            if len(detail) > 300:
                detail = detail[:297] + "..."
            if detail:
                return f"{message}: {detail}"
        return message