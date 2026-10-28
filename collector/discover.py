"""国服 / 海外服不同步游戏发现：NPPA 版号公示 × Steam 区服对照。

数据源（全部公开、免 Key，仅用标准库）：
- 国家新闻出版署「国产网络游戏审批信息」月度公示（近两月）
- 国家新闻出版署「进口网络游戏审批信息」年度累计（当年）
- Steam storesearch：按游戏名找 appid
- Steam appdetails：分别查 cc=us / cc=cn 的区服状态

进口批次排在前面优先处理：判断「国服是否领先」必须先在 Steam 找到页面，
进口批次几乎都有页面；国产批次以手游为主，Steam 命中率极低，只作补充。

判定口径（2026-10-22 与用户确认，当日修正方向）：
只找「国服领先」——国服已获版号，而海外（Steam 美区）尚未上架、尚未发售，
或发售日不早于国服版号批准日：
- cn_first：国服版号早于美区发售 / 美区尚未定档 / 美区没有页面
海外先发（美区已发售且早于国服版号）不是目标，直接丢弃。

「领先」同时看四个维度（2026-10-22 用户补充）：
- 阶段：无页面 < 已获版号 < 预约中 < 已上线，国服阶段高于美区即领先
- 发售 / 上线日期：国服早于美区
- 版本号：取 Steam 最新公告里的版本号（Steam 公告按 appid 共享，无国区单独源）
- 测试：美区仍在 Playtest，国服已上线或已定档，同样算领先

结果写入候选池 data/candidates.json，由前端「新发现」区展示。
命中的候选随后由 collector/update.py 的 promote_candidates 自动并入 games 清单：
能从版号 / Steam 读出来的字段自动填，读不出来的定性字段留「待补」占位，
由人工复核时补，所以自动并入不会掩盖「这条还没人看过」。
"""

import json
import re
import time
from datetime import date, datetime
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import quote, urljoin

from . import feeds
from .textutil import clip

NPPA_LIST_URL = "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/gcwlyxspxx/"
NPPA_IMPORT_LIST_URL = "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/jkwlyxspxx/"
STEAM_SEARCH_URL = (
    "https://store.steampowered.com/api/storesearch/?term={term}&cc=cn&l=schinese"
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

MAX_BATCHES = 2          # 只处理最新的两个月度批次
MIN_SIMILARITY = 0.72    # 中文名匹配阈值，低于此值视为不同游戏
REFRESH_DAYS = 7         # 同一游戏多少天后重新对照 Steam
MAX_NEW_PER_RUN = 20     # 每轮最多新评估多少款，控制请求量与耗时
STATE_MAX_MONTHS = 6     # 状态文件只保留最近半年的版号游戏

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
def fetch_batches(fetcher, list_url, keyword, limit):
    """从审批列表页取最新若干批次（国产按月、进口按年累积）。"""
    response = fetcher.get(list_url)
    if not response.body:
        return []
    html = response.body.decode("utf-8", "replace")
    pattern = re.compile(
        r'href="(\./20\d{4}/t\d{8}_\d+\.html)"[^>]*>\s*([^<]+?)\s*</a>', re.S
    )
    batches = []
    seen = set()
    for href, title in pattern.findall(html):
        title = _plain(title)
        if keyword not in title:
            continue
        url = urljoin(list_url, href)
        if url in seen:
            continue
        seen.add(url)
        batches.append({"title": title, "url": url})
        if len(batches) >= limit:
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
def steam_search(fetcher, term):
    """返回搜索结果列表；请求失败返回 None（与「搜到了但没匹配」区分开）。"""
    url = STEAM_SEARCH_URL.format(term=quote(term.encode("utf-8")))
    response = fetcher.get(url)
    if not response.body:
        return None
    try:
        payload = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    return items if isinstance(items, list) else []


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


def match_appid(fetcher, nppa_item):
    """按中文名搜索 Steam，并用国区详情页的中文名复核，避免同名误配。

    返回 dict（命中）/ None（确认为没找到）/ KEEP（请求失败，结论未知）。
    """
    target = normalize_name(nppa_item["name"])
    if not target:
        return None
    items = steam_search(fetcher, nppa_item["name"])
    if items is None:
        return KEEP
    failed = False
    for item in items[:3]:
        appid = item.get("id")
        if not isinstance(appid, int):
            continue
        cn = check_app(fetcher, appid, "cn", "schinese")
        if cn.get("listed") is None:  # 详情页没取到，可能是这款，也可能不是
            failed = True
            continue
        if cn.get("listed") is not True:
            continue
        steam_name = normalize_name(cn.get("name") or "")
        if not steam_name:
            continue
        score = SequenceMatcher(None, target, steam_name).ratio()
        if score >= MIN_SIMILARITY or target in steam_name or steam_name in target:
            return {"appid": appid, "name": cn.get("name"), "cn": cn, "score": round(score, 2)}
    return KEEP if failed else None


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
    r"(?:patch|update|version|hotfix|build)\s*v?(\d+(?:\.\d+){1,3})", re.I)
_VERSION_BARE_RE = re.compile(r"\bv(\d+(?:\.\d+){1,3})\b", re.I)
# 正文里版本号后面常直接接字母（v1.0.2.2Balance），不能要求词边界
_VERSION_BODY_RE = re.compile(r"\bv(\d+(?:\.\d+){1,3})", re.I)


def fetch_latest_version(fetcher, appid):
    """从 Steam 最新公告里取版本号，取不到返回 None。

    公告标题经常只写「Patch Notes / Update 15」，版本号落在正文开头，
    所以标题没有时再退到 contents（接口已截断到 300 字符）。
    """
    response = fetcher.get(STEAM_NEWS_URL.format(appid=appid))
    if not response.body:
        return None
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
    """只输出「国服领先」的候选，返回 (gap_type, gap_days, note) 或 None。

    领先的四种情形（按阶段优先级）：
    1. 海外还没页面（none），国服已获版号 → 国服先行
    2. 海外在预约（preorder），国服已上线（launch） → 国服上线领先
    3. 海外定档日不早于国服版号批准日 → 版号领先（算出提前天数）
    4. 两边都已上线，国服上线日早于海外 → 上线领先
    海外先发（美区已发售且早于国服版号）不是目标，一律丢弃。
    """
    approved = parse_cn_date(nppa_item.get("approved_at") or "")
    us_release = parse_steam_date(us.get("release_date"))
    cn_release = parse_steam_date(cn.get("release_date"))
    cn_stage = cn_stage or stage_of(cn, nppa_item.get("approved_at"))
    os_stage = os_stage or stage_of(us)

    if us.get("listed") is not True:
        return "cn_first", None, "国服已获批版号，Steam 美区无页面或未上架"

    if us.get("coming_soon") is True:
        if cn_stage == "launch":
            gap = (cn_release - us_release).days if (cn_release and us_release) else None
            return "cn_first", gap, "国服已上线，Steam 美区仍在预约"
        if approved and us_release and us_release > approved:
            return "cn_first", (us_release - approved).days, "国服版号早于 Steam 美区定档"
        return None  # 两边都还没上线，谈不上领先

    if cn_stage == "launch" and cn_release and us_release and cn_release < us_release:
        return "cn_first", (us_release - cn_release).days, "国服上线早于 Steam 美区发售"

    if us_release and approved:
        if us_release >= approved:
            return "cn_first", (us_release - approved).days, "国服版号早于 Steam 美区发售"
        return None  # 美区已发售且早于国服版号 → 海外先发，不是目标
    return None


def evaluate(fetcher, nppa_item, probe_news=True):
    """返回候选 dict / None（确认不领先）/ KEEP（查询失败，结论未知）。

    只有「Steam 搜索与详情页都正常返回」时才敢下结论：请求失败时不返回 None，
    否则网络一断就会被当成「这款不领先」，把候选池清空。
    """
    matched = match_appid(fetcher, nppa_item)
    if matched is KEEP:
        return KEEP
    if not matched:
        return None
    appid = matched["appid"]
    cn = matched["cn"]
    us = check_app(fetcher, appid, "us", "english")
    if us.get("listed") is None:
        # 美区详情没取到：既不能确认领先，也不能确认不领先
        return KEEP
    cn_stage = stage_of(cn, nppa_item.get("approved_at"))
    os_stage = stage_of(us)
    verdict = classify(nppa_item, us, cn, cn_stage, os_stage)
    if not verdict:
        return None
    gap_type, gap_days, note = verdict

    timeline = build_timeline(nppa_item, us, cn, appid)
    if probe_news:
        timeline = enrich_from_news(fetcher, {
            "name_cn": nppa_item.get("name"),
            "name_en": clip(us.get("name") or matched.get("name"), 120),
            "appid": appid,
        }, timeline)
        if timeline is None:
            return None  # 公告显示国际服更早，不是国服领先

    # 阶段与版本只在确认是候选后再抓，避免对全部游戏额外发请求
    version = fetch_latest_version(fetcher, appid)
    playtest = detect_playtest(fetcher, appid)
    if playtest and os_stage == "preorder":
        note = note + "（美区仍在 Playtest 测试）"

    return {
        "origin": nppa_item.get("origin"),
        "name_cn": nppa_item["name"],
        "name_en": clip(us.get("name") or matched.get("name"), 120),
        "appid": appid,
        "gap_type": gap_type,
        "gap_days": gap_days,
        "note": note,
        "timeline": timeline,
        "stage": {
            "cn": cn_stage,
            "os": os_stage,
            "cn_release": cn.get("release_parsed"),
            "os_release": us.get("release_parsed"),
        },
        "version": {
            "steam": version["value"] if version else None,
            "source": version["source"] if version else None,
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
            "match_score": matched.get("score"),
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
    # 判定结构升级过（例如新增 timeline）的旧候选要重评一次，
    # 否则旧候选会一直缺新字段，直到自然过期 7 天才补上。
    # 只针对「已有候选」：candidate 为空的条目本来就没有 timeline，
    # 若无条件重评，确认过不领先的游戏会每轮都占用评估预算。
    candidate = (entry or {}).get("candidate")
    if candidate and "timeline" not in candidate:
        return True
    return (today - checked).days >= REFRESH_DAYS


def collect_games(fetcher):
    """抓进口（当年累计）+ 国产（近两月）两个审批来源，进口排前面。

    判断「国服是否领先」必须先在 Steam 找到页面：进口批次几乎都有 Steam 页面，
    国产批次以手游为主、Steam 命中率极低（实测近两月名单前 20 款 0 命中），
    所以先用预算把进口批次跑完，国产批次作为补充。
    """
    games = []
    specs = (
        (NPPA_IMPORT_LIST_URL, "进口网络游戏审批信息", "import", 1),
        (NPPA_LIST_URL, "国产网络游戏审批信息", "domestic", MAX_BATCHES),
    )
    for list_url, keyword, origin, limit in specs:
        for batch in fetch_batches(fetcher, list_url, keyword, limit):
            for game in parse_batch(fetcher, batch):
                game["origin"] = origin
                games.append(game)
    # 先按批准时间倒序，再按来源分组（稳定排序），实现「进口优先、组内最新优先」
    games.sort(key=lambda g: g.get("approved_at") or "", reverse=True)
    games.sort(key=lambda g: 0 if g.get("origin") == "import" else 1)
    return games


def run(fetcher, state, max_new=MAX_NEW_PER_RUN, deadline=None, today=None):
    """抓版号 → 对照 Steam → 返回 (candidates, state, batches, stats)。"""
    today = today or date.today()
    items = state.get("items") or {}

    # 同名只保留批准时间最新的一条（进口与国产分开计）
    latest = {}
    for game in collect_games(fetcher):
        normalized = normalize_name(game["name"])
        if not normalized:
            continue
        key = "{0}:{1}".format(game.get("origin"), normalized)
        old = latest.get(key)
        if not old or (game.get("approved_at") or "") > (old.get("approved_at") or ""):
            latest[key] = game
    games = list(latest.values())

    # 清理超过半年的旧状态
    if today.month <= STATE_MAX_MONTHS:
        cutoff = "{0}-{1:02d}".format(today.year - 1, today.month - STATE_MAX_MONTHS + 12)
    else:
        cutoff = "{0}-{1:02d}".format(today.year, today.month - STATE_MAX_MONTHS)
    items = {k: v for k, v in items.items()
             if (v.get("approved_at") or "")[:7] >= cutoff}

    stats = {"nppa_games": len(games), "evaluated": 0, "matched": 0,
             "skipped_budget": 0, "deferred": 0}
    for game in games:
        key = "{0}:{1}".format(game.get("origin"), normalize_name(game["name"]))
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
            candidate = evaluate(fetcher, game)
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

    candidates = []
    for entry in items.values():
        candidate = entry.get("candidate")
        if candidate:
            candidate["last_checked"] = entry.get("checked_at")
            candidates.append(candidate)
    candidates.sort(key=lambda c: (
        -(c.get("gap_days") or 0),
        c.get("name_cn") or "",
    ))

    state = {"updated_at": utc_now_iso(), "items": items}
    sources = [
        {"origin": "import", "title": "进口网络游戏审批信息（当年累计）", "url": NPPA_IMPORT_LIST_URL},
        {"origin": "domestic", "title": "国产网络游戏审批信息（近两月）", "url": NPPA_LIST_URL},
    ]
    return candidates, state, sources, stats


def build_payload(candidates, batches, stats, today=None):
    today = today or date.today()
    return {
        "generated_at": today.isoformat(),
        "generated_at_utc": utc_now_iso(),
        "source": {
            "name": "国家新闻出版署 · 国产网络游戏审批信息",
            "url": NPPA_LIST_URL,
            "steam": "Steam appdetails（cc=us / cc=cn）",
        },
        "batches": batches,
        "stats": stats,
        "candidates": candidates,
        "note": "命中候选由 collector/update.py 自动并入 data/games.json，定性字段留「待补」待人复核。",
    }
