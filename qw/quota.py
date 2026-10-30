"""额度报告：账户余额 + 当天/前一天的请求量。

为什么单独一个模块
------------------
``/qweather 额度`` 查询和 ``/qweather 额度推送`` 定时推送必须说同一套话。渲染放在这儿，
两边都调它，改文案只改一处。

「当天 / 前一天」从哪来
----------------------
接口只给最近 24 小时，切不出完整的昨天，所以真正的日统计靠 :class:`qw.usage.UsageLedger`
每次拉数据时顺手攒下来。账本为空（刚装上、或者控制台权限还没开）时退化成
「最近 24 小时」的说法，不硬编一个假的「昨天」。

用哪套 key 不在这里决定：插件里的 ``_client_for`` 已经实现了「会话 key 优先、否则系统
默认 key」的解析顺序，这里只负责拿一个 ``QWeatherClient`` 把数字拼成人话。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .client import QWeatherClient, QWeatherError
from .usage import UsageLedger

# 控制台 API 默认是关的，两个接口都会 403。提示写一次，省得用户到处找。
PERMISSION_HINT = (
    "控制台 API 默认关闭。去「控制台 → 项目管理 → 点开项目 → 凭据 → 下滑到控制台权限」，"
    "勾选这几项后点保存：\n"
    "　· 财务和费用 → 允许访问财务汇总数据\n"
    "　· 指标和统计 → 允许访问请求量汇总统计\n"
    "　· 指标和统计 → 允许访问指定项目的请求量统计（下拉选你的项目）\n"
    "　· 指标和统计 → 允许访问指定凭据的请求量统计（下拉选你的凭据）\n"
    "只勾前两项不够——后两项没选具体对象时，统计接口仍会 403。"
)


def _thousands(value: int) -> str:
    return f"{value:,}"


def _api_line(row: dict, label: str, limit: int = 5) -> str:
    """按接口拆一行，取量最大的几个。"""
    merged: dict[str, int] = {}
    for field in ("ok", "err"):
        for api, count in (row.get(field) or {}).items():
            merged[api] = merged.get(api, 0) + int(count)
    if not merged:
        return ""
    top = sorted(merged.items(), key=lambda kv: -kv[1])[:limit]
    body = "　".join(f"{api} {_thousands(n)}" for api, n in top if n)
    return f"  {label}：{body}" if body else ""


def _day_block(ledger: UsageLedger | None, now: datetime) -> list[str]:
    """今天 + 昨天 + 近 7 天日均。账本没数据就返回空。"""
    if ledger is None:
        return []
    today = now.date()
    yesterday = today - timedelta(days=1)

    lines: list[str] = []
    if ledger.has(today):
        row = ledger.day(today)
        err = sum((row.get("err") or {}).values())
        lines.append(
            f"今天 {today:%m-%d}（截至 {now:%H:%M}）：{_thousands(row['total'])} 次"
            + (f"，其中失败 {_thousands(err)} 次" if err else "")
        )
    if ledger.has(yesterday):
        row = ledger.day(yesterday)
        err = sum((row.get("err") or {}).values())
        lines.append(
            f"昨天 {yesterday:%m-%d}：{_thousands(row['total'])} 次"
            + (f"，其中失败 {_thousands(err)} 次" if err else "")
        )
    elif ledger.has(today):
        lines.append("昨天：没有数据（插件昨天没运行过，接口本身也不给历史）")

    avg = ledger.average(7)
    if avg:
        lines.append(f"近 7 天日均：{_thousands(round(avg))} 次")

    if ledger.has(today):
        tail = _api_line(ledger.day(today), "今天按接口")
        if tail:
            lines.append(tail)
    return lines


async def build_report(
    client: QWeatherClient,
    *,
    key_note: str = "",
    ledger: UsageLedger | None = None,
    now: datetime | None = None,
) -> str:
    """把余额、应计费用、当天/前一天请求量拼成一段。取不到的接口列在最后。"""
    moment = now or datetime.now()
    lines: list[str] = []
    errors: list[str] = []

    if key_note:
        lines.append(f"凭据：{key_note}")

    try:
        finance = await client.finance_summary()
    except QWeatherError as exc:
        finance = None
        errors.append(f"财务汇总：{exc}")

    if finance:
        as_of = (finance.get("asOf") or "")[:16].replace("T", " ")
        currency = finance.get("currency") or ""
        lines.append(f"账户余额：{finance.get('balance')} {currency}（数据截至 {as_of} UTC）")
        charges = finance.get("accruedCharges") or {}
        lines.append(
            f"应计费用：本月 {charges.get('thisMonth', '-')}　"
            f"昨日 {charges.get('previousDay', '-')}"
        )
        plans = finance.get("availableResourcePlans") or []
        if plans:
            for plan in plans[:3]:
                lines.append(
                    f"资源包 {plan.get('billNumber')}：共 {plan.get('requests')} 次，"
                    f"已用 {plan.get('utilized')}"
                )
        else:
            lines.append("资源包：无（免费订阅额度不在资源包里，看不到剩余量）")
        bills = finance.get("pendingBills") or []
        unpaid = [b for b in bills if b.get("status") == "unpaid"]
        if unpaid:
            lines.append(f"待付账单：{len(unpaid)} 笔")

    try:
        stats = await client.metrics_stats()
    except QWeatherError as exc:
        stats = None
        errors.append(f"请求量统计：{exc}")

    if stats:
        # 先记账，再渲染：这样本次拉到的数字马上就能显示在「今天」里
        if ledger is not None:
            try:
                await ledger.merge(stats)
            except Exception as exc:  # noqa: BLE001 记账失败不能连累报告
                errors.append(f"用量记账：{type(exc).__name__}: {exc}")

        detail = _day_block(ledger, moment)
        if lines:
            lines.append("")
        if detail:
            lines.extend(detail)
        else:
            as_of = (stats.get("asOf") or "")[:16].replace("T", " ")
            success = stats.get("success") or []
            failure = stats.get("errors") or []
            total_ok = sum(sum(item.get("hours") or []) for item in success)
            total_err = sum(sum(item.get("hours") or []) for item in failure)
            lines.append(
                f"最近 24 小时（截至 {as_of} UTC）：成功 {total_ok} 次，失败 {total_err} 次"
            )

    if errors:
        if lines:
            lines.append("")
        lines.append("⚠️ 以下接口未取到：")
        lines.extend(f"  {e}" for e in errors)
        if any("403" in e for e in errors):
            lines.append("")
            lines.append(PERMISSION_HINT)

    return "\n".join(lines) if lines else "没取到任何数据。"


def yesterday_of(now: datetime | None = None) -> date:
    """给「只推前一天」之类的场景用；单独拎出来方便测试。"""
    return (now or datetime.now()).date() - timedelta(days=1)
