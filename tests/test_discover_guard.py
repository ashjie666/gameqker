"""候选发现的护栏：Steam 先行漏斗 + 网络失败不能当成「判定不领先」。

一次网络抖动如果被当成淘汰结论，候选池会被整轮清空；这些用例锁住四条线：
1. 列表解析只认 appid + 标题，Demo 条目不能混进正式版；
2. 版号名比对要能兜住副标题写法，又不能拿两字短名乱撞；
3. appdetails 失败时 evaluate_app 返回 KEEP，而不是 None；
4. run() 遇到 KEEP 或异常时不覆盖已有条目，保留上次结果等下轮重试。
"""

import json
import unittest
from unittest import mock

from collector import discover

NP_PA = {
    "name": "勇者传说",
    "origin": "domestic",
    "approved_at": "2026-08-28",
    "batch_url": "https://www.nppa.gov.cn/example/t20260831.html",
}


class _FakeResponse:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error


class _FakeFetcher:
    """按 URL 顺序返回预设的 JSON，模拟分页列表。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, use_cache=None, timeout=None):
        self.calls.append(url)
        body = self.pages.pop(0) if self.pages else None
        if body is None:
            return _FakeResponse(error="timeout")
        return _FakeResponse(body=json.dumps(body).encode("utf-8"))


def _side(listed=None, coming_soon=None, release_date=None, name="勇者传说"):
    return {"listed": listed, "coming_soon": coming_soon,
            "release_date": release_date, "name": name}


def _list_html(rows):
    return "".join(
        '<a href="https://store.steampowered.com/app/{0}/x/" data-ds-appid="{0}" '
        'class="search_result_row"><div class="search_name ellipsis">'
        '<span class="title">{1}</span></div></a>'.format(appid, title)
        for appid, title in rows)


class ParseListHtmlTest(unittest.TestCase):
    def test_extracts_appid_and_title(self):
        rows = discover.parse_list_html(_list_html([(1001, "勇者传说"), (1002, "九幽卷")]))

        self.assertEqual(rows, [(1001, "勇者传说"), (1002, "九幽卷")])

    def test_duplicate_appid_kept_once(self):
        rows = discover.parse_list_html(
            _list_html([(1001, "勇者传说")]) + _list_html([(1001, "勇者传说")]))

        self.assertEqual(len(rows), 1)


class SteamNewReleasesTest(unittest.TestCase):
    def test_pages_stop_when_short_page(self):
        page = [(1001, "勇者传说")]
        fetcher = _FakeFetcher([
            {"success": 1, "results_html": _list_html(page)},
            {"success": 1, "results_html": _list_html(page)},
        ])

        releases = discover.steam_new_releases(fetcher, pages=3)

        # 两个列表各只返回 1 条（< PAGE_SIZE），各翻一页就停
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual([r["appid"] for r in releases], [1001])
        self.assertTrue(releases[0]["coming_soon"] is False)

    def test_demo_entries_are_skipped(self):
        fetcher = _FakeFetcher([
            {"success": 1, "results_html": _list_html([
                (1001, "九幽卷 Demo"), (1002, "勇者传说")])},
            {"success": 1, "results_html": _list_html([])},
        ])

        releases = discover.steam_new_releases(fetcher, pages=1)

        self.assertEqual([r["appid"] for r in releases], [1002])

    def test_failed_list_is_empty_not_an_error(self):
        releases = discover.steam_new_releases(_FakeFetcher([None, None]), pages=2)

        self.assertEqual(releases, [])


class MatchApprovalTest(unittest.TestCase):
    def setUp(self):
        self.index = discover.build_approval_index([
            NP_PA,
            {"name": "龙魂", "origin": "domestic", "approved_at": "2026-09-01"},
            {"name": "时空传说2黑暗之路", "origin": "domestic", "approved_at": "2026-09-02"},
        ])

    def test_exact_name_matches(self):
        game, score = discover.match_approval("勇者传说", self.index)

        self.assertEqual(game["name"], "勇者传说")
        self.assertEqual(score, 1.0)

    def test_subtitle_variant_matches(self):
        game, _ = discover.match_approval("时空传说2黑暗之路 - 序章", self.index)

        self.assertEqual(game["name"], "时空传说2黑暗之路")

    def test_short_name_is_not_allowed_to_collide(self):
        # 版号「龙魂」只有两个字，不参与包含匹配，避免撞上「龙魂天下」
        self.assertIsNone(discover.match_approval("龙魂天下", self.index))

    def test_unrelated_title_is_a_miss(self):
        self.assertIsNone(discover.match_approval("完全无关的游戏", self.index))


class _HtmlFetcher:
    """返回固定 HTML 的假抓取器，用来测 Bing / 百度资讯解析。"""

    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error

    def get(self, url, use_cache=None, timeout=None):
        return _FakeResponse(self.body, self.error)


class EvaluateAppGuardTest(unittest.TestCase):
    """只有两国区详情都正常返回时才敢下「不领先」的结论。"""

    def setUp(self):
        # 版本 / 攻略维度在 SignalGateTest 里单独测；这里固定成「无数据」，
        # 让先发判定与网络无关
        patcher = mock.patch.object(discover, "evaluate_signals",
                                    return_value=(None, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_us_detail_failure_is_unknown(self):
        with mock.patch.object(discover, "check_app",
                               return_value={"listed": None, "error": "timeout"}):
            self.assertIs(discover.evaluate_app(None, NP_PA, 1001), discover.KEEP)

    def test_cn_detail_failure_is_unknown(self):
        us = _side(True, False, "Dec 1, 2026")
        with mock.patch.object(discover, "check_app", side_effect=[
                us, {"listed": None, "error": "timeout"}]):
            self.assertIs(discover.evaluate_app(None, NP_PA, 1001), discover.KEEP)

    def test_no_overseas_page_is_not_in_scope(self):
        with mock.patch.object(discover, "check_app", return_value=_side(False)):
            self.assertIsNone(discover.evaluate_app(None, NP_PA, 1001, probe_news=False))

    def test_us_released_before_approval_is_definitely_not_leading(self):
        with mock.patch.object(discover, "check_app", side_effect=[
                _side(True, False, "Jan 1, 2026"), _side(False)]):
            self.assertIsNone(discover.evaluate_app(None, NP_PA, 1001, probe_news=False))

    def test_us_released_while_cn_still_preorder_is_not_leading(self):
        # 版号批得早不算领先：美区已发售、国服还没上线，国内此刻玩不到
        us = _side(True, False, "Oct 27, 2026")
        cn = _side(True, True, None)
        with mock.patch.object(discover, "check_app", side_effect=[us, cn]):
            self.assertIsNone(discover.evaluate_app(None, NP_PA, 1001, probe_news=False))

    def test_cn_launched_before_overseas_is_a_candidate(self):
        with mock.patch.object(discover, "check_app", side_effect=[
                _side(True, False, "Dec 1, 2026"),
                _side(True, False, "Sep 1, 2026")]), \
                mock.patch.object(discover, "fetch_latest_version", return_value=None), \
                mock.patch.object(discover, "detect_playtest", return_value=None):
            candidate = discover.evaluate_app(None, NP_PA, 1001, "勇者传说", 1.0,
                                             probe_news=False)

        self.assertEqual(candidate["gap_type"], "cn_first")
        self.assertEqual(candidate["appid"], 1001)
        self.assertEqual(candidate["steam"]["match_score"], 1.0)

    def test_same_day_launch_is_counted_as_sync(self):
        # 同日上线没有先发优势，但按用户要求一律收录，单独标 cn_sync
        with mock.patch.object(discover, "check_app", side_effect=[
                _side(True, False, "Sep 1, 2026"), _side(True, False, "Sep 1, 2026")]), \
                mock.patch.object(discover, "detect_playtest", return_value=None):
            candidate = discover.evaluate_app(None, NP_PA, 1001, probe_news=False)

        self.assertEqual(candidate["gap_type"], "cn_sync")
        self.assertEqual(candidate["gap_days"], 0)
        self.assertEqual(candidate["advantages"], ["cn_sync"])

    def test_cn_launched_later_is_not_counted(self):
        # 国服比国际服更晚上线：海外先发，不是目标
        with mock.patch.object(discover, "check_app", side_effect=[
                _side(True, False, "Sep 1, 2026"), _side(True, False, "Oct 1, 2026")]):
            self.assertIsNone(discover.evaluate_app(None, NP_PA, 1001, probe_news=False))


class VersionSignalTest(unittest.TestCase):
    """版本号抽取与比较：补齐位数、避免把日期当版本号。"""

    def test_extracts_version_with_chinese_context(self):
        self.assertEqual(discover.extract_version("《勇者传说》1.2.3 版本更新上线"), "1.2.3")

    def test_extracts_version_with_keyword_prefix(self):
        self.assertEqual(discover.extract_version("Update 15 hotfix"), "15")

    def test_extracts_chinese_version_keyword(self):
        self.assertEqual(discover.extract_version("版本 2.5 已上线"), "2.5")

    def test_quantity_word_is_not_a_version(self):
        self.assertIsNone(discover.extract_version("本次更新 3 个新角色"))

    def test_date_is_not_taken_as_version(self):
        self.assertIsNone(discover.extract_version("2026年10月28日 上线公告"))

    def test_padding_makes_1_2_equal_1_2_0(self):
        self.assertFalse(discover.version_ahead("1.2", "1.2.0"))

    def test_numeric_compare_not_lexical(self):
        self.assertTrue(discover.version_ahead("1.10", "1.9"))

    def test_missing_side_is_unknown(self):
        self.assertIsNone(discover.version_ahead(None, "1.2"))


class NewsBreakerTest(unittest.TestCase):
    """api.steampowered.com 连不通时熔断，别让每款游戏都白等一次超时。"""

    def setUp(self):
        discover.reset_news_breaker()
        self.addCleanup(discover.reset_news_breaker)

    def test_two_failures_open_the_breaker(self):
        fetcher = _FakeFetcher([None, None])

        discover.fetch_latest_version(fetcher, 1001)
        discover.fetch_latest_version(fetcher, 1002)
        self.assertTrue(discover.news_breaker_open())

        # 熔断后不再发请求，直接按「版本未知」处理
        self.assertIsNone(discover.fetch_latest_version(fetcher, 1003))
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(discover.news_breaker_skipped(), 1)

    def test_success_resets_the_failure_streak(self):
        news = {"appnews": {"newsitems": [{"title": "Update 1.2.3", "contents": ""}]}}
        fetcher = _FakeFetcher([None, news, None])

        self.assertIsNone(discover.fetch_latest_version(fetcher, 1001))
        self.assertEqual(discover.fetch_latest_version(fetcher, 1002)["value"], "1.2.3")
        self.assertIsNone(discover.fetch_latest_version(fetcher, 1003))
        # 中间成功过一次，失败计数归零，两次失败不能连起来触发熔断
        self.assertFalse(discover.news_breaker_open())

    def test_reset_closes_the_breaker(self):
        fetcher = _FakeFetcher([None, None])
        discover.fetch_latest_version(fetcher, 1001)
        discover.fetch_latest_version(fetcher, 1002)
        self.assertTrue(discover.news_breaker_open())

        discover.reset_news_breaker()

        self.assertFalse(discover.news_breaker_open())
        self.assertEqual(discover.news_breaker_skipped(), 0)


class CnVersionFetchTest(unittest.TestCase):
    def test_failed_search_is_unknown(self):
        self.assertIs(discover.fetch_cn_version(_HtmlFetcher(None), "勇者传说"), discover.KEEP)

    def test_version_from_title(self):
        items = [{"title": "《勇者传说》1.2.3 版本更新", "summary": "",
                  "link": "https://example.com/a"}]
        with mock.patch.object(discover.feeds, "parse_search_items", return_value=items):
            result = discover.fetch_cn_version(_HtmlFetcher(b"<html></html>"), "勇者传说")

        self.assertEqual(result["value"], "1.2.3")
        self.assertEqual(result["url"], "https://example.com/a")

    def test_result_without_game_name_is_ignored(self):
        items = [{"title": "别的游戏 1.2.3 版本更新", "summary": "",
                  "link": "https://example.com/b"}]
        with mock.patch.object(discover.feeds, "parse_search_items", return_value=items), \
                mock.patch.object(discover.feeds, "article_text", return_value=None):
            result = discover.fetch_cn_version(_HtmlFetcher(b"<html></html>"), "勇者传说")

        self.assertIsNone(result)

    def test_non_version_news_is_ignored(self):
        # 标题提到游戏但没有版本字样，里面的数字不能当版本号
        items = [{"title": "《勇者传说》登顶 2026 年 10 月销量榜", "summary": "",
                  "link": "https://example.com/c"}]
        with mock.patch.object(discover.feeds, "parse_search_items", return_value=items):
            result = discover.fetch_cn_version(_HtmlFetcher(b"<html></html>"), "勇者传说")

        self.assertIsNone(result)


class GuideSignalTest(unittest.TestCase):
    def test_parses_bing_count(self):
        fetcher = _HtmlFetcher(
            '<span class="sb_count">约 101,000 条结果</span>'.encode("utf-8"))

        self.assertEqual(discover.bing_result_count(fetcher, "勇者传说 攻略", "zh-CN"), 101000)

    def test_failed_page_is_unknown(self):
        self.assertIs(discover.bing_result_count(_HtmlFetcher(None), "x", "zh-CN"),
                      discover.KEEP)

    def test_missing_count_is_not_a_verdict(self):
        self.assertIsNone(discover.bing_result_count(_HtmlFetcher(b"<html></html>"), "x", "zh-CN"))

    def test_ratio_below_threshold_is_not_advantage(self):
        with mock.patch.object(discover, "bing_result_count", side_effect=[1000, 900]):
            result = discover.evaluate_guides(None, "勇者传说", "Brave Legend")

        self.assertFalse(result["advantage"])

    def test_tiny_chinese_sample_is_not_advantage(self):
        with mock.patch.object(discover, "bing_result_count", side_effect=[10, 2]):
            result = discover.evaluate_guides(None, "勇者传说", "Brave Legend")

        self.assertFalse(result["advantage"])

    def test_clear_chinese_lead_is_advantage(self):
        with mock.patch.object(discover, "bing_result_count", side_effect=[3000, 900]):
            result = discover.evaluate_guides(None, "勇者传说", "Brave Legend")

        self.assertTrue(result["advantage"])
        self.assertEqual(result["ratio"], 3.3)


class SignalGateTest(unittest.TestCase):
    """先发不成立时，版本领先 / 攻略成熟度单独成立也算候选。"""

    def _candidate(self, signals, us=None, cn=None):
        # 默认「国服更晚上线」：既没有先发优势也没有同步，才轮得到版本 / 攻略判定
        us = us or _side(True, False, "Oct 20, 2026")
        cn = cn or _side(True, False, "Oct 25, 2026")
        with mock.patch.object(discover, "check_app", side_effect=[us, cn]), \
                mock.patch.object(discover, "evaluate_signals", return_value=signals), \
                mock.patch.object(discover, "detect_playtest", return_value=None):
            return discover.evaluate_app(None, NP_PA, 1001, "勇者传说", 1.0, probe_news=False)

    def test_version_ahead_alone_is_a_candidate(self):
        version = {"cn": "1.3", "steam": "1.2", "source": "Update", "ahead": True}
        guides = {"cn": 10, "intl": 100, "ratio": 0.1, "advantage": False}

        candidate = self._candidate((version, guides))

        self.assertEqual(candidate["gap_type"], "cn_version")
        self.assertEqual(candidate["advantages"], ["cn_version"])
        self.assertIn("1.3", candidate["note"])

    def test_guides_advantage_alone_is_a_candidate(self):
        version = {"cn": None, "steam": None, "ahead": False}
        guides = {"cn": 3000, "intl": 900, "ratio": 3.3, "advantage": True}

        candidate = self._candidate((version, guides))

        self.assertEqual(candidate["gap_type"], "cn_guides")
        self.assertEqual(candidate["advantages"], ["cn_guides"])

    def test_no_advantage_is_not_a_candidate(self):
        version = {"cn": "1.0", "steam": "1.2", "ahead": False}
        guides = {"cn": 100, "intl": 100, "ratio": 1.0, "advantage": False}

        self.assertIsNone(self._candidate((version, guides)))

    def test_unknown_version_signal_defers_instead_of_rejecting(self):
        guides = {"cn": 100, "intl": 100, "ratio": 1.0, "advantage": False}

        self.assertIs(self._candidate((discover.KEEP, guides)), discover.KEEP)

    def test_unknown_guides_signal_defers_instead_of_rejecting(self):
        version = {"cn": None, "steam": None, "ahead": False}

        self.assertIs(self._candidate((version, discover.KEEP)), discover.KEEP)

    def test_first_move_candidate_survives_unknown_signals(self):
        us = _side(True, False, "Dec 1, 2026")
        cn = _side(True, False, "Sep 1, 2026")

        candidate = self._candidate((discover.KEEP, discover.KEEP), us, cn)

        self.assertEqual(candidate["gap_type"], "cn_first")
        self.assertEqual(candidate["advantages"], ["cn_first"])


class CollectGamesWindowTest(unittest.TestCase):
    """只取国产批次，索引窗口开到列表页能拿到的全部批次。"""

    def test_only_domestic_batches(self):
        calls = []

        def fake_batches(fetcher, url, keyword, limit, pages=1):
            calls.append((keyword, limit, pages))
            return [{"title": keyword, "url": url + "#batch"}]

        def fake_parse(fetcher, batch):
            return [{"name": "乙", "approved_at": "2026-09-29"}]

        with mock.patch.object(discover, "fetch_batches", side_effect=fake_batches), \
                mock.patch.object(discover, "parse_batch", side_effect=fake_parse):
            games = discover.collect_games(mock.Mock())

        self.assertEqual(calls, [("国产网络游戏审批信息", 24, 1)])
        self.assertEqual({game["origin"] for game in games}, {"domestic"})


class StaleTest(unittest.TestCase):
    """已确认不领先的条目不该每轮都重评，白占评估预算。"""

    TODAY = discover.date(2026, 10, 28)

    def test_empty_candidate_is_not_re_evaluated_every_run(self):
        entry = {"checked_at": "2026-10-27", "candidate": None}

        self.assertFalse(discover._is_stale(entry, self.TODAY))

    def test_candidate_without_timeline_is_re_evaluated_once(self):
        entry = {"checked_at": "2026-10-27", "candidate": {"name_cn": "勇者传说"}}

        self.assertTrue(discover._is_stale(entry, self.TODAY))

    def test_candidate_with_timeline_waits_for_refresh_window(self):
        entry = {"checked_at": "2026-10-27",
                 "candidate": {"name_cn": "勇者传说", "timeline": {}, "guides": {}}}

        self.assertFalse(discover._is_stale(entry, self.TODAY))

    def test_candidate_without_guides_is_re_evaluated_once(self):
        # 判定结构新增了 guides，旧候选要重评一次把新字段补齐
        entry = {"checked_at": "2026-10-27",
                 "candidate": {"name_cn": "勇者传说", "timeline": {}}}

        self.assertTrue(discover._is_stale(entry, self.TODAY))


class RunGuardTest(unittest.TestCase):
    """run() 不能因为查询失败就丢掉已入库的候选。"""

    GAME = {"name": "勇者传说", "origin": "domestic", "approved_at": "2026-08-28"}
    RELEASE = {"appid": 1001, "title": "勇者传说", "coming_soon": True}
    KEY = "domestic:勇者传说"

    def _state(self):
        return {"items": {self.KEY: {
            "checked_at": "2026-10-01",
            "approved_at": "2026-08-28",
            "candidate": {"name_cn": "勇者传说", "appid": 1001,
                          "timeline": {"cn": {}, "intl": {}}},
        }}}

    def _run(self, evaluate_result=None, evaluate_error=None):
        with mock.patch.object(discover, "collect_games", return_value=[self.GAME]), \
                mock.patch.object(discover, "steam_new_releases",
                                  return_value=[self.RELEASE]), \
                mock.patch.object(discover, "evaluate_app",
                                  return_value=evaluate_result,
                                  side_effect=evaluate_error):
            return discover.run(None, self._state(), today=discover.date(2026, 10, 28))

    def test_unknown_keeps_previous_candidate_and_date(self):
        candidates, state, _, stats = self._run(evaluate_result=discover.KEEP)

        entry = state["items"][self.KEY]
        self.assertEqual(entry["checked_at"], "2026-10-01")
        self.assertEqual(entry["candidate"]["appid"], 1001)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(stats["deferred"], 1)

    def test_exception_keeps_previous_candidate(self):
        candidates, state, _, stats = self._run(evaluate_error=RuntimeError("boom"))

        self.assertEqual(state["items"][self.KEY]["candidate"]["appid"], 1001)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(len(stats["errors"]), 1)

    def test_confirmed_not_leading_does_remove_candidate(self):
        candidates, state, _, stats = self._run(evaluate_result=None)

        self.assertIsNone(state["items"][self.KEY]["candidate"])
        self.assertEqual(state["items"][self.KEY]["checked_at"], "2026-10-28")
        self.assertEqual(candidates, [])
        self.assertEqual(stats["deferred"], 0)

    def test_name_miss_is_never_evaluated(self):
        with mock.patch.object(discover, "collect_games", return_value=[self.GAME]), \
                mock.patch.object(discover, "steam_new_releases",
                                  return_value=[{"appid": 2002, "title": "完全无关的游戏",
                                                 "coming_soon": False}]), \
                mock.patch.object(discover, "evaluate_app") as evaluate_app:
            candidates, state, _, stats = discover.run(
                None, self.state_empty(), today=discover.date(2026, 10, 28))

        evaluate_app.assert_not_called()
        self.assertEqual(stats["evaluated"], 0)
        self.assertEqual(candidates, [])
        self.assertEqual(state["items"], {})

    def state_empty(self):
        return {"items": {}}

    def test_budget_caps_new_evaluations(self):
        releases = [{"appid": 1001 + i, "title": "勇者传说" + "甲" * i,
                     "coming_soon": False} for i in range(3)]
        with mock.patch.object(discover, "collect_games", return_value=[self.GAME]), \
                mock.patch.object(discover, "steam_new_releases", return_value=releases), \
                mock.patch.object(discover, "evaluate_app", return_value=None) as evaluate_app:
            _, _, _, stats = discover.run(None, self.state_empty(), max_new=1,
                                          today=discover.date(2026, 10, 28))

        # 三条列表项都指向同一款游戏，去重后只评一次，剩下两条不算超预算
        self.assertEqual(stats["evaluated"], 1)
        self.assertEqual(evaluate_app.call_count, 1)

    def test_old_approval_is_still_evaluated(self):
        # 前年获批、今年才在海外上线的游戏正是要抓的对象，不能因为批准日老就被裁掉
        old = dict(self.GAME, approved_at="2025-11-26")
        with mock.patch.object(discover, "collect_games", return_value=[old]), \
                mock.patch.object(discover, "steam_new_releases",
                                  return_value=[self.RELEASE]), \
                mock.patch.object(discover, "evaluate_app", return_value=None) as evaluate_app:
            _, state, _, stats = discover.run(None, self.state_empty(),
                                              today=discover.date(2026, 10, 28))

        self.assertEqual(evaluate_app.call_count, 1)
        self.assertEqual(stats["evaluated"], 1)
        self.assertEqual(state["items"][self.KEY]["approved_at"], "2025-11-26")

    def test_state_drops_entries_not_checked_for_a_year(self):
        state = self._state()
        state["items"][self.KEY]["checked_at"] = "2025-09-01"
        with mock.patch.object(discover, "collect_games", return_value=[self.GAME]), \
                mock.patch.object(discover, "steam_new_releases", return_value=[]):
            _, new_state, _, _ = discover.run(None, state, today=discover.date(2026, 10, 28))

        self.assertEqual(new_state["items"], {})


class _PagedFetcher:
    """按顺序返回不同 HTML 的假抓取器，用来测列表页翻页。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, use_cache=None, timeout=None):
        self.calls.append(url)
        body = self.pages.pop(0) if self.pages else None
        if body is None:
            return _FakeResponse(error="timeout")
        return _FakeResponse(body=body.encode("utf-8"))


def _batch_page(rows, page_count=None):
    html = ""
    if page_count is not None:
        html += "<script>var _pageCount = {0};</script>".format(page_count)
    for date, month in rows:
        html += ('<a href="./{0}/t{1}_1.html">{2}年{3}月份国产网络游戏审批信息</a>'
                 .format(date[:6], date, date[:4], month))
    return html


class FetchBatchesPagingTest(unittest.TestCase):
    def test_follows_pages_until_page_count(self):
        fake = _PagedFetcher([
            _batch_page([("20260901", 9), ("20260801", 8)], page_count=2),
            _batch_page([("20250901", 9)]),
        ])
        batches = discover.fetch_batches(
            fake, "https://www.nppa.gov.cn/base/", "国产网络游戏审批信息", 10, pages=5)

        self.assertEqual(len(batches), 3)
        self.assertTrue(fake.calls[1].endswith("index_1.html"))

    def test_default_single_page_keeps_daily_cost(self):
        fake = _PagedFetcher([_batch_page([("20260901", 9), ("20260801", 8)], page_count=13)])
        batches = discover.fetch_batches(
            fake, "https://www.nppa.gov.cn/base/", "国产网络游戏审批信息", 10)

        self.assertEqual(len(batches), 2)
        self.assertEqual(len(fake.calls), 1, "日常巡检不应该额外翻页")

    def test_stops_when_a_page_yields_nothing(self):
        fake = _PagedFetcher([_batch_page([("20260901", 9)], page_count=9), "<html></html>"])
        batches = discover.fetch_batches(
            fake, "https://www.nppa.gov.cn/base/", "国产网络游戏审批信息", 10, pages=9)

        self.assertEqual(len(batches), 1)
        self.assertEqual(len(fake.calls), 2)


class SteamSearchTest(unittest.TestCase):
    def test_rows_parsed_from_results_html(self):
        payload = {"success": 1, "results_html": _list_html([(1001, "勇者传说"), (1002, "影之刃零")])}
        rows = discover.steam_search(_FakeFetcher([payload]), "勇者传说")
        self.assertEqual(rows, [(1001, "勇者传说"), (1002, "影之刃零")])

    def test_fetch_failure_is_none_not_empty(self):
        self.assertIsNone(discover.steam_search(_FakeFetcher([None]), "勇者传说"))

    def test_empty_results_is_empty_list(self):
        payload = {"success": 1, "results_html": ""}
        self.assertEqual(discover.steam_search(_FakeFetcher([payload]), "勇者传说"), [])


class PickSearchMatchTest(unittest.TestCase):
    def test_exact_name_wins(self):
        match = discover.pick_search_match("勇者传说", [(1, "勇者传说"), (2, "勇者传说 外传")])
        self.assertEqual(match[0], 1)

    def test_demo_skipped(self):
        match = discover.pick_search_match("勇者传说", [(1, "勇者传说 Demo")])
        self.assertIsNone(match)

    def test_unrelated_result_rejected(self):
        self.assertIsNone(discover.pick_search_match("勇者传说", [(1, "九幽卷")]))

    def test_short_name_does_not_hit_long_title(self):
        # 「龙魂」只占「龙魂天下」一半，低于阈值，不能乱配 appid
        self.assertIsNone(discover.pick_search_match("龙魂", [(1, "龙魂天下")]))

    def test_sequel_is_not_matched_to_original(self):
        # 回填结果会被自动并入主表，《古剑奇谭二》不能当成《古剑奇谭》
        self.assertIsNone(discover.pick_search_match("古剑奇谭", [(570770, "古剑奇谭二")]))

    def test_exact_plus_suffix_still_matches(self):
        match = discover.pick_search_match("影之刃零", [(4115450, "影之刃零")])
        self.assertEqual(match[0], 4115450)


class BackfillTest(unittest.TestCase):
    """存量回填：命中就评估，搜不到就记进度，查询失败不能当结论。"""

    def setUp(self):
        self.game = dict(NP_PA)
        self.state = {"items": {}}
        self.seen = {}

    def test_match_is_evaluated_and_candidate_saved(self):
        candidate = {"name_cn": "勇者传说", "gap_type": "cn_first", "gap_days": 30}
        with mock.patch.object(discover, "steam_search", return_value=[(1001, "勇者传说")]), \
                mock.patch.object(discover, "evaluate_app", return_value=candidate) as evaluate_app:
            candidates, state, seen, stats = discover.backfill(
                None, self.state, self.seen, [self.game], today=discover.date(2026, 10, 28))

        self.assertEqual(evaluate_app.call_args[0][2], 1001)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(seen["domestic:勇者传说"]["appid"], 1001)
        self.assertIn("domestic:勇者传说", state["items"])

    def test_no_match_records_no_page(self):
        with mock.patch.object(discover, "steam_search", return_value=[]), \
                mock.patch.object(discover, "evaluate_app") as evaluate_app:
            _, state, seen, stats = discover.backfill(
                None, self.state, self.seen, [self.game], today=discover.date(2026, 10, 28))

        self.assertEqual(evaluate_app.call_count, 0)
        self.assertEqual(stats["no_page"], 1)
        self.assertEqual(seen["domestic:勇者传说"]["status"], "no_page")
        self.assertEqual(state["items"], {})

    def test_search_failure_writes_no_progress(self):
        with mock.patch.object(discover, "steam_search", return_value=None), \
                mock.patch.object(discover, "evaluate_app") as evaluate_app:
            _, _, seen, stats = discover.backfill(
                None, self.state, self.seen, [self.game], today=discover.date(2026, 10, 28))

        self.assertEqual(evaluate_app.call_count, 0)
        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(seen, {}, "查询失败不能写成「搜不到」，否则再也不会重试")

    def test_budget_limits_games_per_run(self):
        games = [dict(self.game, name="游戏{0}".format(i)) for i in range(5)]
        with mock.patch.object(discover, "steam_search", return_value=[]):
            _, _, _, stats = discover.backfill(
                None, self.state, self.seen, games, budget=2,
                today=discover.date(2026, 10, 28))

        self.assertEqual(stats["attempted"], 2)

    def test_recent_no_page_is_skipped(self):
        self.seen["domestic:勇者传说"] = {"checked_at": "2026-10-20", "status": "no_page"}
        with mock.patch.object(discover, "steam_search") as search:
            _, _, _, stats = discover.backfill(
                None, self.state, self.seen, [self.game], today=discover.date(2026, 10, 28))

        self.assertEqual(search.call_count, 0)
        self.assertEqual(stats["skipped"], 1)

    def test_known_appid_reevaluated_without_search(self):
        self.seen["domestic:勇者传说"] = {"checked_at": "2026-10-01", "status": "page",
                                          "appid": 1001, "title": "勇者传说", "score": 1.0}
        with mock.patch.object(discover, "steam_search") as search, \
                mock.patch.object(discover, "evaluate_app", return_value=None) as evaluate_app:
            discover.backfill(None, self.state, self.seen, [self.game],
                              today=discover.date(2026, 10, 28))

        self.assertEqual(search.call_count, 0)
        self.assertEqual(evaluate_app.call_args[0][2], 1001)

    def test_keep_does_not_overwrite_state(self):
        with mock.patch.object(discover, "steam_search", return_value=[(1001, "勇者传说")]), \
                mock.patch.object(discover, "evaluate_app", return_value=discover.KEEP):
            _, state, _, stats = discover.backfill(
                None, self.state, self.seen, [self.game], today=discover.date(2026, 10, 28))

        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(state["items"], {})

    def test_aborts_after_consecutive_network_failures(self):
        # Steam 断链时不该继续白等超时，连续失败就收工
        games = [dict(self.game, name="游戏{0}".format(i))
                 for i in range(discover.BACKFILL_FAILURE_LIMIT + 5)]
        with mock.patch.object(discover, "steam_search", return_value=None):
            _, _, seen, stats = discover.backfill(
                None, self.state, self.seen, games, budget=0,
                today=discover.date(2026, 10, 28))

        self.assertTrue(stats.get("aborted"))
        self.assertEqual(stats["attempted"], discover.BACKFILL_FAILURE_LIMIT)
        self.assertEqual(seen, {})

    def test_exhausted_index_marks_done(self):
        # 全部条目都已有结论、没被预算打断 → 标记扫完，循环脚本据此收工
        self.seen["domestic:勇者传说"] = {"checked_at": "2026-10-20", "status": "no_page"}
        with mock.patch.object(discover, "steam_search") as search:
            _, _, _, stats = discover.backfill(
                None, self.state, self.seen, [self.game],
                today=discover.date(2026, 10, 28))

        self.assertEqual(search.call_count, 0)
        self.assertTrue(stats.get("done"))

    def test_budget_break_is_not_done(self):
        with mock.patch.object(discover, "steam_search", return_value=[]):
            _, _, _, stats = discover.backfill(
                None, self.state, self.seen, [dict(self.game), dict(self.game, name="另一款")],
                budget=1, today=discover.date(2026, 10, 28))

        self.assertFalse(stats.get("done"))


if __name__ == "__main__":
    unittest.main()
