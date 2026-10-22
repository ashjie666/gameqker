"""collector/events.py 的单元测试：Steam 日期解析与多源事件合并。"""

import unittest

from collector import events

NOW = "2026-10-22T09:00:00Z"


def _game(**kwargs):
    base = {
        "id": "phantom-blade-zero",
        "name_cn": "影之刃零",
        "name_en": "Phantom Blade Zero",
        "steam_release_date": "Oct 28, 2026",
        "watch": {"steam_appid": 4115450},
    }
    base.update(kwargs)
    return base


class ParseSteamDateTest(unittest.TestCase):
    def test_month_first(self):
        self.assertEqual(events.parse_steam_date("Oct 28, 2026"), "2026-10-28")

    def test_day_first(self):
        self.assertEqual(events.parse_steam_date("28 Oct, 2026"), "2026-10-28")

    def test_vague_values_are_not_guessed(self):
        # 给不出具体某天的一律不入倒计时
        for raw in ("Q1 2027", "To be announced", "2026", "", None):
            self.assertIsNone(events.parse_steam_date(raw), raw)

    def test_non_string_value_is_not_a_crash(self):
        # 这个字段来自外部 JSON，可能是数字
        self.assertIsNone(events.parse_steam_date(2027))


class EventsFromPayloadTest(unittest.TestCase):
    def test_steam_release_date_becomes_launch_event(self):
        found = events.events_from_payload({"games": [_game()]})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["date"], "2026-10-28")
        self.assertEqual(found[0]["type"], "launch")
        self.assertEqual(found[0]["sources"][0]["type"], "steam")
        self.assertIn("4115450", found[0]["store_url"])

    def test_unparsable_date_is_dropped(self):
        found = events.events_from_payload(
            {"games": [_game(steam_release_date="Coming soon")]})
        self.assertEqual(found, [])

    def test_official_signal_only_used_when_asked(self):
        game = _game(steam_release_date=None, official_signal={
            "matched": True,
            "signals": ["release_date"],
            "site": "某游戏官网",
            "url": "https://example.com/news",
            "snippet": "本作将于2026年11月5日正式上线",
        })
        self.assertEqual(events.events_from_payload({"games": [game]}), [])

        found = events.events_from_payload({"games": [game]}, with_official=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["date"], "2026-11-05")
        self.assertEqual(found[0]["sources"][0]["type"], "official")

    def test_unrelated_official_signal_is_ignored(self):
        game = _game(steam_release_date=None, official_signal={
            "matched": True,
            "signals": ["multi_language"],   # 不是上线类信号
            "snippet": "本作将于2026年11月5日正式上线",
            "url": "https://example.com/",
        })
        self.assertEqual(
            events.events_from_payload({"games": [game]}, with_official=True), [])


class BuildPayloadTest(unittest.TestCase):
    def _news_event(self, game_id, iso_date, text, name="机核", url="https://www.gcores.com/a"):
        return {
            "game_id": game_id,
            "name_cn": "影之刃零",
            "name_en": "Phantom Blade Zero",
            "type": "launch",
            "date": iso_date,
            "date_text": text,
            "store_url": None,
            "sources": [{"type": "rss", "name": name, "url": url, "title": "稿件"}],
        }

    def test_steam_wins_conflict_and_other_date_goes_to_alt(self):
        payload = events.build_payload(
            {"games": [_game()]}, None, NOW,
            news_events=[self._news_event("phantom-blade-zero", "2026-10-29", "10月29日")])

        self.assertEqual(len(payload["events"]), 1)
        event = payload["events"][0]
        self.assertEqual(event["date"], "2026-10-28")          # Steam 商店优先
        self.assertEqual(event["alt_dates"][0]["date"], "2026-10-29")
        self.assertEqual([source["name"] for source in event["sources"]],
                         ["Steam 商店", "机核"])                # 来源一个都不丢
        self.assertEqual(event["origin"], "current")

    def test_news_only_game_is_kept(self):
        payload = events.build_payload(
            {"games": []}, None, NOW,
            news_events=[self._news_event("some-other-game", "2026-11-01", "11月1日")])

        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["origin"], "news")

    def test_same_date_from_two_sources_is_not_a_conflict(self):
        payload = events.build_payload(
            {"games": [_game()]}, None, NOW,
            news_events=[self._news_event("phantom-blade-zero", "2026-10-28", "10月28日")])

        event = payload["events"][0]
        self.assertEqual(event["alt_dates"], [])
        self.assertEqual(len(event["sources"]), 2)

    def test_events_are_sorted_by_date(self):
        payload = events.build_payload({"games": [
            _game(),
            _game(id="ananta", name_cn="无限大", name_en="ANANTA",
                  steam_release_date="Jan 15, 2027", watch={"steam_appid": 4814120}),
        ]}, None, NOW)

        self.assertEqual([event["date"] for event in payload["events"]],
                         ["2026-10-28", "2027-01-15"])


if __name__ == "__main__":
    unittest.main()
