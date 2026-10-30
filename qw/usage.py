"""按本地日期记账的请求量账本。

为什么需要这本账
----------------
``/metrics/v1/stats`` 只给「最近 24 小时」，最后一条是 ``asOf`` 所在的那个小时。
直接拿它切「今天 / 昨天」会缺一大截：晚上 21 点查询时，窗口从昨天 21 点开始，
「昨天」只剩 3 个小时可见。所以插件自己按本地日期把每小时的数字攒起来。

为什么「取较大值」就够
----------------------
窗口是滚动的 24 小时，某一天在窗口里露出的小时数只会越来越多，直到整条滑出去。
已经过去的小时计数不会变，所以同一天的累计值单调不减——取较大值等于取最全的那次，
滑出窗口之后自然就定稿了。

局限性（要如实告诉用户）
------------------------
插件停机错过的那段时间是补不回来的：接口本身不给历史。所以账本标 ``partial``，
报告里也会写明「这天的数字来自 N 次采样」。

存储
----
``data/plugin_data/astrbot_plugin_qweather/usage.json``，只保留最近 60 天，
一天一个 dict，几十字节，不会长胖。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

KEEP_DAYS = 60


def _blank_day() -> dict[str, Any]:
    return {"ok": {}, "err": {}, "total": 0, "samples": 0}


def split_hours(stats: dict[str, Any], tz: timezone | None = None) -> dict[str, dict[str, Any]]:
    """把 ``hours`` 数组按**本地日期**分桶。

    ``hours`` 的语义以官方文档为准：长度 24，最后一条是 ``asOf`` 所在的整点小时
    （UTC）。所以第 ``i`` 条对应 ``asOf 整点 - (len-1-i) 小时``。
    """
    local_tz = tz or datetime.now().astimezone().tzinfo or timezone.utc
    as_of = str(stats.get("asOf") or "")
    try:
        moment = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # 先在 UTC 侧取整点，再转本地日期：中国是整小时偏移，这样不会把一小时切两天。
    last_hour = moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

    out: dict[str, dict[str, Any]] = {}
    for bucket, field in (("success", "ok"), ("errors", "err")):
        for item in stats.get(bucket) or []:
            api = str(item.get("api") or "?")
            hours = item.get("hours") or []
            span = len(hours)
            for idx, count in enumerate(hours):
                if not count:
                    continue
                slot = last_hour - timedelta(hours=span - 1 - idx)
                day = slot.astimezone(local_tz).date().isoformat()
                row = out.setdefault(day, _blank_day())
                row[field][api] = row[field].get(api, 0) + int(count)
    for row in out.values():
        row["total"] = sum(row["ok"].values()) + sum(row["err"].values())
    return out


class UsageLedger:
    """按天累计请求量。写盘是覆盖式的，但只用较大值合并，所以不会丢数据。"""

    def __init__(self, path: Path, keep_days: int = KEEP_DAYS) -> None:
        self._path = path
        self._keep = keep_days
        self._days: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 读写

    async def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._days = {}
            return
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[和风天气] 用量账本读不出来，先当空的：{exc}")
            self._days = {}
            return
        days = raw.get("days") if isinstance(raw, dict) else None
        self._days = {}
        if isinstance(days, dict):
            for key, value in days.items():
                if isinstance(value, dict):
                    row = _blank_day()
                    row.update({k: value[k] for k in row if k in value})
                    self._days[str(key)] = row

    async def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "days": self._days}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        tmp.replace(self._path)

    # ---------------------------------------------------------------- 记账

    async def merge(self, stats: dict[str, Any]) -> bool:
        """把一次 ``/metrics/v1/stats`` 的结果并进账本。返回是否真的变了。"""
        if not isinstance(stats, dict) or not (stats.get("success") or stats.get("errors")):
            return False
        fresh = split_hours(stats)
        if not fresh:
            return False

        async with self._lock:
            changed = False
            for day, row in fresh.items():
                old = self._days.get(day) or _blank_day()
                for field in ("ok", "err"):
                    merged: dict[str, int] = dict(old.get(field) or {})
                    for api, count in row[field].items():
                        if count > merged.get(api, 0):
                            merged[api] = count
                            changed = True
                    old[field] = merged
                total = sum(old["ok"].values()) + sum(old["err"].values())
                if total != old.get("total"):
                    old["total"] = total
                    changed = True
                old["samples"] = int(old.get("samples") or 0) + 1
                self._days[day] = old

            if changed:
                self._prune()
                await self._flush()
            return changed

    def _prune(self) -> None:
        cutoff = (date.today() - timedelta(days=self._keep)).isoformat()
        for key in [k for k in self._days if k < cutoff]:
            self._days.pop(key, None)

    # ---------------------------------------------------------------- 查询

    def day(self, which: date) -> dict[str, Any]:
        return self._days.get(which.isoformat()) or _blank_day()

    def has(self, which: date) -> bool:
        return which.isoformat() in self._days

    def recent_days(self, count: int = 7) -> list[tuple[date, dict[str, Any]]]:
        """最近 N 天里有记录的那些，按日期倒序。"""
        today = date.today()
        out: list[tuple[date, dict[str, Any]]] = []
        for back in range(count):
            day = today - timedelta(days=back)
            row = self._days.get(day.isoformat())
            if row:
                out.append((day, row))
        return out

    def average(self, count: int = 7) -> float:
        rows = [row["total"] for _, row in self.recent_days(count) if row.get("total")]
        return sum(rows) / len(rows) if rows else 0.0

    def history(self) -> dict[str, dict[str, Any]]:
        return self._days
