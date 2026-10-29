"""后台推送主循环：定时预报、雨雪提醒、灾害预警。

为什么是单循环而不是每条规则一个 task
--------------------------------------
规则由用户在对话里随时增删。每规则一个 asyncio.Task 意味着每次改动都要取消重建，
漏掉一个就是泄漏的 task 在后台继续推东西。单循环每 30 秒把所有会话过一遍，
增删规则只是改了个 list，没有生命周期要管。

三个推送源的成本控制
--------------------
- 定时规则：命中才取数，没命中零调用。
- 灾害预警：``/v7/warning/list?range=cn`` 一次调用覆盖全国所有地区（实测 597 个地区、
  17KB、130ms），所以无论多少用户，每个轮询周期只花 **1 次** 调用。只有城市真的在列表里
  才去查详情。
- 雨雪提醒：先用逐小时预报的降水概率预筛，概率为 0 就整轮跳过，不碰分钟降水接口。
  逐小时结果缓存 1 小时，10 分钟一轮也就 1 次调用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from astrbot.api import logger

from . import render, schedule
from .client import QWeatherClient, QWeatherError
from .keys import KeyStore
from .store import SubStore, level_rank

ClientFactory = Callable[[str], Awaitable[QWeatherClient]]
Sender = Callable[[str, str], Awaitable[bool]]

TICK_SECONDS = 30
SUN_CACHE_SECONDS = 1800
HOURLY_CACHE_SECONDS = 3600
WARNING_LIST_MIN_INTERVAL = 300  # 即使某会话配了 5 分钟，全局列表最快 5 分钟拉一次


def location_matches(city: dict[str, Any], ids: set[str]) -> bool:
    """判断城市是否命中预警列表。

    列表是**区县级**粒度：天津市级 ID ``101030100`` 不会出现在列表里，但它下辖区县
    ``101030200`` 会出现。所以大陆 9 位 ID 必须按前 6 位（省市）前缀匹配。
    台湾省等地区是 5 位字母数字 ID，只能精确匹配。
    """
    cid = str(city.get("id") or "")
    if not cid:
        return False
    if cid.isdigit() and len(cid) == 9:
        prefix = cid[:6]
        return any(i.startswith(prefix) for i in ids)
    return cid in ids


class Pusher:
    def __init__(
        self,
        store: SubStore,
        keys: KeyStore,
        client_factory: ClientFactory,
        send: Sender,
    ) -> None:
        self._store = store
        self._keys = keys
        self._client = client_factory
        self._send = send
        self._task: asyncio.Task | None = None
        self._running = False

        # 各类缓存的「上次刷新时间戳」
        self._sun_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._hourly_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._warning_ids: set[str] = set()
        self._warning_at = 0.0
        self._session_alert_at: dict[str, float] = {}
        self._session_precip_at: dict[str, float] = {}

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="qweather-pusher")

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 退出时不再抛
                pass

    async def _loop(self) -> None:
        logger.info("[和风天气] 推送循环已启动，tick=30s")
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 单次 tick 失败不能让循环死掉
                logger.warning(f"[和风天气] 推送循环异常：{type(exc).__name__}: {exc}")
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self) -> None:
        now = datetime.now()
        sessions = await self._store.all_sessions()
        if not sessions:
            return

        for umo, session in sessions.items():
            city = session.get("city")
            if not city or not city.get("lat"):
                continue

            await self._run_rules(umo, session, now)
            await self._run_alerts(umo, session, now)
            await self._run_precip(umo, session, now)

    # ---------------------------------------------------------------- 定时规则

    async def _run_rules(self, umo: str, session: dict[str, Any], now: datetime) -> None:
        rules = session.get("rules") or []
        if not rules:
            return
        fired = (session.get("state") or {}).get("fired") or {}
        city = session["city"]

        for rule in rules:
            if not rule.get("enabled", True):
                continue
            sun = None
            if rule.get("kind") == "solar":
                sun = await self._get_sun(umo, city)
                if not sun:
                    continue
            stamp = schedule.target_stamp(rule, now, sun)
            if not stamp or stamp != now.strftime("%Y-%m-%d %H:%M"):
                continue
            if fired.get(rule.get("id")) == stamp:
                continue

            text = await self._render_rule(umo, city, rule)
            if not text:
                continue
            if await self._send(umo, text):
                await self._store.mark_fired(umo, rule["id"], stamp)

    async def _render_rule(self, umo: str, city: dict[str, Any], rule: dict[str, Any]) -> str:
        content = rule.get("content") or ["now", "daily"]
        try:
            bundle = await self._fetch(umo, city, content)
        except QWeatherError as exc:
            logger.warning(f"[和风天气] 规则 {rule.get('id')} 取数失败：{exc}")
            return ""
        return render.render_report(
            city, content, bundle, moment=datetime.now(), title=rule.get("title") or "天气"
        )

    async def _fetch(self, umo: str, city: dict[str, Any], content: list[str]) -> dict[str, Any]:
        client = await self._client(umo)
        lat, lon = float(city["lat"]), float(city["lon"])
        need: set[str] = set()
        from .commands import CONTENT_FETCH

        for item in content:
            need.update(CONTENT_FETCH.get(item, ()))

        bundle: dict[str, Any] = {}
        if "now" in need:
            bundle["now"] = await client.current(lat, lon)
        if "daily" in need:
            bundle["daily"] = await client.daily(lat, lon, 4)
        if "hourly" in need:
            bundle["hourly"] = await client.hourly(lat, lon, 12)
        if "air" in need:
            try:
                bundle["air"] = await client.air_now(lat, lon)
            except QWeatherError:
                bundle["air"] = None
        if "indices" in need:
            try:
                bundle["indices"] = await client.indices(lat, lon, "1,2,3,5")
            except QWeatherError:
                bundle["indices"] = []
        if "alerts" in need:
            try:
                bundle["alerts"] = await client.warning_now(lat, lon)
            except QWeatherError:
                bundle["alerts"] = []
        if "minutely" in need:
            try:
                bundle["minutely"] = await client.minutely(lat, lon)
            except QWeatherError:
                bundle["minutely"] = None
        return bundle

    async def _get_sun(self, umo: str, city: dict[str, Any]) -> dict[str, str] | None:
        key = f"{city.get('id') or city.get('lat')}"
        cached = self._sun_cache.get(key)
        now_ts = datetime.now().timestamp()
        if cached and now_ts - cached[0] < SUN_CACHE_SECONDS:
            return cached[1]
        try:
            client = await self._client(umo)
            days = await client.daily(float(city["lat"]), float(city["lon"]), 1)
        except (QWeatherError, KeyError, ValueError):
            return None
        if not days:
            return None
        sun = {"sunrise": days[0].get("sunrise") or "", "sunset": days[0].get("sunset") or ""}
        self._sun_cache[key] = (now_ts, sun)
        return sun

    # ---------------------------------------------------------------- 灾害预警

    async def _run_alerts(self, umo: str, session: dict[str, Any], now: datetime) -> None:
        cfg = session.get("alert") or {}
        if not cfg.get("enabled"):
            return
        if not schedule.in_windows(now, cfg.get("windows") or []):
            return

        interval = max(5, int(cfg.get("poll_interval") or 60)) * 60
        last = self._session_alert_at.get(umo, 0.0)
        now_ts = now.timestamp()
        if now_ts - last < interval:
            return
        self._session_alert_at[umo] = now_ts

        ids = await self._ensure_warning_list(now_ts)
        if not ids:
            return

        city = session["city"]
        if not location_matches(city, ids):
            return

        # 命中才查详情——这一步才有额外调用
        try:
            client = await self._client(umo)
            alerts = await client.warning_now(float(city["lat"]), float(city["lon"]))
        except QWeatherError as exc:
            logger.warning(f"[和风天气] 预警详情查询失败：{exc}")
            return
        if not alerts:
            return

        min_level = cfg.get("min_level") or "yellow"
        allow_upgrade = bool(cfg.get("notify_on_upgrade", True))
        state = (session.get("state") or {}).get("alerts") or {}
        pushed = state.get(str(city.get("id"))) or {}

        for alert in alerts:
            if level_rank(alert.get("level")) < level_rank(min_level):
                continue
            alert_id = alert.get("id")
            if not alert_id:
                continue
            previous = pushed.get(alert_id)
            if previous:
                # 同一条已推过；只有等级升高才算需要再推
                if not (
                    allow_upgrade
                    and level_rank(alert.get("level")) > level_rank(previous.get("level"))
                ):
                    continue
            else:
                # 可能是同一条预警的修订版（related 指向旧 id）
                related = alert.get("related")
                if (
                    related
                    and related in pushed
                    and not (
                        allow_upgrade
                        and level_rank(alert.get("level"))
                        > level_rank(pushed[related].get("level"))
                    )
                ):
                    continue

            text = self._alert_text(city, alert)
            if await self._send(umo, text):
                await self._store.remember_alert(
                    umo,
                    str(city.get("id")),
                    alert_id,
                    {
                        "level": alert.get("level"),
                        "type": alert.get("type"),
                        "at": now.isoformat(timespec="minutes"),
                    },
                )

    async def _ensure_warning_list(self, now_ts: float) -> set[str]:
        if self._warning_at and now_ts - self._warning_at < WARNING_LIST_MIN_INTERVAL:
            return self._warning_ids
        # 用第一个能取到 key 的会话去拉，列表本身与凭据无关
        sessions = await self._store.all_sessions()
        for umo, session in sessions.items():
            if not (session.get("alert") or {}).get("enabled"):
                continue
            try:
                client = await self._client(umo)
                ids = await client.warning_list()
            except QWeatherError as exc:
                logger.warning(f"[和风天气] 预警列表拉取失败：{exc}")
                return self._warning_ids
            self._warning_ids = set(ids)
            self._warning_at = now_ts
            return self._warning_ids
        return self._warning_ids

    @staticmethod
    def _alert_text(city: dict[str, Any], alert: dict[str, Any]) -> str:
        lines = [
            f"⚠️ 【{city.get('name')}】{alert.get('level')}{alert.get('type_name')}预警",
            alert.get("title") or "",
            f"发布：{alert.get('sender') or '—'}　{alert.get('start')} 至 {alert.get('end')}",
        ]
        if alert.get("text"):
            lines.append("")
            lines.append(alert["text"])
        return "\n".join(x for x in lines if x)

    # ---------------------------------------------------------------- 雨雪提醒

    async def _run_precip(self, umo: str, session: dict[str, Any], now: datetime) -> None:
        cfg = session.get("precip") or {}
        if not cfg.get("enabled"):
            return
        if not (cfg.get("on_start") or cfg.get("on_stop")):
            return
        if not schedule.in_windows(now, cfg.get("windows") or []):
            return

        interval = max(5, int(cfg.get("poll_interval") or 10)) * 60
        now_ts = now.timestamp()
        if now_ts - self._session_precip_at.get(umo, 0.0) < interval:
            return

        city = session["city"]
        state = (session.get("state") or {}).get("precip") or {}
        was_wet = state.get(str(city.get("id"))) == "wet"

        # 预筛：先看逐小时降水概率。干燥且历史也干燥 → 整轮跳过，不碰分钟接口。
        if not was_wet:
            chance = await self._precip_chance(umo, city)
            if chance is not None and chance <= 0:
                self._session_precip_at[umo] = now_ts
                return

        self._session_precip_at[umo] = now_ts
        try:
            client = await self._client(umo)
            data = await client.minutely(float(city["lat"]), float(city["lon"]))
        except QWeatherError as exc:
            logger.warning(f"[和风天气] 分钟降水查询失败：{exc}")
            return

        points = data.get("points") or []
        if not points:
            return
        lead = max(1, int(cfg.get("lead_minutes") or 60) // 5)
        wet_now = points[0]["precip"] > 0

        start_at = next((i for i, p in enumerate(points) if p["precip"] > 0), None)
        dry_at = (
            next((i for i, p in enumerate(points) if p["precip"] <= 0), None) if wet_now else None
        )

        text = ""
        if (
            not was_wet
            and not wet_now
            and cfg.get("on_start")
            and start_at is not None
            and start_at <= lead
        ):
            text = self._precip_text(city, "start", start_at * 5, data.get("summary") or "")
        elif was_wet and wet_now and cfg.get("on_stop") and dry_at is not None and dry_at <= lead:
            text = self._precip_text(city, "stop", dry_at * 5, data.get("summary") or "")

        new_state = "wet" if wet_now else "dry"
        if text and await self._send(umo, text):
            await self._store.mark_precip_state(umo, str(city.get("id")), new_state)
            return
        if new_state != state.get(str(city.get("id"))):
            await self._store.mark_precip_state(umo, str(city.get("id")), new_state)

    async def _precip_chance(self, umo: str, city: dict[str, Any]) -> int | None:
        """未来 2 小时的最大降水概率。None 表示取不到（那就不要挡着，继续查）。"""
        key = str(city.get("id") or city.get("lat"))
        cached = self._hourly_cache.get(key)
        now_ts = datetime.now().timestamp()
        if cached and now_ts - cached[0] < HOURLY_CACHE_SECONDS:
            hours = cached[1]
        else:
            try:
                client = await self._client(umo)
                hours = await client.hourly(float(city["lat"]), float(city["lon"]), 3)
            except (QWeatherError, KeyError, ValueError):
                return None
            self._hourly_cache[key] = (now_ts, hours)
        probs = [h.get("precip_prob") or 0 for h in hours[:2]]
        return max(probs) if probs else None

    @staticmethod
    def _precip_text(city: dict[str, Any], kind: str, minutes: int, summary: str) -> str:
        if kind == "start":
            head = f"☔ 降水提醒：{city.get('name')} 预计 {minutes} 分钟后开始"
        else:
            head = f"🌤 降水提醒：{city.get('name')} 预计 {minutes} 分钟后停止"
        return f"{head}\n{summary}" if summary else head
