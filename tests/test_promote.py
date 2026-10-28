"""collector/update.py 候选自动并入的单元测试。

纯函数测试：不发网络请求，不读写 data/ 下的真实文件。
"""

import unittest

from collector import update

CANDIDATE = {
    "origin": "domestic",
    "name_cn": "勇者传说",
    "name_en": "Tensei Brave",
    "appid": 2445430,
    "gap_type": "cn_first",
    "gap_days": 56,
    "note": "国服版号早于 Steam 美区定档",
    "stage": {
        "cn": "preorder", "os": "preorder",
        "cn_release": "2026-10-23", "os_release": "2026-10-23",
    },
    "version": {"steam": None, "source": None},
    "playtest_os": False,
    "nppa": {
        "approved_at": "2026-08-28",
        "publisher": "重庆天健电子音像出版有限公司",
        "operator": "重庆贰伍八八网络科技有限公司",
        "approval_no": "国新出审〔2026〕2091号",
        "batch": "2026年8月份国产网络游戏审批信息",
        "batch_url": "https://www.nppa.gov.cn/example/t20260831_1005702.html",
    },
    "steam": {
        "store_url": "https://store.steampowered.com/app/2445430/",
        "us_listed": True,
        "us_release_date": "Oct 23, 2026",
        "us_release_parsed": "2026-10-23",
        "us_coming_soon": True,
        "cn_listed": True,
        "cn_release_date": "2026 年 10 月 23 日",
        "cn_release_parsed": "2026-10-23",
        "match_score": 0.8,
    },
    "evidence": [
        "https://www.nppa.gov.cn/example/t20260831_1005702.html",
        "https://store.steampowered.com/app/2445430/",
    ],
    "first_seen": "2026-10-22T07:09:06Z",
    "last_checked": "2026-10-22",
}


def _payload(games=None):
    return {"generated_at": "2026-10-22T09:00:00Z", "games": list(games or [])}


class SlugTest(unittest.TestCase):
    def test_english_name_becomes_slug(self):
        self.assertEqual(update._candidate_slug(CANDIDATE), "tensei-brave")

    def test_punctuation_and_case_normalised(self):
        slug = update._candidate_slug({"name_en": "After Inc: Revival!", "appid": 1})
        self.assertEqual(slug, "after-inc-revival")

    def test_falls_back_to_appid_without_english_name(self):
        self.assertEqual(update._candidate_slug({"name_cn": "甲", "appid": 777}),
                         "steam-777")

    def test_falls_back_to_hash_without_name_or_appid(self):
        slug = update._candidate_slug({"name_cn": "无用之人：登神长阶"})
        self.assertTrue(slug.startswith("cand-"))
        self.assertEqual(len(slug), len("cand-") + 8)

    def test_hash_is_stable_across_calls(self):
        data = {"name_cn": "无用之人：登神长阶"}
        self.assertEqual(update._candidate_slug(data), update._candidate_slug(data))

    def test_different_game_names_get_different_hashes(self):
        self.assertNotEqual(update._candidate_slug({"name_cn": "甲"}),
                            update._candidate_slug({"name_cn": "乙"}))


class GameFromCandidateTest(unittest.TestCase):
    def test_facts_are_filled_from_nppa_and_steam(self):
        game = update.game_from_candidate(CANDIDATE, "2026-10-22")

        self.assertEqual(game["id"], "tensei-brave")
        self.assertEqual(game["name_cn"], "勇者传说")
        self.assertEqual(game["name_en"], "Tensei Brave")
        # 厂商取版号公示里的运营单位，不是猜的
        self.assertEqual(game["developer"], "重庆贰伍八八网络科技有限公司")
        # 阶段文案直接由 stage 折出来
        self.assertIn("预约中", game["cn_status"])
        self.assertIn("2026-10-23", game["cn_status"])
        self.assertIn("预约中", game["global_status"])
        # Steam 上架状态与美区日期沿用候选里的实测值
        self.assertTrue(game["steam_listed"])
        self.assertEqual(game["steam_release_date"], "Oct 23, 2026")
        self.assertEqual(game["watch"], {"steam_appid": 2445430})
        self.assertEqual(game["gap_window"], "国服版号早于 Steam 美区定档")

    def test_qualitative_fields_are_placeholders_not_invented(self):
        game = update.game_from_candidate(CANDIDATE, "2026-10-22")

        for field in ("genre", "monetization", "monetization_level", "overseas_heat"):
            self.assertEqual(game[field], update.PLACEHOLDER, field)
        for field in ("target_markets", "keywords", "content_opportunities", "risks"):
            self.assertEqual(game[field], [], field)

    def test_auto_import_is_marked(self):
        game = update.game_from_candidate(CANDIDATE, "2026-10-22")

        self.assertTrue(game["auto_imported"])
        self.assertEqual(game["auto_imported_at"], "2026-10-22")
        # 新并入的条目本轮还没巡检过，留空让下一轮填
        self.assertIsNone(game["last_checked"])
        self.assertEqual(game["changes"], [])

    def test_sources_keep_both_provenance_links(self):
        game = update.game_from_candidate(CANDIDATE, "2026-10-22")
        urls = [item["url"] for item in game["sources"]]

        self.assertIn("https://www.nppa.gov.cn/example/t20260831_1005702.html", urls)
        self.assertIn("https://store.steampowered.com/app/2445430/", urls)

    def test_missing_sections_do_not_crash(self):
        game = update.game_from_candidate({"name_cn": "甲", "name_en": "Jia"}, "2026-10-22")

        self.assertEqual(game["watch"], {})
        self.assertEqual(game["sources"], [])
        self.assertEqual(game["developer"], update.PLACEHOLDER)
        self.assertIsNone(game["steam_listed"])
        self.assertIsNone(game["steam_release_date"])

    def test_unlisted_store_does_not_invent_release_date(self):
        game = update.game_from_candidate(
            {"name_cn": "甲", "name_en": "Jia", "steam": {"us_listed": False}},
            "2026-10-22")

        self.assertFalse(game["steam_listed"])
        self.assertIsNone(game["steam_release_date"])

    def test_lowest_priority_by_default(self):
        game = update.game_from_candidate(CANDIDATE, "2026-10-22")
        self.assertEqual(game["priority"], update.AUTO_PRIORITY)
        self.assertEqual(game["priority"], 3)

    def test_chinese_name_en_is_blanked(self):
        # 候选的 name_en 有时是原样照抄的中文名，填进「英文名」栏没意义
        game = update.game_from_candidate(
            {"name_cn": "无用之人：登神长阶", "name_en": "无用之人：登神长阶"}, "2026-10-22")

        self.assertEqual(game["name_en"], "")
        self.assertEqual(game["name_cn"], "无用之人：登神长阶")

    def test_mixed_name_en_is_kept(self):
        game = update.game_from_candidate(
            {"name_cn": "影之刃零", "name_en": "Phantom Blade 影之刃零"}, "2026-10-22")
        self.assertEqual(game["name_en"], "Phantom Blade 影之刃零")


class PromoteCandidatesTest(unittest.TestCase):
    def test_new_candidate_is_appended(self):
        payload = _payload()
        added = update.promote_candidates(payload, [CANDIDATE], "2026-10-22")

        self.assertEqual(added, 1)
        self.assertEqual(len(payload["games"]), 1)
        self.assertEqual(payload["games"][0]["id"], "tensei-brave")

    def test_running_twice_adds_nothing(self):
        payload = _payload()
        update.promote_candidates(payload, [CANDIDATE], "2026-10-22")
        added = update.promote_candidates(payload, [CANDIDATE], "2026-10-23")

        self.assertEqual(added, 0)
        self.assertEqual(len(payload["games"]), 1)
        # 首次并入的时间不应该被后续跑动改写
        self.assertEqual(payload["games"][0]["auto_imported_at"], "2026-10-22")

    def test_existing_manual_game_is_not_duplicated(self):
        existing = {"id": "tensei-brave", "name_cn": "勇者传说", "priority": 1}
        payload = _payload([existing])
        added = update.promote_candidates(payload, [CANDIDATE], "2026-10-22")

        self.assertEqual(added, 0)
        self.assertEqual(len(payload["games"]), 1)
        # 人工录入的优先级不能被自动条目覆盖
        self.assertEqual(payload["games"][0]["priority"], 1)

    def test_only_new_ones_are_added_from_a_batch(self):
        other = dict(CANDIDATE, id="ignored", name_cn="劫后公司",
                     name_en="After Inc Revival", appid=2101111)
        payload = _payload([{"id": "tensei-brave", "name_cn": "勇者传说"}])
        added = update.promote_candidates(payload, [CANDIDATE, other], "2026-10-22")

        self.assertEqual(added, 1)
        self.assertEqual(payload["games"][-1]["id"], "after-inc-revival")

    def test_duplicate_ids_inside_one_batch_are_collapsed(self):
        payload = _payload()
        added = update.promote_candidates(payload, [CANDIDATE, dict(CANDIDATE)],
                                          "2026-10-22")

        self.assertEqual(added, 1)
        self.assertEqual(len(payload["games"]), 1)

    def test_candidate_without_any_name_is_skipped(self):
        payload = _payload()
        added = update.promote_candidates(payload, [{"appid": 1}], "2026-10-22")

        self.assertEqual(added, 0)
        self.assertEqual(payload["games"], [])

    def test_empty_or_missing_candidates(self):
        payload = _payload()
        self.assertEqual(update.promote_candidates(payload, [], "2026-10-22"), 0)
        self.assertEqual(update.promote_candidates(payload, None, "2026-10-22"), 0)
        self.assertEqual(payload["games"], [])

    def test_missing_games_list_is_created(self):
        payload = {"generated_at": "2026-10-22T09:00:00Z"}
        added = update.promote_candidates(payload, [CANDIDATE], "2026-10-22")

        self.assertEqual(added, 1)
        self.assertEqual(len(payload["games"]), 1)

    def test_games_not_a_list_is_replaced(self):
        payload = {"games": None}
        added = update.promote_candidates(payload, [CANDIDATE], "2026-10-22")

        self.assertEqual(added, 1)
        self.assertIsInstance(payload["games"], list)

    def test_imported_at_defaults_to_today(self):
        payload = _payload()
        update.promote_candidates(payload, [CANDIDATE])

        self.assertEqual(payload["games"][0]["auto_imported_at"], update.today_str())

    def test_low_match_score_is_left_in_candidates(self):
        # 版号名和 Steam 页面对不上时，宁可留在候选区等人工，也不写进主表
        weak = dict(CANDIDATE, name_en="Wrong Game", steam=dict(CANDIDATE["steam"], match_score=0.3))
        payload = _payload()
        added = update.promote_candidates(payload, [weak], "2026-10-22")

        self.assertEqual(added, 0)
        self.assertEqual(payload["games"], [])

    def test_score_at_threshold_is_accepted(self):
        borderline = dict(CANDIDATE, steam=dict(CANDIDATE["steam"], match_score=update.MIN_MATCH_SCORE))
        payload = _payload()
        self.assertEqual(update.promote_candidates(payload, [borderline], "2026-10-22"), 1)

    def test_missing_score_does_not_block(self):
        # 没有 match_score 时按可并入处理，避免字段缺失导致候选永远进不了主表
        no_score = dict(CANDIDATE, steam={"us_listed": True})
        payload = _payload()
        self.assertEqual(update.promote_candidates(payload, [no_score], "2026-10-22"), 1)


if __name__ == "__main__":
    unittest.main()
