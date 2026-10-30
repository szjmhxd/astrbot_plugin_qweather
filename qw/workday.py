"""工作日日历：节假日 / 调休数据的获取、缓存与逐年轮换。

数据从哪来
----------
`NateScarlet/holiday-cn <https://github.com/NateScarlet/holiday-cn>`_ 每年一份 JSON，
结构是 ``days: [{name, date, isOffDay}]``：``isOffDay=true`` 是放假，``false`` 是调休上班。
它由国务院公告整理而来，也是「摸鱼薪资计算器」那类纯静态项目在用的数据源。

为什么要有「抓取窗口」而不是启动就抓
------------------------------------
节假日安排每年 10 月底到 11 月才公布。公布之前那条 JSON 要么 404，要么 ``days`` 是空数组
（2027.json 现在就是 200 + 空数组）。所以判断「有没有」不能看 HTTP 状态码，只能看 ``days``
是否非空。策略因此是：**从次年数据开放日（默认 11-01）起每天 0 点试一次**，拿到非空的
``days`` 就永久停手；没拿到就第二天再来。加载时如果已经过了开放日，立刻补一次，不必等到
第二天 0 点。

为什么文件不会越长越大
----------------------
每年 2 月 1 日（可配）清理一次旧年份，但**当前年和次年永远保留**——它们是「今天是不是
工作日」要用的数据。这样文件里最多三四年、几万字节。保护当前年和次年还有个好处：即使
用户把清理日期改到年中，也不会把正在用的数据删掉。

判断规则
--------
1. 在 ``makeup``（调休上班）里 → 工作日，哪怕这天是周六；
2. 在 ``holidays``（放假）里 → 休息日，哪怕这天是周三；
3. 其余按星期几算：周一到周五工作日，周六周日休息。

数据缺失的年份退化成第 3 条（纯周末规则），并把年份标成未就绪，查询指令会提示。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

# 主源 + 两个备源。国内访问 jsdelivr 偶尔抽风，raw.githubusercontent 兜底。
DEFAULT_SOURCE = "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
FALLBACK_SOURCES = (
    "https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
)

DEFAULT_FETCH_DATE = (11, 1)
DEFAULT_CALENDAR_DATE = (11, 1)
DEFAULT_CLEANUP_DATE = (2, 1)

_MIN_DAYS = 1  # days 少于这个数就是「还没公布」
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$")


def parse_month_day(text: object, default: tuple[int, int]) -> tuple[int, int]:
    """``"11-01"`` / ``"11/1"`` / ``"11月1日"`` → ``(11, 1)``。写坏了回落默认值。"""
    raw = str(text or "").strip()
    match = _MONTH_DAY_RE.match(raw)
    if not match:
        return default
    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return default
    try:
        date(2000, month, day)  # 用闰年试一下，2-29 也算合法
    except ValueError:
        return default
    return month, day


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v or "") for k, v in value.items() if _DATE_RE.match(str(k))}


def _date_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(v) for v in value if _DATE_RE.match(str(v))})


class WorkdayCalendar:
    """一年的工作日 / 休息日判断，外加数据自身的维护周期。"""

    def __init__(
        self,
        path: Path,
        *,
        source: str = DEFAULT_SOURCE,
        fetch_date: tuple[int, int] = DEFAULT_FETCH_DATE,
        calendar_date: tuple[int, int] = DEFAULT_CALENDAR_DATE,
        cleanup_date: tuple[int, int] = DEFAULT_CLEANUP_DATE,
        timeout: float = 10.0,
    ) -> None:
        self._path = path
        self._source = source.strip() or DEFAULT_SOURCE
        self._fetch_date = fetch_date
        self._calendar_date = calendar_date
        self._cleanup_date = cleanup_date
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._years: dict[int, dict[str, Any]] = {}
        self._attempts: dict[int, str] = {}
        self._loaded = False
        self._pending: asyncio.Task | None = None

    # ---------------------------------------------------------------- 读写

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> None:
        async with self._lock:
            self._loaded = True
            if not self._path.exists():
                return
            try:
                raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                parsed = json.loads(raw)
            except (OSError, ValueError) as exc:
                logger.warning(f"[和风天气] 工作日日历读取失败，按空数据处理：{exc}")
                return
            if not isinstance(parsed, dict):
                return
            years = parsed.get("years")
            for key, value in (years or {}).items() if isinstance(years, dict) else ():
                if not isinstance(value, dict):
                    continue
                try:
                    year = int(key)
                except (TypeError, ValueError):
                    continue
                self._years[year] = self._repair_year(year, value)
            attempts = parsed.get("attempts")
            for key, value in (attempts or {}).items() if isinstance(attempts, dict) else ():
                try:
                    self._attempts[int(key)] = str(value)
                except (TypeError, ValueError):
                    continue

    @staticmethod
    def _blank(year: int) -> dict[str, Any]:
        return {
            "year": year,
            "status": "",
            "source": "",
            "fetched_at": "",
            "holidays": {},
            "makeup": {},
            "weekends": [],
            "rests": [],
            "built_at": "",
            "updated_at": "",
        }

    @classmethod
    def _repair_year(cls, year: int, item: dict[str, Any]) -> dict[str, Any]:
        blank = cls._blank(year)
        blank["status"] = "ready" if item.get("status") == "ready" else ""
        for key in ("source", "fetched_at", "built_at", "updated_at"):
            blank[key] = str(item.get(key) or "")
        blank["holidays"] = _str_map(item.get("holidays"))
        blank["makeup"] = _str_map(item.get("makeup"))
        blank["weekends"] = _date_list(item.get("weekends"))
        blank["rests"] = _date_list(item.get("rests"))
        if blank["holidays"]:
            blank["status"] = "ready"
        return blank

    async def _flush(self) -> None:
        payload = json.dumps(
            {
                "version": 1,
                "years": {str(k): v for k, v in sorted(self._years.items())},
                "attempts": {str(k): v for k, v in sorted(self._attempts.items())},
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        tmp = self._path.with_suffix(".tmp")

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

        await asyncio.to_thread(_write)

    # ---------------------------------------------------------------- 判断

    def year_data(self, year: int) -> dict[str, Any]:
        return dict(self._years.get(year) or self._blank(year))

    def has_data(self, year: int) -> bool:
        return (self._years.get(year) or {}).get("status") == "ready"

    def is_workday(self, day: date) -> bool:
        item = self._years.get(day.year) or {}
        key = day.isoformat()
        if key in (item.get("makeup") or {}):
            return True
        if key in (item.get("holidays") or {}):
            return False
        return day.weekday() < 5

    def is_rest(self, day: date) -> bool:
        return not self.is_workday(day)

    def next_workday(self, day: date, *, horizon: int = 40) -> date | None:
        cursor = day + timedelta(days=1)
        for _ in range(horizon):
            if self.is_workday(cursor):
                return cursor
            cursor += timedelta(days=1)
        return None

    def next_rest(self, day: date, *, horizon: int = 40) -> date | None:
        cursor = day + timedelta(days=1)
        for _ in range(horizon):
            if self.is_rest(cursor):
                return cursor
            cursor += timedelta(days=1)
        return None

    def day_note(self, day: date) -> str:
        """这一天的「为什么」，给查询指令用：元旦 / 调休上班 / 周末 / 普通工作日。"""
        item = self._years.get(day.year) or {}
        key = day.isoformat()
        if key in (item.get("holidays") or {}):
            return f"{item['holidays'][key]}放假"
        if key in (item.get("makeup") or {}):
            return f"{item['makeup'][key]}调休上班"
        return "周末" if day.weekday() >= 5 else "普通工作日"

    def years(self) -> list[int]:
        return sorted(self._years)

    def status_line(self, year: int) -> str:
        item = self._years.get(year) or {}
        if item.get("status") == "ready":
            got = (item.get("fetched_at") or "")[:10]
            return f"{year} 年节假日数据：已就绪（{got} 获取，来源 holiday-cn）"
        return f"{year} 年节假日数据：未就绪，按周一至周五估算"

    def schedule_text(self) -> str:
        def _fmt(value: tuple[int, int]) -> str:
            return f"{value[0]:02d}-{value[1]:02d}"

        return (
            f"次年数据开放日 {_fmt(self._fetch_date)}，"
            f"次年日历生成 {_fmt(self._calendar_date)}，"
            f"旧数据清理 {_fmt(self._cleanup_date)}"
        )

    # ---------------------------------------------------------------- 日历生成

    def build_weekends(self, year: int, *, today: date | None = None) -> bool:
        """算出这一年的全部周六周日。纯本地计算，不需要网络，重复调用是幂等的。"""
        item = self._years.setdefault(year, self._blank(year))
        if item.get("weekends"):
            self._refresh_rests(year)
            return False
        cursor = date(year, 1, 1)
        end = date(year, 12, 31)
        weekends: list[str] = []
        while cursor <= end:
            if cursor.weekday() >= 5:
                weekends.append(cursor.isoformat())
            cursor += timedelta(days=1)
        item["weekends"] = weekends
        item["built_at"] = (today or date.today()).isoformat()
        self._refresh_rests(year)
        return True

    def _refresh_rests(self, year: int) -> None:
        """实际休息日 = 周末 - 调休上班 + 节假日。没有节假日数据就先不算。"""
        item = self._years.get(year)
        if not item or item.get("status") != "ready":
            return
        makeup = set(item.get("makeup") or {})
        rests = {d for d in item.get("weekends") or [] if d not in makeup}
        rests.update(item.get("holidays") or {})
        item["rests"] = sorted(rests)

    # ---------------------------------------------------------------- 抓取

    async def _get_json(self, url: str) -> Any:
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers={"User-Agent": "astrbot-plugin-qweather"}
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
            logger.debug(f"[和风天气] 节假日数据源不可用 {url}：{type(exc).__name__}: {exc}")
            return None

    async def fetch_year(self, year: int, *, today: date | None = None) -> bool:
        """拉一年的节假日。``days`` 为空视为「还没公布」，返回 False 让调用方明天再试。"""
        for template in (self._source, *FALLBACK_SOURCES):
            url = template.format(year=year)
            payload = await self._get_json(url)
            if not isinstance(payload, dict):
                continue
            days = payload.get("days")
            if not isinstance(days, list):
                continue
            if len(days) < _MIN_DAYS:
                # 源可达、但这一年还没公布。不用再试备源——它们读的是同一份数据，
                # 换源只会多花两次超时。
                return False

            holidays: dict[str, str] = {}
            makeup: dict[str, str] = {}
            for entry in days:
                if not isinstance(entry, dict):
                    continue
                raw_date = str(entry.get("date") or "").strip()
                if not _DATE_RE.match(raw_date) or not raw_date.startswith(f"{year}-"):
                    continue
                name = str(entry.get("name") or "").strip()[:12]
                if entry.get("isOffDay"):
                    holidays[raw_date] = name
                else:
                    makeup[raw_date] = name
            if not holidays:
                continue

            item = self._years.setdefault(year, self._blank(year))
            item.update(
                {
                    "status": "ready",
                    "source": url,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "holidays": holidays,
                    "makeup": makeup,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self.build_weekends(year, today=today)
            self._refresh_rests(year)
            self._attempts[year] = (today or date.today()).isoformat()
            await self._flush()
            logger.info(
                f"[和风天气] 已获取 {year} 年节假日："
                f"{len(holidays)} 天放假、{len(makeup)} 天调休上班"
            )
            return True
        return False

    # ---------------------------------------------------------------- 维护周期

    def kick(self, now: datetime | None = None, *, startup: bool = False) -> None:
        """非阻塞入口：真正需要抓数据时才起后台任务，不拖慢 30 秒一次的推送循环。

        故意写成同步函数——调用方忘了 ``await`` 也不会静默丢任务。
        """
        if self._pending is not None and not self._pending.done():
            return
        try:
            self._pending = asyncio.create_task(self._guard(now, startup=startup))
        except RuntimeError:  # 没有事件循环（离线测试）时直接忽略
            self._pending = None

    async def _guard(self, now: datetime | None, *, startup: bool) -> None:
        try:
            await self.maintain(now, startup=startup)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 日历维护失败不能影响推送
            logger.warning(f"[和风天气] 工作日日历维护失败：{type(exc).__name__}: {exc}")

    async def maintain(self, now: datetime | None = None, *, startup: bool = False) -> list[str]:
        """跑一遍维护流程，返回这次做了什么（日志和测试用）。

        每次 tick 都会调用，但绝大多数时候只做内存判断，只有跨到「今天还没试过」才会发请求。
        """
        now = now or datetime.now()
        today = now.date()
        year = today.year
        events: list[str] = []

        async with self._lock:
            if not self._loaded:
                self._loaded = True

            # 1) 当年日历：缺就补，缺数据也先把周末算出来
            if not (self._years.get(year) or {}).get("weekends"):
                if self.build_weekends(year, today=today):
                    events.append(f"生成 {year} 年周末日历")
            if not self.has_data(year):
                state = await self._try_fetch(year, today, startup=startup)
                if state == "ok":
                    events.append(f"获取 {year} 年节假日")
                elif state == "pending":
                    events.append(f"{year} 年节假日还没拿到，明天再试")

            # 2) 次年：过了开放日才动，且数据没到手之前每天 0 点试一次
            target = year + 1
            if today >= self._date_in(year, self._fetch_date):
                if not self.has_data(target):
                    state = await self._try_fetch(target, today, startup=startup)
                    if state == "ok":
                        events.append(f"获取 {target} 年节假日")
                    elif state == "pending":
                        events.append(f"{target} 年节假日还没公布，明天再试")
            # 3) 次年周末日历：到日子生成一次（纯本地计算）
            if today >= self._date_in(year, self._calendar_date):
                if not (self._years.get(target) or {}).get("weekends"):
                    if self.build_weekends(target, today=today):
                        events.append(f"生成 {target} 年周末日历")

            # 4) 清理旧年份
            removed = await self._cleanup(today)
            if removed:
                events.append("清理 " + "、".join(str(y) for y in removed) + " 年数据")
            if events:
                await self._flush()

        if events:
            logger.info("[和风天气] 工作日日历：" + "；".join(events))
        return events

    async def _try_fetch(self, year: int, today: date, *, startup: bool) -> str:
        """一天最多试一次。返回 ``"ok"`` / ``"pending"``（还没公布）/ ``""``（今天试过了）。"""
        if not startup and self._attempts.get(year) == today.isoformat():
            return ""
        self._attempts[year] = today.isoformat()
        if await self.fetch_year(year, today=today):
            return "ok"
        # 记下尝试日期，明天再试
        logger.debug(f"[和风天气] {year} 年节假日还没公布，明天再试")
        return "pending"

    @staticmethod
    def _date_in(year: int, month_day: tuple[int, int]) -> date:
        try:
            return date(year, month_day[0], month_day[1])
        except ValueError:
            return date(year, 11, 1)

    async def _cleanup(self, today: date) -> list[int]:
        mark = self._date_in(today.year, self._cleanup_date)
        if today < mark:
            return []
        # 当前年和次年必须留着，它们是判断今天、明天用不用推的依据
        keep = {today.year, today.year + 1}
        removed = [y for y in self._years if y not in keep and y < today.year]
        if not removed:
            return []
        for year in removed:
            self._years.pop(year, None)
            self._attempts.pop(year, None)
        await self._flush()
        return removed
