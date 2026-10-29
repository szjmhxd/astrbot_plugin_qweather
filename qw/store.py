"""订阅配置与去重状态的持久化。

一个 ``subs.json`` 装下所有会话的配置：

    {
      "sessions": {
        "<unified_msg_origin>": {
          "city":   {"name","id","lat","lon","adm1"},
          "rules":  [ {规则...} ],
          "precip": {"enabled", "windows", "on_start", "on_stop", ...},
          "alert":  {"enabled", "poll_interval", "min_level", ...},
          "state":  {"alerts": {...}, "precip": {...}, "fired": {rule_id: "2026-10-29T07:30"}}
        }
      }
    }

为什么状态要落盘
----------------
去重状态如果只在内存，插件重启后会把当前所有生效预警重推一遍——用户会收到一串重复通知。
``fired`` 记的是「这条规则上次在哪个 HH:MM 触发过」，同样必须落盘，否则刚好在 07:30 重启
会重复推送早报。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

MAX_SESSIONS = 2000

DEFAULT_PRECIP: dict[str, Any] = {
    "enabled": False,
    "windows": [{"start": "07:00", "end": "23:00"}],
    "on_start": True,
    "on_stop": True,
    "lead_minutes": 60,
    "poll_interval": 10,
}

DEFAULT_ALERT: dict[str, Any] = {
    "enabled": False,
    "poll_interval": 60,
    "min_level": "yellow",
    "windows": [{"start": "07:00", "end": "23:00"}],
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


def blank_session() -> dict[str, Any]:
    return {
        "city": None,
        "rules": [],
        "precip": json.loads(json.dumps(DEFAULT_PRECIP)),
        "alert": json.loads(json.dumps(DEFAULT_ALERT)),
        "state": {"alerts": {}, "precip": {}, "fired": {}},
    }


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

    @staticmethod
    def _repair(session: dict[str, Any]) -> dict[str, Any]:
        """兼容旧版本 / 手工编辑过的文件：缺字段补默认，脏字段丢弃。"""
        base = blank_session()
        base["city"] = session.get("city") if isinstance(session.get("city"), dict) else None
        rules = session.get("rules")
        if isinstance(rules, list):
            cleaned: list[dict[str, Any]] = []
            for item in rules:
                if not isinstance(item, dict):
                    continue
                rule = dict(DEFAULT_RULE)
                rule.update({k: v for k, v in item.items() if k in DEFAULT_RULE or k == "id"})
                if not rule.get("id"):
                    rule["id"] = new_rule_id()
                cleaned.append(rule)
            base["rules"] = cleaned
        for section, defaults in (("precip", DEFAULT_PRECIP), ("alert", DEFAULT_ALERT)):
            cfg = session.get(section)
            if isinstance(cfg, dict):
                merged = dict(defaults)
                merged.update({k: v for k, v in cfg.items() if k in defaults})
                base[section] = merged
        state = session.get("state")
        if isinstance(state, dict):
            base["state"] = {
                "alerts": state.get("alerts") if isinstance(state.get("alerts"), dict) else {},
                "precip": state.get("precip") if isinstance(state.get("precip"), dict) else {},
                "fired": state.get("fired") if isinstance(state.get("fired"), dict) else {},
            }
        return base

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
            return json.loads(json.dumps(item)) if item is not None else None

    def _trim(self) -> None:
        overflow = len(self._data) - MAX_SESSIONS
        if overflow <= 0:
            return
        # 优先淘汰既没城市又没规则的空会话
        empties = [k for k, v in self._data.items() if not v.get("city") and not v.get("rules")]
        for key in empties[:overflow]:
            self._data.pop(key, None)

    async def all_sessions(self) -> dict[str, dict[str, Any]]:
        await self._ensure()
        async with self._lock:
            return json.loads(json.dumps(self._data))

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

    # ---------------------------------------------------------------- 城市

    async def set_city(self, umo: str, city: dict[str, Any]) -> None:
        def _do(item: dict[str, Any]) -> None:
            item["city"] = {
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

        await self._mutate(umo, _do)

    async def get_city(self, umo: str) -> dict[str, Any] | None:
        item = await self.session(umo)
        return (item or {}).get("city")

    async def clear_city(self, umo: str) -> bool:
        def _do(item: dict[str, Any]) -> bool:
            had = bool(item.get("city"))
            item["city"] = None
            return had

        return await self._mutate(umo, _do)

    # ---------------------------------------------------------------- 规则

    async def add_rule(self, umo: str, rule: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(DEFAULT_RULE)
        prepared.update(rule)
        prepared["id"] = new_rule_id()

        def _do(item: dict[str, Any]) -> dict[str, Any]:
            item["rules"].append(prepared)
            return prepared

        return await self._mutate(umo, _do)

    async def list_rules(self, umo: str) -> list[dict[str, Any]]:
        item = await self.session(umo)
        return list((item or {}).get("rules") or [])

    async def find_rule(self, umo: str, ref: str) -> dict[str, Any] | None:
        """``ref`` 可以是 rule id，也可以是 1-based 序号。先按 id 找，再按序号。"""
        rules = await self.list_rules(umo)
        by_id = next((r for r in rules if r.get("id") == ref), None)
        if by_id is not None:
            return by_id
        if ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(rules):
                return rules[idx]
        return None

    async def update_rule(
        self, umo: str, rule_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        def _do(item: dict[str, Any]) -> dict[str, Any] | None:
            for rule in item["rules"]:
                if rule.get("id") == rule_id:
                    rule.update(patch)
                    return rule
            return None

        return await self._mutate(umo, _do)

    async def delete_rule(self, umo: str, rule_id: str) -> bool:
        def _do(item: dict[str, Any]) -> bool:
            before = len(item["rules"])
            item["rules"] = [r for r in item["rules"] if r.get("id") != rule_id]
            item["state"]["fired"].pop(rule_id, None)
            return len(item["rules"]) < before

        return await self._mutate(umo, _do)

    # ---------------------------------------------------------------- 雨雪 / 预警配置

    async def get_section(self, umo: str, section: str) -> dict[str, Any]:
        item = await self.session(umo)
        defaults = DEFAULT_PRECIP if section == "precip" else DEFAULT_ALERT
        merged = dict(defaults)
        merged.update((item or {}).get(section) or {})
        return merged

    async def patch_section(self, umo: str, section: str, patch: dict[str, Any]) -> dict[str, Any]:
        def _do(item: dict[str, Any]) -> dict[str, Any]:
            item[section].update(patch)
            return dict(item[section])

        return await self._mutate(umo, _do)

    # ---------------------------------------------------------------- 去重状态

    async def state(self, umo: str) -> dict[str, Any]:
        item = await self.session(umo)
        state = (item or {}).get("state") or {}
        return {
            "alerts": state.get("alerts") or {},
            "precip": state.get("precip") or {},
            "fired": state.get("fired") or {},
        }

    async def patch_state(self, umo: str, patch: dict[str, Any]) -> None:
        def _do(item: dict[str, Any]) -> None:
            item["state"].update(patch)

        await self._mutate(umo, _do)

    async def mark_fired(self, umo: str, rule_id: str, stamp: str) -> None:
        def _do(item: dict[str, Any]) -> None:
            item["state"]["fired"][rule_id] = stamp

        await self._mutate(umo, _do)

    async def mark_precip_state(self, umo: str, location_id: str, value: str) -> None:
        """记下某个城市当前是 ``wet`` 还是 ``dry``，用于判断降水开始/结束的跳变。"""

        def _do(item: dict[str, Any]) -> None:
            item["state"]["precip"][location_id] = value

        await self._mutate(umo, _do)

    async def remember_alert(
        self, umo: str, location_id: str, alert_id: str, meta: dict[str, Any]
    ) -> None:
        """记下已推送的预警。只保留最近 50 条，避免无限增长。"""

        def _do(item: dict[str, Any]) -> None:
            bucket = item["state"]["alerts"].setdefault(location_id, {})
            bucket[alert_id] = meta
            if len(bucket) > 50:
                ordered = sorted(bucket.items(), key=lambda kv: str(kv[1].get("at", "")))
                for key, _ in ordered[: len(bucket) - 50]:
                    bucket.pop(key, None)

        await self._mutate(umo, _do)


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M")
