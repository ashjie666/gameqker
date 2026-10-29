"""存量回填：全量版号索引 × Steam 美区定向搜索。

日常巡检（python -m collector.update）只看 Steam 新发售 / 即将发售的最近几页，
只能发现「刚出海」的增量。本命令把国家新闻出版署国产审批的**全部历史批次**
（列表页 13 页，2010 年至今）当索引，逐款按名字在 Steam 美区搜，命中就照常走
同一套判定，补上「早已出海」的存量游戏。

进度分两处保存，都支持断点续跑：
- data/backfill_state.json：搜过哪些游戏、搜到哪个 appid（含「搜不到」）
- data/discover_state.json：判定结果（与日常巡检共用，候选直接出现在新发现区）

用法：
    python -m collector.backfill --budget 200 --minutes 8
    python -m collector.backfill --pages 2 --budget 40      # 只翻近两年批次
    python -m collector.backfill --budget 0 --minutes 0     # 只看索引规模，不联网

跑完后执行 python -m collector.update --no-git 即可把候选并入主表。
"""

import argparse
import json
import os
import sys
import time

from . import discover
from .http import Fetcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "discover_state.json")
BACKFILL_STATE_PATH = os.path.join(ROOT, "data", "backfill_state.json")
INDEX_PATH = os.path.join(ROOT, "data", "nppa_index.json")
CANDIDATES_PATH = os.path.join(ROOT, "data", "candidates.json")
WEB_CANDIDATES_PATH = os.path.join(ROOT, "web", "data", "candidates.json")

SOURCES = [
    {"origin": "domestic", "title": "国产网络游戏审批信息（历史全量批次）",
     "url": discover.NPPA_LIST_URL},
    {"origin": "intl", "title": "Steam 美区按名定向搜索（cc=us / l=schinese）",
     "url": "https://store.steampowered.com/search/?cc=us&l=schinese"},
]


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write(path, payload):
    """tmp + os.replace 原子写，避免页面读到半截文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def build_index(fetcher, batches):
    """把批次详情页解析成游戏列表，已解析过的批次走本地缓存。

    回填要跑很多轮（全量索引两万多款，一轮只处理几百款），而批次页 URL 一旦
    发布就不再变，所以没必要每轮都把 180 个批次重新抓一遍——只抓新增批次。
    """
    cache = _load(INDEX_PATH, {})
    stored = cache.get("batches") or {}
    games = []
    fetched = 0
    for batch in batches:
        entry = stored.get(batch["url"])
        if entry and entry.get("games"):
            rows = entry["games"]
        else:
            rows = discover.parse_batch(fetcher, batch)
            stored[batch["url"]] = {"title": batch["title"], "games": rows}
            fetched += 1
        for game in rows:
            game = dict(game)          # 缓存里的条目不能在别处被就地改写
            game["origin"] = "domestic"
            games.append(game)
    if fetched:
        _write(INDEX_PATH, {"updated_at": discover.utc_now_iso(),
                            "batches": stored})
    print("版号索引缓存：复用 {0} 个批次 / 新抓 {1} 个".format(
        len(batches) - fetched, fetched))
    return games


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="版号全量索引 × Steam 定向搜索（存量回填）")
    parser.add_argument("--budget", type=int, default=200,
                        help="本轮最多处理多少款（0 表示不限）")
    parser.add_argument("--minutes", type=float, default=8.0,
                        help="本轮最长运行分钟数（0 表示不限）")
    parser.add_argument("--pages", type=int, default=discover.BACKFILL_PAGE_COUNT,
                        help="版号列表页翻多少页（1 页 ≈ 15 个批次 ≈ 1 年）")
    parser.add_argument("--batch-limit", type=int, default=400,
                        help="最多取多少个版号批次")
    parser.add_argument("--dry-run", action="store_true",
                        help="只建版号索引并打印规模，不搜 Steam、不写候选")
    args = parser.parse_args(argv)

    fetcher = Fetcher(cache_dir=os.path.join(ROOT, "data", "cache"),
                      timeout=15, host_interval=1.0, retries=2)
    deadline = (time.monotonic() + args.minutes * 60) if args.minutes else None

    batches = discover.fetch_batches(
        fetcher, discover.NPPA_LIST_URL, "国产网络游戏审批信息",
        args.batch_limit, pages=max(1, args.pages))
    games = build_index(fetcher, batches)
    games.sort(key=lambda g: g.get("approved_at") or "", reverse=True)
    print("版号批次 {0} 个 / 游戏 {1} 款（最新 {2} / 最早 {3}）".format(
        len(batches), len(games),
        games[0].get("approved_at") if games else "-",
        games[-1].get("approved_at") if games else "-"))
    if args.dry_run or not games:
        return 0

    state = _load(STATE_PATH, {})
    progress = _load(BACKFILL_STATE_PATH, {})
    seen = progress.get("seen") or {}
    before = len(seen)
    candidates, state, seen, stats = discover.backfill(
        fetcher, state, seen, games, budget=args.budget, deadline=deadline)

    _write(STATE_PATH, state)
    progress = {"updated_at": discover.utc_now_iso(),
                "batch_pages": args.pages, "seen": seen}
    _write(BACKFILL_STATE_PATH, progress)
    payload = discover.build_payload(
        candidates, SOURCES, stats,
        source_name="Steam 美区按名定向搜索 × 国家新闻出版署 · 国产网络游戏审批信息（存量回填）")
    _write(CANDIDATES_PATH, payload)
    _write(WEB_CANDIDATES_PATH, payload)

    print("已搜 {0} 款 → 命中页面 {1} 款 / 无页面 {2} 款 / 评估 {3} 款 / 判为候选 {4} 款 / "
          "网络失败 {5} 款 / 跳过 {6} 款".format(
              stats["attempted"], stats["hit"], stats["no_page"], stats["evaluated"],
              stats["matched"], stats["deferred"], stats["skipped"]))
    print("进度表 {0} → {1} 条；候选池共 {2} 条（{3}）".format(
        before, len(seen), len(candidates), WEB_CANDIDATES_PATH))
    if discover.news_breaker_open():
        print("Steam 版本公告接口本轮不通，已熔断跳过 {0} 次查询（版本按未知处理）。".format(
            discover.news_breaker_skipped()))
    if stats.get("aborted"):
        print("Steam 连续 {0} 款都连不上，本轮提前中止；网络恢复后重跑即可续上。".format(
            discover.BACKFILL_FAILURE_LIMIT), file=sys.stderr)
    if stats.get("done"):
        print("存量已扫完：索引里 {0} 款都有结论了。".format(stats["indexed"]))
    if stats.get("errors"):
        print("出错 {0} 款：{1}".format(len(stats["errors"]), "、".join(stats["errors"][:5])),
              file=sys.stderr)
    print("提示：执行 python -m collector.update --no-git 可把候选并入主表。")
    # 退出码给循环脚本用：3 = Steam 断链中止，4 = 全量扫完，0 = 正常推进
    if stats.get("aborted"):
        return 3
    if stats.get("done"):
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
