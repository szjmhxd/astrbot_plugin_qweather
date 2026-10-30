"""订阅配置与去重状态的持久化。

一个 ``subs.json`` 装下所有会话的配置。**一个会话可以有多个地址**，每个地址有各自
独立的推送规则、雨雪开关、预警设置和去重状态：

    {
      "sessions": {
        "<unified_msg_origin>": {
          "active": "L3f2a",                      ← 当前操作的地址
          "locations": [
            {
              "lid": "L3f2a",                     ← 地址 id，前缀 L 避免和序号混淆
              "name": "公司",                      ← 用户起的别名，可空
              "city": {"name","id","adm1","adm2","lat","lon","coord","level"},
              "rules":  [ {规则...} ],
              "precip": {"enabled","windows","on_start","on_stop",...},
              "alert":  {"enabled","poll_interval","min_level",...},
              "state":  {"alerts":{}, "precip":"dry", "fired":{rule_id:"2026-10-29T07:30"}}
            }
          ]
        }
      }
    }

为什么状态要落盘
----------------
去重状态如果只在内存，插件重启后会把当前所有生效预警重推一遍——用户会收到一串重复通知。
``fired`` 记的是「这条规则上次在哪个 HH:MM 触发过」，同样必须落盘，否则刚好在 07:30 重启
会重复推送早报。

为什么地址 id 要带 ``L`` 前缀
------------------------------
地址既能用 id 引用，也能用 1-based 序号引用。纯十六进制 id 有概率全是数字，会被当成序号，
因此统一加 ``L`` 前缀，序号和 id 两个命名空间永不重叠。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

MAX_SESSIONS = 2000
MAX_LOCATIONS = 20  # 每个会话最多几个地址。再多就该考虑是不是该按群拆分了

DEFAULT_PRECIP: dict[str, Any] = {
    "enabled": False,
    # days: all / workday / restday，只在对应日子生效（工作日判断见 qw.workday）
    "windows": [{"start": "07:00", "end": "23:00", "days": "all"}],
    "on_start": True,
    "on_stop": True,
    "lead_minutes": 60,
    "poll_interval": 10,
}

DEFAULT_ALERT: dict[str, Any] = {
    "enabled": False,
    "poll_interval": 60,
    "min_level": "yellow",
    "windows": [{"start": "07:00", "end": "23:00", "days": "all"}],
    "notify_on_upgrade": True,
}

DEFAULT_RULE: dict[str, Any] = {
    "kind": "clock",
    "enabled": True,
    "at": "07:30",
    "weekdays": [],
    "monthdays": [],
    "hours": [],
    "minutes": [0],
    "solar": "sunrise",
    "offset": 0,
    "content": ["now", "daily"],
    "days": "all",
    "title": "",
}

# 额度推送的规则：触发方式与天气规则同一套，但没有「内容」可选——额度报告永远是那一份。
DEFAULT_QUOTA_RULE: dict[str, Any] = {
    "kind": "clock",
    "enabled": True,
    "at": "09:00",
    "weekdays": [],
    "monthdays": [],
    "hours": [],
    "minutes": [0],
    "solar": "sunrise",
    "offset": 0,
    "days": "all",
    "title": "",
}

LEVEL_ORDER = {
    "blue": 1,
    "yellow": 2,
    "orange": 3,
    "red": 4,
    "白": 1,
    "蓝": 1,
    "黄": 2,
    "橙": 3,
    "红": 4,
}
LEVEL_ALIAS = {
    "blue": "blue",
    "蓝": "blue",
    "yellow": "yellow",
    "黄": "yellow",
    "orange": "orange",
    "橙": "orange",
    "red": "red",
    "红": "red",
}


def normalize_level(text: str) -> str | None:
    return LEVEL_ALIAS.get((text or "").strip().lower())


def level_rank(text: str) -> int:
    return LEVEL_ORDER.get((text or "").strip().lower(), 0)


def new_rule_id() -> str:
    # 前缀 r 是必需的：纯十六进制 id 有约 6% 概率全是数字，会被 find_rule 当成序号
    return "r" + secrets.token_hex(3)


def new_lid() -> str:
    return "L" + secrets.token_hex(2)


def _copy(obj: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(obj))


def blank_location(city: dict[str, Any] | None = None, name: str = "") -> dict[str, Any]:
    return {
        "lid": new_lid(),
        "name": name,
        "city": normalize_city(city or {}),
        "rules": [],
        "precip": _copy(DEFAULT_PRECIP),
        "alert": _copy(DEFAULT_ALERT),
        "state": {"alerts": {}, "precip": "dry", "fired": {}},
    }


def blank_quota() -> dict[str, Any]:
    return {"rules": [], "state": {"fired": {}}}


def blank_session() -> dict[str, Any]:
    # 额度是「按会话」的，不挂在地址上：一个会话一套 key，余额和请求量也只跟这套 key 有关
    return {"active": "", "locations": [], "quota": blank_quota()}


def normalize_windows(value: Any) -> list[dict[str, str]]:
    """把时段列表洗成统一结构，顺带兼容没有 ``days`` 字段的老数据。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start") or "").strip()
        end = str(item.get("end") or "").strip()
        if not start or not end:
            continue
        days = str(item.get("days") or "all").strip() or "all"
        out.append({"start": start, "end": end, "days": days})
    return out


def normalize_city(city: dict[str, Any]) -> dict[str, Any]:
    """只留认识的字段，避免把接口里的大对象原样存下来。"""
    if not city:
        return {}
    return {
        "name": city.get("name") or "",
        "id": str(city.get("id") or ""),
        "adm1": city.get("adm1") or "",
        "adm2": city.get("adm2") or "",
        "lat": float(city.get("lat") or 0),
        "lon": float(city.get("lon") or 0),
        # True 表示用户直接给的是坐标，lat/lon 是精度到 2 位的原始值，
        # 不是行政区中心点；显示时要标出来，否则用户会以为定位到了区县中心。
        "coord": bool(city.get("coord")),
        # city / district：决定预警是按前缀匹配还是精确匹配，见 pushers。
        "level": "district" if city.get("coord") else str(city.get("level") or "city"),
    }


def pick_rule(rules: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """``ref`` 可以是 rule id，也可以是 1-based 序号。先按 id 找，再按序号。"""
    by_id = next((r for r in rules if r.get("id") == ref), None)
    if by_id is not None:
        return by_id
    if str(ref or "").isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(rules):
            return rules[idx]
    return None


class SubStore:
    """会话级订阅配置。读多写少，内存为主、写穿到磁盘。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ---------------------------------------------------------------- 读写

    async def load(self) -> None:
        async with self._lock:
            self._loaded = True
            if not self._path.exists():
                self._data = {}
                return
            try:
                raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                parsed = json.loads(raw)
                sessions = parsed.get("sessions") if isinstance(parsed, dict) else None
                self._data = {
                    str(k): self._repair(v)
                    for k, v in (sessions or {}).items()
                    if isinstance(v, dict)
                }
            except (OSError, ValueError, json.JSONDecodeError):
                self._data = {}

    @classmethod
    def _repair(cls, session: dict[str, Any]) -> dict[str, Any]:
        """兼容旧版本 / 手工编辑过的文件：缺字段补默认，脏字段丢弃。

        v1.2 之前是「一个会话一个城市」，字段直接平铺在 session 上。读到这种结构就
        包成一个地址，用户不用重新配。
        """
        raw_locations = session.get("locations")
        if not isinstance(raw_locations, list):
            migrated = cls._migrate_v1(session)
            return migrated

        base = blank_session()
        base["quota"] = cls._repair_quota(session.get("quota"))
        for item in raw_locations:
            if not isinstance(item, dict):
                continue
            loc = cls._repair_location(item)
            if loc["city"]:
                base["locations"].append(loc)
        if not base["locations"]:
            return base
        active = str(session.get("active") or "")
        if not any(loc["lid"] == active for loc in base["locations"]):
            active = base["locations"][0]["lid"]
        base["active"] = active
        return base

    @classmethod
    def _migrate_v1(cls, session: dict[str, Any]) -> dict[str, Any]:
        base = blank_session()
        base["quota"] = cls._repair_quota(session.get("quota"))
        city = session.get("city")
        if not isinstance(city, dict) or not city:
            return base
        loc = blank_location(city)
        rules = session.get("rules")
        if isinstance(rules, list):
            for item in rules:
                if isinstance(item, dict):
                    loc["rules"].append(cls._repair_rule(item))
        for section, defaults in (("precip", DEFAULT_PRECIP), ("alert", DEFAULT_ALERT)):
            cfg = session.get(section)
            if isinstance(cfg, dict):
                merged = dict(defaults)
                merged.update({k: v for k, v in cfg.items() if k in defaults})
                loc[section] = merged
        state = session.get("state")
        if isinstance(state, dict):
            # 旧结构是 {location_id: {...}}，现在地址本身就是隔离维度，取第一个即可
            alerts: dict[str, Any] = {}
            for bucket in (state.get("alerts") or {}).values():
                if isinstance(bucket, dict):
                    alerts.update(bucket)
            precip = state.get("precip") or {}
            wet = "wet" in precip.values()
            fired = state.get("fired") if isinstance(state.get("fired"), dict) else {}
            loc["state"] = {"alerts": alerts, "precip": "wet" if wet else "dry", "fired": fired}
        base["locations"] = [loc]
        base["active"] = loc["lid"]
        return base

    @staticmethod
    def _repair_rule(
        item: dict[str, Any], defaults: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        template = defaults or DEFAULT_RULE
        rule = dict(template)
        rule.update({k: v for k, v in item.items() if k in template or k == "id"})
        if not rule.get("id"):
            rule["id"] = new_rule_id()
        return rule

    @classmethod
    def _repair_quota(cls, raw: Any) -> dict[str, Any]:
        base = blank_quota()
        if not isinstance(raw, dict):
            return base
        rules = raw.get("rules")
        if isinstance(rules, list):
            base["rules"] = [
                cls._repair_rule(item, DEFAULT_QUOTA_RULE)
                for item in rules
                if isinstance(item, dict)
            ]
        state = raw.get("state")
        if isinstance(state, dict) and isinstance(state.get("fired"), dict):
            base["state"]["fired"] = {
                str(k): str(v) for k, v in state["fired"].items() if isinstance(v, str)
            }
        return base

    @classmethod
    def _repair_location(cls, item: dict[str, Any]) -> dict[str, Any]:
        loc = blank_location()
        loc["lid"] = str(item.get("lid") or new_lid())
        loc["name"] = str(item.get("name") or "")[:20]
        loc["city"] = normalize_city(item.get("city") if isinstance(item.get("city"), dict) else {})
        rules = item.get("rules")
        if isinstance(rules, list):
            loc["rules"] = [cls._repair_rule(r) for r in rules if isinstance(r, dict)]
        for section, defaults in (("precip", DEFAULT_PRECIP), ("alert", DEFAULT_ALERT)):
            cfg = item.get(section)
            if isinstance(cfg, dict):
                merged = dict(defaults)
                merged.update({k: v for k, v in cfg.items() if k in defaults})
                merged["windows"] = normalize_windows(merged.get("windows"))
                loc[section] = merged
        state = item.get("state")
        if isinstance(state, dict):
            loc["state"] = {
                "alerts": state.get("alerts") if isinstance(state.get("alerts"), dict) else {},
                "precip": "wet" if state.get("precip") == "wet" else "dry",
                "fired": state.get("fired") if isinstance(state.get("fired"), dict) else {},
            }
        return loc

    async def _ensure(self) -> None:
        if not self._loaded:
            await self.load()

    async def _flush(self) -> None:
        payload = json.dumps({"sessions": self._data}, ensure_ascii=False, indent=2)
        tmp = self._path.with_suffix(".tmp")

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

        await asyncio.to_thread(_write)

    # ---------------------------------------------------------------- 会话

    async def session(self, umo: str, *, create: bool = False) -> dict[str, Any] | None:
        await self._ensure()
        async with self._lock:
            item = self._data.get(umo)
            if item is None and create:
                item = blank_session()
                self._data[umo] = item
                if len(self._data) > MAX_SESSIONS:
                    self._trim()
                await self._flush()
            return _copy(item) if item is not None else None

    def _trim(self) -> None:
        overflow = len(self._data) - MAX_SESSIONS
        if overflow <= 0:
            return
        # 空会话才回收：只配了额度推送、没有地址的会话不能误删
        empties = [
            k
            for k, v in self._data.items()
            if not v.get("locations") and not ((v.get("quota") or {}).get("rules"))
        ]
        for key in empties[:overflow]:
            self._data.pop(key, None)

    async def all_sessions(self) -> dict[str, dict[str, Any]]:
        await self._ensure()
        async with self._lock:
            return _copy(self._data)

    async def _mutate(self, umo: str, fn) -> Any:
        await self._ensure()
        async with self._lock:
            item = self._data.get(umo)
            if item is None:
                item = blank_session()
                self._data[umo] = item
            result = fn(item)
            await self._flush()
            return result

    # ---------------------------------------------------------------- 地址

    @staticmethod
    def _pick(locations: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
        """``ref`` 支持地址 id、别名、1-based 序号。id 优先，其次别名，最后序号。"""
        ref = str(ref or "").strip()
        if not ref:
            return None
        for loc in locations:
            if loc.get("lid") == ref:
                return loc
        for loc in locations:
            if (loc.get("name") or "") == ref:
                return loc
        if ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(locations):
                return locations[idx]
        return None

    async def locations(self, umo: str) -> list[dict[str, Any]]:
        item = await self.session(umo)
        return list((item or {}).get("locations") or [])

    async def location(self, umo: str, ref: str) -> dict[str, Any] | None:
        """按 id / 别名 / 序号取一个地址的快照。"""
        return self._pick(await self.locations(umo), ref)

    async def active(self, umo: str) -> dict[str, Any] | None:
        """当前地址。没有显式设置时退回第一个。"""
        item = await self.session(umo)
        if not item:
            return None
        locations = item.get("locations") or []
        if not locations:
            return None
        active = item.get("active") or ""
        return self._pick(locations, active) or locations[0]

    async def set_active(self, umo: str, ref: str) -> dict[str, Any] | None:
        return await self._mutate(umo, lambda item: self._do_set_active(item, ref))

    @classmethod
    def _do_set_active(cls, item: dict[str, Any], ref: str) -> dict[str, Any] | None:
        loc = cls._pick(item["locations"], ref)
        if loc is None:
            return None
        item["active"] = loc["lid"]
        return _copy(loc)

    async def add_location(
        self, umo: str, city: dict[str, Any], name: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        """新增地址并设为当前。返回 ``(地址, 错误)``；同地点重名会被挡住。"""

        def _do(item: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
            if len(item["locations"]) >= MAX_LOCATIONS:
                return None, f"最多 {MAX_LOCATIONS} 个地址，先删掉不用的。"
            normalized = normalize_city(city)
            for existing in item["locations"]:
                old = existing.get("city") or {}
                if (
                    old.get("id")
                    and old.get("id") == normalized.get("id")
                    and abs(float(old.get("lat") or 0) - normalized["lat"]) < 0.005
                    and abs(float(old.get("lon") or 0) - normalized["lon"]) < 0.005
                ):
                    item["active"] = existing["lid"]
                    return _copy(existing), "already"
            loc = blank_location(city, name[:20])
            item["locations"].append(loc)
            item["active"] = loc["lid"]
            return _copy(loc), ""

        return await self._mutate(umo, _do)

    async def delete_location(self, umo: str, ref: str) -> dict[str, Any] | None:
        def _do(item: dict[str, Any]) -> dict[str, Any] | None:
            loc = self._pick(item["locations"], ref)
            if loc is None:
                return None
            was_active = item.get("active") == loc["lid"]
            item["locations"] = [x for x in item["locations"] if x["lid"] != loc["lid"]]
            if was_active:
                item["active"] = item["locations"][0]["lid"] if item["locations"] else ""
            elif item.get("active") not in {x["lid"] for x in item["locations"]}:
                item["active"] = item["locations"][0]["lid"] if item["locations"] else ""
            return _copy(loc)

        return await self._mutate(umo, _do)

    async def update_location(
        self, umo: str, ref: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        def _do(item: dict[str, Any]) -> dict[str, Any] | None:
            loc = self._pick(item["locations"], ref)
            if loc is None:
                return None
            if "name" in patch:
                loc["name"] = str(patch["name"] or "")[:20]
            if isinstance(patch.get("city"), dict):
                loc["city"] = normalize_city(patch["city"])
            return _copy(loc)

        return await self._mutate(umo, _do)

    # ---------------------------------------------------------------- 规则

    async def add_rule(self, umo: str, lid: str, rule: dict[str, Any]) -> dict[str, Any] | None:
        prepared = dict(DEFAULT_RULE)
        prepared.update(rule)
        prepared["id"] = new_rule_id()

        def _do(item: dict[str, Any]) -> dict[str, Any] | None:
            loc = self._pick(item["locations"], lid)
            if loc is None:
                return None
            loc["rules"].append(prepared)
            return dict(prepared)

        return await self._mutate(umo, _do)

    async def list_rules(self, umo: str, lid: str) -> list[dict[str, Any]]:
        loc = await self.location(umo, lid)
        return list((loc or {}).get("rules") or [])

    async def find_rule(self, umo: str, lid: str, ref: str) -> dict[str, Any] | None:
        """``ref`` 可以是 rule id，也可以是 1-based 序号。先按 id 找，再按序号。"""
        return pick_rule(await self.list_rules(umo, lid), ref)

    async def update_rule(
        self, umo: str, lid: str, rule_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        def _do(item: dict[str, Any]) -> dict[str, Any] | None:
            loc = self._pick(item["locations"], lid)
            if loc is None:
                return None
            for rule in loc["rules"]:
                if rule.get("id") == rule_id:
                    rule.update(patch)
                    return dict(rule)
            return None

        return await self._mutate(umo, _do)

    async def delete_rule(self, umo: str, lid: str, rule_id: str) -> bool:
        def _do(item: dict[str, Any]) -> bool:
            loc = self._pick(item["locations"], lid)
            if loc is None:
                return False
            before = len(loc["rules"])
            loc["rules"] = [r for r in loc["rules"] if r.get("id") != rule_id]
            loc["state"]["fired"].pop(rule_id, None)
            return len(loc["rules"]) < before

        return await self._mutate(umo, _do)

    # ---------------------------------------------------------------- 雨雪 / 预警配置

    async def get_section(self, umo: str, lid: str, section: str) -> dict[str, Any]:
        defaults = DEFAULT_PRECIP if section == "precip" else DEFAULT_ALERT
        loc = await self.location(umo, lid)
        merged = dict(defaults)
        merged.update((loc or {}).get(section) or {})
        merged["windows"] = normalize_windows(merged.get("windows"))
        return merged

    async def patch_section(
        self, umo: str, lid: str, section: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        def _do(item: dict[str, Any]) -> dict[str, Any]:
            loc = self._pick(item["locations"], lid)
            if loc is None:
                return {}
            loc[section].update(patch)
            return dict(loc[section])

        return await self._mutate(umo, _do)

    # ---------------------------------------------------------------- 去重状态

    async def state(self, umo: str, lid: str) -> dict[str, Any]:
        loc = await self.location(umo, lid)
        state = (loc or {}).get("state") or {}
        return {
            "alerts": state.get("alerts") or {},
            "precip": state.get("precip") or "dry",
            "fired": state.get("fired") or {},
        }

    async def mark_fired(self, umo: str, lid: str, rule_id: str, stamp: str) -> None:
        def _do(item: dict[str, Any]) -> None:
            loc = self._pick(item["locations"], lid)
            if loc is not None:
                loc["state"]["fired"][rule_id] = stamp

        await self._mutate(umo, _do)

    async def mark_precip_state(self, umo: str, lid: str, value: str) -> None:
        """记下某个地址当前是 ``wet`` 还是 ``dry``，用于判断降水开始/结束的跳变。"""

        def _do(item: dict[str, Any]) -> None:
            loc = self._pick(item["locations"], lid)
            if loc is not None:
                loc["state"]["precip"] = value

        await self._mutate(umo, _do)

    async def remember_alert(self, umo: str, lid: str, alert_id: str, meta: dict[str, Any]) -> None:
        """记下已推送的预警。只保留最近 50 条，避免无限增长。"""

        def _do(item: dict[str, Any]) -> None:
            loc = self._pick(item["locations"], lid)
            if loc is None:
                return
            bucket = loc["state"]["alerts"]
            bucket[alert_id] = meta
            if len(bucket) > 50:
                ordered = sorted(bucket.items(), key=lambda kv: str(kv[1].get("at", "")))
                for key, _ in ordered[: len(bucket) - 50]:
                    bucket.pop(key, None)

        await self._mutate(umo, _do)

    # ---------------------------------------------------------------- 额度推送规则

    async def quota(self, umo: str) -> dict[str, Any]:
        item = await self.session(umo)
        return _copy((item or {}).get("quota") or blank_quota())

    async def quota_rules(self, umo: str) -> list[dict[str, Any]]:
        return list((await self.quota(umo)).get("rules") or [])

    async def add_quota_rule(self, umo: str, rule: dict[str, Any]) -> dict[str, Any] | None:
        prepared = dict(DEFAULT_QUOTA_RULE)
        prepared.update({k: v for k, v in rule.items() if k in DEFAULT_QUOTA_RULE})
        prepared["id"] = new_rule_id()

        def _do(item: dict[str, Any]) -> dict[str, Any]:
            bucket = item.setdefault("quota", blank_quota())
            bucket["rules"].append(prepared)
            return dict(prepared)

        return await self._mutate(umo, _do)

    async def find_quota_rule(self, umo: str, ref: str) -> dict[str, Any] | None:
        return pick_rule(await self.quota_rules(umo), ref)

    async def update_quota_rule(
        self, umo: str, rule_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        def _do(item: dict[str, Any]) -> dict[str, Any] | None:
            for rule in item.setdefault("quota", blank_quota())["rules"]:
                if rule.get("id") == rule_id:
                    rule.update(patch)
                    return dict(rule)
            return None

        return await self._mutate(umo, _do)

    async def delete_quota_rule(self, umo: str, rule_id: str) -> bool:
        def _do(item: dict[str, Any]) -> bool:
            bucket = item.setdefault("quota", blank_quota())
            before = len(bucket["rules"])
            bucket["rules"] = [r for r in bucket["rules"] if r.get("id") != rule_id]
            bucket["state"]["fired"].pop(rule_id, None)
            return len(bucket["rules"]) < before

        return await self._mutate(umo, _do)

    async def mark_quota_fired(self, umo: str, rule_id: str, stamp: str) -> None:
        def _do(item: dict[str, Any]) -> None:
            item.setdefault("quota", blank_quota())["state"]["fired"][rule_id] = stamp

        await self._mutate(umo, _do)


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M")
