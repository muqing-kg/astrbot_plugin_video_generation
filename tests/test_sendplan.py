"""Self-check for the shared video send-plan. Run: python tests/test_sendplan.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.shared.sendplan import build_video_candidates


class FakeVideo:
    """Mimics astrbot Video: fromURL rejects non-http, fromBase64 wraps bytes."""

    instances: list["FakeVideo"] = []

    def __init__(self, file: str, **_):
        self.file = file
        self.kwargs = _
        FakeVideo.instances.append(self)

    @staticmethod
    def fromFileSystem(path, **_):
        from pathlib import Path as P

        return FakeVideo(file=P(path).resolve(strict=False).as_uri(), path=str(path))

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


def kinds(candidates):
    out = []
    for c in candidates:
        if isinstance(c, FakeVideo):
            out.append("url" if c.file.startswith("http") else "base64" if c.file.startswith("base64://") else "file")
        else:
            out.append("File")
    return out


with tempfile.TemporaryDirectory() as tmp:
    mp4 = Path(tmp) / "task.mp4"
    mp4.write_bytes(b"\x00\x01" * 64)

    # 1. No callback host: base64 inline first, then URI/path Video, then File.
    FakeVideo.instances = []
    plan = build_video_candidates(FakeComp, mp4, "")
    assert kinds(plan) == ["base64", "file", "file", "File"], kinds(plan)
    assert plan[0].file == "base64://" + mp4.read_bytes().hex() or plan[0].file.startswith("base64://")
    assert plan[-1].name == "task.mp4" and plan[-1].file == str(mp4.resolve()), plan[-1].__dict__

    # 2. callback_api_base configured: no inline bytes (to_dict would treat base64 as path).
    plan = build_video_candidates(FakeComp, mp4, "", callback_configured=True)
    assert kinds(plan) == ["file", "file", "File"], kinds(plan)

    # 3. MP4 hygiene failed: inline is skipped even without callback host.
    plan = build_video_candidates(FakeComp, mp4, "", allow_inline=False)
    assert kinds(plan) == ["file", "file", "File"], kinds(plan)

    # 4. Oversized file: inline skipped by the byte ceiling.
    plan = build_video_candidates(FakeComp, mp4, "", max_inline_bytes=8)
    assert kinds(plan) == ["file", "file", "File"], kinds(plan)

    # 5. Public URL result: URL Video slot appears before the File fallback.
    plan = build_video_candidates(FakeComp, mp4, "https://gw.example/v/content")
    assert kinds(plan) == ["base64", "file", "file", "url", "File"], kinds(plan)

    # 6. Non-http result_url (bearer-protected gateway) never becomes a candidate.
    plan = build_video_candidates(FakeComp, mp4, "ftp://x")
    assert "url" not in kinds(plan), kinds(plan)

    # 7. Comp without File class: plan still returns the Video chain.
    class VideoOnly:
        Video = FakeVideo

    plan = build_video_candidates(VideoOnly, mp4, "")
    assert kinds(plan) == ["base64", "file", "file"], kinds(plan)

    # 8. Comp without Video class: only the File attachment remains.
    class FileOnly:
        File = FakeFile

    plan = build_video_candidates(FileOnly, mp4, "")
    assert kinds(plan) == ["File"], kinds(plan)

    # 9. Missing file: inline silently skipped, chain still buildable.
    ghost = Path(tmp) / "ghost.mp4"
    plan = build_video_candidates(FakeComp, ghost, "")
    assert kinds(plan) == ["file", "file", "File"], kinds(plan)

print("sendplan self-check: all 9 cases passed")
