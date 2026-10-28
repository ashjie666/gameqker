"""双市场上市时间轴的单元测试。

全部离线：Steam 数据直接构造，新闻探测用 mock 顶替。
"""

import unittest
from datetime import date
from unittest import mock

from collector import discover, update

NP_PA = {
    "name": "勇者传说",
    "origin": "domestic",
    "approved_at": "2026-08-28",
    "batch_url": "https://www.nppa.gov.cn/example/t20260831.html",
}


def _side(listed=None, coming_soon=None, release_date=None):
    return {"listed": listed, "coming_soon": coming_soon, "release_date": release_date}


class BuildTimelineTest(unittest.TestCase):
    """每个时间点都要说清「什么阶段 + 什么日期 + 来自哪个源」。"""

    def test_overseas_without_page_has_no_date(self):
        timeline = discover.build_timeline(
            NP_PA, _side(), _side(), appid=1)

        self.assertEqual(timeline["intl"]["basis"], "unknown")
        self.assertIsNone(timeline["intl"]["date"])

    def test_approval_date_is_marked_as_lower_bound(self):
        timeline = discover.build_timeline(NP_PA, _side(), _side(), appid=1)

        cn = timeline["cn"]
        self.assertEqual(cn["basis"], "approved")
        self.assertEqual(cn["date"], "2026-08-28")
        # 版号日只是「够资格上线」的下界，不能当上线日
        self.assertTrue(cn["estimated"])
        self.assertEqual(cn["source"], "版号公示")

    def test_preorder_date_is_taken_from_store(self):
        timeline = discover.build_timeline(
            NP_PA, _side(True, True, "Oct 23, 2026"), _side(False), appid=1)

        self.assertEqual(timeline["intl"]["basis"], "preorder")
        self.assertEqual(timeline["intl"]["date"], "2026-10-23")
        self.assertFalse(timeline["intl"]["estimated"])

    def test_released_side_is_not_estimated(self):
        timeline = discover.build_timeline(
            NP_PA, _side(True, False, "Aug 19, 2026"), _side(True, False, "Aug 19, 2026"),
            appid=1)

        self.assertEqual(timeline["intl"]["basis"], "released")
        self.assertEqual(timeline["cn"]["basis"], "released")
        self.assertFalse(timeline["cn"]["estimated"])

    def test_domestic_store_date_wins_over_approval_date(self):
        timeline = discover.build_timeline(
            NP_PA, _side(True, True, "Dec 1, 2026"), _side(True, True, "Nov 1, 2026"),
            appid=1)

        self.assertEqual(timeline["cn"]["basis"], "preorder")
        self.assertEqual(timeline["cn"]["date"], "2026-11-01")
        # 有 Steam 国区页面时不再退回版号日
        self.assertFalse(timeline["cn"]["estimated"])

    def test_preorder_without_date_keeps_stage_but_no_date(self):
        timeline = discover.build_timeline(
            NP_PA, _side(True, True, None), _side(), appid=1)

        self.assertEqual(timeline["intl"]["basis"], "preorder")
        self.assertIsNone(timeline["intl"]["date"])

    def test_no_page_and_no_approval_is_unknown(self):
        timeline = discover.build_timeline(
            {"name": "甲"}, _side(), _side(), appid=1)

        self.assertEqual(timeline["cn"]["basis"], "unknown")
        self.assertIsNone(timeline["cn"]["date"])

    def test_source_links_point_at_their_provenance(self):
        timeline = discover.build_timeline(NP_PA, _side(), _side(), appid=777)

        self.assertEqual(timeline["cn"]["url"], NP_PA["batch_url"])

    def test_intl_source_url_uses_store_link(self):
        timeline = discover.build_timeline(
            NP_PA, _side(True, True, "Oct 23, 2026"), _side(), appid=777)

        self.assertIn("/app/777/", timeline["intl"]["url"])
        self.assertEqual(timeline["intl"]["source"], "Steam 商店（国际服）")

    def test_missing_appid_leaves_url_empty(self):
        timeline = discover.build_timeline(
            NP_PA, _side(True, True, "Oct 23, 2026"), _side(), appid=None)

        self.assertIsNone(timeline["intl"]["url"])


class EnrichIntlFromNewsTest(unittest.TestCase):
    """Steam 给不出国际服时间时，才去公告 / 国内媒体补一次。"""

    def _timeline(self, intl_date=None, cn_basis="approved", cn_date="2026-08-28"):
        return {
            "cn": {"date": cn_date, "basis": cn_basis, "estimated": True,
                   "source": "版号公示", "url": None},
            "intl": {"date": intl_date, "basis": "preorder" if intl_date else "unknown",
                     "estimated": False, "source": None, "url": None},
        }

    def test_skips_probe_when_steam_already_has_a_date(self):
        timeline = self._timeline(intl_date="2026-10-23")
        with mock.patch.object(discover.feeds, "probe_release_date") as probe:
            result = discover.enrich_intl_from_news(mock.Mock(), {}, timeline)

        probe.assert_not_called()
        self.assertEqual(result["intl"]["date"], "2026-10-23")

    def test_skips_probe_when_domestic_side_unknown(self):
        timeline = self._timeline(cn_basis="unknown", cn_date=None)
        with mock.patch.object(discover.feeds, "probe_release_date") as probe:
            discover.enrich_intl_from_news(mock.Mock(), {}, timeline)

        probe.assert_not_called()

    def test_no_hit_leaves_timeline_untouched(self):
        timeline = self._timeline()
        with mock.patch.object(discover.feeds, "probe_release_date", return_value=None):
            result = discover.enrich_intl_from_news(mock.Mock(), {}, timeline)

        self.assertEqual(result["intl"]["basis"], "unknown")
        self.assertIsNone(result["intl"]["date"])

    def test_later_announced_date_fills_intl_side(self):
        timeline = self._timeline()
        found = {"date": "2026-12-01", "source": "机核", "url": "https://gcores.com/x"}
        with mock.patch.object(discover.feeds, "probe_release_date", return_value=found):
            result = discover.enrich_intl_from_news(mock.Mock(), {}, timeline)

        self.assertEqual(result["intl"]["date"], "2026-12-01")
        self.assertEqual(result["intl"]["basis"], "announced")
        self.assertEqual(result["intl"]["source"], "机核")
        self.assertFalse(result["intl"]["estimated"])

    def test_earlier_announced_date_disqualifies_candidate(self):
        # 公告显示国际服比国服还早，就不是「国服领先」，返回 None 让上层淘汰
        timeline = self._timeline()
        found = {"date": "2026-07-01", "source": "机核", "url": None}
        with mock.patch.object(discover.feeds, "probe_release_date", return_value=found):
            result = discover.enrich_intl_from_news(mock.Mock(), {}, timeline)

        self.assertIsNone(result)

    def test_same_day_announcement_is_kept(self):
        timeline = self._timeline()
        found = {"date": "2026-08-28", "source": "机核", "url": None}
        with mock.patch.object(discover.feeds, "probe_release_date", return_value=found):
            result = discover.enrich_intl_from_news(mock.Mock(), {}, timeline)

        self.assertEqual(result["intl"]["date"], "2026-08-28")

    def test_missing_source_falls_back_to_generic_label(self):
        timeline = self._timeline()
        found = {"date": "2026-12-01", "source": None, "url": None}
        with mock.patch.object(discover.feeds, "probe_release_date", return_value=found):
            result = discover.enrich_intl_from_news(mock.Mock(), {}, timeline)

        self.assertEqual(result["intl"]["source"], "新闻公告")


class TimelineTextTest(unittest.TestCase):
    """写进主表的文案要能区分「已上线」「版号获批下界」「无公开时间」。"""

    def test_released_point(self):
        text = update.timeline_text(
            {"date": "2026-10-23", "basis": "released", "estimated": False})
        self.assertEqual(text, "已上线 · 2026-10-23")

    def test_approval_point_is_labelled_as_lower_bound(self):
        text = update.timeline_text(
            {"date": "2026-08-28", "basis": "approved", "estimated": True})
        self.assertEqual(text, "版号获批 · 2026-08-28（下界）")

    def test_point_without_date_omits_separator(self):
        text = update.timeline_text({"date": None, "basis": "unknown"})
        self.assertEqual(text, "无公开时间")

    def test_preorder_without_date(self):
        self.assertEqual(
            update.timeline_text({"date": None, "basis": "preorder"}), "预约中")

    def test_empty_and_invalid_inputs(self):
        self.assertIsNone(update.timeline_text(None))
        self.assertIsNone(update.timeline_text({}))
        self.assertIsNone(update.timeline_text("junk"))

    def test_unknown_basis_label(self):
        self.assertEqual(
            update.timeline_text({"date": None, "basis": "weird"}), "未知")


class CandidateTimelineWiringTest(unittest.TestCase):
    """候选的时间轴要一路带到主表记录里。"""

    def test_game_record_carries_timeline_text(self):
        candidate = {
            "name_cn": "勇者传说", "name_en": "Tensei Brave",
            "timeline": {
                "cn": {"date": "2026-08-28", "basis": "approved", "estimated": True},
                "intl": {"date": None, "basis": "unknown", "estimated": False},
            },
        }
        game = update.game_from_candidate(candidate, "2026-10-22")

        self.assertEqual(game["cn_status"], "版号获批 · 2026-08-28（下界）")
        self.assertEqual(game["global_status"], "无公开时间")
        self.assertEqual(game["timeline"], candidate["timeline"])

    def test_falls_back_to_stage_when_timeline_missing(self):
        # 旧候选没有 timeline 字段时，仍要能折出阶段文案
        candidate = {
            "name_cn": "勇者传说", "name_en": "Tensei Brave",
            "stage": {"cn": "preorder", "os": "preorder",
                      "cn_release": "2026-10-23", "os_release": "2026-10-23"},
        }
        game = update.game_from_candidate(candidate, "2026-10-22")

        self.assertEqual(game["cn_status"], "预约中 · 2026-10-23")
        self.assertEqual(game["global_status"], "预约中 · 2026-10-23")


if __name__ == "__main__":
    unittest.main()
