"""gameqker 每日巡检入口。

用法：
    python -m collector.update                 # 巡检并写回 data/games.json（同步导出 web 副本）
    python -m collector.update --probe         # 只体检，不落盘
    python -m collector.update --dry-run       # 跑完流程但不写文件
    python -m collector.update --only=a,b      # 只巡检指定 id
    python -m collector.update --no-cache      # 忽略本地 HTTP 缓存
    python -m collector.update --budget-seconds 180
    python -m collector.update --no-git        # 巡检后不提交本地 git 快照

写回原则：**只允许覆盖 DYNAMIC_KEYS**（last_checked / steam_listed /
steam_release_date / official_signal），cn_status、keywords 等人工定性字段
原样带过，绝不改写。

写回同时会导出 web/data/events.json：把当前快照与 git 历史快照里的 Steam 发售日
合并成「上线倒计时」数据，供页面顶部的倒计时面板使用（不做版本更新）。

写回之后：若项目内是 git 仓库，会把 data/games.json 与 web/data/games.json
提交成一条「每日巡检快照」commit，作为当天数据的留存（用 --no-git 关闭）；
随后自动 rebase 到 origin/main 并推送，与 GitHub Actions 的每日提交收敛到同一条数据线。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone

from . import __version__, discover, events, official, steam, trends
from .http import Fetcher
from .official import signal_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data", "games.json")
CACHE_DIR = os.path.join(ROOT, "data", "cache")
WEB_DIR = os.path.join(ROOT, "web")
WEB_EXPORT_PATH = os.path.join(WEB_DIR, "data", "games.json")
EVENTS_PATH = os.path.join(ROOT, "data", "events.json")
WEB_EVENTS_PATH = os.path.join(WEB_DIR, "data", "events.json")
HISTORY_PATH = os.path.join(ROOT, "data", "history.json")
WEB_HISTORY_PATH = os.path.join(WEB_DIR, "data", "history.json")
CANDIDATES_PATH = os.path.join(ROOT, "data", "candidates.json")
WEB_CANDIDATES_PATH = os.path.join(WEB_DIR, "data", "candidates.json")
DISCOVER_STATE_PATH = os.path.join(ROOT, "data", "discover_state.json")
# 海外新闻源直连失败时的代理兜底（本机 SOCKS5；CI 上没有代理会快速失败，不影响主流程）
PROXY_URL = os.environ.get("GAMEQKER_PROXY", "socks5h://127.0.0.1:1080")

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
# 每日快照：把巡检结果提交进本地 git 历史
# --------------------------------------------------------------------------
SNAPSHOT_PATHS = ("data/games.json", "web/data/games.json",
                  "data/events.json", "web/data/events.json",
                  "data/history.json", "web/data/history.json",
                  "data/candidates.json", "web/data/candidates.json",
                  "data/discover_state.json")
GIT_NAME = "gameqker-bot"
GIT_EMAIL = "gameqker-bot@users.noreply.github.com"


def resolve_git():
    """便携 git（项目内 .tools/git）优先，其次 PATH 里的 git。"""
    override = os.environ.get("GAMEQKER_GIT")
    if override and os.path.exists(override):
        return override
    local = os.path.join(ROOT, ".tools", "git", "cmd", "git.exe")
    if os.path.exists(local):
        return local
    return shutil.which("git")


def git_run(git, *extra):
    """以项目根为工作目录执行一次 git，返回 CompletedProcess（不抛异常）。

    统一带上提交身份：commit 会新建提交、rebase 会重写提交，两者都要 committer，
    而本仓库没有配全局身份（避免污染用户的全局 git 配置），所以每处都得显式给。
    """
    return subprocess.run(
        [git, "-c", "user.name=" + GIT_NAME, "-c", "user.email=" + GIT_EMAIL,
         "-C", ROOT] + list(extra),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def sync_with_remote(git, run):
    """把本地快照对齐到 origin/main 并推回远端。

    本地巡检和 GitHub Actions 都会提交 data/games.json：本地攒一条、Actions 攒一条，
    两边就会分叉，之后推送会被拒。这里在每次快照后自动 rebase 再 push，让两边收敛到
    同一条数据线；两边都改过同一个数据文件时，冲突处取本地这条快照的版本。
    离线、未配 origin、工作区不干净、rebase 或 push 失败时都只提示并回退，
    绝不让同步失败影响已经写好的数据和已经提交的快照。返回 True/False/None。
    """
    remotes = run("remote")
    if remotes.returncode != 0 or "origin" not in (remotes.stdout or "").split():
        print("跳过远端同步：未配置 origin")
        return None

    if run("rev-parse", "--verify", "--quiet", "origin/main").returncode != 0:
        print("跳过远端同步：远端还没有 main 分支")
        return None

    if run("fetch", "origin").returncode != 0:
        print("跳过远端同步：fetch 失败（离线或鉴权问题）", file=sys.stderr)
        return False

    # origin/main 已经是 HEAD 的祖先时不必 rebase（本地已包含远端全部提交）
    if run("merge-base", "--is-ancestor", "origin/main", "HEAD").returncode != 0:
        # rebase 要求工作区干净；只看已跟踪文件，未跟踪文件不影响
        dirty = run("status", "--porcelain", "--untracked-files=no")
        if dirty.returncode != 0 or (dirty.stdout or "").strip():
            print("跳过远端同步：工作区有未提交的改动，rebase 需要干净的工作区")
            return None

        # -X theirs：两边都改过 data/games.json，冲突是常态，这里让本地这条快照胜出。
        # （rebase 里 theirs 指「正在被应用的那条提交」，也就是本地快照，方向与直觉相反，
        #   已实测确认。）这样 rebase 能一次跑完，不需要人工解冲突。
        rebased = run("rebase", "-X", "theirs", "origin/main")
        if rebased.returncode != 0:
            run("rebase", "--abort")
            print("远端同步失败：rebase 未能完成，已回退到同步前状态；"
                  "推送前请手动执行 git pull --rebase 解决", file=sys.stderr)
            return False

    # 本地没有超前提交就没什么可推的（空跑一轮、或上一轮已经推过）
    ahead = run("rev-list", "--count", "origin/main..HEAD")
    if ahead.returncode != 0:
        print("跳过远端同步：无法判断本地是否超前于远端", file=sys.stderr)
        return False
    if (ahead.stdout or "").strip() == "0":
        return True

    if run("push").returncode != 0:
        print("远端同步失败：本地快照已提交，但推送未成功；联网后手动执行 git push 即可",
              file=sys.stderr)
        return False

    print("远端同步：本地快照已 rebase 到 origin/main 并推送（冲突取本地版本）")
    return True


def snapshot_git():
    """提交一条当日快照 commit，随后把本地对齐（rebase）到 origin/main 并推送。

    只在「当前是 git 仓库 + 找得到 git」时执行；任何失败都只打印提示，
    绝不让 git 的问题中断巡检本身。返回 True/False/None。
    """
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        return None
    git = resolve_git()
    if not git:
        print("跳过 git 快照：未找到 git（可用 GAMEQKER_GIT 指定路径）")
        return None

    def run(*extra):
        return git_run(git, *extra)

    try:
        # events.json 是后加的产物，导出失败时可能不存在；缺了就跳过，别让 git add 整体失败
        paths = [item for item in SNAPSHOT_PATHS if os.path.exists(os.path.join(ROOT, item))]
        added = run("add", *paths)
        if added.returncode != 0:
            print("git 快照失败（add）：" + (added.stderr or "").strip(), file=sys.stderr)
            return False

        # 退出码 0 表示暂存区没有变化（例如只更新了 last_checked 之外的字段）
        if run("diff", "--cached", "--quiet").returncode == 0:
            print("git 快照：本次无数据变更，跳过提交")
            sync_with_remote(git, run)
            return True

        message = "chore(data): 每日巡检快照 " + today_str()
        committed = run("commit", "-m", message)
        if committed.returncode != 0:
            print("git 快照失败（commit）：" + (committed.stderr or "").strip(), file=sys.stderr)
            return False

        head = (committed.stdout or "").strip().splitlines()
        print("git 快照：已提交 " + (head[0] if head else message))
        sync_with_remote(git, run)
        return True
    except Exception as exc:  # noqa: BLE001 - 快照失败不影响巡检结果
        print("git 快照异常：{0}: {1}".format(type(exc).__name__, exc), file=sys.stderr)
        return False


def export_events(payload, fetcher=None):
    """导出上线倒计时数据（data/events.json + web 副本）。

    合并当前快照与 git 历史快照里的 Steam 发售日。倒计时只是页面上的一块附加面板，
    任何失败都只提示，绝不影响已经写好的巡检结果。
    """
    try:
        git = resolve_git() if os.path.isdir(os.path.join(ROOT, ".git")) else None
        news_events = []
        if fetcher:
            from . import feeds
            try:
                news_events = feeds.collect(fetcher, payload)
                print("新闻/RSS 源匹配到 {0} 条上线事件".format(len(news_events)))
            except Exception as exc:
                print("新闻/RSS 抓取失败：{0}: {1}".format(type(exc).__name__, exc),
                      file=sys.stderr)
        events_payload = events.build_payload(payload, git, utc_now_iso(), news_events)
        write_json(EVENTS_PATH, events_payload)
        write_json(WEB_EVENTS_PATH, events_payload)
        items = events_payload["events"]
        upcoming = [item for item in items if item["date"] >= today_str()]
        print("已导出 {0}（上线事件 {1} 条，其中 {2} 条尚未到）".format(
            WEB_EVENTS_PATH, len(items), len(upcoming)))
    except Exception as exc:  # noqa: BLE001 - 倒计时导出失败不影响巡检结果
        print("倒计时导出失败：{0}: {1}".format(type(exc).__name__, exc), file=sys.stderr)


def export_trends(payload, fetcher, deadline=None):
    """导出热度趋势数据（data/history.json + web 副本）。

    按 A→B→C 逐级兜底取值（Google Trends → Steam 评测数 → 自建信号活跃度）。
    任何失败都只提示，绝不影响已经写好的巡检结果。
    """
    try:
        stats = trends.export(payload, fetcher, deadline=deadline)
        print("已导出 {0}（趋势来源：Trends {1} / Steam 评测 {2} / 自建信号 {3}）".format(
            WEB_HISTORY_PATH, stats["trends"], stats["reviews"], stats["signal"]))
    except Exception as exc:  # noqa: BLE001 - 趋势导出失败不影响巡检结果
        print("热度趋势导出失败：{0}: {1}".format(type(exc).__name__, exc), file=sys.stderr)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def export_discovery(fetcher, deadline=None):
    """导出「国服 / 海外服不同步」候选池（data/candidates.json + web 副本）。

    数据源：国家新闻出版署国产版号公示 × Steam 美区/国区对照。
    返回本轮候选列表，交给 promote_candidates 并入主表；失败返回空列表。
    任何失败都只提示，绝不影响巡检结果。
    """
    try:
        state = _load_json(DISCOVER_STATE_PATH, {})
        candidates, new_state, batches, stats = discover.run(
            fetcher, state, deadline=deadline)
        payload = discover.build_payload(candidates, batches, stats)
        write_json(CANDIDATES_PATH, payload)
        write_json(WEB_CANDIDATES_PATH, payload)
        # 只在判定结果真的变化时落盘状态，避免每天产生无意义的 diff
        if new_state.get("items") != state.get("items"):
            write_json(DISCOVER_STATE_PATH, new_state)
        print("已导出 {0}（候选 {1} 条 / 本轮评估 {2} 款 / 匹配 {3} 款）".format(
            WEB_CANDIDATES_PATH, len(candidates), stats["evaluated"], stats["matched"]))
        return candidates
    except Exception as exc:  # noqa: BLE001 - 发现失败不影响巡检结果
        print("候选发现失败：{0}: {1}".format(type(exc).__name__, exc), file=sys.stderr)
        return []


# 定性字段自动并入时填这个，前端会显示成浅色占位，提醒还没人看过
PLACEHOLDER = "待补"
# 自动并入的条目优先级取一个远超人工排名的值（人工是 1..10），
# 保证它们永远排在主表末尾，不插队打乱人工排定的顺序，复核时再往上调。
AUTO_PRIORITY = 99
# 版号游戏 ↔ Steam 页面的模糊匹配下限：低于这个分数不自动并入，
# 只留在候选池等人工确认，免得错配的 appid 直接把脏数据写进主表
MIN_MATCH_SCORE = 0.6
STAGE_TEXT = {"none": "无页面", "approved": "已获版号", "preorder": "预约中", "launch": "已上线"}


def _candidate_slug(candidate):
    """给候选生成稳定的 games.id：英文名转 slug → appid → 中文名哈希。"""
    name_en = (candidate.get("name_en") or "").strip()
    if name_en:
        slug = re.sub(r"[^a-z0-9]+", "-", name_en.lower()).strip("-")
        if slug:
            return slug
    if candidate.get("appid"):
        return "steam-{0}".format(candidate["appid"])
    digest = hashlib.md5((candidate.get("name_cn") or "").encode("utf-8")).hexdigest()
    return "cand-{0}".format(digest[:8])


def _english_name(candidate):
    """候选的 name_en 可能是原样照抄的中文名，这种没信息量，置空。"""
    name_en = (candidate.get("name_en") or "").strip()
    return name_en if re.search(r"[A-Za-z]", name_en) else ""


def _stage_text(stage, release):
    return STAGE_TEXT.get(stage, "未知") + (" · {0}".format(release) if release else "")


def game_from_candidate(candidate, imported_at):
    """把一条候选折成 games 里的一条记录。

    能直接从版号 / Steam 读出来的事实（厂商、国服与海外阶段、上架状态）自动填，
    读不出来的定性字段（类型、关键词、内容机会、变现、风险）留 PLACEHOLDER，
    由人在复核时补。绝不为了填满而编造内容。
    """
    steam = candidate.get("steam") or {}
    nppa = candidate.get("nppa") or {}
    stage = candidate.get("stage") or {}

    watch = {}
    if candidate.get("appid"):
        watch["steam_appid"] = candidate["appid"]

    sources = []
    if nppa.get("batch_url"):
        sources.append({"label": "版号公示", "url": nppa["batch_url"]})
    if steam.get("store_url"):
        sources.append({"label": "Steam 商店", "url": steam["store_url"]})

    return {
        "id": _candidate_slug(candidate),
        "name_cn": candidate.get("name_cn") or "",
        "name_en": _english_name(candidate),
        "developer": nppa.get("operator") or nppa.get("publisher") or PLACEHOLDER,
        "genre": PLACEHOLDER,
        "priority": AUTO_PRIORITY,
        "cn_status": _stage_text(stage.get("cn"), stage.get("cn_release")),
        "global_status": _stage_text(stage.get("os"), stage.get("os_release")),
        "overseas_heat": PLACEHOLDER,
        "overseas_heat_checked": None,
        "gap_window": candidate.get("note") or PLACEHOLDER,
        "target_markets": [],
        "keywords": [],
        "content_opportunities": [],
        "monetization_level": PLACEHOLDER,
        "monetization": PLACEHOLDER,
        "risks": [],
        "watch": watch,
        "last_checked": None,
        "steam_listed": steam.get("us_listed"),
        "steam_release_date": steam.get("us_release_date"),
        "official_signal": None,
        "changes": [],
        "sources": sources,
        # 这两个字段让前端和后续维护能一眼认出「机器自己加进来的」
        "auto_imported": True,
        "auto_imported_at": imported_at,
    }


def promote_candidates(payload, candidates, imported_at=None):
    """把候选自动并入主表 games 清单，返回本次新增的条数。

    已存在的 id 一律跳过，所以每天重复跑不会产生重复条目。返回 0 时调用方
    不该重写 games.json，避免每天一份无意义 diff。
    """
    games = payload.get("games")
    if not isinstance(games, list):
        games = payload["games"] = []
    known = {game.get("id") for game in games}
    imported_at = imported_at or today_str()

    added = []
    for candidate in candidates or []:
        # 匹配分太低说明版号名和 Steam 页面可能对不上，宁可留在候选区等人工
        score = (candidate.get("steam") or {}).get("match_score")
        if isinstance(score, (int, float)) and score < MIN_MATCH_SCORE:
            continue
        game = game_from_candidate(candidate, imported_at)
        if game["id"] in known:          # 已经在表里（人工录入或此前自动并入）
            continue
        if not game["name_cn"] and not game["name_en"]:
            continue
        known.add(game["id"])
        games.append(game)
        added.append(game)

    if added:
        print("自动并入 {0} 款候选到主表：{1}".format(
            len(added), "、".join(game["name_cn"] for game in added)))
    return len(added)


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
    parser.add_argument("--no-git", action="store_true", help="写回后不提交本地 git 快照")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    only = {item.strip() for item in args.only.split(",") if item.strip()}

    payload = load_payload()
    deadline = (time.monotonic() + args.budget_seconds) if args.budget_seconds > 0 else None
    fetcher = Fetcher(
        cache_dir=CACHE_DIR,
        timeout=20,               # 单个请求上限；放宽到 20s 以容忍偶发慢站（如 phantombladezero.com 曾超时），
                                  # 整域不可达时仍由 host_interval/retries 与总预算兜底
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

    # 候选发现用独立预算：版号批次每月才变一次，且 Steam 有节流，
    # 与巡检/趋势共用一个 deadline 会让它几乎必被跳过。
    # 放在写盘之前跑，发现结果要能并入主表一起落盘。
    candidates = export_discovery(fetcher, deadline=(time.monotonic() + 100))
    promote_candidates(payload, candidates)

    write_json(DATA_PATH, payload)
    write_json(WEB_EXPORT_PATH, payload)
    print("已写入 {0}".format(DATA_PATH))
    print("已导出 {0}".format(WEB_EXPORT_PATH))
    export_events(payload, fetcher)
    export_trends(payload, fetcher)

    if not args.no_git:
        snapshot_git()

    # 部分失败退出码 0，避免 Actions 假红；全部失败才算真失败。
    if reports and failed == len(reports):
        print("全部数据源失败。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
