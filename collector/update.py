"""gameqker 每日巡检入口。

用法：
    python -m collector.update                 # 巡检并写回 data/games.json（同步导出 web 副本）
    python -m collector.update --probe         # 只体检，不落盘
    python -m collector.update --dry-run       # 跑完流程但不写文件
    python -m collector.update --only=a,b      # 只巡检指定 id
    python -m collector.update --no-cache      # 忽略本地 HTTP 缓存
    python -m collector.update --budget-seconds 180

写回原则：**只允许覆盖 DYNAMIC_KEYS**（last_checked / steam_listed /
steam_release_date / official_signal），cn_status、keywords 等人工定性字段
原样带过，绝不改写。
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone

from . import __version__, official, steam
from .http import Fetcher
from .official import signal_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data", "games.json")
CACHE_DIR = os.path.join(ROOT, "data", "cache")
WEB_DIR = os.path.join(ROOT, "web")
WEB_EXPORT_PATH = os.path.join(WEB_DIR, "data", "games.json")

# 脚本唯一有权改写的字段；其余一律视为人工维护。
DYNAMIC_KEYS = ("last_checked", "steam_listed", "steam_release_date", "official_signal")
STEAM_FIELDS = ("steam_listed", "steam_release_date")
MAX_CHANGES = 90  # 每款游戏的变更历史滚动上限

# 落盘时 official_signal 只保留这些键：诊断用的 pages / 逐次变化的 checked_at
# 留在本次 reports 里，不污染长期数据，也避免每天产生无意义的 git diff。
STORED_OFFICIAL_KEYS = ("matched", "signals", "label", "pattern", "snippet", "url", "site")


# --------------------------------------------------------------------------
# 时间与 I/O
# --------------------------------------------------------------------------
def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_str():
    return date.today().isoformat()


def load_payload():
    with open(DATA_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    """tmp + os.replace 原子写，避免半截文件被页面读到。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------
# 单款巡检
# --------------------------------------------------------------------------
def check_game(game, fetcher, deadline):
    """巡检一款游戏，返回 (updates, report)。任何异常都被吞掉并记进 report。"""
    gid = game.get("id")
    watch = game.get("watch") or {}
    appid = watch.get("steam_appid")
    official_url = watch.get("official")

    updates = {}
    report = {
        "id": gid,
        "name": game.get("name_cn") or game.get("name_en"),
        "status": "ok",
        "steam": None,
        "official": None,
        "skipped": [],
        "error": None,
    }
    errors = []
    checked = False

    # --- Steam -----------------------------------------------------------
    if not appid:
        report["skipped"].append("steam(未配置 appid)")
    elif deadline and time.monotonic() > deadline:
        report["skipped"].append("steam(超预算)")
    else:
        try:
            sr = steam.check_appid(fetcher, appid)
        except Exception as exc:  # noqa: BLE001 - 巡检模块不允许把异常抛给主流程
            sr = {"listed": None, "release_date": None, "coming_soon": None,
                  "name": None, "url": None, "error": "{0}: {1}".format(type(exc).__name__, exc)}
        report["steam"] = {
            "appid": appid,
            "listed": sr["listed"],
            "release_date": sr["release_date"],
            "coming_soon": sr["coming_soon"],
            "name": sr["name"],
            "url": sr["url"],
            "error": sr["error"],
            "stale": bool(sr.get("stale")),
        }
        if sr["error"]:
            errors.append("steam: " + sr["error"])
        else:
            updates["steam_listed"] = sr["listed"]
            updates["steam_release_date"] = sr["release_date"]
            checked = True

    # --- 官网 ------------------------------------------------------------
    if not official_url:
        report["skipped"].append("official(未配置官网)")
    elif deadline and time.monotonic() > deadline:
        report["skipped"].append("official(超预算)")
    else:
        try:
            ev = official.check_official(fetcher, official_url)
        except Exception as exc:  # noqa: BLE001
            ev = {"matched": False, "signals": [], "label": "unknown", "pattern": None,
                  "snippet": None, "url": official_url, "site": None, "pages": [],
                  "checked_at": utc_now_iso(), "error": "{0}: {1}".format(type(exc).__name__, exc)}
        report["official"] = {
            "matched": ev["matched"],
            "label": ev["label"],
            "signals": ev["signals"],
            "url": ev["url"],
            "snippet": ev["snippet"],
            "pages": ev.get("pages") or [],
            "error": ev["error"],
            "stale": bool(ev.get("stale")),
        }
        if ev["error"]:
            errors.append("官网: " + ev["error"])
        else:
            updates["official_signal"] = {key: ev.get(key) for key in STORED_OFFICIAL_KEYS}
            checked = True

    # 只有真的查到过东西才刷新 last_checked；完全没配置的款保持 null。
    if checked:
        updates["last_checked"] = utc_now_iso()

    if errors:
        report["status"] = "partial" if checked else "failed"
        report["error"] = " | ".join(errors)
    elif not checked:
        report["status"] = "skipped"
    return updates, report


def run_checks(payload, fetcher, only, deadline):
    results = {}
    reports = []
    games = payload.get("games", [])
    total = len(games)
    for index, game in enumerate(games, start=1):
        gid = game.get("id")
        if only and gid not in only:
            continue
        start = time.monotonic()
        updates, report = check_game(game, fetcher, deadline)
        report["duration_ms"] = int((time.monotonic() - start) * 1000)
        results[gid] = updates
        reports.append(report)
        print_report_line(index, total, report)
    return results, reports


def print_report_line(index, total, report):
    name = report.get("name") or report.get("id")
    head = "[{0}/{1}] {2}  <{3}>  {4}ms".format(index, total, name, report["status"], report["duration_ms"])
    print(head)
    steam_info = report.get("steam")
    if steam_info:
        if steam_info.get("error"):
            print("       steam : 失败 - {0}".format(steam_info["error"]))
        else:
            print("       steam : listed={0} release={1} coming_soon={2}{3}".format(
                steam_info["listed"], steam_info["release_date"], steam_info["coming_soon"],
                "（陈旧缓存）" if steam_info.get("stale") else ""))
    official_info = report.get("official")
    if official_info:
        if official_info.get("error"):
            print("       官网  : 失败 - {0}".format(official_info["error"]))
        else:
            flag = official_info["label"] if official_info["matched"] else "unknown"
            print("       官网  : {0} @ {1}{2}".format(
                flag, official_info["url"], "（陈旧缓存）" if official_info.get("stale") else ""))
    if report.get("skipped"):
        print("       跳过  : {0}".format("、".join(report["skipped"])))


# --------------------------------------------------------------------------
# 写回
# --------------------------------------------------------------------------
def _differs(field, old, new):
    if field == "official_signal":
        return signal_key(old) != signal_key(new)
    return old != new


def _label_of(value):
    if isinstance(value, dict) and value.get("matched"):
        return value.get("label") or "matched"
    return "unknown"


def _evidence_for(field, report):
    """从 report 里拼一句人可读的证据，写进变更记录。"""
    steam_info = report.get("steam") or {}
    official_info = report.get("official") or {}
    if field == "steam_listed":
        return "Steam cc=us 商店 success={0}；name={1}".format(
            "true" if steam_info.get("listed") else "false", steam_info.get("name") or "-")
    if field == "steam_release_date":
        return "release_date.date={0}；coming_soon={1}".format(
            steam_info.get("release_date") or "-", steam_info.get("coming_soon"))
    if field == "official_signal":
        return official_info.get("snippet") or "-"
    return None


def _url_for(field, game, new_value, report):
    steam_info = report.get("steam") or {}
    if field in STEAM_FIELDS:
        return steam_info.get("url")
    if field == "official_signal" and isinstance(new_value, dict):
        return new_value.get("url")
    return None


def merge_dynamic(payload, results, reports):
    """只写 DYNAMIC_KEYS：人工字段原样带过，被改动的字段追加一条变更记录。"""
    at = utc_now_iso()
    info = {report["id"]: report for report in reports}
    changed = 0

    for game in payload.get("games", []):
        gid = game.get("id")
        updates = results.get(gid)
        if not updates:
            continue
        report = info.get(gid, {})
        for key in DYNAMIC_KEYS:
            if key not in updates:
                continue
            old = game.get(key)
            new = updates[key]
            # last_checked 每次都变，记进变更历史只会淹没有用信息，跳过。
            if key != "last_checked" and _differs(key, old, new):
                record = {
                    "at": at,
                    "game_id": gid,
                    "field": key,
                    "old": _label_of(old) if key == "official_signal" else old,
                    "new": _label_of(new) if key == "official_signal" else new,
                    "url": _url_for(key, game, new, report),
                    "evidence": _evidence_for(key, report),
                }
                history = game.setdefault("changes", [])
                history.insert(0, record)
                del history[MAX_CHANGES:]
                changed += 1
            game[key] = new

    payload["generated_at"] = today_str()
    payload["last_run_at"] = at
    payload["reports"] = reports
    return changed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m collector.update",
        description="巡检国产游戏出海信息差情报表（Steam + 官方站）。")
    parser.add_argument("--probe", action="store_true", help="只体检数据源，不写文件")
    parser.add_argument("--dry-run", action="store_true", help="完整跑一遍但不写文件")
    parser.add_argument("--no-cache", action="store_true", help="忽略本地 HTTP 缓存")
    parser.add_argument("--only", default="", help="逗号分隔的游戏 id，只巡检这些")
    parser.add_argument("--budget-seconds", type=int, default=180, help="单次巡检总预算（默认 180 秒）")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    only = {item.strip() for item in args.only.split(",") if item.strip()}

    payload = load_payload()
    deadline = (time.monotonic() + args.budget_seconds) if args.budget_seconds > 0 else None
    fetcher = Fetcher(
        cache_dir=CACHE_DIR,
        timeout=12,               # 单个请求上限；官方站整域不可达时要尽快放弃
        host_interval=2.0,        # Steam / 官网 同一 host 至少隔 2 秒
        retries=1,
        use_cache=not args.no_cache,
    )

    print("=== gameqker 巡检 {0}（{1} 款{2}）===".format(
        today_str(), len(payload.get("games", [])),
        "，仅 " + ",".join(sorted(only)) if only else ""))
    results, reports = run_checks(payload, fetcher, only, deadline)
    changed = merge_dynamic(payload, results, reports)

    ok = sum(1 for r in reports if r["status"] in ("ok", "partial"))
    failed = sum(1 for r in reports if r["status"] == "failed")
    skipped = sum(1 for r in reports if r["status"] == "skipped")

    print("--- 汇总：正常 {0} / 部分失败并含 {1} / 跳过 {2} / 变更 {3} 处 ---".format(
        ok, failed, skipped, changed))

    if args.probe or args.dry_run:
        print("（{0} 模式，未写入任何文件）".format("probe" if args.probe else "dry-run"))
        return 0

    write_json(DATA_PATH, payload)
    write_json(WEB_EXPORT_PATH, payload)
    print("已写入 {0}".format(DATA_PATH))
    print("已导出 {0}".format(WEB_EXPORT_PATH))

    # 部分失败退出码 0，避免 Actions 假红；全部失败才算真失败。
    if reports and failed == len(reports):
        print("全部数据源失败。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
