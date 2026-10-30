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


def place_label(place: dict[str, Any]) -> str:
    """省·市·区县 拼成可读地名。

    GeoAPI 最多给到区县一级（``adm1`` 省 / ``adm2`` 市 / ``name`` 区县），三层经常重复
    （天津市 / 天津 / 和平），相邻层互相包含时只留更详细的那个。
    用户直接给坐标时，``coord`` 为真，末尾追加坐标，免得被误认为定位到了区县中心。
    """
    name = place.get("name") or "未知位置"
    parts: list[str] = []
    for raw in (place.get("adm1") or "", place.get("adm2") or "", name):
        part = (raw or "").strip()
        if not part:
            continue
        if parts and (part in parts[-1] or parts[-1] in part):
            if len(part) > len(parts[-1]):
                parts[-1] = part
            continue
        parts.append(part)
    label = "·".join(parts) or name
    if place.get("coord"):
        try:
            lat = float(place.get("lat") or 0)
            lon = float(place.get("lon") or 0)
        except (TypeError, ValueError):
            return label
        # 纬度在前，和用户输入顺序一致，否则用户会以为保存的坐标被改了
        label += f"（{lat:.2f},{lon:.2f}）"
    return label


def short_label(place: dict[str, Any]) -> str:
    """推送标题里的短名：只要区县名。

    坐标绑定时补上坐标，否则用户看到「和平」不知道指的是哪个点。
    """
    name = place.get("name") or "未知位置"
    if not place.get("coord"):
        return name
    try:
        lat = float(place.get("lat") or 0)
        lon = float(place.get("lon") or 0)
    except (TypeError, ValueError):
        return name
    return f"{name}({lat:.2f},{lon:.2f})"


def loc_label(loc: dict[str, Any]) -> str:
    """地址的完整展示名：用户起的别名在前，后面跟行政区全名。"""
    city = loc.get("city") or {}
    alias = str(loc.get("name") or "").strip()
    label = place_label(city)
    return f"{alias}｜{label}" if alias else label


def loc_short(loc: dict[str, Any]) -> str:
    """推送标题里的短名。多个地址时必须能一眼分清是哪个，所以别名优先。"""
    city = loc.get("city") or {}
    alias = str(loc.get("name") or "").strip()
    short = short_label(city)
    if not alias:
        return short
    return short if alias == short else f"{alias}·{short}"


def header(
    place: dict[str, Any],
    moment: datetime | None = None,
    title: str = "",
    alias: str = "",
) -> str:
    """标题行。

    ``alias`` 是用户给地址起的别名。一个会话有多个地址时必须带上，否则用户看到
    「天津市·和平」分不清这是「家」还是「公司」推的。
    """
    moment = moment or datetime.now()
    label = place_label(place)
    alias = (alias or "").strip()
    if alias and alias not in label:
        label = f"{alias}｜{label}"
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
        if idx < len(labels):
            label = labels[idx]
        else:
            # 第 4 天起只用「11-02」读者算不清是周几，补上星期
            label = _date_label(str(day.get("date") or ""))
        text = day.get("text_day") or ""
        if day.get("text_night") and day.get("text_night") != text:
            text = f"{text}转{day['text_night']}"
        prob = day.get("precip_prob")
        prob_text = f"　降水 {prob}%" if prob else ""
        lines.append(
            f"{label}：{_temp(day.get('temp_min'))}~{_temp(day.get('temp_max'))}　{text}{prob_text}"
        )
    return "\n".join(lines)


def _date_label(raw: str) -> str:
    """``2026-11-02`` → ``11-02 周一``；解析不了就原样截取。"""
    try:
        day = datetime.strptime(raw[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return raw[5:10] if len(raw) >= 10 else raw
    week = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")[day.isoweekday() % 7]
    return f"{day.month:02d}-{day.day:02d} {week}"


def wrap(text: str, width: int, max_lines: int = 4) -> list[str]:
    """按字数折行，尽量在标点后断开。

    QQ / 微信的聊天窗口宽度不定，一行 120 字的预警正文在手机上要横向读，很难受。
    这里按字宽折，标点处优先断，最多 ``max_lines`` 行，超出用省略号收尾。
    断行处**保留标点**——把「、」吃掉会让「南港海域、天津港锚地海域」读成两个割裂的
    地名，预警正文尤其不能这么干。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    if len(raw) <= width:
        return [raw]
    lines: list[str] = []
    rest = raw
    while rest and len(lines) < max_lines:
        if len(rest) <= width:
            lines.append(rest)
            rest = ""
            break
        window = rest[:width]
        cut = max(window.rfind(p) for p in "，。；！？、：")
        if cut < width * 0.5:  # 标点太靠前就硬切，免得每行太短
            cut = width - 1
        lines.append(rest[: cut + 1])
        rest = rest[cut + 1 :].lstrip()
    if rest and lines:
        lines[-1] = lines[-1][: width - 1] + "…"
    return lines


def block_hourly(hours: list[dict[str, Any]], limit: int = 8, per_line: int = 4) -> str:
    """逐时预报。一条一行太宽，按每行 4 个折开。"""
    if not hours:
        return ""
    cells: list[str] = []
    for item in hours[:limit]:
        time = item.get("time") or ""
        text = item.get("text") or ""
        cells.append(f"{time} {_temp(item.get('temp'))} {text}")
    rows = ["　".join(cells[i : i + per_line]) for i in range(0, len(cells), per_line)]
    return "逐时：" + "\n　　　".join(rows)


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
    """生活指数。一行两条，最多 6 条——16 项全铺出来没人看。"""
    if not items:
        return ""
    picked = [i for i in items if i.get("type") in keep] or items[:4]
    cells = [f"{i.get('name')} {i.get('category')}" for i in picked if i.get("name")][:6]
    if not cells:
        return ""
    rows = ["｜".join(cells[i : i + 2]) for i in range(0, len(cells), 2)]
    return "指数：" + "\n　　　".join(rows)


def block_minutely(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    summary = data.get("summary") or ""
    return f"降水：{summary}" if summary else ""


def block_alerts(alerts: list[dict[str, Any]]) -> str:
    """生效预警。标题一行、发布信息一行、正文折行，最多 3 条。

    以前把正文硬截 120 字塞在一行里，手机上要横向拖着读，等于没有。折行之后同样的
    信息量读起来快得多。
    """
    if not alerts:
        return ""
    lines = [f"⚠️ 生效预警（{len(alerts)} 条）"]
    for alert in alerts[:3]:
        title = alert.get("title") or alert.get("type_name") or "预警"
        lines.append(f"· {title}")
        meta: list[str] = []
        if alert.get("sender"):
            meta.append(f"发布：{alert['sender']}")
        if alert.get("start"):
            stamp = f"{alert['start']}"
            if alert.get("end"):
                stamp += f" → {alert['end']}"
            meta.append(stamp)
        if meta:
            lines.append("　" + "　".join(meta))
        for row in wrap(alert.get("text") or "", 34, 4):
            lines.append(f"　{row}")
    return "\n".join(lines)


# 各内容项的渲染器：item -> (函数, 依赖的取数字段)
def render_report(
    place: dict[str, Any],
    content: list[str],
    bundle: dict[str, Any],
    *,
    moment: datetime | None = None,
    title: str = "",
    alias: str = "",
) -> str:
    """按 ``content`` 的顺序拼装。缺数据的项自动跳过，不报错。"""
    want = list(content or ["now", "daily"])
    blocks: list[str] = [header(place, moment, title, alias)]

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
