"""国服 / 海外服不同步游戏发现：NPPA 版号公示 × Steam 区服对照。

数据源（全部公开、免 Key，仅用标准库）：
- 国家新闻出版署「国产网络游戏审批信息」月度公示（近两月）
- 国家新闻出版署「进口网络游戏审批信息」年度累计（当年）
- Steam storesearch：按游戏名找 appid
- Steam appdetails：分别查 cc=us / cc=cn 的区服状态

进口批次排在前面优先处理：这些游戏在 Steam 美区早已发售、国服版号刚批，
是「国服 / 海外服不同步」最典型的一类；国产批次以手游为主，Steam 命中率低。

判定口径（2026-10-22 与用户确认）：
- overseas_first：Steam 美区已发售，且发售日早于国服版号批准日 → 海外先发
- cn_first：国服已有版号，Steam 美区无页面或未上架 → 国服先行
- region_gap：Steam 美区可购、国区不可购 → 国区受限

结果写入候选池 data/candidates.json，由前端「新发现 · 待确认」区展示。
**不直接写入 games 清单**：关键词、变现建议等定性字段是人工维护的，
自动追加会污染报表质量。确认后由人工并入 data/games.json。
"""

import json
import re
import time
from datetime import date, datetime
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import quote, urljoin

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

MAX_BATCHES = 2          # 只处理最新的两个月度批次
MIN_SIMILARITY = 0.72    # 中文名匹配阈值，低于此值视为不同游戏
REFRESH_DAYS = 7         # 同一游戏多少天后重新对照 Steam
MAX_NEW_PER_RUN = 20     # 每轮最多新评估多少款，控制请求量与耗时
STATE_MAX_MONTHS = 6     # 状态文件只保留最近半年的版号游戏

_PUNCT_RE = re.compile(r"[\s\u3000·・:：\-—_()（）\[\]【】《》<>\"'!！?？,，.。、/\\|]+")


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
    url = STEAM_SEARCH_URL.format(term=quote(term.encode("utf-8")))
    response = fetcher.get(url)
    if not response.body:
        return []
    try:
        payload = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return []
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
    """按中文名搜索 Steam，并用国区详情页的中文名复核，避免同名误配。"""
    target = normalize_name(nppa_item["name"])
    if not target:
        return None
    for item in steam_search(fetcher, nppa_item["name"])[:3]:
        appid = item.get("id")
        if not isinstance(appid, int):
            continue
        cn = check_app(fetcher, appid, "cn", "schinese")
        if cn.get("listed") is not True:
            continue
        steam_name = normalize_name(cn.get("name") or "")
        if not steam_name:
            continue
        score = SequenceMatcher(None, target, steam_name).ratio()
        if score >= MIN_SIMILARITY or target in steam_name or steam_name in target:
            return {"appid": appid, "name": cn.get("name"), "cn": cn, "score": round(score, 2)}
    return None


def classify(nppa_item, us, cn):
    """返回 (gap_type, gap_days, note) 或 None。"""
    if us.get("listed") is not True:
        # 国服已有版号、Steam 海外查不到 → 国服先行
        return "cn_first", None, "国服已获批版号，Steam 美区无页面或未上架"

    approved = parse_cn_date(nppa_item.get("approved_at") or "")
    us_release = parse_steam_date(us.get("release_date"))
    overseas_live = us.get("coming_soon") is False and us_release is not None

    if overseas_live:
        if approved and us_release < approved:
            return "overseas_first", (approved - us_release).days, \
                "Steam 美区已发售，早于国服版号批准"
        return "overseas_first", None, "Steam 美区已发售，国服版号刚获批"

    if us.get("coming_soon") is True:
        if approved and us_release and us_release < approved:
            return "overseas_first", (approved - us_release).days, \
                "Steam 美区定档早于国服版号批准"
        return None  # 都还没上线，谈不上不同步

    if cn.get("listed") is False:
        return "region_gap", None, "Steam 美区可购、国区不可购"
    return None


def evaluate(fetcher, nppa_item):
    matched = match_appid(fetcher, nppa_item)
    if not matched:
        return None
    appid = matched["appid"]
    cn = matched["cn"]
    us = check_app(fetcher, appid, "us", "english")
    verdict = classify(nppa_item, us, cn)
    if not verdict:
        return None
    gap_type, gap_days, note = verdict
    return {
        "origin": nppa_item.get("origin"),
        "name_cn": nppa_item["name"],
        "name_en": clip(us.get("name") or matched.get("name"), 120),
        "appid": appid,
        "gap_type": gap_type,
        "gap_days": gap_days,
        "note": note,
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
    return (today - checked).days >= REFRESH_DAYS


def collect_games(fetcher):
    """抓国产（近两月）+ 进口（当年累计）两个审批来源。

    进口批次命中率高得多：这些游戏在 Steam 美区早已发售，国服版号刚批，
    是「国服 / 海外服不同步」最典型的一类；国产批次里以手游为主，
    Steam 有页面的比例很低，但一旦命中就是真正的「海外先发」。
    进口排前面，保证预算先用在有效候选上。
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

    stats = {"nppa_games": len(games), "evaluated": 0, "matched": 0, "skipped_budget": 0}
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
        candidate = None
        try:
            candidate = evaluate(fetcher, game)
        except Exception as exc:  # 单款失败不影响整轮
            candidate = None
            stats.setdefault("errors", []).append("{0}: {1}".format(game["name"], type(exc).__name__))
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
        0 if c.get("gap_type") == "overseas_first" else 1,
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
        "note": "候选池仅供确认；确认后再人工并入 data/games.json 的 games 清单。",
    }
