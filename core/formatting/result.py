"""Result and start-message formatting."""

from __future__ import annotations

from ..shared.constants import UNSPECIFIED_LABEL
from ..tasks.models import VideoTaskRecord

DEFAULT_START_TEMPLATE = "已开始视频生成任务{reference_images_block} [{duration}s] [{aspect_ratio}] [任务ID: {task_id}]"


def format_start_task_message(
    template: str,
    *,
    task_id: str,
    prompt: str,
    duration: int,
    aspect_ratio: str,
    resolution: str,
    model: str,
    reference_image_count: int,
    mode: str,
    preset_names: list[str] | None = None,
) -> str:
    reference_images_block = ""
    if reference_image_count > 0:
        reference_images_block = f" [{reference_image_count}张参考图]"
    names = [name for name in (preset_names or []) if name]
    preset_block = f" [预设: {'、'.join(names)}]" if names else ""
    preset_joined = "、".join(names)
    duration_display = f"{duration}s" if duration else UNSPECIFIED_LABEL
    values = {
        "task_id": task_id,
        "prompt": prompt,
        "duration": duration_display,
        "seconds": duration_display,
        "aspect_ratio": aspect_ratio or UNSPECIFIED_LABEL,
        "resolution": resolution or UNSPECIFIED_LABEL,
        "model": model,
        "reference_image_count": str(reference_image_count),
        "reference_images_block": reference_images_block,
        "preset_block": preset_block,
        "preset": preset_joined,
        "presets": preset_joined,
        "mode": mode,
    }
    text = (template or "").strip() or DEFAULT_START_TEMPLATE
    try:
        return text.format(**values)
    except Exception:
        return DEFAULT_START_TEMPLATE.format(**values)


def format_task_detail(record: VideoTaskRecord) -> str:
    lines = [
        f"任务ID: {record.task_id}",
        f"状态: {record.status_label}",
        f"模式: {'图生视频' if record.mode == 'image' else '文生视频'}",
        f"模型: {record.model or '-'}",
        f"秒数: {f'{record.duration}s' if record.duration else UNSPECIFIED_LABEL}",
        f"比例: {record.aspect_ratio or UNSPECIFIED_LABEL}",
        f"分辨率: {record.resolution or UNSPECIFIED_LABEL}",
        f"参考图: {record.reference_image_count} 张",
        f"提示词: {record.prompt[:120]}",
    ]
    if record.upstream_request_id:
        lines.append(f"上游任务: {record.upstream_request_id}")
    if record.error:
        lines.append(f"错误: {record.error}")
    if record.duration_seconds is not None:
        lines.append(f"耗时: {record.duration_seconds:.1f}s")
    return "\n".join(lines)


def format_task_list(records: list[VideoTaskRecord]) -> str:
    if not records:
        return "当前没有进行中的视频任务"
    lines = ["进行中的视频任务:"]
    for index, record in enumerate(records, start=1):
        duration_display = f"{record.duration}s" if record.duration else UNSPECIFIED_LABEL
        lines.append(
            f"{index}. [{record.status_label}] {record.task_id} | {duration_display} {record.aspect_ratio or UNSPECIFIED_LABEL} | {record.prompt[:40]}"
        )
    return "\n".join(lines)


def format_result_info(record: VideoTaskRecord, items: list[str]) -> str:
    parts: list[str] = []
    mapping = {
        "模型": record.model,
        "耗时": f"{record.duration_seconds:.1f}s" if record.duration_seconds is not None else None,
        "任务ID": record.task_id,
        "秒数": f"{record.duration}s" if record.duration else UNSPECIFIED_LABEL,
        "比例": record.aspect_ratio or UNSPECIFIED_LABEL,
        "分辨率": record.resolution or UNSPECIFIED_LABEL,
    }
    for item in items:
        value = mapping.get(item)
        if value:
            parts.append(f"{item}: {value}")
    return " | ".join(parts)
