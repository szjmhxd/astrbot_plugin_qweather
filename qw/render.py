"""纯文本渲染。

为什么先不做图文卡片
--------------------
卡片要靠 Typst / Playwright 在容器里渲图，NAS 上那套依赖已经踩过一轮坑（字体、chromium、
apt 源）。推送是高频功能，一次渲染失败的代价是用户彻底收不到天气。纯文本没有失败面，
先把功能跑通，卡片以后作为一个可选开关再加。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _temp(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.0f}°"
    except (TypeError, ValueError):
        return str(value)


def _temp1(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}°C"
    except (TypeError, ValueError):
        return str(value)


def _wind(compass: str, scale: Any) -> str:
    """和风给的是英文方位（w/nw/se），转成中文。"""
    mapping = {
        "n": "北",
        "ne": "东北",
        "e": "东",
        "se": "东南",
        "s": "南",
        "sw": "西南",
        "w": "西",
        "nw": "西北",
        "c": "静风",
    }
    direction = mapping.get((compass or "").lower(), compass or "")
    if scale is None:
        return direction or "—"
    return f"{direction}{scale}级" if direction else f"{scale}级"


def header(place: dict[str, Any], moment: datetime | None = None, title: str = "") -> str:
    moment = moment or datetime.now()
    name = place.get("name") or "未知位置"
    adm1 = place.get("adm1") or ""
    label = f"{adm1}·{name}" if adm1 and adm1 not in name else name
    stamp = moment.strftime("%m-%d %H:%M")
    return f"【{title or '天气'}｜{label}｜{stamp}】"


def block_now(data: dict[str, Any]) -> str:
    if not data:
        return ""
    lines = [
        f"实况：{data.get('text') or '—'} {_temp1(data.get('temp'))}"
        f"　体感 {_temp1(data.get('feels_like'))}"
    ]
    extras = [
        _wind(data.get("wind_dir") or "", data.get("wind_scale")),
        f"湿度 {data.get('humidity')}%" if data.get("humidity") is not None else "",
        f"气压 {data.get('pressure'):.0f}hPa" if data.get("pressure") else "",
        f"能见度 {(data.get('visibility') or 0) / 1000:.1f}km" if data.get("visibility") else "",
    ]
    tail = "　".join(x for x in extras if x)
    if tail:
        lines.append(tail)
    if data.get("precip"):
        lines.append(f"当前降水 {data.get('precip')}mm")
    return "\n".join(lines)


def block_daily(days: list[dict[str, Any]], limit: int = 4) -> str:
    if not days:
        return ""
    lines: list[str] = []
    labels = ["今天", "明天", "后天"]
    for idx, day in enumerate(days[:limit]):
        label = labels[idx] if idx < len(labels) else str(day.get("date") or "")[5:]
        text = day.get("text_day") or ""
        if day.get("text_night") and day.get("text_night") != text:
            text = f"{text}转{day['text_night']}"
        prob = day.get("precip_prob")
        prob_text = f"　降水 {prob}%" if prob else ""
        lines.append(
            f"{label}：{_temp(day.get('temp_min'))}~{_temp(day.get('temp_max'))}　{text}{prob_text}"
        )
    return "\n".join(lines)


def block_hourly(hours: list[dict[str, Any]], limit: int = 8) -> str:
    if not hours:
        return ""
    parts: list[str] = []
    for item in hours[:limit]:
        time = item.get("time") or ""
        text = item.get("text") or ""
        parts.append(f"{time} {_temp(item.get('temp'))}{text}")
    return "逐时：" + "｜".join(parts)


def block_sun(day: dict[str, Any] | None) -> str:
    if not day:
        return ""
    rise, down = day.get("sunrise"), day.get("sunset")
    if not rise or not down:
        return ""
    return f"日出 {rise}　日落 {down}"


def block_air(air: dict[str, Any] | None) -> str:
    if not air:
        return ""
    primary = f"（首要 {air['primary']}）" if air.get("primary") else ""
    return f"空气：AQI {air.get('aqi')} {air.get('category') or ''}{primary}"


def block_indices(items: list[dict[str, Any]], keep: tuple[str, ...] = ("1", "2", "3", "5")) -> str:
    if not items:
        return ""
    picked = [i for i in items if i.get("type") in keep] or items[:4]
    parts = [f"{i.get('name')} {i.get('category')}" for i in picked if i.get("name")]
    return "指数：" + "｜".join(parts) if parts else ""


def block_minutely(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    summary = data.get("summary") or ""
    return f"降水：{summary}" if summary else ""


def block_alerts(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return ""
    lines = ["⚠️ 生效预警："]
    for alert in alerts[:3]:
        lines.append(f"· {alert.get('title') or alert.get('type_name') or '预警'}")
        if alert.get("text"):
            lines.append(f"  {alert['text'][:120]}")
    return "\n".join(lines)


# 各内容项的渲染器：item -> (函数, 依赖的取数字段)
def render_report(
    place: dict[str, Any],
    content: list[str],
    bundle: dict[str, Any],
    *,
    moment: datetime | None = None,
    title: str = "",
) -> str:
    """按 ``content`` 的顺序拼装。缺数据的项自动跳过，不报错。"""
    want = list(content or ["now", "daily"])
    blocks: list[str] = [header(place, moment, title)]

    # sun 依赖 daily，提前准备好
    days = bundle.get("daily") or []
    today = days[0] if days else None

    if "now" in want:
        blocks.append(block_now(bundle.get("now") or {}))
    if "daily" in want:
        blocks.append(block_daily(days))
    if "hourly" in want:
        blocks.append(block_hourly(bundle.get("hourly") or []))
    if "sun" in want:
        blocks.append(block_sun(today))
    if "air" in want:
        blocks.append(block_air(bundle.get("air")))
    if "indices" in want:
        blocks.append(block_indices(bundle.get("indices") or []))
    if "minutely" in want:
        blocks.append(block_minutely(bundle.get("minutely")))
    if "alert" in want:
        blocks.append(block_alerts(bundle.get("alerts") or []))

    return "\n".join(x for x in blocks if x)


def summary_line(place: dict[str, Any], bundle: dict[str, Any]) -> str:
    """一行式摘要，给主动推送的开头用。"""
    name = place.get("name") or ""
    now = bundle.get("now") or {}
    days = bundle.get("daily") or []
    if not now:
        return name
    text = f"{name} {now.get('text') or ''} {_temp1(now.get('temp'))}".strip()
    if days:
        text += f"，今日 {_temp(days[0].get('temp_min'))}~{_temp(days[0].get('temp_max'))}"
    return text
