"""官方站巡检：在官网页面里按规则表找「出海信号」关键词。

只输出确定性证据 `{matched, signals, pattern, snippet, url}`。
没命中一律写 unknown，**绝不推断**（宁可空着，也不能编一个发布日期）。
"""

from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .rules import SIGNAL_LABELS, label_for, paths_for, rules_for
from .textutil import clip, strip_html

MAX_PAGES = 2          # 最多查 2 个页面（主 URL + 规则表里第一个候选页）
SNIPPET_RADIUS = 80    # 命中位置前后各取多少字符做证据


def _candidate_urls(url):
    """主 URL 优先，其次补站点规则表里的候选子页（如 /news/、/faq/）。"""
    parts = urlsplit(url)
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    candidates = [url]
    for path in paths_for(url):
        candidate = origin + path
        if candidate.rstrip("/") != url.rstrip("/") and candidate not in candidates:
            candidates.append(candidate)
    return candidates[:MAX_PAGES]


def _scan(text, patterns):
    """返回 [(tag, weight, regex_text, snippet)]，按命中顺序。"""
    hits = []
    for compiled, tag, weight in patterns:
        match = compiled.search(text)
        if not match:
            continue
        start = max(0, match.start() - SNIPPET_RADIUS)
        end = min(len(text), match.end() + SNIPPET_RADIUS)
        hits.append((tag, weight, compiled.pattern, clip(text[start:end], 200)))
    return hits


def check_official(fetcher, url):
    """巡检一个官方站 URL，返回 evidence dict；失败时 error 非空、matched=False。"""
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    result = {
        "matched": False,
        "signals": [],
        "label": "unknown",
        "pattern": None,
        "snippet": None,
        "url": url,
        "site": label_for(url),
        "pages": [],
        "checked_at": checked_at,
        "error": None,
        "stale": False,
    }
    if not url:
        result["error"] = "no official url configured"
        return result

    _, patterns = rules_for(url)
    best = None  # (weight, tag, regex, snippet, page_url)
    pages_ok = 0
    errors = []

    for candidate in _candidate_urls(url):
        response = fetcher.get(candidate)
        if response.body is None:
            errors.append("{0}: {1}".format(candidate, response.error or "error"))
            result["pages"].append({"url": candidate, "status": response.status, "ok": False})
            # 整个 host 连不上（超时/DNS）时，再试它的子页也只会再等一轮；
            # 但 404/410 说明只是这个页面不存在，候选页仍值得一试。
            if response.status not in (404, 410):
                break
            continue
        if response.error:
            result["stale"] = True
        pages_ok += 1
        result["pages"].append({"url": candidate, "status": response.status, "ok": True})
        text = strip_html(response.body.decode("utf-8", "replace"))
        for tag, weight, regex_text, snippet in _scan(text, patterns):
            if best is None or weight > best[0]:
                best = (weight, tag, regex_text, snippet, candidate)

    if pages_ok == 0:
        result["error"] = "; ".join(errors) or "all pages failed"
        return result

    if best is not None:
        _, tag, regex_text, snippet, page_url = best
        result["matched"] = True
        result["signals"] = [tag]
        result["label"] = SIGNAL_LABELS.get(tag, tag)
        result["pattern"] = regex_text
        result["snippet"] = snippet
        result["url"] = page_url
    return result


def signal_key(value):
    """把 official_signal 压成一个可比较的短键，用于变更判定。

    只比较「命中与否 + 命中了哪些信号」，不比较原文片段——官网文案措辞微调
    不该天天算作变更。
    """
    if not isinstance(value, dict) or not value.get("matched"):
        return "unknown"
    return "matched:" + ",".join(sorted(value.get("signals") or []))
