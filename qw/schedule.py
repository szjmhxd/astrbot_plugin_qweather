"""规则引擎：判断某时刻该不该触发。

设计取舍
--------
不做 cron 解析，也不为每条规则起一个 asyncio task。主循环每 30 秒醒一次，把所有会话的
规则过一遍，命中就推。这样规则增删不用取消/重建 task，也不会有几十个 task 抢 GIL。

触发判定用「分钟字符串完全相等」而不是「大于等于上次触发时间」：

    目标 = "2026-10-29 07:30"
    now  = "2026-10-29 07:30"  → 命中
    已推过同一个字符串 → 跳过

好处是幂等——同一分钟内 tick 两次、或刚好在 07:30 重启，靠 ``state.fired`` 里的戳就能
避免重复推送。代价是如果进程卡住超过一分钟会漏推，但 30 秒的 tick 间隔下这基本不会发生。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any

WEEKDAY_ALIAS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "周一": 1,
    "周二": 2,
    "周三": 3,
    "周四": 4,
    "周五": 5,
    "周六": 6,
    "周日": 7,
    "周天": 7,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
    "sun": 7,
}

SOLAR_ALIAS = {
    "日出": "sunrise",
    "sunrise": "sunrise",
    "sun": "sunrise",
    "日落": "sunset",
    "sunset": "sunset",
}

KIND_LABEL = {
    "clock": "每天",
    "weekday": "每周",
    "monthday": "每月",
    "hourly": "每小时",
    "solar": "日出日落",
}

WEEKDAY_LABEL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}

CONTENT_LABEL = {
    "now": "实况",
    "daily": "预报",
    "hourly": "逐时",
    "air": "空气",
    "indices": "指数",
    "alert": "预警",
    "sun": "日出日落",
    "minutely": "降水",
}


# ---------------------------------------------------------------- 解析


def parse_clock(text: str) -> tuple[int, int]:
    """``"7:30"`` / ``"07:30"`` / ``"730"`` → ``(7, 30)``。"""
    raw = (text or "").strip().replace("：", ":")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        match = re.fullmatch(r"(\d{1,2})点(\d{1,2})?", raw)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
        else:
            if re.fullmatch(r"\d{3,4}", raw) and len(raw) % 2 == 0:
                hour, minute = int(raw[:-2]), int(raw[-2:])
            elif re.fullmatch(r"\d{1,2}", raw):
                hour, minute = int(raw), 0
            else:
                raise ValueError(f"时间格式不对：{text}（要 07:30 这种）")
    else:
        hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间越界：{text}")
    return hour, minute


def fmt_clock(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def parse_range(text: str, low: int, high: int, what: str) -> list[int]:
    """``"8-12,14"`` → ``[8,9,10,11,12,14]``。支持区间与逗号混写。"""
    result: set[int] = set()
    for chunk in re.split(r"[,，、\s]+", (text or "").strip()):
        if not chunk:
            continue
        if "-" in chunk or "~" in chunk or "到" in chunk:
            parts = re.split(r"[-~到]", chunk)
            if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
                raise ValueError(f"{what}格式不对：{chunk}")
            start, end = int(parts[0]), int(parts[1])
            if start > end:
                raise ValueError(f"{what}区间反了：{chunk}")
            result.update(range(start, end + 1))
        else:
            if not chunk.isdigit():
                raise ValueError(f"{what}格式不对：{chunk}")
            result.add(int(chunk))
    values = sorted(result)
    if not values:
        raise ValueError(f"{what}不能为空")
    if values[0] < low or values[-1] > high:
        raise ValueError(f"{what}取值范围 {low}-{high}")
    return values


def parse_hours(text: str) -> list[int]:
    return parse_range(text, 0, 23, "小时")


def parse_minutes(text: str) -> list[int]:
    return parse_range(text, 0, 59, "分钟")


def parse_weekdays(text: str) -> list[int]:
    result: list[int] = []
    for chunk in re.split(r"[,，、\s]+", (text or "").strip()):
        if not chunk:
            continue
        key = chunk.lower().removeprefix("周")
        if key not in WEEKDAY_ALIAS and chunk not in WEEKDAY_ALIAS:
            raise ValueError(f"星期写法不认识：{chunk}（用 一,三,五）")
        result.append(WEEKDAY_ALIAS.get(key) or WEEKDAY_ALIAS[chunk])
    if not result:
        raise ValueError("星期不能为空")
    return sorted(set(result))


def parse_monthdays(text: str) -> list[int]:
    return parse_range(text, 1, 31, "日期")


def parse_solar(text: str) -> str:
    key = (text or "").strip().lower()
    if key not in SOLAR_ALIAS:
        raise ValueError(f"日出日落写法不认识：{text}（用 日出 / 日落）")
    return SOLAR_ALIAS[key]


def parse_offset(text: str) -> int:
    raw = (text or "").strip().replace(" ", "")
    if not raw:
        return 0
    match = re.fullmatch(r"([+-]?)(\d{1,3})", raw)
    if not match:
        raise ValueError(f"偏移量格式不对：{text}（用 -30 或 +15）")
    value = int(match.group(2))
    return -value if match.group(1) == "-" else value


def parse_content(text: str) -> list[str]:
    items = [c for c in re.split(r"[,，、\s]+", (text or "").strip()) if c]
    if not items:
        raise ValueError("内容不能为空")
    unknown = [c for c in items if c not in CONTENT_LABEL]
    if unknown:
        raise ValueError(f"不认识的内容项：{', '.join(unknown)}")
    # 去重但保序
    seen: set[str] = set()
    ordered = [c for c in items if not (c in seen or seen.add(c))]
    return ordered


# ---------------------------------------------------------------- 判定


def _minutes_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute


def sun_datetime(day: date, clock: str) -> datetime | None:
    """把 ``"06:34"`` 拼成今天的 datetime。"""
    if not clock or ":" not in clock:
        return None
    try:
        hour, minute = parse_clock(clock)
    except ValueError:
        return None
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def solar_target(
    rule: dict[str, Any], now: datetime, sun: dict[str, str] | None
) -> datetime | None:
    """日出/日落 ± 偏移 得到的目标时刻。没有天文数据时返回 None。"""
    if not sun:
        return None
    clock = sun.get(rule.get("solar") or "sunrise") or ""
    base = sun_datetime(now.date(), clock)
    if base is None:
        return None
    return base + timedelta(minutes=int(rule.get("offset") or 0))


def is_due(
    rule: dict[str, Any],
    now: datetime,
    fired_stamp: str = "",
    sun: dict[str, str] | None = None,
) -> bool:
    """这一刻该不该触发。``fired_stamp`` 是上次触发记的 ``"YYYY-MM-DD HH:MM"``。"""
    if not rule.get("enabled", True):
        return False
    target = target_stamp(rule, now, sun)
    if target is None:
        return False
    return target == now.strftime("%Y-%m-%d %H:%M") and target != fired_stamp


def target_stamp(
    rule: dict[str, Any], now: datetime, sun: dict[str, str] | None = None
) -> str | None:
    """算出「如果今天要触发，戳应该是什么」。

    只有戳等于当前分钟才算命中，所以这个函数同时也是 ``is_due`` 的核心。
    """
    kind = rule.get("kind") or "clock"
    day = now.date()

    if kind == "clock":
        hour, minute = parse_clock(rule.get("at") or "07:30")
        return (
            datetime.combine(day, datetime.min.time())
            .replace(hour=hour, minute=minute)
            .strftime("%Y-%m-%d %H:%M")
        )

    if kind == "weekday":
        weekdays = rule.get("weekdays") or []
        if now.isoweekday() not in weekdays:
            return None
        hour, minute = parse_clock(rule.get("at") or "07:30")
        return (
            datetime.combine(day, datetime.min.time())
            .replace(hour=hour, minute=minute)
            .strftime("%Y-%m-%d %H:%M")
        )

    if kind == "monthday":
        monthdays = rule.get("monthdays") or []
        if day.day not in monthdays:
            return None
        hour, minute = parse_clock(rule.get("at") or "07:30")
        return (
            datetime.combine(day, datetime.min.time())
            .replace(hour=hour, minute=minute)
            .strftime("%Y-%m-%d %H:%M")
        )

    if kind == "hourly":
        if now.hour not in (rule.get("hours") or []):
            return None
        if now.minute not in (rule.get("minutes") or [0]):
            return None
        return now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    if kind == "solar":
        moment = solar_target(rule, now, sun)
        if moment is None:
            return None
        return moment.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    return None


def next_runs(
    rule: dict[str, Any],
    now: datetime,
    sun: dict[str, str] | None,
    limit: int = 3,
    horizon_days: int = 40,
) -> list[datetime]:
    """往后推下一次（几次）触发时间，给用户看配置效果用。"""
    if not rule.get("enabled", True):
        return []
    out: list[datetime] = []
    kind = rule.get("kind") or "clock"
    cursor = now.replace(second=0, microsecond=0)

    if kind == "solar":
        moment = solar_target(rule, now, sun)
        if moment and moment > now:
            out.append(moment)
        elif moment:
            tomorrow = now + timedelta(days=1)
            nxt = solar_target(rule, tomorrow, sun)
            if nxt:
                out.append(nxt)
        return out

    for _ in range(horizon_days * 24 * 60):
        cursor += timedelta(minutes=1)
        if cursor.date() > now.date() + timedelta(days=horizon_days):
            break
        stamp = target_stamp(rule, cursor, sun)
        if stamp and stamp == cursor.strftime("%Y-%m-%d %H:%M"):
            out.append(cursor)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- 时段


def in_windows(now: datetime, windows: list[dict[str, str]]) -> bool:
    """判断当前时刻是否落在任一可提醒时段内。时段支持跨零点（22:00-06:00）。"""
    if not windows:
        return True
    current = now.hour * 60 + now.minute
    for window in windows:
        try:
            start_h, start_m = parse_clock(window.get("start") or "00:00")
            end_h, end_m = parse_clock(window.get("end") or "23:59")
        except ValueError:
            continue
        start = _minutes_of_day(start_h, start_m)
        end = _minutes_of_day(end_h, end_m)
        if start <= end:
            if start <= current <= end:
                return True
        else:  # 跨零点
            if current >= start or current <= end:
                return True
    return False


def window_text(windows: list[dict[str, str]]) -> str:
    if not windows:
        return "全天"
    return "、".join(f"{w.get('start', '?')}-{w.get('end', '?')}" for w in windows)


# ---------------------------------------------------------------- 摘要


def describe_rule(rule: dict[str, Any], idx: int | None = None) -> str:
    """一行中文摘要，给 ``/天气推送`` 列表用。"""
    kind = rule.get("kind") or "clock"
    head = f"{idx}. " if idx is not None else ""
    flag = "" if rule.get("enabled", True) else "【已关闭】"
    title = f"「{rule['title']}」" if rule.get("title") else ""

    if kind == "clock":
        when = f"每天 {rule.get('at')}"
    elif kind == "weekday":
        days = "".join(WEEKDAY_LABEL.get(d, str(d)) for d in rule.get("weekdays") or [])
        when = f"每周{days} {rule.get('at')}"
    elif kind == "monthday":
        days = ",".join(str(d) for d in rule.get("monthdays") or [])
        when = f"每月 {days} 号 {rule.get('at')}"
    elif kind == "hourly":
        hours = rule.get("hours") or []
        span = f"{min(hours)}-{max(hours)}点" if hours else "?"
        minutes = ",".join(f"{m:02d}" for m in rule.get("minutes") or [0])
        when = f"{span} 每小时第 {minutes} 分"
    elif kind == "solar":
        base = "日出" if rule.get("solar") == "sunrise" else "日落"
        offset = int(rule.get("offset") or 0)
        when = base if offset == 0 else f"{base}{offset:+d} 分钟"
    else:
        when = kind

    content = "+".join(CONTENT_LABEL.get(c, c) for c in rule.get("content") or [])
    return f"{head}{flag}{when}｜{content}{title}"


def month_days(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
