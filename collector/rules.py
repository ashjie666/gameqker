"""官方站关键词规则表。

替代 gameseek 的 sources.py：这里不抓 RSS，而是按 host 给出「官网新闻/公告页
出现哪些关键词，就算作有出海相关信号」的确定性规则。

设计原则：
- 只做确定性证据匹配，**绝不推断**。命中就输出命中片段，没命中就写 unknown。
- 规则按 host 覆盖，未覆盖的 host 走 DEFAULT_RULES（同一套通用关键词）。
"""

import re

# --------------------------------------------------------------------------
# 信号目录
# --------------------------------------------------------------------------
# tag -> 中文标签，用于前端展示与变更对比
SIGNAL_LABELS = {
    "global_launch": "全球上线",
    "release_date": "发售日期",
    "coming_soon": "即将推出",
    "test": "测试/公测",
    "english": "英文/多语言",
    "xbox_console": "主机/Game Pass",
}

# 通用规则：英文官方站 + 中文官网都能覆盖。
DEFAULT_RULES = {
    "label": "通用官方站",
    "paths": [],  # 相对站点根目录的候选页面；空表示只查 watch.official 给的 URL
    "patterns": [
        {"tag": "global_launch", "weight": 5,
         "regex": r"global\s+(?:launch|release|version)|worldwide\s+(?:launch|release)|国际版|英文版|海外版|全球(?:上线|发行|同步)"},
        {"tag": "release_date", "weight": 4,
         "regex": r"release\s+date|(?:launch|release)(?:es|ed|ing)?\s+(?:on|in)\b|上线日期|发行日期|正式上线|开服"},
        {"tag": "coming_soon", "weight": 3,
         "regex": r"coming\s+(?:soon|to)|due\s+(?:out|on)|set\s+for|arriv(?:es|ing)|敬请期待|即将(?:上线|推出|开服)"},
        {"tag": "test", "weight": 2,
         "regex": r"\b(?:closed|open)?\s*beta\b|\bCBT\b|\bOBT\b|playtest|首测|二测|内测|公测|测试"},
        {"tag": "english", "weight": 3,
         "regex": r"\benglish\b|多语言|英文(?:化|字幕|语音|支持)|language\s+support"},
        {"tag": "xbox_console", "weight": 2,
         "regex": r"\bxbox\b|game\s*pass|playstation|\bps5\b|console"},
    ],
}

# 按 host 的覆盖规则（key 为不含 www. 的小写 host）
SITE_RULES = {
    "phantombladezero.com": {
        "label": "影之刃零官网",
        "paths": ["/", "/news/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "wherewindsmeetgame.com": {
        "label": "燕云十六声英文官网",
        "paths": ["/", "/news/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "lordofthemysteriesgame.com": {
        "label": "诡秘之主官网",
        "paths": ["/", "/faq/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "zx.wanmei.com": {
        "label": "诛仙世界官网",
        "paths": ["/", "/news/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "nte.perfectworld.com": {
        "label": "异环官网",
        "paths": ["/", "/news/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "scrolloftaiwu.com": {
        "label": "太吾绘卷官网",
        "paths": ["/", "/news/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "ananta.163.com": {
        "label": "无限大官网",
        "paths": ["/", "/news/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "wy.shiyue.com": {
        "label": "望月官网",
        "paths": ["/", "/news/"],
        "patterns": DEFAULT_RULES["patterns"],
    },
    "store.steamchina.com": {
        "label": "蒸汽平台商店页",
        "paths": [],
        "patterns": DEFAULT_RULES["patterns"],
    },
}

# 预编译，避免每次巡检重复编译。
_COMPILED = {
    host: [(re.compile(item["regex"], re.IGNORECASE), item["tag"], item["weight"]) for item in cfg["patterns"]]
    for host, cfg in SITE_RULES.items()
}
_DEFAULT_COMPILED = [
    (re.compile(item["regex"], re.IGNORECASE), item["tag"], item["weight"])
    for item in DEFAULT_RULES["patterns"]
]


def normalize_host(url):
    """从 URL 取出用于规则匹配的 host（去掉 www. 前缀）。"""
    from urllib.parse import urlsplit

    host = (urlsplit(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0]


def rules_for(url):
    """返回 (host, 编译后的模式列表)。未覆盖的 host 走通用规则。"""
    host = normalize_host(url)
    return host, _COMPILED.get(host, _DEFAULT_COMPILED)


def label_for(url):
    """该 host 对应的规则集名称，用于报表展示。"""
    host, _ = rules_for(url)
    cfg = SITE_RULES.get(host)
    return cfg["label"] if cfg else DEFAULT_RULES["label"]


def paths_for(url):
    """该 host 配置的候选子页列表（相对站点根）。"""
    host, _ = rules_for(url)
    cfg = SITE_RULES.get(host) or DEFAULT_RULES
    return list(cfg.get("paths") or [])
