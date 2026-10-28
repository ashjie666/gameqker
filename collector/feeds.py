"""新闻 / RSS / 资讯搜索：为上线倒计时补充 Steam 之外的发售与上线日期。

三条路：订阅源 RSS、按游戏名搜资讯（百度资讯，摘要没日期就抓原文正文）、
Steam 官方公告。原则与官网巡检一致：只输出「能对上 games.json 里某款游戏」
且「正文给出明确年月日」的事件；匹配不到游戏、只有「即将上线」这类模糊措辞、
或明显是版本更新公告的，一律丢弃，绝不猜日期。

每条事件都带 sources（来源名称 + 原文链接）和 market（cn / intl，标明消息属于
国服还是国际服），前端据此标注信息源，判定时两个市场分开算。
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote, urlparse

# 新闻源以国内为主：国内源用默认超时，海外源只给短超时，超时直接跳过不拖累整轮。
# overseas=True 的源失败是常态（跨境网络不稳），静默 skip，不算错误。
RSS_SOURCES = (
    # 国内游戏门户
    {"id": "gcores", "name": "机核", "url": "https://www.gcores.com/rss"},
    {"id": "yystv", "name": "游研社", "url": "https://www.yystv.cn/rss/feed"},
    {"id": "youxituoluo", "name": "游戏陀螺", "url": "https://www.youxituoluo.com/feed"},
    # 海外源：连得上就用，连不上就算了
    {"id": "gematsu", "name": "Gematsu", "url": "https://www.gematsu.com/feed", "overseas": True},
    {"id": "ps_blog", "name": "PlayStation Blog", "url": "https://blog.playstation.com/feed/", "overseas": True},
)

# 海外源单请求上限（秒）。跨境链路常年抖动，长超时只会把整轮巡检拖到几分钟。
OVERSEAS_TIMEOUT = 8

STEAM_NEWS_API = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count=10&maxlength=400"
)

# RSS 只覆盖最近几十条，冷门新作必须按游戏名主动去搜。
# 百度资讯的结果页是服务端渲染的，每条结果都带标题、摘要、发布日期和原文链接，
# 摘要里没有日期时再抓原文正文补一次——这是「拿到正文」的关键一步。
SEARCH_URL = "https://news.baidu.com/ns?word={term}&tn=news&rn=10&cl=2&ct=1"
MAX_SEARCH_RESULTS = 10   # 一次搜索最多看多少条结果
MAX_ARTICLE_FETCHES = 3   # 每款游戏最多抓几篇原文（摘要没日期时才抓）
ARTICLE_MAX_CHARS = 6000  # 正文只留前 6000 字符，够找日期就够了
# 这些站点没有可读的新闻正文（百科 / 问答 / 视频页），抓了也没用
SKIP_ARTICLE_HOSTS = (
    "baike.baidu.com", "zhihu.com", "bilibili.com", "weibo.com",
    "douyin.com", "xiaohongshu.com", "tieba.baidu.com",
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
# 从「10月29日」+ 发文日期补年份时，允许的最大前瞻天数
INFER_WINDOW_DAYS = 120
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


# 「10月29日」/「29 号」这类没写年份的日期；新闻正文里绝大多数都是这种写法
_MD_CN_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# 英文的 "October 29" / "Oct 29th"，后面不能直接跟年份（那种 find_date 已经处理）
_MD_EN_RE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{4})")


def find_undated(text):
    """找没写年份的「X 月 X 日」，返回 (month, day, 原文片段) 或 None。

    同一段里常同时出现两个日期（"10月27日开启预下载，10月29日解锁上线"），
    所以取离上线关键词最近的那一个，而不是第一个。
    """
    anchors = [match.start() for match in LAUNCH_RE.finditer(text)]
    if not anchors:
        return None
    best = None
    for match in _MD_CN_RE.finditer(text):
        month, day = int(match.group(1)), int(match.group(2))
        gap = min(abs(match.start() - anchor) for anchor in anchors)
        if best is None or gap < best[0]:
            best = (gap, month, day, match.group(0))
    for match in _MD_EN_RE.finditer(text):
        month = _MONTHS.get(match.group(1)[:3].lower())
        if not month:
            continue
        gap = min(abs(match.start() - anchor) for anchor in anchors)
        if best is None or gap < best[0]:
            best = (gap, month, int(match.group(2)), match.group(0))
    if best is None:
        return None
    return best[1], best[2], best[3]


def _published_date(item):
    """把 RSS pubDate / Atom published 解析成日期；解析不出返回 None。"""
    raw = (item.get("published") or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date()
    except (TypeError, ValueError, OverflowError):
        pass
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def resolve_date(item):
    """优先用正文里的完整年月日；没有年份就读发文日期补年份。

    中文游戏媒体报道发售日时几乎都只写「10月29日」，不写年份。完全丢掉这些
    新闻等于国内源一条都用不上，所以按发文年份补全；补出来的日期早于发文日期
    就顺延一年（12 月报道次年 1 月的事）。仍然补不出来就返回 (None, None)，
    绝不硬猜。

    补全出来的日期必须离发文日期不超过 INFER_WINDOW_DAYS：行业分析稿里顺口提
    到的游戏名旁边常挂着某个日期，靠年份顺延能凑出一年后的假发售日，这个窗口
    就是用来挡掉这种的。
    """
    text = item["title"] + " " + item["summary"]
    found, raw = find_date(text)
    if found:
        return found, raw
    published = _published_date(item)
    if not published:
        return None, None
    guess = find_undated(text)
    if not guess:
        return None, None
    month, day, raw = guess
    for year in (published.year, published.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None, None
        if candidate >= published:
            if (candidate - published).days > INFER_WINDOW_DAYS:
                return None, None
            return candidate, raw
    return None, None


_SQUASH_RE = re.compile(
    r"[\s\u3000·・:：\-—_()（）\[\]【】《》<>\"'!！?？,，.。、/\\|]+")


def _squash(text):
    """去掉标点空格再比较：媒体写法常是《无用之人:登神长阶》，版号名没有冒号。"""
    return _SQUASH_RE.sub("", (text or "").lower())


def _match_game(text, games):
    """按中文名 / 英文名在一段文本里找对应的游戏；找不到返回 None。"""
    lower = text.lower()
    squashed = _squash(text)
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
            elif name in text or _squash(name) in squashed:
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


def _fetch(fetcher, fallback, url, timeout=None):
    """先直连，失败再走代理兜底；两者都失败返回最后一次结果。"""
    response = fetcher.get(url, timeout=timeout)
    if response.body or not fallback:
        return response
    return fallback.get(url, timeout=timeout)


# --------------------------------------------------------------------------
# 资讯搜索 + 原文正文
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
# 百度资讯把每条结果的结构化数据放在 <!--s-data:{...}--> 里，
# 比解析 DOM 稳得多：标题、摘要、发布日期、来源一次拿全。
_S_DATA_RE = re.compile(r"<!--s-data:(\{.*?\})-->", re.S)
_REL_DAY_RE = re.compile(r"(\d+)\s*天前")


def _strip_tags(text):
    """去掉强调/高亮这类行内标签并反转义。

    不能把标签换成空格：百度资讯的标题是「《<em>影之刃零</em>》29日发售」，
    换成空格会变成「《 影之刃零 》」，游戏名就匹配不上了。
    """
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub("", text or ""))).strip()


_BLOCK_TAGS_RE = re.compile(
    r"</?(?:p|div|br|h[1-6]|li|tr|td|section|article|main|nav|header|footer)[^>]*>",
    re.I)


def _html_to_text(html):
    """正文 HTML 转纯文本：块级标签留空格断句，行内标签直接去掉。"""
    html = _BLOCK_RE.sub(" ", html or "")
    html = _BLOCK_TAGS_RE.sub(" ", html)
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub("", html))).strip()


def disp_time_iso(text, today=None):
    """把搜索结果的展示时间（2025年11月20日 / 3天前 / 昨天）折成 ISO 日期。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    match = (re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", raw)
             or re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw))
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)),
                        int(match.group(3))).isoformat()
        except ValueError:
            return ""
    today = today or date.today()
    if "前天" in raw:
        return (today - timedelta(days=2)).isoformat()
    if "昨天" in raw:
        return (today - timedelta(days=1)).isoformat()
    if "今天" in raw or "刚刚" in raw:
        return today.isoformat()
    match = _REL_DAY_RE.search(raw)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    return ""


def parse_search_items(body):
    """解析百度资讯结果页，返回 [{title, link, summary, published, source}]。"""
    html = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else (body or "")
    items = []
    for match in _S_DATA_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        title = _strip_tags(str(data.get("title") or ""))
        link = str(data.get("titleUrl") or "").strip()
        if not title or not link.startswith("http"):
            continue
        items.append({
            "title": title,
            "link": link,
            "summary": _strip_tags(str(data.get("summary") or "")),
            "published": disp_time_iso(data.get("dispTime")),
            "source": str(data.get("sourceName") or "").strip() or "百度资讯",
        })
        if len(items) >= MAX_SEARCH_RESULTS:
            break
    return items


def article_text(fetcher, url):
    """抓原文并把 HTML 剥成纯文本；拿不到正文返回 None。

    摘要里只写「定档 10 月 29 日」这种半句时，完整年月日往往在正文第一段，
    所以这一步是搜索路径能真正产出日期的前提。
    """
    if not url or not url.startswith("http"):
        return None
    host = urlparse(url).netloc.lower()
    if any(host == skip or host.endswith("." + skip) for skip in SKIP_ARTICLE_HOSTS):
        return None
    response = fetcher.get(url)
    if not response.body:
        return None
    return _html_to_text(response.body.decode("utf-8", "replace"))[:ARTICLE_MAX_CHARS] or None


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
        timeout = OVERSEAS_TIMEOUT if source.get("overseas") else None
        response = _fetch(fetcher, fallback, source["url"], timeout)
        if not response.body:
            continue  # 海外源超时/403 是常态，直接跳过这一条源
        for item in _parse_feed(response.body.decode("utf-8", "replace")):
            text = item["title"] + " " + item["summary"]
            if not LAUNCH_RE.search(text) or PATCH_RE.search(text):
                continue
            game = _match_game(text, games)
            if not game:
                continue
            found, raw = resolve_date(item)
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
                "market": "intl" if source.get("overseas") else "cn",
            }))
    return events


def _search_events(fetcher, games, today, seen):
    """按游戏名搜资讯：先看摘要里有没有日期，没有就抓原文正文再找。

    只处理「标题/摘要里既有上线关键词、又确实提到这款游戏」的结果：
    搜索对中文的分词很粗，同名页游、汉字解释都会被带出来，不收紧就会误报。
    """
    events = []
    for game in games:
        term = (game.get("name_cn") or "").strip()
        if len(term) < MIN_NAME_LEN_CN:
            continue
        response = fetcher.get(SEARCH_URL.format(term=quote(term.encode("utf-8"))))
        if not response.body:
            continue
        fetched = 0
        for item in parse_search_items(response.body):
            text = item["title"] + " " + item["summary"]
            if not LAUNCH_RE.search(text) or PATCH_RE.search(text):
                continue
            if not _match_game(text, [game]):
                continue
            found, raw = resolve_date(item)
            if not found and fetched < MAX_ARTICLE_FETCHES:
                fetched += 1
                body = article_text(fetcher, item["link"])
                if body:
                    found, raw = resolve_date({
                        "title": item["title"],
                        "summary": body,
                        "published": item["published"],
                    })
            if not found or not _in_window(found, today):
                continue
            key = (game.get("id"), found.isoformat())
            if key in seen:
                continue
            seen.add(key)
            events.append(_event(game, found, raw, {
                "type": "search",
                "name": item.get("source") or "百度资讯",
                "url": item["link"],
                "title": item["title"][:120],
                "market": "cn",
            }))
    return events


def _steam_news_events(fetcher, fallback, games, today, seen):
    """Steam 官方公告里找「launch / release + 日期」，只查配置了 appid 的游戏。"""
    events = []
    for game in games:
        appid = (game.get("watch") or {}).get("steam_appid")
        if not appid:
            continue
        response = _fetch(fetcher, fallback, STEAM_NEWS_API.format(appid=appid),
                          OVERSEAS_TIMEOUT)
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
                "market": "intl",
            }))
    return events


def probe_release_dates(fetcher, game, today=None):
    """一次抓齐国服 / 国际服两边的公告日期，返回 {"cn": point, "intl": point}。

    Steam 公告算国际服，国内媒体（RSS、资讯搜索）算国服——两边分别取最近的一条。
    只抓一轮就分流，避免同一款游戏把 RSS 和搜索重复跑两遍。
    """
    today = today or date.today()
    seen = set()
    events = _steam_news_events(fetcher, None, [game], today, seen)
    events += _rss_events(fetcher, None, [game], today, seen)
    events += _search_events(fetcher, [game], today, seen)
    found = {}
    for market in ("cn", "intl"):
        subset = [event for event in events
                  if (event["sources"][0] or {}).get("market") == market]
        if not subset:
            continue
        earliest = min(subset, key=lambda event: event["date"])
        source = (earliest.get("sources") or [{}])[0]
        found[market] = {
            "date": earliest["date"],
            "date_text": earliest.get("date_text"),
            "source": source.get("name"),
            "url": source.get("url"),
            "market": market,
        }
    return found


def probe_release_date(fetcher, game, today=None, market=None):
    """查单款游戏在公告 / 国内媒体里有没有明确的上线日期。

    market 限定市场（cn / intl）；不传则返回两个市场里最早的一条。
    拿不到日期就返回 None，绝不猜。
    """
    dates = probe_release_dates(fetcher, game, today)
    if market:
        return dates.get(market)
    if not dates:
        return None
    return min(dates.values(), key=lambda point: point["date"])


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
    events += _search_events(fetcher, games, today, seen)
    events += _steam_news_events(fetcher, fallback, games, today, seen)
    return events
