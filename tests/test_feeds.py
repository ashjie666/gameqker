"""collector/feeds.py 的单元测试。

全部离线跑：用假 Fetcher 顶替真实抓取，不发任何网络请求。
"""

import unittest
from datetime import date
from unittest import mock

from collector import feeds

GAMES = [
    {"id": "phantom-blade-zero", "name_cn": "影之刃零",
     "name_en": "Phantom Blade Zero", "watch": {"steam_appid": 4115450}},
    {"id": "ananta", "name_cn": "无限大", "name_en": "ANANTA", "watch": {}},
]

TODAY = date(2026, 10, 22)


class FakeResponse:
    def __init__(self, body, error=None):
        self.body = body
        self.error = error


class FakeFetcher:
    """按 URL 返回预置内容；没预置的一律当作超时（body=None）。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, use_cache=None, timeout=None):
        self.calls.append((url, timeout))
        return self.responses.get(url, FakeResponse(None, error="timed out"))


def _rss(items):
    """把若干 <item> 拼成一个最小的 RSS 2.0 文档。"""
    body = "".join(
        "<item><title>{0}</title><link>{1}</link>"
        "<description>{2}</description><pubDate>{3}</pubDate></item>".format(
            item.get("title", ""), item.get("link", ""),
            item.get("summary", ""), item.get("published", ""))
        for item in items)
    return ("<?xml version=\"1.0\" encoding=\"utf-8\"?><rss version=\"2.0\">"
            "<channel>{0}</channel></rss>".format(body)).encode("utf-8")


class FindDateTest(unittest.TestCase):
    """带年份的完整日期解析。"""

    def test_chinese_date(self):
        found, raw = feeds.find_date("《影之刃零》定档 2026年10月29日上线")
        self.assertEqual(found, date(2026, 10, 29))
        self.assertEqual(raw, "2026年10月29日")

    def test_english_month_first(self):
        found, _ = feeds.find_date("Phantom Blade Zero launches Oct 28, 2026")
        self.assertEqual(found, date(2026, 10, 28))

    def test_english_day_first(self):
        found, _ = feeds.find_date("The game launches 28 Oct, 2026 worldwide")
        self.assertEqual(found, date(2026, 10, 28))

    def test_quarter_is_not_a_date(self):
        # "Q1 2027" 给不出具体某天，宁可不进倒计时也不能猜
        self.assertEqual(feeds.find_date("Q1 2027"), (None, None))

    def test_impossible_date_is_dropped(self):
        self.assertEqual(feeds.find_date("2026年2月30日上线"), (None, None))


class FindUndatedTest(unittest.TestCase):
    """没有年份的「X 月 X 日」。"""

    def test_picks_date_nearest_to_launch_keyword(self):
        # 同一句里有预下载和上线两个日期，必须挑离「上线」最近的那个
        guess = feeds.find_undated("游戏将于10月27日10:00开启预下载，并于10月29日10:00全平台全面解锁上线。")
        self.assertEqual(guess, (10, 29, "10月29日"))

    def test_requires_launch_keyword(self):
        # 没有上线相关措辞，光有日期不认
        self.assertIsNone(feeds.find_undated("补货时间 10月29日"))

    def test_english_month_day(self):
        guess = feeds.find_undated("Phantom Blade Zero launches on October 29")
        self.assertEqual(guess[0], 10)
        self.assertEqual(guess[1], 29)


class PublishedDateTest(unittest.TestCase):
    """RSS / Atom 的发文时间。"""

    def test_rfc822(self):
        self.assertEqual(
            feeds._published_date({"published": "Wed, 21 Oct 2026 10:00:00 +0800"}),
            date(2026, 10, 21))

    def test_atom_iso(self):
        self.assertEqual(
            feeds._published_date({"published": "2026-10-21T10:00:00Z"}),
            date(2026, 10, 21))

    def test_garbage(self):
        self.assertIsNone(feeds._published_date({"published": "昨天"}))
        self.assertIsNone(feeds._published_date({"published": ""}))
        self.assertIsNone(feeds._published_date({}))


class ResolveDateTest(unittest.TestCase):
    """正文优先，缺年份才按发文日期补。"""

    def test_explicit_date_wins(self):
        item = {"title": "《影之刃零》2026年10月28日发售", "summary": "",
                "published": "Wed, 21 Oct 2026 10:00:00 +0800"}
        self.assertEqual(feeds.resolve_date(item)[0], date(2026, 10, 28))

    def test_infer_year_from_published(self):
        item = {
            "title": "主线剧情首次亮相！《影之刃零》最终PV放出，27号开启预下载",
            "summary": "游戏将于10月27日10:00正式开启全平台预下载，"
                       "并于10月29日10:00全平台全面解锁上线。",
            "published": "Wed, 21 Oct 2026 10:00:00 +0800",
        }
        self.assertEqual(feeds.resolve_date(item), (date(2026, 10, 29), "10月29日"))

    def test_year_rolls_over_past_new_year(self):
        # 12 月 31 日的稿子说的是次年 1 月 5 日
        item = {"title": "1月5日正式上线", "summary": "",
                "published": "Thu, 31 Dec 2026 09:00:00 +0800"}
        self.assertEqual(feeds.resolve_date(item)[0], date(2027, 1, 5))

    def test_inferred_date_far_ahead_is_rejected(self):
        # 行业稿里顺口提到的日期，靠年份顺延会凑出近一年后的假档期，必须挡掉
        item = {"title": "大厂在做什么游戏？10月8日上线了一款新品", "summary": "",
                "published": "Wed, 21 Oct 2026 10:00:00 +0800"}
        self.assertEqual(feeds.resolve_date(item), (None, None))

    def test_no_published_date_means_no_guess(self):
        item = {"title": "10月29日上线", "summary": "", "published": ""}
        self.assertEqual(feeds.resolve_date(item), (None, None))


class MatchGameTest(unittest.TestCase):
    """游戏名匹配。"""

    def test_chinese_name(self):
        self.assertEqual(feeds._match_game("《影之刃零》最终PV", GAMES)["id"],
                         "phantom-blade-zero")

    def test_english_name_case_insensitive(self):
        self.assertEqual(feeds._match_game("phantom blade zero launch trailer", GAMES)["id"],
                         "phantom-blade-zero")

    def test_english_name_respects_word_boundary(self):
        # "Phantom Blade Zeroes" 不是这款游戏，不能靠子串蒙到
        self.assertIsNone(feeds._match_game("phantom blade zeroes announced", GAMES))

    def test_too_short_english_name_is_ignored(self):
        games = [{"id": "vox", "name_cn": "沃克斯", "name_en": "Vox"}]
        self.assertIsNone(feeds._match_game("the vox populi", games))


class ParseFeedTest(unittest.TestCase):
    """RSS 2.0 / Atom 解析。"""

    def test_rss2(self):
        body = _rss([{"title": "影之刃零 上线", "link": "https://e.com/a",
                      "summary": "desc", "published": "Wed, 21 Oct 2026 10:00:00 +0800"}])
        items = feeds._parse_feed(body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://e.com/a")
        self.assertIn("desc", items[0]["summary"])

    def test_atom(self):
        body = ("<feed xmlns=\"http://www.w3.org/2005/Atom\"><entry>"
                "<title>t</title><link href=\"https://e.com/b\"/>"
                "<summary>s</summary><published>2026-10-21T10:00:00Z</published>"
                "</entry></feed>").encode("utf-8")
        items = feeds._parse_feed(body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://e.com/b")

    def test_broken_xml(self):
        self.assertEqual(feeds._parse_feed(b"<rss><channel>"), [])


class InWindowTest(unittest.TestCase):
    """日期窗口边界：过去 7 天 ~ 未来 730 天。"""

    def test_boundaries(self):
        self.assertTrue(feeds._in_window(date(2026, 10, 15), TODAY))    # 刚好 7 天前
        self.assertFalse(feeds._in_window(date(2026, 10, 14), TODAY))   # 再早一天就不算
        self.assertTrue(feeds._in_window(TODAY, TODAY))
        self.assertTrue(feeds._in_window(date(2028, 10, 21), TODAY))    # 刚好 730 天后
        self.assertFalse(feeds._in_window(date(2028, 10, 22), TODAY))   # 多一天就超窗


class RssEventsTest(unittest.TestCase):
    """RSS 抓取主流程（用假 Fetcher）。"""

    def _patch_sources(self):
        sources = (
            {"id": "gcores", "name": "机核", "url": "https://www.gcores.com/rss"},
            {"id": "gematsu", "name": "Gematsu",
             "url": "https://www.gematsu.com/feed", "overseas": True},
        )
        patcher = mock.patch.object(feeds, "RSS_SOURCES", sources)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_overseas_timeout_is_skipped_silently(self):
        self._patch_sources()
        body = _rss([{
            "title": "《影之刃零》10月29日正式上线",
            "link": "https://www.gcores.com/articles/1",
            "summary": "", "published": "Wed, 21 Oct 2026 10:00:00 +0800"}])
        fetcher = FakeFetcher({"https://www.gcores.com/rss": FakeResponse(body)})

        events = feeds._rss_events(fetcher, None, GAMES, TODAY, set())

        self.assertEqual(len(events), 1)  # Gematsu 超时，没影响机核命中
        self.assertEqual(events[0]["date"], "2026-10-29")
        self.assertEqual(events[0]["sources"][0]["name"], "机核")
        # 海外源必须用短超时，国内源用默认超时
        timeouts = dict(fetcher.calls)
        self.assertEqual(timeouts["https://www.gematsu.com/feed"], feeds.OVERSEAS_TIMEOUT)
        self.assertIsNone(timeouts["https://www.gcores.com/rss"])

    def test_patch_notes_are_not_launch_events(self):
        self._patch_sources()
        body = _rss([{
            "title": "《影之刃零》版本更新公告：10月29日上线新内容",
            "link": "https://www.gcores.com/articles/2",
            "summary": "", "published": "Wed, 21 Oct 2026 10:00:00 +0800"}])
        fetcher = FakeFetcher({"https://www.gcores.com/rss": FakeResponse(body)})
        self.assertEqual(feeds._rss_events(fetcher, None, GAMES, TODAY, set()), [])

    def test_same_game_and_date_reported_once(self):
        sources = (
            {"id": "a", "name": "机核", "url": "https://a.test/rss"},
            {"id": "b", "name": "游研社", "url": "https://b.test/rss"},
        )
        patcher = mock.patch.object(feeds, "RSS_SOURCES", sources)
        patcher.start()
        self.addCleanup(patcher.stop)
        item = {"title": "《影之刃零》10月29日正式上线", "link": "https://a.test/1",
                "summary": "", "published": "Wed, 21 Oct 2026 10:00:00 +0800"}
        fetcher = FakeFetcher({
            "https://a.test/rss": FakeResponse(_rss([item])),
            "https://b.test/rss": FakeResponse(_rss([item])),
        })

        events = feeds._rss_events(fetcher, None, GAMES, TODAY, set())

        self.assertEqual(len(events), 1)


class SteamNewsEventsTest(unittest.TestCase):
    """Steam 公告 API 解析。"""

    def test_launch_announcement_becomes_event(self):
        import json

        appid = 4115450
        body = json.dumps({"appnews": {"newsitems": [
            {"title": "Phantom Blade Zero launches October 29, 2026",
             "contents": "", "url": "https://store.steampowered.com/news/1"},
            {"title": "Patch notes 1.0.2", "contents": "fixed crash",
             "url": "https://store.steampowered.com/news/2"},
        ]}}).encode("utf-8")
        fetcher = FakeFetcher({
            feeds.STEAM_NEWS_API.format(appid=appid): FakeResponse(body)})

        events = feeds._steam_news_events(fetcher, None, GAMES, TODAY, set())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2026-10-29")
        self.assertEqual(events[0]["sources"][0]["name"], "Steam 公告")

    def test_game_without_appid_is_not_queried(self):
        fetcher = FakeFetcher({})
        feeds._steam_news_events(fetcher, None,
                                 [{"id": "x", "name_cn": "甲", "watch": {}}], TODAY, set())
        self.assertEqual(fetcher.calls, [])


class CollectTest(unittest.TestCase):
    def test_empty_game_list(self):
        self.assertEqual(feeds.collect(FakeFetcher({}), {"games": []}), [])
        self.assertEqual(feeds.collect(FakeFetcher({}), None), [])


SEARCH_HTML = (
    '<div><!--s-data:{"title":"电子斗蛐蛐《<em>无用之人</em>:<em>登神长阶</em>》正式发售",'
    '"titleUrl":"https://www.ali213.net/news/1.html",'
    '"summary":"今天(11月20日)《<em>无用之人</em>:<em>登神长阶</em>》正式发售",'
    '"dispTime":"2025年11月20日","sourceName":"游侠网"}--></div>'
    '<div><!--s-data:{"title":"纯广告",'
    '"titleUrl":"https://example.com/ad",'
    '"summary":"加速器推荐","dispTime":"昨天12:00","sourceName":"某站"}--></div>'
)

ARTICLE_URL = "https://www.ali213.net/news/2.html"
ARTICLE_HTML = (
    "<html><head><script>var x = '2020年1月1日';</script></head>"
    "<body><h1>《影之刃零》定档公告</h1>"
    "<p>官方今日宣布，本作将于 2026年10月29日 正式上线。</p></body></html>"
)


class ParseSearchItemsTest(unittest.TestCase):
    """百度资讯的边说边搜（s-data）要能稳定解析出标题、链接、日期、来源。"""

    def test_parses_embedded_structured_data(self):
        items = feeds.parse_search_items(SEARCH_HTML.encode("utf-8"))

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "电子斗蛐蛐《无用之人:登神长阶》正式发售")
        self.assertEqual(items[0]["link"], "https://www.ali213.net/news/1.html")
        self.assertEqual(items[0]["published"], "2025-11-20")
        self.assertEqual(items[0]["source"], "游侠网")
        self.assertNotIn("<em>", items[0]["summary"])

    def test_garbage_html_yields_nothing(self):
        self.assertEqual(feeds.parse_search_items(b"<html>no data</html>"), [])
        self.assertEqual(feeds.parse_search_items(b"<!--s-data:{broken json}-->"), [])


class DispTimeTest(unittest.TestCase):
    """搜索结果的时间展示有绝对日期也有相对说法，都要折成 ISO。"""

    def test_absolute_date(self):
        self.assertEqual(feeds.disp_time_iso("2026年10月29日"), "2026-10-29")
        self.assertEqual(feeds.disp_time_iso("2026-10-29 10:23"), "2026-10-29")

    def test_relative_dates(self):
        today = date(2026, 10, 28)
        self.assertEqual(feeds.disp_time_iso("今天08:10", today), "2026-10-28")
        self.assertEqual(feeds.disp_time_iso("昨天11:34", today), "2026-10-27")
        self.assertEqual(feeds.disp_time_iso("前天15:59", today), "2026-10-26")
        self.assertEqual(feeds.disp_time_iso("3天前", today), "2026-10-25")

    def test_unknown_format_is_empty(self):
        self.assertEqual(feeds.disp_time_iso("上周"), "")
        self.assertEqual(feeds.disp_time_iso(None), "")


class ArticleTextTest(unittest.TestCase):
    """摘要没日期时要靠原文正文，正文抓取必须去掉脚本和标签。"""

    def test_strips_scripts_and_tags(self):
        fetcher = FakeFetcher({ARTICLE_URL: FakeResponse(ARTICLE_HTML.encode("utf-8"))})

        text = feeds.article_text(fetcher, ARTICLE_URL)

        self.assertIn("2026年10月29日", text)
        self.assertNotIn("2020年1月1日", text)  # script 里的日期不能当新闻内容
        self.assertNotIn("<p>", text)

    def test_blocked_hosts_are_not_fetched(self):
        fetcher = FakeFetcher({})

        self.assertIsNone(feeds.article_text(fetcher, "https://baike.baidu.com/item/x"))
        self.assertEqual(fetcher.calls, [])

    def test_fetch_failure_returns_none(self):
        self.assertIsNone(feeds.article_text(FakeFetcher({}), ARTICLE_URL))


class MatchGameSquashTest(unittest.TestCase):
    """媒体写法带冒号书名号，版号名不带，必须能对上。"""

    def test_colon_in_media_title_matches_registry_name(self):
        game = {"id": "wu", "name_cn": "无用之人登神长阶"}

        self.assertIs(feeds._match_game("《无用之人:登神长阶》正式发售", [game]), game)

    def test_unrelated_text_does_not_match(self):
        game = {"id": "wu", "name_cn": "无用之人登神长阶"}

        self.assertIsNone(feeds._match_game("汉字解释：无的意思", [game]))


class SearchEventsTest(unittest.TestCase):
    """搜索路径：摘要能给出日期就直接用，给不出就抓正文补。"""

    GAME = {"id": "wu", "name_cn": "无用之人登神长阶", "watch": {}}

    def _fetcher(self, search_html=SEARCH_HTML, article=None):
        responses = {feeds.SEARCH_URL.format(term=feeds.quote(self.GAME["name_cn"].encode("utf-8"))):
                     FakeResponse(search_html.encode("utf-8"))}
        if article:
            responses[ARTICLE_URL] = FakeResponse(article.encode("utf-8"))
        return FakeFetcher(responses)

    def test_date_from_snippet(self):
        # 摘要里有完整年月日，不必抓正文
        html = ('<div><!--s-data:{"title":"《无用之人登神长阶》正式发售",'
                '"titleUrl":"https://example.com/a",'
                '"summary":"将于 2026年10月29日 上线","dispTime":"2026年10月27日",'
                '"sourceName":"游侠网"}--></div>')
        events = feeds._search_events(self._fetcher(html), [self.GAME], TODAY, set())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2026-10-29")
        self.assertEqual(events[0]["sources"][0]["type"], "search")
        self.assertEqual(events[0]["sources"][0]["market"], "cn")

    def test_date_from_article_body_when_snippet_has_none(self):
        html = ('<div><!--s-data:{"title":"《无用之人登神长阶》定档",'
                '"titleUrl":"' + ARTICLE_URL + '",'
                '"summary":"官方公布上线时间","dispTime":"2026年10月27日",'
                '"sourceName":"游侠网"}--></div>')
        events = feeds._search_events(
            self._fetcher(html, ARTICLE_HTML), [self.GAME], TODAY, set())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2026-10-29")
        self.assertEqual(events[0]["sources"][0]["url"], ARTICLE_URL)

    def test_stale_news_is_dropped(self):
        # 2025 年的发售稿不再进倒计时
        events = feeds._search_events(self._fetcher(), [self.GAME], TODAY, set())

        self.assertEqual(events, [])


class ProbeReleaseDatesTest(unittest.TestCase):
    """一次抓取要把国服 / 国际服的消息分开，不能混算。"""

    def test_events_are_split_by_market(self):
        cn_event = {"date": "2026-11-01", "date_text": "11月1日",
                    "sources": [{"name": "游侠网", "url": "u1", "market": "cn"}]}
        intl_event = {"date": "2026-12-01", "date_text": "Dec 1, 2026",
                      "sources": [{"name": "Steam 公告", "url": "u2", "market": "intl"}]}
        with mock.patch.object(feeds, "_steam_news_events", return_value=[intl_event]), \
                mock.patch.object(feeds, "_rss_events", return_value=[]), \
                mock.patch.object(feeds, "_search_events", return_value=[cn_event]):
            found = feeds.probe_release_dates(FakeFetcher({}), {"name_cn": "甲"}, TODAY)

        self.assertEqual(found["cn"]["date"], "2026-11-01")
        self.assertEqual(found["intl"]["date"], "2026-12-01")

    def test_market_filter_on_probe_release_date(self):
        intl_event = {"date": "2026-12-01", "date_text": "Dec 1, 2026",
                      "sources": [{"name": "Steam 公告", "url": "u2", "market": "intl"}]}
        with mock.patch.object(feeds, "_steam_news_events", return_value=[intl_event]), \
                mock.patch.object(feeds, "_rss_events", return_value=[]), \
                mock.patch.object(feeds, "_search_events", return_value=[]):
            self.assertIsNone(feeds.probe_release_date(
                FakeFetcher({}), {"name_cn": "甲"}, TODAY, market="cn"))
            self.assertEqual(feeds.probe_release_date(
                FakeFetcher({}), {"name_cn": "甲"}, TODAY, market="intl")["date"],
                "2026-12-01")


if __name__ == "__main__":
    unittest.main()
