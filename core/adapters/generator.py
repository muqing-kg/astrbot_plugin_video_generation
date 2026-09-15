"""Video adapter orchestrator: dispatch to the provider's fixed protocol."""

from __future__ import annotations

from ..shared.types import ProviderConfig, VideoAdapterType, VideoRequest, VideoResult
from .grok import GrokAdapter
from .sora import SoraAdapter
from .unified_media import UnifiedMediaAdapter

ADAPTER_MAP = {
    VideoAdapterType.UNIFIED_MEDIA: UnifiedMediaAdapter,
    VideoAdapterType.SORA: SoraAdapter,
    VideoAdapterType.GROK: GrokAdapter,
}


class VideoGenerator:
    """Owns the active provider and dispatches generation requests to it."""

    def __init__(self, provider: ProviderConfig):
        self.provider = provider
        adapter_cls = ADAPTER_MAP.get(provider.type)
        if adapter_cls is None:
            raise ValueError(f"不支持的适配器类型: {provider.type}")
        self.adapter = adapter_cls(provider)

    async def generate(
        self, request: VideoRequest, *, should_cancel=None
    ) -> VideoResult:
        try:
            return await self.adapter.generate(request, should_cancel=should_cancel)
        except Exception as exc:
            return VideoResult(error=f"请求异常: {exc}")

    async def close(self) -> None:
        await self.adapter.close()
