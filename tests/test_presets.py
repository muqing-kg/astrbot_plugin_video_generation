"""Self-check for presets and parser changes. Run: python tests/test_presets.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.generation.parser import parse_video_command, normalize_aspect
from core.generation.presets import combine_prompt, match_presets, parse_presets

# --- parse_presets ---
entries = [
    "电影感:电影感画面，浅景深",
    "赛博朋克：赛博朋克城市夜景，霓虹灯",  # Chinese colon
    '夜景:{"prompt":"城市夜景延时","aspect_ratio":"21:9","resolution":"4k","duration":8,"model":"seedance-2.5"}',
    "坏条目没有冒号",
    "空提示词:",
]
presets = parse_presets(entries)
assert set(presets) == {"电影感", "赛博朋克", "夜景"}, set(presets)
assert presets["电影感"].prompt == "电影感画面，浅景深"
assert presets["夜景"].prompt == "城市夜景延时"
assert presets["夜景"].aspect_ratio == "21:9"
assert presets["夜景"].resolution == "4k"
assert presets["夜景"].duration == 8
assert presets["夜景"].model == "seedance-2.5"

# later entries overwrite, 不指定 values are cleared
presets2 = parse_presets(["A:旧", "A:新", 'B:{"prompt":"x","aspect_ratio":"不指定"}'])
assert presets2["A"].prompt == "新"
assert presets2["B"].aspect_ratio == ""

# --- match_presets: leading consecutive, longest first, each once ---
presets3 = parse_presets(["电影感:画面", "电影感人像:人物", "猫:喵"])
matched, rest = match_presets("电影感 猫 在跑", presets3)
assert [p.name for p in matched] == ["电影感", "猫"] and rest == "在跑", (matched, rest)
matched, rest = match_presets("电影感人像 微笑", presets3)
assert [p.name for p in matched] == ["电影感人像"] and rest == "微笑"
# mid-string names are NOT matched by default
matched, rest = match_presets("一只电影感的猫", presets3)
assert matched == [] and rest == "一只电影感的猫"

# --- combine_prompt ---
assert combine_prompt([presets3["电影感"]], "") == "画面"
assert combine_prompt([presets3["电影感"]], "海边") == "画面\n\n海边"
multi = combine_prompt([presets3["电影感"], presets3["猫"]], "海边")
assert multi == "[预设提示词]\n画面\n喵\n\n[附加提示词]\n海边", multi
assert combine_prompt([], "只有附加") == "只有附加"

# --- parser: any W:H accepted, duration_explicit flag ---
parsed = parse_video_command("/视频 6s 21:9 海浪")
assert parsed.duration == 6 and parsed.aspect_ratio == "21:9"
assert parsed.duration_explicit and parsed.aspect_explicit and parsed.prompt == "海浪"

parsed = parse_video_command("/视频 海浪", default_aspect_ratio="", default_resolution="")
assert parsed.aspect_ratio == "" and parsed.resolution == ""
assert not parsed.duration_explicit

parsed = parse_video_command("/视频 电影感 6s 海浪")
assert parsed.prompt == "电影感 海浪" and parsed.duration == 6 and parsed.duration_explicit

# --- positional params in ANY order incl. resolution ---
parsed = parse_video_command("/视频 720p 9:16 6s 城市夜景")
assert parsed.duration == 6 and parsed.aspect_ratio == "9:16" and parsed.resolution == "720p"
assert parsed.prompt == "城市夜景"
assert parsed.duration_explicit and parsed.aspect_explicit and parsed.resolution_explicit

# --- inline resolution extraction (with/without Chinese prefix) ---
parsed = parse_video_command("/视频 分辨率1080p 海浪")
assert parsed.resolution == "1080p" and parsed.resolution_explicit and parsed.prompt == "海浪"
parsed = parse_video_command("/视频 4k 城市夜景延时")
assert parsed.resolution == "4k" and parsed.resolution_explicit and parsed.prompt == "城市夜景延时"

# --- duration default 0 = 不指定 ---
parsed = parse_video_command("/视频 海浪", default_duration=0)
assert parsed.duration == 0 and not parsed.duration_explicit

assert normalize_aspect("9/16") == "9:16"
assert normalize_aspect("100:200") == "100:200"
assert normalize_aspect("0:9") is None
assert normalize_aspect("横屏") == "16:9"
assert normalize_aspect("abc") is None

print("presets & parser self-check: all cases passed")
