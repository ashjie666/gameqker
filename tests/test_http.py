"""collector/http.py 的单元测试：curl 白名单路由与单请求超时覆盖。

不发起真实网络请求：curl 子进程与 urllib 都被替换成假实现。
"""

import tempfile
import unittest
from unittest import mock

from collector import feeds, http


class FakeRawResponse:
    """冒充 urllib 返回的响应对象（上下文管理器）。"""

    status = 200
    headers = {}

    def read(self):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class CurlHostsTest(unittest.TestCase):
    def test_dead_overseas_hosts_are_not_whitelisted(self):
        # 这两个域名长期超时 / 403，留在白名单里只会白等
        self.assertNotIn("news.google.com", http.DEFAULT_CURL_HOSTS)
        self.assertNotIn("news.xbox.com", http.DEFAULT_CURL_HOSTS)

    def test_core_steam_hosts_are_whitelisted(self):
        self.assertIn("store.steampowered.com", http.DEFAULT_CURL_HOSTS)
        self.assertIn("api.steampowered.com", http.DEFAULT_CURL_HOSTS)

    def test_domestic_host_uses_urllib(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            fetcher = http.Fetcher(cache_dir=cache_dir)
            self.assertFalse(fetcher._use_curl("https://www.gcores.com/rss"))

    def test_whitelisted_host_uses_curl(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            fetcher = http.Fetcher(cache_dir=cache_dir)
            self.assertTrue(fetcher._use_curl("https://store.steampowered.com/api/appdetails"))

    def test_explicit_proxy_forces_curl(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            fetcher = http.Fetcher(cache_dir=cache_dir, proxy="socks5h://127.0.0.1:1080")
            self.assertTrue(fetcher._use_curl("https://www.gcores.com/rss"))


class TimeoutOverrideTest(unittest.TestCase):
    """海外源要用短超时，所以 get() 必须能按请求覆盖实例超时。"""

    def setUp(self):
        self._cache = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache.cleanup)
        self.fetcher = http.Fetcher(
            cache_dir=self._cache.name, timeout=20, host_interval=0, retries=0)

    def _run_curl(self, **kwargs):
        completed = mock.Mock(returncode=0, stdout=b"200", stderr=b"")
        with mock.patch("collector.http.shutil.which", return_value="curl.exe"), \
                mock.patch("collector.http.subprocess.run", return_value=completed) as runner:
            self.fetcher.get("https://api.steampowered.com/x", **kwargs)
        args = runner.call_args[0][0]
        return args[args.index("--max-time") + 1]

    def test_override_reaches_curl(self):
        self.assertEqual(self._run_curl(timeout=feeds.OVERSEAS_TIMEOUT), "8")

    def test_default_is_instance_timeout(self):
        self.assertEqual(self._run_curl(), "20")

    def test_override_reaches_urllib(self):
        with mock.patch("collector.http.urllib.request.urlopen",
                        return_value=FakeRawResponse()) as opener:
            self.fetcher.get("https://www.gcores.com/rss", timeout=3)
        self.assertEqual(opener.call_args.kwargs["timeout"], 3)


class FetchResultTest(unittest.TestCase):
    def test_ok_requires_body_and_no_error(self):
        self.assertTrue(http.FetchResult("u", status=200, body=b"").ok)
        self.assertFalse(http.FetchResult("u", error="timeout").ok)
        self.assertFalse(http.FetchResult("u", body=None).ok)


if __name__ == "__main__":
    unittest.main()
