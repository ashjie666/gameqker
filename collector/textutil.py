"""Small text helpers shared by the parser, extractor and deduper."""

import email.utils
import html
import re
from datetime import datetime, timezone

_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]*>")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

# Titles often carry the publisher name as a suffix: "Foo announced - Eurogamer"
_TITLE_SUFFIX_RE = re.compile(r"\s*[-–—|·]\s*[^-–—|·]{2,24}$")


def strip_html(value):
    """Turn a feed's HTML summary into plain text."""
    if not value:
        return ""
    text = html.unescape(str(value))
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def clip(value, limit):
    """Trim to `limit` characters on a word boundary where possible."""
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:，。；：") + "…"


def norm_title(value):
    """Normalize a headline for fuzzy comparison (not for display)."""
    text = strip_html(value).lower()
    text = _TITLE_SUFFIX_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def norm_entity(value):
    """Normalize a game / company name so 'Elden Ring™' == 'elden ring'."""
    if not value:
        return ""
    text = str(value).lower()
    text = text.replace("™", "").replace("®", "")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _to_iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_FALLBACK_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日",
    "%d %b %Y",
    "%b %d, %Y",
)


def parse_date(value):
    """Best-effort feed timestamp -> ISO 8601 UTC string (or None)."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            return _to_iso(dt)
    except (TypeError, ValueError, OverflowError):
        pass

    try:
        return _to_iso(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        pass

    for fmt in _FALLBACK_FORMATS:
        try:
            return _to_iso(datetime.strptime(raw[: len(fmt) + 6], fmt))
        except ValueError:
            continue
    return None
