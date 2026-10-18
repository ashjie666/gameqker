"""热度趋势：为每款游戏维护一条按天的热度序列（data/history.json）。

取值优先级（逐级兜底）——A 有数据就用 A，没有就用 B，B 没有再用 C：
    A. trends  —— Google Trends 关键词搜索热度（0-100 指数；需 pytrends，且执行环境要能访问 Google）
    B. reviews —— Steam 评测总数（官方公开接口，需 appid 且已上架美区）
    C. signal  —— 自建信号活跃度（当日变更条数 + 官网信号命中 + 已上架，纯自建、零依赖）

每轮巡检为每款游戏追加当天一条记录（同一天重复跑只覆盖当天那条），按 MAX_DAYS 滚动裁剪。
任何一步抓取失败都只降级或跳过，绝不抛给主巡检流程。

输出：data/history.json 与 web/data/history.json（web 副本供静态页读取）。
"""

import json
import os
import time
from datetime import date

from .textutil import clip

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(ROOT, "data", "history.json")
WEB_HISTORY_PATH = os.path.join(ROOT, "web", "data", "history.json")

MAX_DAYS = 90                    # 每款保留最近 90 天
TRENDS_TIMEFRAME = "today 1-m"   # 最近 30 天、日粒度
TRENDS_INTERVAL = 2.5            # 连续调用 Trends 的间隔，避免触发 Google 限流

SOURCE_LABELS = {
    "trends": "Google Trends 关键词热度（0-100）",
    "reviews": "Steam 评测总数",
    "signal": "自建信号活跃度（当日证据强度 0-10）",
}

REVIEWS_URL = ("https://store.steampowered.com/appreviews/{appid}"
               "?json=1&language=all&purchase_type=all&num_per_page=0&filter=all")


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------
def load_history(path=None):
    """读已有序列；缺失或损坏都当作空序列，绝不阻断巡检。"""
    try:
        with open(path or HISTORY_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload.get("series"), dict):
            return payload
    except (OSError, ValueError):
        pass
    return {"series": {}}


def write_json(path, payload):
    """tmp + os.replace 原子写，避免页面读到半截文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------
# 三个取值来源
# --------------------------------------------------------------------------
def primary_keyword(game):
    """取该款的主关键词（keywords 的第一条）作为 Trends 查询词。"""
    for item in game.get("keywords") or []:
        text = clip(item, 100)
        if text:
            return text
    return None


def trends_value(keyword, timeout=15):
    """A：Google Trends 关键词热度。返回 (value, error)；value 为 None 表示不可用。"""
    if not keyword:
        return None, "未配置关键词"
    try:
        from pytrends.request import TrendReq  # 可选依赖：装不上就降级
    except ImportError:
        return None, "pytrends 未安装"
    try:
        client = TrendReq(hl="en-US", tz=0, timeout=(6, timeout))
        client.build_payload([keyword], timeframe=TRENDS_TIMEFRAME)
        frame = client.interest_over_time()
        if frame is None or len(frame.index) == 0:
            return None, "Trends 无数据"
        column = frame[keyword] if keyword in frame.columns else frame.iloc[:, 0]
        values = [int(item) for item in column.tolist()]
        # 末尾常带一个未完成的 isPartial 点，用倒数第二个已完成点更稳
        if len(values) >= 2:
            values = values[:-1]
        return values[-1], None
    except Exception as exc:  # noqa: BLE001 - 抓取失败一律降级，不抛给主流程
        return None, "{0}: {1}".format(type(exc).__name__, str(exc)[:120])


def reviews_value(fetcher, appid):
    """B：Steam 评测总数。返回 (value, error)。"""
    if not appid:
        return None, "未配置 appid"
    response = fetcher.get(REVIEWS_URL.format(appid=appid))
    if response.body is None:
        return None, response.error or "fetch failed"
    try:
        payload = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None, "decode failed"
    total = ((payload or {}).get("query_summary") or {}).get("total_reviews")
    # 评测数为 0（或缺失）说明该 appid 还没产生任何评测，当作「无数据」降级到 C：
    # 免得一条恒为 0 的曲线把 B 级来源压平，也避免和 C 的量纲混在一起。
    if not isinstance(total, int) or total <= 0:
        return None, "无评测数据"
    return total, None


def signal_value(game):
    """C：自建信号活跃度（0-10）。当日变更条数 ×2 + 官网信号命中 +2 + 已上架 +1。"""
    score = 0
    today = date.today().isoformat()
    for change in game.get("changes") or []:
        if str(change.get("at") or "")[:10] == today:
            score += 2
    if (game.get("official_signal") or {}).get("matched"):
        score += 2
    if game.get("steam_listed") is True:
        score += 1
    return min(score, 10)


# --------------------------------------------------------------------------
# 序列维护
# --------------------------------------------------------------------------
def _record(series, gid, today, source, value):
    """追加/覆盖当天记录，并裁掉超出窗口的旧条目。"""
    entries = series.setdefault(gid, [])
    entry = {"date": today, "source": source, "value": value}
    if entries and entries[-1].get("date") == today:
        entries[-1] = entry
    else:
        entries.append(entry)
    del entries[:-MAX_DAYS]


def update_history(history, games, fetcher, use_trends=True, deadline=None):
    """为每款游戏按 A→B→C 取一个当天热度值并写入序列，返回各来源计数。"""
    series = history.setdefault("series", {})
    today = date.today().isoformat()
    stats = {"trends": 0, "reviews": 0, "signal": 0}
    trending = bool(use_trends)

    for index, game in enumerate(games):
        gid = game.get("id")
        if not gid:
            continue
        # 超预算就整体停止 Trends 调用、降到 B/C，避免把 CI 拖长
        if trending and deadline is not None and time.monotonic() > deadline:
            trending = False

        source = None
        value = None

        # A：Google Trends
        if trending:
            keyword = primary_keyword(game)
            if keyword:
                if index:
                    time.sleep(TRENDS_INTERVAL)
                value, error = trends_value(keyword)
                if value is not None:
                    source = "trends"

        # B：Steam 评测总数
        if source is None:
            appid = (game.get("watch") or {}).get("steam_appid")
            if appid and game.get("steam_listed") is True:
                value, error = reviews_value(fetcher, appid)
                if value is not None:
                    source = "reviews"

        # C：自建信号活跃度兜底
        if source is None:
            source = "signal"
            value = signal_value(game)

        _record(series, gid, today, source, value)
        stats[source] += 1

    return stats


def export(payload, fetcher, use_trends=True, deadline=None):
    """导出热度趋势数据（data/history.json + web 副本），返回来源计数。"""
    history = load_history()
    stats = update_history(history, payload.get("games") or [], fetcher,
                           use_trends=use_trends, deadline=deadline)
    history["generated_at"] = date.today().isoformat()
    history["note"] = ("每款游戏按天的热度序列；取值优先级 A→B→C："
                       "Google Trends 关键词热度 → Steam 评测总数 → 自建信号活跃度。")
    history["days"] = MAX_DAYS
    history["sources"] = SOURCE_LABELS
    write_json(HISTORY_PATH, history)
    write_json(WEB_HISTORY_PATH, history)
    return stats
