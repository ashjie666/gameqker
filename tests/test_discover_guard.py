"""候选发现的安全护栏：网络失败不能当成「判定不领先」。

一次网络抖动如果被当成淘汰结论，候选池会被整轮清空；这些用例锁住三条线：
1. Steam 搜索 / 详情页失败时，match_appid 与 evaluate 返回 KEEP，而不是 None；
2. run() 遇到 KEEP 或异常时不覆盖已有条目，保留上次结果等下轮重试；
3. 只有「查询成功且确实不领先」时才允许把条目改成 None。
"""

import unittest
from unittest import mock

from collector import discover

NP_PA = {
    "name": "勇者传说",
    "origin": "domestic",
    "approved_at": "2026-08-28",
    "batch_url": "https://www.nppa.gov.cn/example/t20260831.html",
}


def _side(listed=None, coming_soon=None, release_date=None):
    return {"listed": listed, "coming_soon": coming_soon,
            "release_date": release_date, "name": "勇者传说"}


def _matched(cn=None):
    return {"appid": 1001, "name": "勇者传说", "score": 0.9,
            "cn": cn if cn is not None else _side(True, True, "Nov 1, 2026")}


class MatchAppidGuardTest(unittest.TestCase):
    """搜索失败 ≠ 没搜到。"""

    def test_search_request_failure_is_unknown(self):
        with mock.patch.object(discover, "steam_search", return_value=None):
            self.assertIs(discover.match_appid(None, NP_PA), discover.KEEP)

    def test_empty_search_result_is_a_definite_miss(self):
        with mock.patch.object(discover, "steam_search", return_value=[]):
            self.assertIsNone(discover.match_appid(None, NP_PA))

    def test_detail_failure_without_match_is_unknown(self):
        items = [{"id": 1001}]
        with mock.patch.object(discover, "steam_search", return_value=items), \
                mock.patch.object(discover, "check_app", return_value=_side(None)):
            self.assertIs(discover.match_appid(None, NP_PA), discover.KEEP)

    def test_detail_failure_does_not_hide_a_later_match(self):
        items = [{"id": 1001}, {"id": 1002}]
        responses = [_side(None), _side(True, True, "Nov 1, 2026")]
        with mock.patch.object(discover, "steam_search", return_value=items), \
                mock.patch.object(discover, "check_app", side_effect=responses):
            matched = discover.match_appid(None, NP_PA)

        self.assertEqual(matched["appid"], 1002)

    def test_all_details_answered_but_name_differs_is_a_miss(self):
        items = [{"id": 1001}]
        other = {"listed": True, "coming_soon": False, "release_date": None,
                 "name": "完全无关的游戏名"}
        with mock.patch.object(discover, "steam_search", return_value=items), \
                mock.patch.object(discover, "check_app", return_value=other):
            self.assertIsNone(discover.match_appid(None, NP_PA))


class EvaluateGuardTest(unittest.TestCase):
    """只有美区详情正常返回时才敢下「不领先」的结论。"""

    def test_match_unknown_propagates(self):
        with mock.patch.object(discover, "match_appid", return_value=discover.KEEP):
            self.assertIs(discover.evaluate(None, NP_PA), discover.KEEP)

    def test_us_detail_failure_is_unknown(self):
        with mock.patch.object(discover, "match_appid", return_value=_matched()), \
                mock.patch.object(discover, "check_app",
                                  return_value={"listed": None, "error": "timeout"}):
            self.assertIs(discover.evaluate(None, NP_PA), discover.KEEP)

    def test_us_released_before_approval_is_definitely_not_leading(self):
        us = _side(True, False, "Jan 1, 2026")
        with mock.patch.object(discover, "match_appid", return_value=_matched()), \
                mock.patch.object(discover, "check_app", return_value=us):
            self.assertIsNone(discover.evaluate(None, NP_PA, probe_news=False))

    def test_no_overseas_page_is_not_in_scope(self):
        us = _side(False)  # 美区查得到、但确实没有页面 → 不在「海外上线」范围内
        with mock.patch.object(discover, "match_appid", return_value=_matched()), \
                mock.patch.object(discover, "check_app", return_value=us):
            self.assertIsNone(discover.evaluate(None, NP_PA, probe_news=False))

    def test_cn_launched_before_overseas_is_a_candidate(self):
        us = _side(True, False, "Dec 1, 2026")
        cn = _side(True, False, "Sep 1, 2026")
        with mock.patch.object(discover, "match_appid", return_value=_matched(cn)), \
                mock.patch.object(discover, "check_app", return_value=us), \
                mock.patch.object(discover, "fetch_latest_version", return_value=None), \
                mock.patch.object(discover, "detect_playtest", return_value=None):
            candidate = discover.evaluate(None, NP_PA, probe_news=False)

        self.assertEqual(candidate["gap_type"], "cn_first")
        self.assertEqual(candidate["appid"], 1001)

    def test_same_day_launch_is_not_leading(self):
        us = _side(True, False, "Sep 1, 2026")
        cn = _side(True, False, "Sep 1, 2026")
        with mock.patch.object(discover, "match_appid", return_value=_matched(cn)), \
                mock.patch.object(discover, "check_app", return_value=us):
            self.assertIsNone(discover.evaluate(None, NP_PA, probe_news=False))


class CollectGamesWindowTest(unittest.TestCase):
    """只取国产批次，窗口锁在 3 个月。"""

    def test_only_domestic_batches(self):
        calls = []

        def fake_batches(fetcher, url, keyword, limit):
            calls.append((keyword, limit))
            return [{"title": keyword, "url": url + "#batch"}]

        def fake_parse(fetcher, batch):
            return [{"name": "乙", "approved_at": "2026-09-29"}]

        with mock.patch.object(discover, "fetch_batches", side_effect=fake_batches), \
                mock.patch.object(discover, "parse_batch", side_effect=fake_parse):
            games = discover.collect_games(mock.Mock())

        self.assertEqual(calls, [("国产网络游戏审批信息", 3)])
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
                 "candidate": {"name_cn": "勇者传说", "timeline": {}}}

        self.assertFalse(discover._is_stale(entry, self.TODAY))


class RunGuardTest(unittest.TestCase):
    """run() 不能因为查询失败就丢掉已入库的候选。"""

    GAME = {"name": "勇者传说", "origin": "domestic", "approved_at": "2026-08-28"}
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
                mock.patch.object(discover, "evaluate",
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


if __name__ == "__main__":
    unittest.main()
