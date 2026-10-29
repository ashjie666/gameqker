"""国服 / 海外服不同步游戏发现：Steam 美区新作 × NPPA 版号交叉。

漏斗是「Steam 先行」的：海外有没有动静由 Steam 美区列表决定，不是拿版号
名单逐款去搜 Steam。这样每轮只需要几条列表请求，而不是几百次搜索。

数据源（全部公开、免 Key，仅用标准库）：
- Steam 搜索列表：美区新发售 / 即将发售且支持简中的游戏（返回 JSON 里的
  results_html，一条请求一页），标题按 l=schinese 返回中文名，便于和版号名比对
- 国家新闻出版署「国产网络游戏审批信息」月度公示（列表页能拿到的全部批次，
  现约近一年半，作名称索引）
- Steam appdetails：只对「名字命中版号」的 appid 查 cc=us / cc=cn 的区服状态
- 新闻 / RSS / 百度资讯公告：补充国内定档日期

流程：Steam 美区列表 → 中文名与版号名比对（命中即确认「国产」并取版号
批准日）→ 只对这些 appid 查两国区详情 → 判「国服是否领先」。

圈子先定范围：只看国产游戏（进口批次是外国厂商的产品，不属目标），
且海外（Steam 美区）必须有商店页——没页面就无从谈「海外上线」，直接不算。
在这个范围里再筛「国服先于海外」的信息差。

判定口径（2026-10-28 与用户确认）：
在「国产 + 海外有页面」的范围里，留「国服先于海外」与「同日上线」——
- 国服已获版号，海外还在预约中，且美区定档日不早于版号批准日
- 国服已上线，海外还在预约
- 两边都已上线，国服上线日早于美区
- 两边都已上线且同一天：一律收录，标「国服国际服同步」
海外已发售而国服还没上线（版号批得再早也一样）属于海外先发，一律丢弃。

「信息优势」有三个判断依据（满足任一即可，2026-10-28 与用户确认）：
1. 先发：国服阶段或上线日期领先国际服
   - 阶段：已获版号 < 预约中 < 已上线，国服阶段高于美区即领先
   - 发售 / 上线日期：国服早于美区
   - 测试：美区仍在 Playtest，国服已上线或已定档，同样算领先
2. 版本领先：国服版本号高于国际服（国际服取 Steam 公告版本号）
3. 攻略成熟度：中文攻略检索量显著高于英文（Bing 结果数，中文 ≥20 条且 ≥英文 1.5 倍）
4. 同步上线：两国服同一天上线。按用户要求一律收录并统计，单独标注，
   不代表存在信息优势，只是不因「日期相同」被丢弃

前两项都取不到时不能当成「不领先」：版本 / 攻略查询失败返回 KEEP 等下轮重试。

结果写入候选池 data/candidates.json，由前端「新发现」区展示。
命中的候选随后由 collector/update.py 的 promote_candidates 自动并入 games 清单：
能从版号 / Steam 读出来的字段自动填，读不出来的定性字段留「待补」占位，
由人工复核时补，所以自动并入不会掩盖「这条还没人看过」。
"""

import json
import re
import time
from datetime import date, datetime
from html import unescape
from urllib.parse import quote, urljoin

from . import feeds
from .textutil import clip

NPPA_LIST_URL = "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/gcwlyxspxx/"
# 美区列表 + 简中标题：cc=us 保证「海外确实上架」，l=schinese 让标题是中文，
# 才能直接和版号名比对；supportedlang=schinese 先把没有简中的海外游戏滤掉。
STEAM_LIST_URL = (
    "https://store.steampowered.com/search/results/?query&start={start}&count={count}"
    "&sort_by=Released_DESC&supportedlang=schinese&cc=us&l=schinese&infinite=1"
)
STEAM_COMING_SOON_URL = (
    "https://store.steampowered.com/search/results/?query&start={start}&count={count}"
    "&filter=comingsoon&sort_by=Released_ASC&supportedlang=schinese&cc=us"
    "&l=schinese&infinite=1"
)
STEAM_LIST_PAGE_URL = (
    "https://store.steampowered.com/search/?filter=comingsoon"
    "&supportedlang=schinese&cc=us&l=schinese"
)
APPDETAILS_URL = (
    "https://store.steampowered.com/api/appdetails?appids={appid}&cc={cc}&l={lang}"
)
STEAM_STORE_URL = "https://store.steampowered.com/app/{appid}/"
STEAM_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count=5&maxlength=300"
)
STEAM_PAGE_URL = "https://store.steampowered.com/app/{appid}/?l=english&cc=us"
# 存量回填用：按名字在美区搜一款游戏（不限中文支持，搜索词就是版号名）
STEAM_SEARCH_URL = (
    "https://store.steampowered.com/search/results/?term={term}&start=0&count={count}"
    "&cc=us&l=schinese&infinite=1"
)

MAX_INDEX_BATCHES = 24   # 版号索引取尽列表页能拿到的批次（现约最近 15 个月）
STEAM_PAGE_SIZE = 100    # 列表一页（Steam 上限）
STEAM_MAX_PAGES = 3      # 新发售 / 即将发售各翻三页，覆盖约两周的新条目
MIN_MATCH_RATIO = 0.8    # 短名占长名的比例下限，低于此值视为不同游戏
MIN_MATCH_CHARS = 3      # 参与包含匹配的最短字数，避免两字短名乱撞
REFRESH_DAYS = 7         # 同一游戏多少天后重新对照 Steam
MAX_NEW_PER_RUN = 20     # 每轮最多新评估多少款，控制请求量与耗时
STATE_MAX_MONTHS = 12    # 状态文件保留最近一年内有巡检结果的条目
BACKFILL_PAGE_COUNT = 13 # NPPA 列表页共 13 页（2010 年至今约 180 个批次）
BACKFILL_SEARCH_COUNT = 20  # 定向搜索一次看前多少条结果
BACKFILL_REVISIT_DAYS = 180  # 已确认「搜不到」的游戏多久后重搜一次
BACKFILL_PICK_SCORE = 0.9   # 定向搜索的匹配下限，比列表比对更严（会写进主表）
BACKFILL_FAILURE_LIMIT = 8  # 连续这么多款都连不上就中止本轮（Steam 断链）
# api.steampowered.com 会出现「能建连但请求一直超时」的状态，每款都要白等一次
# 超时。连续失败这么多次就熔断：本轮后续跳过版本查询，版本按未知处理。
NEWS_FAILURE_LIMIT = 2

# 攻略成熟度：用 Bing 结果数当代理指标（Steam 指南页与评测接口都不可用）。
# 中文「游戏名 攻略」对比英文「"name" guide」，中文量太少或倍率不够都不算优势。
BING_URL = "https://www.bing.com/search?q={term}&mkt={mkt}"
BING_COUNT_RE = re.compile(r'class="sb_count"[^>]*>(.*?)</span>', re.S)
MIN_GUIDE_CN = 20        # 中文结果数下限，低于此值样本太小
MIN_GUIDE_RATIO = 1.5    # 中文结果数至少是英文的 1.5 倍

_PUNCT_RE = re.compile(r"[\s\u3000·・:：\-—_()（）\[\]【】《》<>\"'!！?？,，.。、/\\|]+")

# 「查询失败」和「判定不领先」必须分开：前者不代表任何结论。
# 遇到网络错误时 evaluate 返回这个哨兵，调用方保留上次结果并下轮重试，
# 否则一次网络抖动就会把候选池当成「全部不领先」清空。
KEEP = object()


def utc_now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# --------------------------------------------------------------------------
# 通用解析
# --------------------------------------------------------------------------
def _plain(fragment):
    """去标签、反转义、压缩空白。"""
    text = re.sub(r"<[^>]+>", "", fragment or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def normalize_name(name):
    """用于中文名相似度比较：去标点空白、全角转半角。"""
    text = (name or "").strip()
    text = text.replace("　", " ")
    for src, dst in (("：", ":"), ("（", "("), ("）", ")"), ("《", ""), ("》", "")):
        text = text.replace(src, dst)
    return _PUNCT_RE.sub("", text).lower()


def parse_cn_date(text):
    """支持 '2026年09月29日' 与 ISO '2026-09-29'（版号数据内部统一成后者）。"""
    raw = (text or "").strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", raw)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_steam_date(text):
    """兼容 'Oct 28, 2026' / '2026 年 10 月 28 日' / '28 Oct, 2026'。"""
    text = (text or "").strip()
    if not text:
        return None
    cn = parse_cn_date(text)
    if cn:
        return cn
    match = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s*(\d{4})", text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                return None
    match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(1)))
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------
# NPPA 版号
# --------------------------------------------------------------------------
_BATCH_LINK_RE = re.compile(
    r'href="(\./20\d{4}/t\d{8}_\d+\.html)"[^>]*>\s*([^<]+?)\s*</a>', re.S)
_PAGE_COUNT_RE = re.compile(r"_pageCount\s*=\s*(\d+)")


def fetch_batches(fetcher, list_url, keyword, limit, pages=1):
    """从审批列表页取批次。

    列表页的分页是 JS 生成的（index.html / index_1.html …），页码总数写在
    页面的 _pageCount 变量里。日常巡检只取第一页（最新批次），存量回填要
    把 pages 开到全部页，才能覆盖 2010 年以来的历史批次。
    """
    batches = []
    seen = set()
    page_count = max(1, pages)
    for page in range(page_count):
        url = list_url if page == 0 else "{0}index_{1}.html".format(list_url, page)
        response = fetcher.get(url)
        if not response.body:
            break
        html = response.body.decode("utf-8", "replace")
        if page == 0:
            match = _PAGE_COUNT_RE.search(html)
            if match:
                page_count = min(page_count, max(1, int(match.group(1))))
        found = 0
        for href, title in _BATCH_LINK_RE.findall(html):
            title = _plain(title)
            if keyword not in title:
                continue
            batch_url = urljoin(list_url, href)
            if batch_url in seen:
                continue
            seen.add(batch_url)
            batches.append({"title": title, "url": batch_url})
            found += 1
            if len(batches) >= limit:
                return batches
        # 该页一条都没解析出来说明已经到底（最后一页可能不满）
        if not found:
            break
    return batches


def _row_fields(cells):
    """把一行 td 映射成字段。

    页面里「申报类别」列是用 document.write 动态生成的，剔掉 script 后
    该列会消失，所以真实 td 可能是 8 列（含类别）或 7 列（不含类别）。
    """
    if len(cells) >= 8:
        return {"name": cells[1], "publisher": cells[3], "operator": cells[4],
                "approval_no": cells[5], "isbn": cells[6], "approved": cells[7]}
    if len(cells) >= 7:
        return {"name": cells[1], "publisher": cells[2], "operator": cells[3],
                "approval_no": cells[4], "isbn": cells[5], "approved": cells[6]}
    return None


def parse_batch(fetcher, batch):
    """解析一个批次详情页里的游戏行。

    列序：序号 / 名称 / [申报类别] / 出版单位 / 运营单位 / 批复文号 /
    出版物号 / 批准时间。
    """
    response = fetcher.get(batch["url"])
    if not response.body:
        return []
    html = response.body.decode("utf-8", "replace")
    rows = re.findall(r"<tr[^>]*class=['\"]item['\"][^>]*>(.*?)</tr>", html, re.S)
    games = []
    for row in rows:
        cells = [_plain(cell) for cell in
                 re.findall(r"<td[^>]*>(.*?)</td>", re.sub(r"<script.*?</script>", "", row, flags=re.S), re.S)]
        fields = _row_fields(cells)
        if not fields or not fields["name"]:
            continue
        approved = parse_cn_date(fields["approved"])
        games.append({
            "name": fields["name"],
            "publisher": fields["publisher"],
            "operator": fields["operator"],
            "approval_no": fields["approval_no"],
            "isbn": fields["isbn"],
            "approved_at": approved.isoformat() if approved else None,
            "batch": batch["title"],
            "batch_url": batch["url"],
        })
    return games


# --------------------------------------------------------------------------
# Steam 对照
# --------------------------------------------------------------------------
def parse_list_html(html):
    """从搜索结果 HTML 里取 (appid, 标题)。

    Steam 的无限滚动接口把每条结果包在 <a data-ds-appid="..."> 里，
    标题在紧随其后的 <span class="title"> 中。
    """
    rows = []
    seen = set()
    for appid, title in re.findall(
            r'data-ds-appid="(\d+)".*?<span class="title">(.*?)</span>', html or "", re.S):
        if appid in seen:
            continue
        seen.add(appid)
        rows.append((int(appid), _plain(title)))
    return rows


_DEMO_SUFFIX_RE = re.compile(
    r"(?:demo|playtest|试玩版|测试版|beta|体验版)\s*$", re.I)


def _is_demo(title):
    """Demo / Playtest 的 appid 是独立条目，发售日也是测试页的，不能当正式版。"""
    return bool(_DEMO_SUFFIX_RE.search((title or "").strip()))


def steam_new_releases(fetcher, pages=STEAM_MAX_PAGES, deadline=None):
    """抓美区「新发售 + 即将发售」且支持简中的列表。

    返回 [{appid, title, coming_soon}]。列表抓失败只返回空列表：列表里没有
    某款游戏不能当作「它不领先」，所以这里不需要 KEEP 语义——只有真的对某个
    appid 查过详情，才可能下结论。
    """
    releases = {}
    for template, coming_soon in ((STEAM_LIST_URL, False), (STEAM_COMING_SOON_URL, True)):
        for page in range(pages):
            if deadline is not None and time.monotonic() >= deadline:
                return list(releases.values())
            response = fetcher.get(template.format(
                start=page * STEAM_PAGE_SIZE, count=STEAM_PAGE_SIZE))
            if not response.body:
                break
            try:
                payload = json.loads(response.body.decode("utf-8", "replace"))
            except (ValueError, AttributeError):
                break
            html = str((payload or {}).get("results_html") or "")
            rows = parse_list_html(html)
            if not rows:
                break
            for appid, title in rows:
                if not title or _is_demo(title):
                    continue
                releases.setdefault(appid, {
                    "appid": appid, "title": title, "coming_soon": coming_soon,
                })
            if len(rows) < STEAM_PAGE_SIZE:
                break
    return list(releases.values())


def steam_search(fetcher, term, count=BACKFILL_SEARCH_COUNT):
    """按名字在 Steam 美区搜一款游戏，返回 [(appid, title)]。

    None 与 [] 语义不同：None 是「查询失败」，不能当成「没有页面」；
    [] 才是真的搜不到。调用方据此决定要不要写「没页面」的结论。
    """
    response = fetcher.get(STEAM_SEARCH_URL.format(
        term=quote(term or ""), count=count))
    if not response.body:
        return None
    try:
        payload = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    return parse_list_html(str((payload or {}).get("results_html") or ""))


def title_score(name, title):
    """版号名与 Steam 标题的相似分：精确 1.0，包含关系按长度占比。"""
    left, right = norm_title(name), norm_title(title)
    if len(left) < MIN_MATCH_CHARS or len(right) < MIN_MATCH_CHARS:
        return 0.0
    if left == right:
        return 1.0
    if left in right:
        return len(left) / len(right)
    if right in left:
        return len(right) / len(left)
    return 0.0


def pick_search_match(name, rows):
    """在搜索结果里挑与版号名最像的一条，返回 (appid, title, score) 或 None。

    搜索结果里常混进同系列其它作品、DLC 和 Demo，demo 直接跳过；分数低于
    BACKFILL_PICK_SCORE 一律不认——回填结果会被自动并进主表，宁可判「没页面」
    也不能把《古剑奇谭二》当成《古剑奇谭》配进去。
    """
    best = None
    for appid, title in rows or []:
        if _is_demo(title):
            continue
        score = title_score(name, title)
        if score < BACKFILL_PICK_SCORE:
            continue
        if best is None or score > best[2]:
            best = (appid, title, score)
    return best


def norm_title(title):
    """列表标题用于比对前的清洗：去掉 Demo/试玩版后缀再归一化。"""
    text = _DEMO_SUFFIX_RE.sub("", (title or "").strip())
    return normalize_name(text)


def build_approval_index(games):
    """把版号游戏折成 {归一化名: 游戏}，同名保留批准时间最新的一条。"""
    index = {}
    for game in games or []:
        normalized = norm_title(game.get("name"))
        if not normalized:
            continue
        old = index.get(normalized)
        if not old or (game.get("approved_at") or "") > (old.get("approved_at") or ""):
            index[normalized] = game
    return index


def match_approval(title, index):
    """在版号索引里找这款 Steam 游戏，返回 (游戏, 匹配分) 或 None。

    先按归一化名精确命中；命不中再用包含关系兜住「版号名只是主标题、Steam
    标题带副标题」这类写法，但要求短名占长名六成以上，且至少三个字，
    否则「龙魂」会撞上「龙魂天下」这种完全不同的游戏。
    """
    target = norm_title(title)
    if len(target) < MIN_MATCH_CHARS:
        return None
    exact = index.get(target)
    if exact:
        return exact, 1.0
    best = None
    for name, game in index.items():
        if len(name) < MIN_MATCH_CHARS:
            continue
        if name in target:
            score = len(name) / len(target)
        elif target in name:
            score = len(target) / len(name)
        else:
            continue
        if score < MIN_MATCH_RATIO:
            continue
        if best is None or score > best[1]:
            best = (game, score)
    return best


def check_app(fetcher, appid, cc, lang):
    url = APPDETAILS_URL.format(appid=appid, cc=cc, lang=lang)
    result = {"listed": None, "name": None, "release_date": None,
              "coming_soon": None, "release_parsed": None, "error": None}
    response = fetcher.get(url)
    if not response.body:
        result["error"] = response.error or "fetch failed"
        return result
    try:
        payload = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        result["error"] = "decode failed"
        return result
    entry = payload.get(str(appid))
    if not isinstance(entry, dict):
        result["error"] = "unexpected response shape"
        return result
    if not entry.get("success"):
        result["listed"] = False
        return result
    data = entry.get("data") or {}
    release = data.get("release_date") or {}
    parsed = parse_steam_date(release.get("date"))
    result.update({
        "listed": True,
        "name": clip(data.get("name"), 120) or None,
        "release_date": clip(release.get("date"), 60) or None,
        "coming_soon": bool(release.get("coming_soon")),
        "release_parsed": parsed.isoformat() if parsed else None,
    })
    return result


def stage_of(entry, approved_at=None):
    """把一台区服状态折算成阶段：none < approved < preorder < launch。

    approved 只对国服有意义：已获版号，但 Steam 国区还没有页面。
    """
    if entry.get("listed") is not True:
        return "approved" if approved_at else "none"
    if entry.get("coming_soon") is True:
        return "preorder"
    return "launch"


_VERSION_RE = re.compile(
    r"(?:patch|update|version|hotfix|build)\s*v?(\d+(?:\.\d+){0,3})", re.I)
# 中文写作「版本 2.5」「更新 1.2.3」，要求至少带一个小数点，免得把「更新 2 个」
# 这类数量词当版本号
_VERSION_ZH_RE = re.compile(r"(?:版本|更新|补丁)\s*v?(\d+(?:\.\d+){1,3})", re.I)
_VERSION_BARE_RE = re.compile(r"\bv(\d+(?:\.\d+){1,3})\b", re.I)
# 正文里版本号后面常直接接字母（v1.0.2.2Balance），不能要求词边界
_VERSION_BODY_RE = re.compile(r"\bv(\d+(?:\.\d+){1,3})", re.I)


# 版本公告接口的熔断状态：open 后本轮不再请求，失败计数与跳过数留作收尾提示。
_news_state = {"failures": 0, "open": False, "skipped": 0}


def reset_news_breaker():
    """每轮开始重置熔断：上一轮的断链不应该让这一轮一开始就放弃版本查询。"""
    _news_state.update(failures=0, open=False, skipped=0)


def news_breaker_open():
    return _news_state["open"]


def news_breaker_skipped():
    return _news_state["skipped"]


def fetch_latest_version(fetcher, appid):
    """从 Steam 最新公告里取版本号，取不到返回 None。

    公告标题经常只写「Patch Notes / Update 15」，版本号落在正文开头，
    所以标题没有时再退到 contents（接口已截断到 300 字符）。
    接口本身不通时熔断（见 NEWS_FAILURE_LIMIT），避免每款都白等一次超时。
    """
    if _news_state["open"]:
        _news_state["skipped"] += 1
        return None
    response = fetcher.get(STEAM_NEWS_URL.format(appid=appid))
    if not response.body:
        _news_state["failures"] += 1
        if _news_state["failures"] >= NEWS_FAILURE_LIMIT:
            _news_state["open"] = True
        return None
    _news_state["failures"] = 0
    try:
        payload = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    items = (payload.get("appnews") or {}).get("newsitems") or []
    for item in items[:5]:
        title = str(item.get("title") or "")
        match = _VERSION_RE.search(title) or _VERSION_BARE_RE.search(title)
        if not match:
            match = _VERSION_BODY_RE.search(str(item.get("contents") or ""))
        if match:
            return {"value": match.group(1), "source": title[:100],
                    "at": item.get("date")}
    return None


def detect_playtest(fetcher, appid):
    """抓商店页看是否有 Playtest 入口。抓不到返回 None（不当作否定）。"""
    response = fetcher.get(STEAM_PAGE_URL.format(appid=appid))
    if not response.body:
        return None
    html = response.body.decode("utf-8", "replace").lower()
    return "playtest" in html and ("join playtest" in html or "sign up" in html)


# --------------------------------------------------------------------------
# 信息优势的两个辅助维度：版本领先 / 攻略成熟度
# --------------------------------------------------------------------------
# 「1.2.3 版本更新」「1.2.3 更新」这类中文写法，版本号在关键词前面
_VERSION_CTX_RE = re.compile(r"(\d+(?:\.\d+){1,3})\s*(?:版本|更新|补丁|patch|hotfix)", re.I)
_ZH_UPDATE_TERM = "{name} 版本更新"


def extract_version(text):
    """从一段文本里抽版本号；优先带关键词上下文的写法，避免误抓日期。"""
    for pattern in (_VERSION_RE, _VERSION_ZH_RE, _VERSION_BARE_RE, _VERSION_CTX_RE):
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return None


def version_tuple(value):
    parts = []
    for piece in (value or "").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or None


def version_ahead(cn_value, intl_value):
    """国服版本是否高于国际服。补齐位数再比，1.2 与 1.2.0 视为相同。

    任一边取不到版本号都返回 None（未知），不当作「不领先」。
    """
    cn, intl = version_tuple(cn_value or ""), version_tuple(intl_value or "")
    if not cn or not intl:
        return None
    width = max(len(cn), len(intl))
    return cn + (0,) * (width - len(cn)) > intl + (0,) * (width - len(intl))


_VERSION_WORD_RE = re.compile(r"版本|更新|补丁|patch|hotfix|update", re.I)


def fetch_cn_version(fetcher, name_cn):
    """从国内资讯里抽国服版本号：百度资讯「游戏名 版本更新」→ 标题 / 正文。

    返回 {value, source, url} / None（没有可用公告）/ KEEP（搜索请求失败）。

    两条硬门槛挡住「无关资讯里的数字」：标题必须提到这款游戏（不能只出现在
    摘要里），且标题要明确是版本 / 更新相关；正文里也必须再次出现游戏名。
    """
    response = fetcher.get(feeds.SEARCH_URL.format(
        term=quote(_ZH_UPDATE_TERM.format(name=name_cn))))
    if not response.body:
        return KEEP
    target = normalize_name(name_cn)
    if not target:
        return None
    for item in feeds.parse_search_items(response.body)[:feeds.MAX_ARTICLE_FETCHES]:
        title = item.get("title") or ""
        if target not in normalize_name(title) or not _VERSION_WORD_RE.search(title):
            continue
        value = extract_version(title)
        if not value:
            body = feeds.article_text(fetcher, item.get("link")) or ""
            if target in normalize_name(body):
                value = extract_version(body[:1200])
        if value:
            return {"value": value, "source": clip(title, 100),
                    "url": item.get("link")}
    return None


def bing_result_count(fetcher, term, mkt):
    """Bing 搜索结果数，作为攻略 / 讨论成熟度的代理指标。

    抓不到页面返回 KEEP（网络失败，结论未知）；页面正常但没有结果数文案
    返回 None（结构变化或该查询无统计），不当作否定结论。
    """
    response = fetcher.get(BING_URL.format(term=quote(term), mkt=mkt))
    if not response.body:
        return KEEP
    match = BING_COUNT_RE.search(response.body.decode("utf-8", "replace"))
    if not match:
        return None
    digits = re.sub(r"\D", "", unescape(match.group(1)))
    return int(digits) if digits else None


def evaluate_guides(fetcher, name_cn, name_en):
    """比较中英文攻略检索量。返回 {cn, intl, ratio, advantage} 或 KEEP。"""
    cn = bing_result_count(fetcher, "{0} 攻略".format(name_cn), "zh-CN")
    if cn is KEEP:
        return KEEP
    intl = bing_result_count(fetcher, '"{0}" guide'.format(name_en), "en-US") if name_en else None
    if intl is KEEP:
        return KEEP
    ratio = round(cn / intl, 1) if (cn and intl) else None
    return {
        "cn": cn,
        "intl": intl,
        "ratio": ratio,
        "advantage": bool(cn and intl and cn >= MIN_GUIDE_CN and cn >= intl * MIN_GUIDE_RATIO),
    }


def evaluate_signals(fetcher, name_cn, name_en, appid):
    """版本领先 + 攻略成熟度。返回 (version, guides)，分别为 dict / None / KEEP。"""
    steam = fetch_latest_version(fetcher, appid)
    intl = steam["value"] if steam else None
    cn_version = fetch_cn_version(fetcher, name_cn)
    if cn_version is KEEP:
        version = KEEP
    else:
        value = cn_version["value"] if cn_version else None
        version = {
            "cn": value,
            "cn_source": cn_version["source"] if cn_version else None,
            "cn_url": cn_version["url"] if cn_version else None,
            "steam": intl,
            "source": steam["source"] if steam else None,
            "ahead": version_ahead(value, intl) is True,
        }
    return version, evaluate_guides(fetcher, name_cn, name_en)


STAGE_TEXT = {"none": "无页面", "approved": "已获版号", "preorder": "预约中", "launch": "已上线"}
# 时间轴里每个时间点的性质：已上线 / 预约定档 / 仅版号获批（不是上线日）
BASIS_TEXT = {"released": "已上线", "preorder": "预约中", "approved": "版号获批", "unknown": "无公开时间"}
STORE_URL = "https://store.steampowered.com/app/{0}/"


def _point(when, basis, source=None, url=None, estimated=False):
    """时间轴上的一个点。date 缺失时保留 basis，用来说明「处于阶段但没有日期」。"""
    return {
        "date": when.isoformat() if when else None,
        "basis": basis,
        "estimated": estimated,
        "source": source,
        "url": url,
    }


def build_timeline(nppa_item, us, cn, appid=None):
    """把国服 / 国际服两边的上市时间折成可比较的时间轴。

    来源与优先级：
    - 国际服：Steam 美区（页面缺失 → unknown）
    - 国服：Steam 国区优先；没有国区页面时退回版号批准日，
      但版号只代表「有上线资格」，不是上线日，标 estimated，
      前端显示成「版号获批 · 日期」，不会被当成已上线时间。
    两边都拿不到日期时保留 basis 说明当前阶段，不猜日期。
    """
    approved = parse_cn_date(nppa_item.get("approved_at") or "")
    us_release = parse_steam_date(us.get("release_date"))
    cn_release = parse_steam_date(cn.get("release_date"))
    store = STORE_URL.format(appid) if appid else None

    if us.get("listed") is not True:
        intl = _point(None, "unknown")
    elif us.get("coming_soon") is True:
        intl = _point(us_release, "preorder", "Steam 商店（国际服）", store)
    else:
        intl = _point(us_release, "released", "Steam 商店（国际服）", store)

    if cn.get("listed") is True:
        basis = "preorder" if cn.get("coming_soon") is True else "released"
        domestic = _point(cn_release, basis, "Steam 商店（国服）", store)
    elif approved:
        domestic = _point(approved, "approved", "版号公示",
                          nppa_item.get("batch_url"), estimated=True)
    else:
        domestic = _point(None, "unknown")

    return {"cn": domestic, "intl": intl}


def enrich_from_news(fetcher, candidate, timeline, today=None):
    """Steam 给不出上市时间时，用公告把国服 / 国际服的时间补成真实日期。

    - 国服：目前只有版号批准日（下界）时，改用国内媒体的定档 / 上线公告；
      公告日期早于版号日的不可能是国服，忽略。
    - 国际服：Steam 没有日期时用 Steam 公告补；公告日期早于国服时间说明海外先发，
      该候选直接淘汰（返回 None）。

    两边的消息严格按市场分流，不拿一篇中文首发稿同时充当国服和国际服的时间。
    命中率不高（冷门新作基本没有报道），拿不到就保持原时间轴。
    """
    cn_basis = timeline["cn"].get("basis")
    if cn_basis == "unknown":
        return timeline
    needs_cn = cn_basis == "approved"
    needs_intl = not timeline["intl"].get("date")
    if not needs_cn and not needs_intl:
        return timeline

    found = feeds.probe_release_dates(fetcher, {
        "id": candidate.get("name_cn"),
        "name_cn": candidate.get("name_cn"),
        "name_en": candidate.get("name_en"),
        "watch": {"steam_appid": candidate.get("appid")},
    }, today)

    approved = timeline["cn"].get("date")
    if needs_cn and found.get("cn"):
        point = found["cn"]
        if not approved or point["date"] >= approved:
            timeline["cn"] = {
                "date": point["date"],
                "basis": "announced",
                "estimated": False,
                "source": point.get("source") or "国内媒体公告",
                "url": point.get("url"),
            }

    if needs_intl and found.get("intl"):
        point = found["intl"]
        cn_date = timeline["cn"].get("date")
        if cn_date and point["date"] < cn_date:
            return None  # 公告显示国际服更早，不算国服领先
        timeline["intl"] = {
            "date": point["date"],
            "basis": "announced",
            "estimated": False,
            "source": point.get("source") or "Steam 公告",
            "url": point.get("url"),
        }
    return timeline


def classify(nppa_item, us, cn, cn_stage=None, os_stage=None):
    """只输出「国服先于海外」的候选，返回 (gap_type, gap_days, note) 或 None。

    前提：美区必须有商店页（listed=True，含 coming soon）。没页面说明海外还
    没动静，不属于「海外上线的国产游戏」，不评。

    领先的三种情形：
    1. 海外在预约，国服已上线 → 国服上线领先
    2. 海外在预约且定档日不早于国服版号批准日 → 版号领先（算出提前天数）
    3. 两边都已上线，国服上线日早于海外 → 上线领先
    4. 两边都已上线且同一天 → cn_sync（无先发优势，但按要求收录统计）
    海外已发售而国服还没上线（版号批得再早也一样）属于海外先发，丢弃。
    """
    approved = parse_cn_date(nppa_item.get("approved_at") or "")
    us_release = parse_steam_date(us.get("release_date"))
    cn_release = parse_steam_date(cn.get("release_date"))
    cn_stage = cn_stage or stage_of(cn, nppa_item.get("approved_at"))
    os_stage = os_stage or stage_of(us)

    if us.get("listed") is not True:
        return None  # 海外没有商店页，不在目标范围

    if us.get("coming_soon") is True:
        if cn_stage == "launch":
            gap = (cn_release - us_release).days if (cn_release and us_release) else None
            return "cn_first", gap, "国服已上线，Steam 美区仍在预约"
        if approved and us_release and us_release > approved:
            return "cn_first", (us_release - approved).days, "国服版号早于 Steam 美区定档"
        return None  # 两边都还没上线，谈不上领先

    if cn_stage == "launch":
        if cn_release and us_release and cn_release < us_release:
            return "cn_first", (us_release - cn_release).days, "国服上线早于 Steam 美区发售"
        if cn_release and us_release and cn_release == us_release:
            # 同日上线没有先发优势，但按用户要求一律收录，单独标注
            return "cn_sync", 0, "国服与国际服同日上线"
        return None  # 国服更晚上线，不构成国服领先

    # 美区已发售、国服还没上线：海外先发。版号批准日再早也不构成「国服领先」，
    # 因为国内玩家此刻还玩不到，谈不上对海外有内容优势。
    return None


def evaluate_app(fetcher, nppa_item, appid, steam_title=None, score=None, probe_news=True):
    """对「名字已命中版号」的 appid 查两国区详情并判定。

    候选条件（满足任一即可）：
    1. 先发：国服阶段或上线日期领先国际服（classify）
    2. 版本领先：国服版本号高于国际服
    3. 攻略成熟度：中文攻略检索量显著高于英文
    4. 同步上线：两国服同一天上线，一律收录（classify 直接给 cn_sync）

    返回候选 dict / None（确认无信息优势）/ KEEP（关键信号查询失败，结论未知）。

    只有「美区与国区详情都正常返回」时才敢下结论：请求失败时不返回 None，
    否则网络一断就会被当成「这款不领先」，把候选池清空。
    """
    us = check_app(fetcher, appid, "us", "english")
    if us.get("listed") is None:
        # 美区详情没取到：既不能确认领先，也不能确认不领先
        return KEEP
    if us.get("listed") is not True:
        return None  # 美区没有页面，不属「海外上线」范围
    cn = check_app(fetcher, appid, "cn", "schinese")
    if cn.get("listed") is None:
        return KEEP

    cn_stage = stage_of(cn, nppa_item.get("approved_at"))
    os_stage = stage_of(us)
    name_cn = nppa_item["name"]
    name_en = clip(us.get("name") or steam_title, 120)

    verdict = classify(nppa_item, us, cn, cn_stage, os_stage)
    gap_type = gap_days = note = None
    timeline = build_timeline(nppa_item, us, cn, appid)
    if verdict:
        gap_type, gap_days, note = verdict
        if probe_news:
            timeline = enrich_from_news(fetcher, {
                "name_cn": name_cn, "name_en": name_en, "appid": appid,
            }, timeline)
            if timeline is None:
                return None  # 公告显示国际服更早，不是国服领先

    # 版本与攻略：先发成立时只做补充说明；先发不成立时才是判定依据，
    # 所以任一信号查询失败都必须返回 KEEP，不能当成「没有优势」。
    version, guides = evaluate_signals(fetcher, name_cn, name_en, appid)
    if not verdict and (version is KEEP or guides is KEEP):
        return KEEP
    version_info = version if isinstance(version, dict) else None
    guides_info = guides if isinstance(guides, dict) else None

    advantages = []
    if gap_type:
        advantages.append(gap_type)
    if version_info and version_info.get("ahead"):
        advantages.append("cn_version")
    if guides_info and guides_info.get("advantage"):
        advantages.append("cn_guides")
    if not advantages:
        return None

    notes = [note] if note else []
    if "cn_version" in advantages:
        notes.append("国服版本 v{0} 高于 Steam 公告 v{1}".format(
            version_info["cn"], version_info["steam"]))
    if "cn_guides" in advantages:
        notes.append("中文攻略检索量 {0:,} 条，是英文 {1:,} 条的 {2} 倍".format(
            guides_info["cn"], guides_info["intl"], guides_info["ratio"]))
    note = "；".join(notes)
    gap_type = gap_type or advantages[0]

    playtest = detect_playtest(fetcher, appid)
    if playtest and os_stage == "preorder":
        note = note + "（美区仍在 Playtest 测试）"

    return {
        "origin": nppa_item.get("origin"),
        "name_cn": name_cn,
        "name_en": name_en,
        "appid": appid,
        "gap_type": gap_type,
        "gap_days": gap_days,
        "advantages": advantages,
        "note": note,
        "timeline": timeline,
        "stage": {
            "cn": cn_stage,
            "os": os_stage,
            "cn_release": cn.get("release_parsed"),
            "os_release": us.get("release_parsed"),
        },
        "version": version_info or {
            "cn": None, "cn_source": None, "cn_url": None,
            "steam": None, "source": None, "ahead": False,
        },
        "guides": guides_info or {
            "cn": None, "intl": None, "ratio": None, "advantage": False,
        },
        "playtest_os": playtest,
        "nppa": {
            "approved_at": nppa_item.get("approved_at"),
            "publisher": nppa_item.get("publisher"),
            "operator": nppa_item.get("operator"),
            "approval_no": nppa_item.get("approval_no"),
            "batch": nppa_item.get("batch"),
            "batch_url": nppa_item.get("batch_url"),
        },
        "steam": {
            "store_url": STEAM_STORE_URL.format(appid=appid),
            "us_listed": us.get("listed"),
            "us_release_date": us.get("release_date"),
            "us_release_parsed": us.get("release_parsed"),
            "us_coming_soon": us.get("coming_soon"),
            "cn_listed": cn.get("listed"),
            "cn_release_date": cn.get("release_date"),
            "cn_release_parsed": cn.get("release_parsed"),
            "match_score": round(score, 2) if score is not None else None,
        },
        "evidence": [nppa_item.get("batch_url"), STEAM_STORE_URL.format(appid=appid)],
        "first_seen": utc_now_iso(),
    }


# --------------------------------------------------------------------------
# 状态与入口
# --------------------------------------------------------------------------
def _is_stale(entry, today):
    raw = (entry or {}).get("checked_at") or ""
    try:
        checked = date.fromisoformat(raw[:10])
    except ValueError:
        return True
    # 判定结构升级过（例如新增 timeline / guides）的旧候选要重评一次，
    # 否则旧候选会一直缺新字段，直到自然过期 7 天才补上。
    # 只针对「已有候选」：candidate 为空的条目本来就没有 timeline，
    # 若无条件重评，确认过不领先的游戏会每轮都占用评估预算。
    candidate = (entry or {}).get("candidate")
    if candidate and ("timeline" not in candidate or "guides" not in candidate):
        return True
    return (today - checked).days >= REFRESH_DAYS


def collect_games(fetcher, months=MAX_INDEX_BATCHES, pages=1):
    """抓国产审批列表的若干批次。

    只取国产：进口批次是外国厂商的产品，不在「国产游戏出海」的目标范围内。
    这些批次只用来做名称索引，不再逐款去搜 Steam（定向搜索见 backfill）。
    pages>1 时按列表页分页翻，存量回填用它拿全历史批次。
    """
    games = []
    for batch in fetch_batches(fetcher, NPPA_LIST_URL, "国产网络游戏审批信息",
                               months, pages=pages):
        for game in parse_batch(fetcher, batch):
            game["origin"] = "domestic"
            games.append(game)
    games.sort(key=lambda g: g.get("approved_at") or "", reverse=True)
    return games


def _month_cutoff(today, months):
    if today.month <= months:
        return "{0}-{1:02d}".format(today.year - 1, today.month - months + 12)
    return "{0}-{1:02d}".format(today.year, today.month - months)


def run(fetcher, state, max_new=MAX_NEW_PER_RUN, deadline=None, today=None):
    """Steam 美区新作 → 版号交叉 → 返回 (candidates, state, sources, stats)。"""
    today = today or date.today()
    reset_news_breaker()
    items = state.get("items") or {}

    # 版号只作名称索引：命中即确认「国产」并拿到批准日。
    # 索引窗口由 collect_games 的批次数决定（列表页能拿到的都拿），不能再按
    # 批准时间裁剪——一款前年获批的游戏完全可能今年才在海外上线。
    index = build_approval_index(collect_games(fetcher))

    # 状态按「上次巡检时间」清理：按批准时间清会把早年获批、今年才出海
    # 的候选直接丢掉，而这类恰恰是最该盯的。
    cutoff = _month_cutoff(today, STATE_MAX_MONTHS)
    items = {k: v for k, v in items.items()
             if (v.get("checked_at") or "")[:7] >= cutoff}

    releases = steam_new_releases(fetcher, deadline=deadline)
    stats = {"steam_listed": len(releases), "nppa_games": len(index),
             "nppa_matched": 0, "evaluated": 0, "matched": 0,
             "skipped_budget": 0, "deferred": 0}
    seen = set()
    for release in releases:
        hit = match_approval(release["title"], index)
        if not hit:
            continue
        game, score = hit
        key = "{0}:{1}".format(game.get("origin"), normalize_name(game["name"]))
        if key in seen:
            continue
        seen.add(key)
        stats["nppa_matched"] += 1
        entry = items.get(key)
        if entry and not _is_stale(entry, today):
            continue
        if stats["evaluated"] >= max_new:
            stats["skipped_budget"] += 1
            continue
        if deadline is not None and time.monotonic() >= deadline:
            stats["skipped_budget"] += 1
            continue
        stats["evaluated"] += 1
        try:
            candidate = evaluate_app(fetcher, game, release["appid"],
                                     release.get("title"), score)
        except Exception as exc:  # 单款失败不影响整轮
            stats.setdefault("errors", []).append("{0}: {1}".format(game["name"], type(exc).__name__))
            stats["deferred"] += 1
            continue
        if candidate is KEEP:
            # 查询失败：保留上次结果，不更新 checked_at，下轮自然会重试
            stats["deferred"] += 1
            continue
        if candidate:
            stats["matched"] += 1
        items[key] = {
            "checked_at": today.isoformat(),
            "approved_at": game.get("approved_at"),
            "candidate": candidate,
        }

    candidates = candidates_from_items(items)

    state = {"updated_at": utc_now_iso(), "items": items}
    sources = [
        {"origin": "intl", "title": "Steam 美区新发售 / 即将发售（支持简中）",
         "url": STEAM_LIST_PAGE_URL},
        {"origin": "domestic", "title": "国产网络游戏审批信息（列表页全部批次）",
         "url": NPPA_LIST_URL},
    ]
    return candidates, state, sources, stats


def candidates_from_items(items):
    """把状态里的候选折成候选列表，按「提前天数 → 名称」排序。"""
    candidates = []
    for entry in (items or {}).values():
        candidate = entry.get("candidate")
        if candidate:
            candidate["last_checked"] = entry.get("checked_at")
            candidates.append(candidate)
    candidates.sort(key=lambda c: (
        -(c.get("gap_days") or 0),
        c.get("name_cn") or "",
    ))
    return candidates


def _recent(record, today, days):
    """记录的 checked_at 是否在 days 天内；日期缺失或损坏都当作「太久远」。"""
    try:
        return (today - date.fromisoformat(str(record.get("checked_at"))[:10])).days < days
    except (TypeError, ValueError):
        return False


def backfill(fetcher, state, seen, games, budget=60, deadline=None, today=None):
    """存量回填：版号索引逐款按名搜 Steam，命中就照常评估。

    与 run() 的区别只在「怎么找到 appid」：run() 从 Steam 新发售列表出发（只能
    发现刚出海的增量），这里反过来拿全量版号名去搜，把早已出海的作品补上。

    seen 是回填自己的进度表，和候选状态分开：候选状态只存评估过的游戏，而回填
    还要记住「搜过但搜不到」，否则每轮都会把同一批没出海的游戏重搜一遍。
    查询失败（rows is None）不写进度，下一轮自动重试。

    返回 (candidates, state, seen, stats)。
    """
    today = today or date.today()
    reset_news_breaker()
    items = state.get("items") or {}
    stats = {"indexed": len(games or []), "attempted": 0, "hit": 0,
             "evaluated": 0, "matched": 0, "no_page": 0,
             "deferred": 0, "skipped": 0}
    failures = 0
    for game in games or []:
        if budget and stats["attempted"] >= budget:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        # 连续多款都连不上说明 Steam 这一侧断了，继续跑只是白等超时
        if failures >= BACKFILL_FAILURE_LIMIT:
            stats["aborted"] = True
            break
        name = game.get("name")
        key = "{0}:{1}".format(game.get("origin"), normalize_name(name))
        record = seen.get(key) or {}
        appid = record.get("appid")
        title = record.get("title") or name
        score = record.get("score")
        if appid:
            entry = items.get(key)
            if entry and not _is_stale(entry, today):
                stats["skipped"] += 1
                continue
        elif record.get("status") == "no_page" \
                and _recent(record, today, BACKFILL_REVISIT_DAYS):
            stats["skipped"] += 1
            continue
        # 到这里这轮一定要动网络（搜索或直接评估），占一个预算名额
        stats["attempted"] += 1
        if not appid:
            rows = steam_search(fetcher, name)
            if rows is None:           # 查询失败：不写结论，下轮重试
                stats["deferred"] += 1
                failures += 1
                continue
            failures = 0
            match = pick_search_match(name, rows)
            if not match:
                seen[key] = {"checked_at": today.isoformat(), "status": "no_page"}
                stats["no_page"] += 1
                continue
            appid, title, score = match
            seen[key] = {"checked_at": today.isoformat(), "status": "page",
                         "appid": appid, "title": title, "score": round(score, 2)}
            stats["hit"] += 1
        stats["evaluated"] += 1
        try:
            candidate = evaluate_app(fetcher, game, appid, title, score)
        except Exception as exc:  # 单款失败不影响整轮
            stats.setdefault("errors", []).append(
                "{0}: {1}".format(name, type(exc).__name__))
            stats["deferred"] += 1
            continue
        if candidate is KEEP:
            stats["deferred"] += 1
            failures += 1
            continue
        failures = 0
        if candidate:
            stats["matched"] += 1
        items[key] = {
            "checked_at": today.isoformat(),
            "approved_at": game.get("approved_at"),
            "candidate": candidate,
        }
    else:
        # 没被预算 / 超时 / 断链打断，说明整个索引已经走完一遍
        stats["done"] = True

    state["updated_at"] = utc_now_iso()
    state["items"] = items
    return candidates_from_items(items), state, seen, stats


def build_payload(candidates, batches, stats, today=None, source_name=None):
    today = today or date.today()
    return {
        "generated_at": today.isoformat(),
        "generated_at_utc": utc_now_iso(),
        "source": {
            "name": source_name or "Steam 美区新作（支持简中）× 国家新闻出版署 · 国产网络游戏审批信息",
            "url": NPPA_LIST_URL,
            "steam": "Steam 搜索列表（cc=us / l=schinese）+ appdetails（cc=us / cc=cn）",
        },
        "batches": batches,
        "stats": stats,
        "candidates": candidates,
        "note": "命中候选由 collector/update.py 自动并入 data/games.json，定性字段留「待补」待人复核。",
    }
