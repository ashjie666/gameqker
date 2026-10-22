"""新闻 / RSS 源：为上线倒计时补充 Steam 之外的发售与上线日期。

原则与官网巡检一致：只输出「能对上 games.json 里某款游戏」且「正文给出明确
年月日」的事件；匹配不到游戏、只有「即将上线」这类模糊措辞、或明显是版本
更新公告的，一律丢弃，绝不猜日期。

每条事件都带 sources（来源名称 + 原文链接），前端据此标注信息源。
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from html import unescape
from urllib.parse import quote

# 每天抓一次，单个源失败只跳过它自己，不影响其它源
RSS_SOURCES = (
    {"id": "gematsu", "name": "Gematsu", "url": "https://www.gematsu.com/feed"},
    {"id": "ps_blog", "name": "PlayStation Blog", "url": "https://blog.playstation.com/feed/"},
    {"id": "xbox_wire", "name": "Xbox Wire", "url": "https://news.xbox.com/en-us/feed/"},
    {"id": "gcores", "name": "机核", "url": "https://www.gcores.com/rss"},
    {"id": "steam_news", "name": "Steam 新闻", "url": "https://store.steampowered.com/feeds/news.xml"},
)

GOOGLE_NEWS_URL = (
    "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
)
STEAM_NEWS_API = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count=10&maxlength=400"
)

# 多语言「上线 / 发售」关键词：新闻源覆盖中文、英文、俄语、西语、日语
LAUNCH_RE = re.compile(
    r"上线|发售|开服|正式推出|上市|定档|官宣|首发|"
    r"launch(?:es|ed|ing)?|release[sd]?|comes out|hits (?:shelves|stores?)|"
    r"goes live|available on|debuts? on|arrives? on|"
    r"выход|официальн(?:ый|о) релиз|"
    r"se lanza|sale a|estreno|lanzamiento|"
    r"リリース|発売|配信開始",
    re.I)
# 版本更新、补丁、热修不算上线
PATCH_RE = re.compile(
    r"patch notes|hotfix|update \d|version \d|版本更新|更新公告|"
    r"v\d+\.\d+|\b\d+\.\d+\.\d+|补丁|热修|更新维护", re.I)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_MONTH_FIRST_RE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_DAY_FIRST_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})")

MAX_ITEMS_PER_SOURCE = 40
MIN_NAME_LEN_EN = 4   # 英文名太短容易误命中（如 "Void"）
MIN_NAME_LEN_CN = 2


def find_date(text):
    """从任意文本里找第一个「带年份」的日期，返回 (date, 原文片段) 或 (None, None)。"""
    match = _CN_DATE_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))), match.group(0)
        except ValueError:
            return None, None

    for pattern, order in ((_MONTH_FIRST_RE, "month_first"), (_DAY_FIRST_RE, "day_first")):
        for match in pattern.finditer(text):
            if order == "month_first":
                month_text, day, year = match.group(1), match.group(2), match.group(3)
            else:
                day, month_text, year = match.group(1), match.group(2), match.group(3)
            month = _MONTHS.get(month_text[:3].lower())
            if not month:
                continue
            try:
                return date(int(year), month, int(day)), match.group(0)
            except ValueError:
                continue
    return None, None


def _match_game(text, games):
    """按中文名 / 英文名在一段文本里找对应的游戏；找不到返回 None。"""
    lower = text.lower()
    for game in games:
        names = []
        name_cn = (game.get("name_cn") or "").strip()
        name_en = (game.get("name_en") or "").strip()
        if len(name_cn) >= MIN_NAME_LEN_CN:
            names.append((name_cn, False))
        if len(name_en) >= MIN_NAME_LEN_EN:
            names.append((name_en, True))
        for name, is_en in names:
            if is_en:
                if re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", lower):
                    return game
            elif name in text:
                return game
    return None


def _local(tag):
    return tag.split("}")[-1]


def _text(node):
    return unescape("".join(node.itertext())).strip()


def _parse_feed(body):
    """解析 RSS 2.0 / Atom，返回 [{title, link, summary, published}]。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields = {"title": "", "link": "", "summary": "", "published": ""}
        for child in node:
            name = _local(child.tag)
            if name in ("title", "description", "summary", "content"):
                fields["title" if name == "title" else "summary"] += " " + _text(child)
            elif name == "link":
                fields["link"] = (child.get("href") or "").strip() or _text(child)
            elif name in ("pubDate", "published", "updated"):
                fields["published"] = _text(child)
        if fields["title"]:
            items.append(fields)
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break
    return items


def _fetch(fetcher, fallback, url):
    """先直连，失败再走代理兜底；两者都失败返回最后一次结果。"""
    response = fetcher.get(url)
    if response.body or not fallback:
        return response
    return fallback.get(url)


def _in_window(found, today):
    # 已发售太久的新闻不再进倒计时，也不接受两年以上的远期日期
    return (today - timedelta(days=7)) <= found <= (today + timedelta(days=730))


def _event(game, found, raw, source):
    appid = (game.get("watch") or {}).get("steam_appid")
    return {
        "game_id": game.get("id"),
        "name_cn": game.get("name_cn"),
        "name_en": game.get("name_en"),
        "type": "launch",
        "date": found.isoformat(),
        "date_text": raw,
        "store_url": ("https://store.steampowered.com/app/{0}/".format(appid)
                      if appid else None),
        "sources": [source],
    }


def _rss_events(fetcher, fallback, games, today, seen):
    events = []
    for source in RSS_SOURCES:
        response = _fetch(fetcher, fallback, source["url"])
        if not response.body:
            continue
        for item in _parse_feed(response.body.decode("utf-8", "replace")):
            text = item["title"] + " " + item["summary"]
            if not LAUNCH_RE.search(text) or PATCH_RE.search(text):
                continue
            game = _match_game(text, games)
            if not game:
                continue
            found, raw = find_date(text)
            if not found or not _in_window(found, today):
                continue
            key = (game.get("id"), found.isoformat())
            if key in seen:
                continue
            seen.add(key)
            events.append(_event(game, found, raw, {
                "type": "rss",
                "name": source["name"],
                "url": item["link"] or source["url"],
                "title": item["title"].strip()[:120],
            }))
    return events


def _google_news_events(fetcher, fallback, games, today, seen):
    """按游戏名查 Google News RSS：比全站最新流命中率高得多。"""
    events = []
    for game in games:
        specs = []
        name_en = (game.get("name_en") or "").strip()
        name_cn = (game.get("name_cn") or "").strip()
        if len(name_en) >= MIN_NAME_LEN_EN:
            specs.append((name_en, "en-US", "US", "US:en", name_en))
        if len(name_cn) >= MIN_NAME_LEN_CN:
            specs.append((name_cn, "zh-CN", "CN", "CN:zh-Hans", name_cn))

        for name, hl, gl, ceid, needle in specs:
            url = GOOGLE_NEWS_URL.format(
                query=quote('"' + name + '"'), hl=hl, gl=gl, ceid=ceid)
            response = _fetch(fetcher, fallback, url)
            if not response.body:
                continue
            hits = 0
            for item in _parse_feed(response.body.decode("utf-8", "replace")):
                text = item["title"] + " " + item["summary"]
                if needle.lower() not in text.lower():
                    continue
                if not LAUNCH_RE.search(text) or PATCH_RE.search(text):
                    continue
                found, raw = find_date(text)
                if not found or not _in_window(found, today):
                    continue
                key = (game.get("id"), found.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                events.append(_event(game, found, raw, {
                    "type": "news",
                    "name": "Google 新闻",
                    "url": item["link"],
                    "title": item["title"].strip()[:120],
                }))
                hits += 1
            if hits:
                break  # 英文名已经命中就不再查中文名，省请求
    return events


def _steam_news_events(fetcher, fallback, games, today, seen):
    """Steam 官方公告里找「launch / release + 日期」，只查配置了 appid 的游戏。"""
    events = []
    for game in games:
        appid = (game.get("watch") or {}).get("steam_appid")
        if not appid:
            continue
        response = _fetch(fetcher, fallback, STEAM_NEWS_API.format(appid=appid))
        if not response.body:
            continue
        try:
            payload = json.loads(response.body.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            continue
        for item in (payload.get("appnews") or {}).get("newsitems") or []:
            title = str(item.get("title") or "")
            text = title + " " + str(item.get("contents") or "")
            if not LAUNCH_RE.search(text) or PATCH_RE.search(text):
                continue
            found, raw = find_date(text)
            if not found or not _in_window(found, today):
                continue
            key = (game.get("id"), found.isoformat())
            if key in seen:
                continue
            seen.add(key)
            events.append(_event(game, found, raw, {
                "type": "news",
                "name": "Steam 公告",
                "url": item.get("url"),
                "title": title[:120],
            }))
    return events


def collect(fetcher, payload, today=None, fallback=None):
    """抓取 RSS / 新闻 / Steam 公告，返回与 games.json 对得上的上线事件列表。

    fallback 是代理 Fetcher：海外源直连失败时用它重试，国内源不受影响。
    """
    today = today or date.today()
    games = (payload or {}).get("games") or []
    if not games:
        return []

    seen = set()
    events = []
    events += _rss_events(fetcher, fallback, games, today, seen)
    events += _google_news_events(fetcher, fallback, games, today, seen)
    events += _steam_news_events(fetcher, fallback, games, today, seen)
    return events
