"""Shared human-readable names for Android application package names."""

from __future__ import annotations


# A value of None means the app is omitted from historical activity logs.
KNOWN_APPS = {
    # Social / communication
    "com.tencent.mm": "微信",
    "com.tencent.mobileqq": "QQ",
    "com.tencent.tim": "TIM",
    "com.xingin.xhs": "小红书",
    "com.sina.weibo": "微博",
    "com.immomo.momo": "陌陌",
    "com.tencent.wework": "企业微信",
    "com.alibaba.android.rimet": "钉钉",
    "com.lark.messenger": "飞书",
    # Video / live streaming
    "com.ss.android.ugc.aweme": "抖音",
    "com.kuaishou.nebula": "快手",
    "com.smile.gifmaker": "快手",
    "tv.danmaku.bili": "哔哩哔哩",
    "com.phoenix.read": "红果短剧",
    "com.youku.phone": "优酷",
    "com.tencent.qqlive": "腾讯视频",
    "com.qiyi.video": "爱奇艺",
    "com.hunantv.imgo.activity": "芒果TV",
    # Music
    "com.netease.cloudmusic": "网易云音乐",
    "com.tencent.qqmusic": "QQ音乐",
    "com.kugou.android": "酷狗音乐",
    "com.spotify.music": "Spotify",
    # Shopping
    "com.taobao.taobao": "淘宝",
    "com.jingdong.app.mall": "京东",
    "com.xunmeng.pinduoduo": "拼多多",
    "com.achievo.vipshop": "唯品会",
    # Tools / productivity
    "com.tencent.mtt": "QQ浏览器",
    "com.UCMobile": "UC浏览器",
    "com.android.chrome": "Chrome",
    "com.microsoft.emmx": "Edge",
    "com.qihoo.browser": "360浏览器",
    "com.baidu.searchbox": "百度",
    "com.larus.nova": "豆包",
    "com.ss.android.lark.alchemy": "豆包",
    "com.openai.chatgpt": "ChatGPT",
    "com.google.android.apps.maps": "Google Maps",
    "com.autonavi.minimap": "高德地图",
    "com.baidu.BaiduMap": "百度地图",
    # Payments / finance
    "com.eg.android.AlipayGphone": "支付宝",
    "com.tencent.android.qqdownloader": "应用宝",
    # Local services
    "com.sankuai.meituan": "美团",
    "me.ele": "饿了么",
    "com.dianping.v1": "大众点评",
    "com.Qunar": "去哪儿",
    # Reading / knowledge
    "com.zhihu.android": "知乎",
    "com.douban.frodo": "豆瓣",
    "com.ss.android.article.news": "今日头条",
    "com.netease.newsreader.activity": "网易新闻",
    # Games
    "com.miHoYo.Yuanshen": "原神",
    "com.miHoYo.hkrpg": "崩坏：星穹铁道",
    "com.tencent.tmgp.sgame": "王者荣耀",
    "com.tencent.tmgp.pubgmhd": "和平精英",
    # AI assistants
    "com.anthropic.claude": "Claude",
    "com.google.android.googlequicksearchbox": "Google搜索",
    # System apps filtered from historical activity logs
    "com.android.systemui": None,
    "com.android.launcher": None,
    "com.android.launcher3": None,
    "com.bbk.launcher2": None,
    "com.vivo.launcher": None,
    "com.huawei.android.launcher": None,
    "com.miui.home": None,
    "com.oppo.launcher": None,
    "com.sec.android.app.launcher": None,
    # Screen state reports
    "screen_off": "锁屏",
    "screen_on": "亮屏",
    # iQOO / vivo system
    "com.iqoo.powersaving": "省电管理",
}


SYSTEM_APP_DISPLAY_NAMES = {
    "com.bbk.launcher2": "vivo 桌面",
    "com.vivo.launcher": "vivo 桌面",
    "com.android.launcher": "安卓桌面",
    "com.android.launcher3": "安卓桌面",
    "com.huawei.android.launcher": "华为桌面",
    "com.miui.home": "小米桌面",
    "com.oppo.launcher": "OPPO 桌面",
    "com.sec.android.app.launcher": "三星桌面",
}


def display_app_name(app: str) -> str:
    """Return a readable name while preserving unknown package names."""
    app = str(app or "").strip()
    resolved = KNOWN_APPS.get(app)
    if resolved:
        return resolved
    return SYSTEM_APP_DISPLAY_NAMES.get(app, app)


def resolve_app_name(app: str, title: str = "") -> str | None:
    """Resolve an activity-log app name and filter known system packages."""
    app = str(app or "").strip()
    if "." not in app:
        return app
    if app in KNOWN_APPS:
        return KNOWN_APPS[app]
    if title and "." not in title and title != app:
        return title
    return app
