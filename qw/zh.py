"""英文枚举 → 中文。

为什么不是「加个 lang=zh 就完了」
--------------------------------
和风接口支持 ``lang`` 参数，插件也会在每次请求里带上 ``lang=zh``，正常情况下返回的就是
中文。但控制台的**项目设置**里有一项「语言」，它决定的是「不传 lang 时用什么」；一旦
用户把它改成英文，历史缓存、别人改的配置、以及个别字段（比如预警的 ``typeName``）仍会
冒出英文。

所以这里是第二道闸：拿到什么文本都先过一遍，认识的换成中文，不认识的**原样返回**。
不认识的不能吞掉——宁可显示一个英文词，也不要显示空白，用户至少知道发生了什么。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 天气现象
# 键是英文原文（和风 en 文本），值是对应的中文。大小写不敏感。
TEXT_ZH = {
    "sunny": "晴",
    "clear": "晴",
    "clear night": "晴",
    "partly cloudy": "多云",
    "cloudy": "阴",
    "overcast": "阴",
    "few clouds": "少云",
    "scattered clouds": "疏云",
    "broken clouds": "多云",
    "fair": "晴",
    "rain": "雨",
    "light rain": "小雨",
    "moderate rain": "中雨",
    "heavy rain": "大雨",
    "rainstorm": "暴雨",
    "heavy rainstorm": "大暴雨",
    "rain shower": "阵雨",
    "light rain shower": "小阵雨",
    "heavy rain shower": "强阵雨",
    "shower rain": "阵雨",
    "thunderstorm": "雷阵雨",
    "light thunderstorm": "雷阵雨伴有冰雹",
    "sleet": "雨夹雪",
    "light sleet": "小雨夹雪",
    "rain and snow": "雨雪天气",
    "light rain and snow": "雨夹雪",
    "freezing rain": "冻雨",
    "light snow": "小雪",
    "moderate snow": "中雪",
    "heavy snow": "大雪",
    "snowstorm": "暴雪",
    "snow": "雪",
    "snow shower": "阵雪",
    "light snow shower": "小阵雪",
    "heavy snow shower": "强阵雪",
    "flurries": "阵雪",
    "haze": "霾",
    "fog": "雾",
    "mist": "薄雾",
    "dust": "浮尘",
    "sand": "扬沙",
    "sandstorm": "沙尘暴",
    "strong sandstorm": "强沙尘暴",
    "smoke": "烟",
    "windy": "风",
    "gale": "大风",
    "tropical storm": "热带风暴",
    "hurricane": "飓风",
    "typhoon": "台风",
    "tornado": "龙卷风",
    "cold": "寒冷",
    "hot": "炎热",
    "unknown": "未知",
    # 预警标题里常见的拼法
    "gale at sea": "海上大风",
    "rainstorm to blizzard": "暴雨转暴雪",
}

# ---------------------------------------------------------------- 风向
# 和风大部分接口给的是缩写（``N`` / ``NE``），部分给全称，这里都收。
WIND_ZH = {
    "n": "北",
    "north": "北",
    "ne": "东北",
    "northeast": "东北",
    "e": "东",
    "east": "东",
    "se": "东南",
    "southeast": "东南",
    "s": "南",
    "south": "南",
    "sw": "西南",
    "southwest": "西南",
    "w": "西",
    "west": "西",
    "nw": "西北",
    "northwest": "西北",
    "c": "静风",
    "calm": "静风",
    "variable": "风向不定",
    "vrb": "风向不定",
}

# ---------------------------------------------------------------- 预警类型
# 和风的 typeName 在英文环境下是这些，用来给预警补一个中文类型名。
ALERT_TYPE_ZH = {
    "typhoon": "台风",
    "tornado": "龙卷风",
    "rainstorm": "暴雨",
    "rainstorm to blizzard": "暴雨转暴雪",
    "gale at sea": "海上大风",
    "waterlogging": "内涝",
    "geological hazard": "地质灾害",
    "hail": "冰雹",
    "thunderstorm": "雷电",
    "gale": "大风",
    "sandstorm": "沙尘暴",
    "heat wave": "高温",
    "cold wave": "寒潮",
    "blizzard": "暴雪",
    "fog": "大雾",
    "haze": "霾",
    "road icing": "道路结冰",
    "drought": "干旱",
    "lightning": "雷电",
    "freezing rain": "冻雨",
    "snowstorm": "暴雪",
    "heavy fog": "大雾",
    "strong wind": "大风",
}

# ---------------------------------------------------------------- 生活指数
# 指数名称在英文环境下同样是英文，卡片里要显示中文才好读。
INDEX_NAME_ZH = {
    "sport": "运动指数",
    "car washing": "洗车指数",
    "dressing": "穿衣指数",
    "fishing": "钓鱼指数",
    "uv": "紫外线指数",
    "travelling": "旅游指数",
    "allergy": "过敏指数",
    "comfort": "舒适度指数",
    "cold": "感冒指数",
    "air pollution": "空气污染扩散条件指数",
    "air conditioning": "空调开启指数",
    "sunglasses": "太阳镜指数",
    "makeup": "化妆指数",
    "drying": "晾晒指数",
    "traffic": "交通指数",
    "sun protection": "防晒指数",
    "sports": "运动指数",
    "carwash": "洗车指数",
}


def _lookup(text: str, table: dict[str, str]) -> str:
    return table.get((text or "").strip().lower(), "")


def text_zh(value: str) -> str:
    """天气现象转中文。支持 ``Few Clouds转Sunny`` 这种拼接串。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    hit = _lookup(raw, TEXT_ZH)
    if hit:
        return hit
    # 拼接串：按「转」切开分别翻，再用「转」接回去
    if "转" in raw:
        parts = [text_zh(p) for p in raw.split("转")]
        return "转".join(p for p in parts if p)
    return raw


def wind_zh(value: str) -> str:
    """风向转中文。认不出来原样返回，不丢信息。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    return _lookup(raw, WIND_ZH) or raw


def alert_type_zh(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    return _lookup(raw, ALERT_TYPE_ZH) or raw


def index_name_zh(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    return _lookup(raw, INDEX_NAME_ZH) or raw
