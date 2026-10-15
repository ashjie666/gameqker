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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = "data/games.json"

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


def events_from_payload(payload):
    """从一个 games.json 快照里抽出所有「上线」事件。"""
    found = []
    for game in (payload or {}).get("games") or []:
        iso = parse_steam_date(game.get("steam_release_date"))
        if not iso:
            continue
        appid = (game.get("watch") or {}).get("steam_appid")
        found.append({
            "game_id": game.get("id"),
            "name_cn": game.get("name_cn"),
            "name_en": game.get("name_en"),
            "type": "launch",
            "date": iso,
            "date_text": game.get("steam_release_date"),
            "store_url": ("https://store.steampowered.com/app/{0}/".format(appid)
                          if appid else None),
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


def build_payload(current_payload, git, now_iso):
    """合并「当前快照」与「历史快照」里的上线事件，按日期升序输出。

    origin 标出这条是从当前名单来的、还是只在历史里出现过，前端据此提示
    「历史名单中的游戏」。
    """
    merged = {}
    for event in events_from_payload(current_payload):
        merged[(event["game_id"], event["date"])] = dict(
            event, origin="current", first_seen=None)

    if git:
        for event, first_seen in history_events(git):
            key = (event["game_id"], event["date"])
            if key in merged:
                merged[key]["first_seen"] = first_seen
            else:
                merged[key] = dict(event, origin="history", first_seen=first_seen)

    ordered = sorted(merged.values(), key=lambda item: (item["date"], item["game_id"] or ""))
    return {
        "generated_at": now_iso,
        "note": "只汇总「上线」事件（来源：Steam 发售日）；解析不出具体日期的条目不列，不做版本更新。",
        "events": ordered,
    }
