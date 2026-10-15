"""Steam 巡检：某个 appid 在全球（cc=us）商店的状态。

只查官方公开的 appdetails 接口，纯 JSON、无需 Key：
    https://store.steampowered.com/api/appdetails?appids=<id>&cc=us&l=english

语义约定：
- `success: false` = 该地区商店查不到这个 appid → 视为「全球未上架」**正面信号**，
  写 steam_listed=False，这**不是**失败。
- 只有网络/解析异常才算失败，此时不动旧值，只记 error。
"""

from .textutil import clip

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails?appids={appid}&cc=us&l=english"
STORE_URL = "https://store.steampowered.com/app/{appid}/"


def check_appid(fetcher, appid):
    """返回 dict：{listed, release_date, coming_soon, name, url, error}。

    listed 为 None 表示「本次没查到结论」（网络失败 / 无法解析），调用方应保留旧值。
    """
    url = APPDETAILS_URL.format(appid=appid)
    result = {
        "listed": None,
        "release_date": None,
        "coming_soon": None,
        "name": None,
        "url": STORE_URL.format(appid=appid),
        "error": None,
        "stale": False,
    }

    response = fetcher.get(url)
    # 网络失败时会回退到本地陈旧缓存；只要能拿到 body 就照样解析，
    # 只是标记 stale，让报表知道这条不是新鲜数据。
    if response.body is None:
        result["error"] = response.error or "fetch failed"
        return result
    result["stale"] = bool(response.error)

    try:
        import json

        payload = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError) as exc:
        result["error"] = "decode failed: {0}".format(type(exc).__name__)
        return result

    entry = payload.get(str(appid))
    if not isinstance(entry, dict):
        result["error"] = "unexpected response shape"
        return result

    if not entry.get("success"):
        # 该地区查不到 = 全球未上架。这是我们要的正向结论，不是错误。
        result["listed"] = False
        return result

    data = entry.get("data") or {}
    release = data.get("release_date") or {}
    result["listed"] = True
    result["name"] = clip(data.get("name"), 120) or None
    result["release_date"] = clip(release.get("date"), 60) or None
    result["coming_soon"] = bool(release.get("coming_soon"))
    return result
