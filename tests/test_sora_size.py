"""Self-check for the Sora size mapping. Run: python tests/test_sora_size.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.shared.media import aspect_to_size

assert aspect_to_size("16:9", "720p") == "1280x720"
assert aspect_to_size("9:16", "720p") == "720x1280"
assert aspect_to_size("1:1", "720p") == "720x720"
assert aspect_to_size("21:9", "1080p") == "2520x1080"
assert aspect_to_size("4:3", "480p") == "640x480"
assert aspect_to_size("16:9", "2k") == "2560x1440"
assert aspect_to_size("16:9", "4k") == "3840x2160"
assert aspect_to_size("", "720p") is None
assert aspect_to_size("abc", "720p") is None
assert aspect_to_size("0:9", "720p") is None

print("sora size self-check: all 10 cases passed")
