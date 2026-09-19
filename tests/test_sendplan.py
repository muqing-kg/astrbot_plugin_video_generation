"""Self-check for the single-form video send plan. Run: python tests/test_sendplan.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.shared.sendplan import build_video_component


class FakeVideo:
    """Mimics astrbot Video: fromURL rejects non-http, fromBase64 wraps bytes."""

    def __init__(self, file: str, **_):
        self.file = file
        self.kwargs = _

    @staticmethod
    def fromFileSystem(path, **_):
        return FakeVideo(file=Path(path).resolve(strict=False).as_uri(), path=str(path))

    @staticmethod
    def fromURL(url, **_):
        if not url.startswith(("http://", "https://")):
            raise ValueError("not a valid url")
        return FakeVideo(file=url)

    @staticmethod
    def fromBase64(data, **_):
        return FakeVideo(file=f"base64://{data}")


class FakeFile:
    """Mimics astrbot File: name is a required positional arg."""

    def __init__(self, name: str, file: str = "", url: str = ""):
        if not name:
            raise TypeError("name required")
        self.name = name
        self.file = file


class FakeComp:
    Video = FakeVideo
    File = FakeFile


with tempfile.TemporaryDirectory() as tmp:
    mp4 = Path(tmp) / "task.mp4"
    mp4.write_bytes(b"\x00\x01" * 64)

    # 1. callback configured + QQ: exactly one local-path Video (to_dict -> callback URL)
    comp, form = build_video_component(FakeComp, mp4, callback_configured=True, is_qq=True)
    assert form == "callback-url", form
    assert comp.file.startswith("file://") and comp.file.endswith("task.mp4"), comp.file

    # 2. callback configured + WeChat: same single form
    comp, form = build_video_component(FakeComp, mp4, callback_configured=True, is_qq=False)
    assert form == "callback-url" and comp.file.startswith("file://"), (form, comp.file)

    # 3. no callback + QQ: base64 inline, exactly one component
    comp, form = build_video_component(FakeComp, mp4, callback_configured=False, is_qq=True)
    assert form == "base64" and comp.file.startswith("base64://"), (form, comp.file)

    # 4. no callback + WeChat: no form exists; reason names the config gap
    comp, form = build_video_component(FakeComp, mp4, callback_configured=False, is_qq=False)
    assert comp is None and "callback_api_base" in form, (comp, form)

    # 5. inline ceiling: over the limit -> None with a reason, never a partial send
    comp, form = build_video_component(
        FakeComp, mp4, callback_configured=False, is_qq=True, max_inline_bytes=8
    )
    assert comp is None and "上限" in form, (comp, form)

    # 6. missing local file -> None with a reason
    comp, form = build_video_component(
        FakeComp, Path(tmp) / "ghost.mp4", callback_configured=False, is_qq=True
    )
    assert comp is None and "不可读" in form, (comp, form)

    # 7. comp library without Video class -> None with a reason
    class Empty:
        pass

    comp, form = build_video_component(Empty, mp4, callback_configured=True, is_qq=True)
    assert comp is None and "Video" in form, (comp, form)

print("sendplan self-check: all 7 cases passed")
