"""Self-check for payload adaptation. Run: python tests/test_payload_adapt.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.payload_adapt import adapt_payload

BASE = {
    "model": "seedance-2.5",
    "prompt": "a cat running on the beach",
    "duration": 6,
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "image": {"url": "https://example.com/a.jpg"},
}

# Case 1: user-reported Seedance gateway error -> image object flattened to string.
ERR_IMAGE = (
    '{"code":"invalid_json","message":"json: cannot unmarshal object into '
    'Go struct field .Alias.image of type string","data":null}'
)
out = adapt_payload(dict(BASE), ERR_IMAGE)
assert out is not None and out["image"] == "https://example.com/a.jpg", out

# Case 2: data-URL image flattens as-is.
payload = dict(BASE)
payload["image"] = {"url": "data:image/jpeg;base64,AAAA"}
out = adapt_payload(payload, ERR_IMAGE)
assert out is not None and out["image"] == "data:image/jpeg;base64,AAAA", out

# Case 3: image already a string -> no adaptation (error must be about something else).
payload = dict(BASE)
payload["image"] = "https://x/y.jpg"
assert adapt_payload(payload, ERR_IMAGE) is None

# Case 4: unknown field rename aspect_ratio -> ratio.
out = adapt_payload(dict(BASE), 'json: unknown field "aspect_ratio"')
assert out is not None and out.get("ratio") == "16:9" and "aspect_ratio" not in out, out

# Case 5: unknown field drop (no rename hint for resolution).
out = adapt_payload(dict(BASE), 'json: unknown field "resolution"')
assert out is not None and "resolution" not in out and "aspect_ratio" in out, out

# Case 6: unknown duration renames to seconds as string (Sora style).
out = adapt_payload(dict(BASE), 'json: unknown field "duration"')
assert out is not None and out.get("seconds") == "6" and "duration" not in out, out

# Case 7: duration demanded as string.
out = adapt_payload(
    dict(BASE),
    "json: cannot unmarshal number into Go struct field Req.duration of type string",
)
assert out is not None and out.get("duration") == "6", out

# Case 8: plain-English field type error.
out = adapt_payload(dict(BASE), "`image` must be a string")
assert out is not None and out["image"] == "https://example.com/a.jpg", out

# Case 9: irrelevant error -> untouched.
assert adapt_payload(dict(BASE), "upstream exploded") is None

# Case 10: numeric demand on a numeric string.
payload = dict(BASE)
payload["duration"] = "6"
out = adapt_payload(
    payload,
    "json: cannot unmarshal string into Go struct field Req.duration of type int",
)
assert out is not None and out.get("duration") == 6 and isinstance(out["duration"], int), out

# Case 11: value rejected -> drop the field, keep the rest (model default).
out = adapt_payload(dict(BASE), '{"error":"duration must be one of [5, 10]"}')
assert out is not None and "duration" not in out and "aspect_ratio" in out, out

# Case 12: unsupported aspect value -> drop aspect_ratio only.
out = adapt_payload(dict(BASE), "Invalid aspect_ratio 21:9; supported values: 16:9, 9:16")
assert out is not None and "aspect_ratio" not in out and "duration" in out, out

# Case 13: constraint keyword but no droppable field named -> untouched.
assert adapt_payload(dict(BASE), "Invalid API key provided") is None

# Case 14: droppable field word but no constraint keyword -> untouched.
assert adapt_payload(dict(BASE), "quota exceeded for resolution this month") is None

print("payload_adapt self-check: all 14 cases passed")
