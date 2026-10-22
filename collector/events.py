"""上线倒计时数据：把各款游戏的 Steam 发售日汇总成「未来上线」事件。

只做「上线」这一类事件 —— 项目里没有版本更新的数据源，所以不做版本更新。
数据来源两处：
  1. 当前快照 data/games.json 的 steam_release_date；
  2. data/games.json 的所有 git 历史快照 —— 历史里出现过、但可能已经退出当前
     名单的游戏，只要发售日还没到，也应该继续出现在倒计时里。

解析不出具体日期的（"Q1 2027" / "To be announced" / "2026"）一律丢弃，绝不猜日期。
"""

import json
import os
import re
import subprocess
from datetime import date

from .feeds import find_date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = "data/games.json"

# 和上线相关的官网信号标签；只有命中这些才尝试从证据片段里取日期
OFFICIAL_LAUNCH_TAGS = ("global_launch", "release_date", "coming_soon")
# 同一游戏多源日期冲突时的优先级：数值越小越可信
SOURCE_PRIORITY = {"steam": 0, "official": 1, "rss": 2}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Steam 的 release_date.date 见过两种写法："Oct 28, 2026" 与 "28 Oct, 2026"
_MONTH_FIRST = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$")
_DAY_FIRST = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})$")


def parse_steam_date(text):
    """把 Steam 的发行日文案解析成 ISO 日期；解析不出返回 None。

    Steam 这个字段是本地化文案，除了 "Oct 28, 2026" 还可能是 "Q1 2027"、
    "To be announced"、"2026" 这类给不出具体某天的值 —— 这些一律返回 None：
    宁可这一条不进倒计时，也不能猜一个日期出来。
    """
    # 这个字段来自外部 JSON（Steam 抓来的，也可能是人工手改的），未必是字符串：
    # 直接 .strip() 会抛 AttributeError: 'int' object has no attribute 'strip'。
    # 先统一成字符串再判定，非字符串的具体值一律当作「解析不出」丢掉。
    raw = str(text or "").strip()
    if not raw:
        return None

    match = _MONTH_FIRST.match(raw)
    if match:
        month, day, year = match.group(1), int(match.group(2)), int(match.group(3))
    else:
        match = _DAY_FIRST.match(raw)
        if not match:
            return None
        day, month, year = int(match.group(1)), match.group(2), int(match.group(3))

    number = MONTHS.get(month[:3].lower())
    if not number:
        return None
    try:
        return date(year, number, day).isoformat()
    except ValueError:  # 例如 2 月 30 日这种不存在的日期
        return None


def _store_url(game):
    appid = (game.get("watch") or {}).get("steam_appid")
    return ("https://store.steampowered.com/app/{0}/".format(appid) if appid else None)


def events_from_payload(payload, with_official=False):
    """从一个 games.json 快照里抽出所有「上线」事件。

    with_official=True 时额外从官网信号证据片段里取日期（只在当前快照启用，
    历史快照不取，避免同一个官网文案在多个历史版本里反复生成事件）。
    """
    found = []
    for game in (payload or {}).get("games") or []:
        store_url = _store_url(game)
        iso = parse_steam_date(game.get("steam_release_date"))
        if iso:
            found.append({
                "game_id": game.get("id"),
                "name_cn": game.get("name_cn"),
                "name_en": game.get("name_en"),
                "type": "launch",
                "date": iso,
                "date_text": game.get("steam_release_date"),
                "store_url": store_url,
                "sources": [{"type": "steam", "name": "Steam 商店",
                             "url": store_url}],
            })

        if not with_official:
            continue
        signal = game.get("official_signal") or {}
        if not signal.get("matched"):
            continue
        if not set(signal.get("signals") or []) & set(OFFICIAL_LAUNCH_TAGS):
            continue
        found_date, raw = find_date(str(signal.get("snippet") or ""))
        if not found_date:
            continue
        found.append({
            "game_id": game.get("id"),
            "name_cn": game.get("name_cn"),
            "name_en": game.get("name_en"),
            "type": "launch",
            "date": found_date.isoformat(),
            "date_text": raw,
            "store_url": store_url,
            "sources": [{"type": "official",
                         "name": signal.get("site") or "官网",
                         "url": signal.get("url")}],
        })
    return found


def _git(git, *extra):
    return subprocess.run(
        [git, "-C", ROOT] + list(extra),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def history_events(git):
    """遍历 data/games.json 的所有历史快照，返回 [(事件, 首次出现日期)]。

    git log 是「新 → 旧」，所以同一个 (game_id, date) 最后遍历到的那次快照就是最早的，
    正好用来记「首次记录于」。
    """
    log = _git(git, "log", "--format=%H|%ad", "--date=short", "--", HISTORY_PATH)
    if log.returncode != 0:
        return []

    seen = {}
    for line in (log.stdout or "").splitlines():
        if "|" not in line:
            continue
        sha, seen_on = line.split("|", 1)
        blob = _git(git, "show", "{0}:{1}".format(sha.strip(), HISTORY_PATH))
        if blob.returncode != 0 or not blob.stdout:
            continue
        try:
            payload = json.loads(blob.stdout)
        except ValueError:
            continue
        for event in events_from_payload(payload):
            key = (event["game_id"], event["date"])
            if key in seen:
                seen[key][1] = seen_on.strip()  # 继续往旧快照走，覆盖成更早的日期
            else:
                seen[key] = [event, seen_on.strip()]
    return [(event, first_seen) for event, first_seen in seen.values()]


def _priority(event):
    types = [source.get("type") for source in event.get("sources") or []]
    return min([SOURCE_PRIORITY.get(kind, 9) for kind in types] or [9])


def _merge_sources(entry, event):
    existing = {(source.get("name"), source.get("url")) for source in entry["sources"]}
    for source in event.get("sources") or []:
        key = (source.get("name"), source.get("url"))
        if key not in existing:
            entry["sources"].append(source)
            existing.add(key)


def build_payload(current_payload, git, now_iso, news_events=None):
    """合并多源上线事件，按日期升序输出。

    来源优先级 Steam 商店 > 官网 > 新闻/RSS。同一游戏出现多个不同日期时，
    主日期取优先级最高的那个，其余日期记进 alt_dates 供前端提示，不丢弃来源。
    origin 标出这条主要来自当前名单（current）、历史快照（history）、
    官网（official）还是新闻（news）。
    """
    merged = {}

    def add(event, origin, first_seen=None):
        game_id = event.get("game_id") or event.get("name_cn")
        if not game_id:
            return
        entry = merged.get(game_id)
        if entry is None:
            merged[game_id] = {
                "game_id": game_id,
                "name_cn": event.get("name_cn"),
                "name_en": event.get("name_en"),
                "type": "launch",
                "date": event["date"],
                "date_text": event.get("date_text"),
                "store_url": event.get("store_url"),
                "sources": list(event.get("sources") or []),
                "origin": origin,
                "first_seen": first_seen,
                "alt_dates": [],
                "_priority": _priority(event),
            }
            return

        _merge_sources(entry, event)
        if origin == "current" and entry["origin"] == "history":
            entry["origin"] = "current"
            entry["first_seen"] = first_seen
        priority = _priority(event)
        if event["date"] != entry["date"]:
            if priority < entry["_priority"]:
                entry["alt_dates"].append({
                    "date": entry["date"], "date_text": entry.get("date_text")})
                entry["date"] = event["date"]
                entry["date_text"] = event.get("date_text")
                entry["_priority"] = priority
            else:
                entry["alt_dates"].append({
                    "date": event["date"], "date_text": event.get("date_text")})
        elif priority < entry["_priority"]:
            entry["_priority"] = priority

    for event in events_from_payload(current_payload, with_official=True):
        origin = "current" if _priority(event) == 0 else (
            "official" if _priority(event) == 1 else "news")
        add(event, origin)

    if git:
        for event, first_seen in history_events(git):
            add(event, "history", first_seen)

    for event in news_events or []:
        add(event, "news")

    ordered = sorted(merged.values(), key=lambda item: (item["date"], item["game_id"] or ""))
    for item in ordered:
        item.pop("_priority", None)
    return {
        "generated_at": now_iso,
        "note": ("汇总多源上线事件（Steam 商店 > 官网 > 新闻/RSS），每条标注来源；"
                 "解析不出具体日期的条目不列，不做版本更新。"),
        "events": ordered,
    }
