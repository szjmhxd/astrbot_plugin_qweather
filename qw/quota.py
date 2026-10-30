"""额度报告：账户余额 + 最近 24 小时请求量。

为什么单独一个模块
------------------
``/qweather 额度`` 查询和 ``/qweather 额度推送`` 定时推送必须说同一套话。渲染放在这儿，
两边都调它，改文案只改一处。

用哪套 key 不在这里决定：插件里的 ``_client_for`` 已经实现了「会话 key 优先、否则系统
默认 key」的解析顺序，这里只负责拿一个 ``QWeatherClient`` 把数字拼成人话。
"""

from __future__ import annotations

from .client import QWeatherClient, QWeatherError

# 控制台 API 默认是关的，两个接口都会 403。提示写一次，省得用户到处找。
PERMISSION_HINT = (
    "控制台 API 默认关闭。去「控制台 → 项目管理 → 点开凭据 → 下滑到控制台权限」，"
    "勾选「允许访问财务汇总数据」和「允许访问请求量统计」，保存后再试。"
)


async def build_report(client: QWeatherClient, *, key_note: str = "") -> str:
    """把余额、应计费用、资源包和 24 小时请求量拼成一段。取不到的接口列在最后。"""
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
        as_of = (stats.get("asOf") or "")[:16].replace("T", " ")
        success = stats.get("success") or []
        failure = stats.get("errors") or []
        total_ok = sum(sum(item.get("hours") or []) for item in success)
        total_err = sum(sum(item.get("hours") or []) for item in failure)
        if lines:
            lines.append("")
        lines.append(f"最近 24 小时（截至 {as_of} UTC）：成功 {total_ok} 次，失败 {total_err} 次")
        for item in sorted(success, key=lambda x: -sum(x.get("hours") or []))[:6]:
            count = sum(item.get("hours") or [])
            if count:
                lines.append(f"  {item.get('api')}：{count}")

    if errors:
        if lines:
            lines.append("")
        lines.append("⚠️ 以下接口未取到：")
        lines.extend(f"  {e}" for e in errors)
        if any("403" in e for e in errors):
            lines.append("")
            lines.append(PERMISSION_HINT)

    return "\n".join(lines) if lines else "没取到任何数据。"
