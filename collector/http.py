"""Polite HTTP client: conditional requests, on-disk cache, per-host throttle.

Standard library only, so the MVP runs with zero `pip install`.

海外域名在 Windows 上经常走系统代理（全局加速 / 校园网出口），Python urllib
不继承系统代理会直接握手超时；curl.exe 继承 WinHTTP 能通。所以 Fetcher 提供
``curl_hosts`` 白名单：命中时自动改用 curl 子进程，不再依赖 urllib 的网络栈。
"""

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

# Identify the aggregator honestly while still looking like a normal browser,
# because a lot of publisher CDNs reject obviously-bot user agents outright.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "GameqkerIntel/1.0 (+personal market intel sheet)"
)

# 这些主机在国内网络下通常需要走系统代理才能访问；
# 命中时 Fetcher 自动改用 curl（继承 WinHTTP / 全局加速）。
DEFAULT_CURL_HOSTS = {
    "api.steampowered.com", "store.steampowered.com", "steamcommunity.com",
    "www.gematsu.com", "gematsu.com",
    "blog.playstation.com",
    "www.ign.com",
}


class FetchResult:
    __slots__ = ("url", "status", "body", "error", "from_cache", "not_modified")

    def __init__(self, url, status=None, body=None, error=None, from_cache=False, not_modified=False):
        self.url = url
        self.status = status
        self.body = body
        self.error = error
        self.from_cache = from_cache
        self.not_modified = not_modified

    @property
    def ok(self):
        return self.error is None and self.body is not None


class Fetcher:
    """GET with ETag/Last-Modified revalidation and a simple robots-friendly pace."""

    def __init__(self, cache_dir, timeout=25, host_interval=1.0, retries=2, verbose=False,
                 use_cache=True, proxy=None, curl_hosts=None):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.host_interval = host_interval
        self.retries = retries
        self.verbose = verbose
        self.use_cache = use_cache
        # 显式代理（如 socks5h://127.0.0.1:1080）；为空则 curl 继承系统代理
        self.proxy = proxy
        # 命中这些主机时改用 curl 子进程
        self.curl_hosts = set(curl_hosts) if curl_hosts is not None else set(DEFAULT_CURL_HOSTS)
        self._last_hit = {}
        os.makedirs(cache_dir, exist_ok=True)

    # -- cache plumbing ----------------------------------------------------
    def _paths(self, url):
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, digest + ".body"), os.path.join(self.cache_dir, digest + ".json")

    def _read_meta(self, meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _write_cache(self, body_path, meta_path, body, headers):
        try:
            with open(body_path, "wb") as handle:
                handle.write(body)
            with open(meta_path, "w", encoding="utf-8") as handle:
                json.dump({"etag": headers.get("etag"), "last_modified": headers.get("last-modified")}, handle)
        except OSError:
            pass

    # -- pacing ------------------------------------------------------------
    def _wait_for_host(self, url):
        host = urllib.parse.urlsplit(url).netloc
        previous = self._last_hit.get(host)
        if previous is not None:
            gap = self.host_interval - (time.monotonic() - previous)
            if gap > 0:
                time.sleep(gap)
        self._last_hit[host] = time.monotonic()

    def _use_curl(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        if not host:
            return False
        if self.proxy:
            return True  # 显式代理一定走 curl（urllib 不支持 SOCKS）
        return host in self.curl_hosts

    # -- main entry --------------------------------------------------------
    def get(self, url, use_cache=None, timeout=None):
        use_cache = self.use_cache if use_cache is None else use_cache
        timeout = self.timeout if timeout is None else timeout
        if self._use_curl(url):
            return self._get_via_curl(url, use_cache, timeout)

        body_path, meta_path = self._paths(url)
        meta = self._read_meta(meta_path) if use_cache else {}
        had_cache = use_cache and os.path.exists(body_path)

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "gzip, deflate",
        }
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

        last_error = None
        for attempt in range(self.retries + 1):
            self._wait_for_host(url)
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = self._decode(response.read(), response.headers.get("Content-Encoding"))
                    if use_cache:
                        self._write_cache(body_path, meta_path, raw, response.headers)
                    return FetchResult(url, status=response.status, body=raw)
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and had_cache:
                    with open(body_path, "rb") as handle:
                        return FetchResult(url, status=304, body=handle.read(), from_cache=True, not_modified=True)
                last_error = "HTTP {0} {1}".format(exc.code, exc.reason)
                if exc.code in (400, 401, 403, 404, 410):
                    break  # retrying will not help
            except Exception as exc:  # noqa: BLE001 - network stack throws many types
                last_error = "{0}: {1}".format(type(exc).__name__, exc)
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))

        if had_cache:
            try:
                with open(body_path, "rb") as handle:
                    return FetchResult(url, status=None, body=handle.read(), error=last_error, from_cache=True)
            except OSError:
                pass
        return FetchResult(url, error=last_error or "unknown error")

    def _get_via_curl(self, url, use_cache, timeout):
        """用 curl 子进程取页面。代理模式或命中 curl_hosts 时都走这条路径：
        - proxy 显式设置 → --proxy <proxy>
        - proxy 未设置 → curl 继承系统代理 / 全局加速
        """
        body_path, meta_path = self._paths(url)
        had_cache = use_cache and os.path.exists(body_path)
        self._wait_for_host(url)

        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            return FetchResult(url, error="curl not found for proxy mode")

        handle, tmp = tempfile.mkstemp()
        os.close(handle)
        error = None
        try:
            args = [curl, "-sS", "-L", "--compressed", "--max-time", str(timeout),
                    "-A", USER_AGENT,
                    "-H", "Accept: application/rss+xml, application/atom+xml, "
                          "application/xml, text/xml, application/json, */*"]
            if self.proxy:
                args += ["--proxy", self.proxy]
            args += ["-o", tmp, "-w", "%{http_code}", url]

            result = subprocess.run(args, capture_output=True, timeout=timeout + 10)
            code = (result.stdout or b"").decode("ascii", "replace").strip()
            if result.returncode == 0 and code.startswith("2"):
                with open(tmp, "rb") as stream:
                    raw = stream.read()
                if use_cache:
                    self._write_cache(body_path, meta_path, raw, {})
                return FetchResult(url, status=int(code), body=raw)
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()[:160]
            error = "curl exit {0} http {1} {2}".format(result.returncode, code, detail)
        except Exception as exc:  # noqa: BLE001
            error = "{0}: {1}".format(type(exc).__name__, exc)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        if had_cache:
            try:
                with open(body_path, "rb") as stream:
                    return FetchResult(url, status=None, body=stream.read(),
                                       error=error, from_cache=True)
            except OSError:
                pass
        return FetchResult(url, error=error or "curl fetch failed")

    @staticmethod
    def _decode(payload, content_encoding):
        encoding = (content_encoding or "").lower()
        if encoding == "gzip":
            try:
                return gzip.decompress(payload)
            except OSError:
                return payload
        if encoding == "deflate":
            try:
                return zlib.decompress(payload, -zlib.MAX_WBITS)
            except zlib.error:
                try:
                    return zlib.decompress(payload)
                except zlib.error:
                    return payload
        return payload
