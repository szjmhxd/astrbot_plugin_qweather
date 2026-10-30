"""成本估算：把「当前对话的所有设置」折成每月的请求次数和钱。

为什么要这个东西
----------------
和风的计费单位是「一次请求」，而插件里一条推送规则、一次轮询间隔，普通人算不清
一个月到底会打多少次。用户问「我这样配下来一个月要花多少钱」的时候，答案应该是
一串能对上的明细，而不是一句「大概几块钱吧」。

计价口径
--------
按官方文档「天气和基础服务」阶梯价（所有插件用到的接口都在这档里）：

===================  ==============
每月请求量           单价（元/次）
===================  ==============
0 – 50,000           0（免费）
之后 950,000         0.0007
之后 4,000,000       0.0005
之后 5,000,000       0.00035
之后 40,000,000      0.00015
之后 50,000,000      0.0001
===================  ==============

累进计算，和官方示例一致（100 万次 ≈ 665 元）。

估算的边界
----------
这些数字**不含**用户手动发指令查询的部分（那取决于人，不取决于设置），
但含了插件自己的缓存折减：逐时预报缓存 1 小时、日出日落缓存 30 分钟、
预警列表全局最快 5 分钟一次。所以数字会明显低于「轮询次数 × 接口数」的朴素乘法。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import schedule

# 官方阶梯价：(这一档的请求量, 这一档的单价)
TIERS: list[tuple[int, float]] = [
    (50_000, 0.0),
    (950_000, 0.0007),
    (4_000_000, 0.0005),
    (5_000_000, 0.00035),
    (40_000_000, 0.00015),
    (50_000_000, 0.0001),
]

FREE_QUOTA = TIERS[0][0]

# 逐时预报缓存 1 小时、日出日落 30 分钟、预警列表最快 5 分钟一次（与 pushers 保持一致）
HOURLY_CACHE_MINUTES = 60
ALERT_LIST_MIN_MINUTES = 5


def month_cost(requests: float) -> float:
    """累进阶梯价 → 元。不足 0.01 元的按 0.01 元算，与官方「最小计费金额」一致。"""
    left = max(0.0, float(requests))
    if left <= FREE_QUOTA:
        return 0.0
    left -= FREE_QUOTA
    total = 0.0
    for size, price in TIERS[1:]:
        if left <= 0:
            break
        take = min(left, size)
        total += take * price
        left -= take
    return total


def _window_minutes(window: dict[str, Any]) -> int:
    """一个时段的分钟数。跨零点按模 1440 算，起止相同视为整天。"""
    try:
        sh, sm = schedule.parse_clock(str(window.get("start") or "00:00"))
        eh, em = schedule.parse_clock(str(window.get("end") or "23:59"))
    except ValueError:
        return 0
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return 24 * 60
    span = end - start
    return span if span > 0 else span + 24 * 60


def _hours_of(window: dict[str, Any]) -> int:
    """一个时段跨了几个小时。"""
    try:
        sh, sm = schedule.parse_clock(str(window.get("start") or "00:00"))
        eh, em = schedule.parse_clock(str(window.get("end") or "23:59"))
    except ValueError:
        return 0
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return 24
    span = (end - start) % (24 * 60) or 24 * 60
    return span // 60 + (1 if span % 60 else 0)


def _covered_hours_per_day(windows: list[dict[str, Any]], workdays: int, days: int) -> float:
    """平均每天有几个小时落在可提醒时段里。

    逐时预筛按小时缓存，一小时最多打一次，所以「覆盖小时数」就是预筛的请求数。
    时段之间重叠会重复累加，封顶 24 小时。
    """
    total = 0.0
    for window in windows:
        scale = _day_ratio(schedule.normalize_days(window.get("days")), workdays, days)
        total += _hours_of(window) * scale
    return min(24.0, total)


@dataclass
class Line:
    """一条明细。``runs`` 是月触发次数，``per_run`` 是每次几条请求。"""

    where: str
    what: str
    runs: float
    per_run: float
    note: str = ""

    @property
    def requests(self) -> float:
        return self.runs * self.per_run


@dataclass
class Estimate:
    lines: list[Line] = field(default_factory=list)
    workdays: int = 0
    month_days: int = 0

    @property
    def total(self) -> float:
        return sum(line.requests for line in self.lines)

    @property
    def cost(self) -> float:
        return month_cost(self.total)

    def by_where(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for line in self.lines:
            out[line.where] = out.get(line.where, 0.0) + line.requests
        return out


def _workday_stats(cal: Any, today: date) -> tuple[int, int]:
    """当月工作日数、当月天数。没有日历就按 5/7 估。"""
    days = calendar.monthrange(today.year, today.month)[1]
    if cal is not None:
        count = 0
        for day in range(1, days + 1):
            try:
                if cal.is_workday(date(today.year, today.month, day)):
                    count += 1
            except Exception:  # noqa: BLE001 日历坏了不影响估算
                return round(days * 5 / 7), days
        return count, days
    return round(days * 5 / 7), days


def _day_ratio(mode: str, workdays: int, days: int) -> float:
    if days <= 0:
        return 1.0
    if mode == "workday":
        return workdays / days
    if mode == "restday":
        return (days - workdays) / days
    return 1.0


def _content_calls(content: list[str]) -> int:
    """一次推送实际打几个接口。

    ``sun`` 不单独计费——它是从 ``daily`` 的返回值里取的（见 ``fetch_bundle``），
    所以「全部」8 项实际是 7 次请求。只有单独要日出日落时才仍要拉一次 daily。
    """
    items = [c for c in content if c != "sun"]
    if not items:
        return 1 if content else 0
    return len(items)


def _rule_runs(rule: dict[str, Any], days: int, workdays: int) -> float:
    """一条推送规则的月触发次数（不含日条件折减）。"""
    kind = str(rule.get("kind") or "clock")
    if kind == "weekday":
        return 4.35 * len(rule.get("weekdays") or [])
    if kind == "monthday":
        return float(len(rule.get("monthdays") or []))
    if kind == "hourly":
        hours = len(rule.get("hours") or []) or 1
        minutes = len(rule.get("minutes") or []) or 1
        return days * hours * minutes
    # clock / solar 都是每天一次
    return float(days)


def estimate_session(
    session: dict[str, Any],
    *,
    cal: Any = None,
    today: date | None = None,
    with_location_name: bool = True,
) -> Estimate:
    """整个会话的估算。``cal`` 是 :class:`qw.workday.WorkdayCalendar`，可为 None。"""
    moment = today or date.today()
    workdays, days = _workday_stats(cal, moment)
    est = Estimate(workdays=workdays, month_days=days)

    for loc in session.get("locations") or []:
        where = str(loc.get("name") or loc.get("lid") or "地址")
        if not with_location_name:
            where = f"{loc.get('lid')}"
        city = loc.get("city") or {}
        if not city.get("lat"):
            # 没有坐标就不会有任何请求，不占一行
            continue

        # 1) 定时规则
        for idx, rule in enumerate(loc.get("rules") or [], 1):
            if not rule.get("enabled", True):
                continue
            content = rule.get("content") or []
            runs = _rule_runs(rule, days, workdays)
            runs *= _day_ratio(schedule.normalize_days(rule.get("days")), workdays, days)
            est.lines.append(
                Line(where, schedule.describe_rule(rule, idx), runs, _content_calls(content))
            )

        # 2) 雨雪轮询：每轮一次分钟降水；预筛的逐时预报按小时缓存
        precip = loc.get("precip") or {}
        if precip.get("enabled") and (precip.get("on_start") or precip.get("on_stop")):
            interval = max(5, int(precip.get("poll_interval") or 10))
            windows = precip.get("windows") or []
            rounds = 0.0
            for window in windows:
                scale = _day_ratio(schedule.normalize_days(window.get("days")), workdays, days)
                rounds += _window_minutes(window) / interval * scale
            if not windows:
                rounds = 24 * 60 / interval
            est.lines.append(
                Line(
                    where,
                    f"雨雪轮询 每 {interval} 分钟",
                    rounds * days,
                    1,
                    "每轮查一次分钟降水",
                )
            )
            covered = _covered_hours_per_day(windows, workdays, days) or 24.0
            est.lines.append(
                Line(
                    where,
                    "雨雪预筛 逐时预报",
                    days * covered,
                    1,
                    f"平均每天 {covered:.1f} 小时落在时段内（已按工作日折减），逐时有 1 小时缓存",
                )
            )

        # 3) 预警轮询：每轮一次列表（全局 5 分钟节流）+ 少量详情
        alert = loc.get("alert") or {}
        if alert.get("enabled"):
            interval = max(5, int(alert.get("poll_interval") or 60))
            windows = alert.get("windows") or []
            rounds = 0.0
            for window in windows:
                scale = _day_ratio(schedule.normalize_days(window.get("days")), workdays, days)
                rounds += _window_minutes(window) / interval * scale
            if not windows:
                rounds = 24 * 60 / interval
            est.lines.append(
                Line(
                    where,
                    f"预警轮询 每 {interval} 分钟",
                    rounds * days,
                    1.0,
                    "预警列表全局共享，多地址会摊薄",
                )
            )
            est.lines.append(
                Line(
                    where,
                    "预警详情（列表命中才查）",
                    rounds * days,
                    0.05,
                    "按每 20 轮命中 1 次估，实际取决于有多少预警",
                )
            )

    # 4) 额度推送按会话存，不挂地址
    quota_rules = (session.get("quota") or {}).get("rules") or []
    for idx, rule in enumerate(quota_rules, 1):
        if not rule.get("enabled", True):
            continue
        runs = _rule_runs(rule, days, workdays)
        runs *= _day_ratio(schedule.normalize_days(rule.get("days")), workdays, days)
        est.lines.append(Line("本会话", schedule.describe_rule(rule, idx), runs, 2.0))

    est.lines = [line for line in est.lines if line.runs > 0]
    return est


def _money(value: float) -> str:
    if value <= 0:
        return "0.00"
    if value < 0.01:
        return "0.01"
    return f"{value:.2f}"


def render(est: Estimate, *, key_note: str = "") -> str:
    """明细 + 汇总 + 一句话结论。"""
    lines: list[str] = []
    if key_note:
        lines.append(f"凭据：{key_note}")
    if est.lines:
        lines.append(f"按 {est.month_days} 天一个月、其中 {est.workdays} 个工作日估算：")
        grouped: dict[str, list[Line]] = {}
        for line in est.lines:
            grouped.setdefault(line.where, []).append(line)
        for where, items in grouped.items():
            lines.append("")
            lines.append(f"【{where}】")
            for line in items:
                parts = f"  {line.what}　约 {line.runs:,.0f} 次/月"
                if line.per_run and line.per_run != 1:
                    parts += f" × {line.per_run:g} = {line.requests:,.0f} 次"
                lines.append(parts)
                if line.note:
                    lines.append(f"    （{line.note}）")
    else:
        lines.append("当前没有会消耗额度的设置：没有推送规则、雨雪和预警都没开。")

    total = est.total
    lines.append("")
    lines.append(f"合计：约 {total:,.0f} 次/月")
    if total <= FREE_QUOTA:
        lines.append(
            f"成本：{_money(est.cost)} 元/月（每月前 {FREE_QUOTA:,} 次免费，"
            f"还剩 {FREE_QUOTA - round(total):,} 次余量）"
        )
    else:
        over = total - FREE_QUOTA
        lines.append(
            f"成本：{_money(est.cost)} 元/月（前 {FREE_QUOTA:,} 次免费，"
            f"超出 {over:,.0f} 次按阶梯价计）"
        )
    lines.append(
        "含缓存折减（逐时 1 小时、日出日落 30 分钟、预警列表 5 分钟一次），"
        "不含手动发指令查询的部分。"
    )
    lines.append("单价按官方「天气和基础服务」档累进计算，控制台的价格计算器能对上这个数。")
    return "\n".join(lines)
