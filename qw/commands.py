"""指令执行层：解析对话指令并落地成配置或查询结果。

这一层不导入 AstrBot，全部依赖通过构造参数注入。好处是能脱离框架单测——
``parse_rule`` 之类的纯逻辑可以直接喂字符串验，不用起一个假事件对象。

指令族
------
``/qweather 地址``　　管理多个地址（增删改切换），每个地址有独立的推送方案
``/qweather 推送``  ``/qweather 雨雪``  ``/qweather 预警``  ``/qweather key``  ``/qweather 额度``
另有 ``/qweather 天气`` 负责查天气和设置当前地址的地点（在 main.py 里，
因为要复用 pending 输入机制）。

指令写法一律用 :func:`qw.naming.cmd` 生成，前缀改配置就整体跟着变，不在文案里写死。

地址的作用域规则
----------------
所有推送指令默认作用在**当前地址**上；想临时指定别的地址就在命令里加 ``地址=2``，
不必先切换。这样用户不用为了改一条规则来回切地址。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from . import render, schedule
from .client import QWeatherClient, QWeatherError
from .keys import KeyStore, KeyWizard
from .naming import cmd
from .store import MAX_LOCATIONS, SubStore, normalize_level

ClientFactory = Callable[[str], Awaitable[QWeatherClient]]
# (umo, 用户输入) -> (地点 dict 或 None, 错误提示)
PlaceResolver = Callable[[str, str], Awaitable[tuple[dict[str, Any] | None, str]]]


def push_help() -> str:
    return f"""推送指令（作用在当前地址上，加 地址=2 可指定别的地址）：
{cmd("推送")}　　　　　　　　　　列出规则
{cmd("推送 加 每天 07:30 内容=now,daily,sun")}
{cmd("推送 加 每周 一,三,五 07:30 内容=daily,air")}
{cmd("推送 加 每月 1,15 09:00 内容=daily")}
{cmd("推送 加 每小时 8-20 0,30 内容=now,air")}
{cmd("推送 加 日出 -30 内容=now,daily,sun")}
{cmd("推送 改 1 时间=08:00 内容=now,daily")}
{cmd("推送 开 1")}　　关 1　　删 1　　试 1

内容可选：now 实况 / daily 预报 / hourly 逐时 / air 空气
　　　　　indices 指数 / alert 预警 / sun 日出日落 / minutely 降水

地址：{cmd("地址")} 查看与切换，「{cmd("地址 加 天津")}」新增一个地址。
"""


def address_help() -> str:
    return f"""地址指令：一个会话可以有多个地址，每个地址的推送方案各自独立。
{cmd("地址")}　　　　　　　　　 列出所有地址
{cmd("地址 加 天津")}　　　　　 添加地址（城市名，最多到区县）
{cmd("地址 加 117.21,39.13")}　 添加地址（坐标，精度约 1 km）
{cmd("地址 加 天津 名称=公司")}　带别名，方便区分
{cmd("地址 用 2")}　　　　　　　切换当前地址（也可以写别名）
{cmd("地址 改 2 名称=家")}　　　 改别名
{cmd("地址 改 2 地点=武清")}　　 改绑地点
{cmd("地址 删 2")}　　　　　　　删除地址及其全部推送方案
{cmd("地址 查 2")}　　　　　　　查该地址的天气，不切换

推送指令默认作用在当前地址，加「地址=2」可临时指定。"""


LEVEL_LABEL = {"blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色"}

# 内容项 -> 需要拉哪些数据（minutely 单独处理，因为要按降水概率预筛）
CONTENT_FETCH = {
    "now": ("now",),
    "daily": ("daily",),
    "hourly": ("hourly",),
    "air": ("air",),
    "indices": ("indices",),
    "alert": ("alerts",),
    "sun": ("daily",),
    "minutely": ("minutely",),
}

_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")
_ADDR_KV = re.compile(r"^地址[=＝](.+)$")


def _kv_split(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """把 ``内容=now,daily`` 这类键值对从位置参数里拆出来。"""
    positional: list[str] = []
    kv: dict[str, str] = {}
    for token in tokens:
        match = re.match(r"^([^=＝]+)[=＝](.*)$", token)
        if match:
            key = match.group(1).strip()
            alias = {
                "content": "内容",
                "title": "标题",
                "time": "时间",
                "level": "等级",
                "interval": "间隔",
                "name": "名称",
                "place": "地点",
            }
            kv[alias.get(key.lower(), key)] = match.group(2).strip()
        else:
            positional.append(token)
    return positional, kv


def _pop_address(tokens: list[str]) -> tuple[list[str], str]:
    """摘出 ``地址=2`` 这种全局修饰，剩下的才是子命令参数。"""
    rest: list[str] = []
    ref = ""
    for token in tokens:
        match = _ADDR_KV.match(token)
        if match:
            ref = match.group(1).strip()
        else:
            rest.append(token)
    return rest, ref


def join_place(tokens: list[str]) -> str:
    """把地点参数拼回去。

    城市名一般是单个词；坐标允许用空格分隔（``117.21 39.13``），这种要拼成一个串，
    否则会被当成两个参数。仅当恰好两个 token 都是纯数字时才合并。
    """
    if len(tokens) == 2 and all(_NUMERIC.match(t) for t in tokens):
        return ",".join(tokens)
    return " ".join(tokens).strip()


def parse_rule(tokens: list[str]) -> dict[str, Any]:
    """``加 ...`` 后面的参数 → 规则 dict。抛 ValueError 说明哪里写错了。"""
    positional, kv = _kv_split(tokens)
    if not positional:
        raise ValueError("缺少触发方式：每天 / 每周 / 每月 / 每小时 / 日出 / 日落")

    kind_token = positional[0]
    rest = positional[1:]
    rule: dict[str, Any] = {"kind": "clock", "enabled": True}

    if kind_token in ("每天", "每日", "daily"):
        rule["kind"] = "clock"
        if not rest:
            raise ValueError("每天模式要跟时间，例如：加 每天 07:30")
        hour, minute = schedule.parse_clock(rest[0])
        rule["at"] = schedule.fmt_clock(hour, minute)

    elif kind_token in ("每周", "weekly"):
        rule["kind"] = "weekday"
        if len(rest) < 2:
            raise ValueError("每周模式要写：加 每周 一,三,五 07:30")
        rule["weekdays"] = schedule.parse_weekdays(rest[0])
        hour, minute = schedule.parse_clock(rest[1])
        rule["at"] = schedule.fmt_clock(hour, minute)

    elif kind_token in ("每月", "monthly"):
        rule["kind"] = "monthday"
        if len(rest) < 2:
            raise ValueError("每月模式要写：加 每月 1,15 09:00")
        rule["monthdays"] = schedule.parse_monthdays(rest[0])
        hour, minute = schedule.parse_clock(rest[1])
        rule["at"] = schedule.fmt_clock(hour, minute)

    elif kind_token in ("每小时", "hourly"):
        rule["kind"] = "hourly"
        if not rest:
            raise ValueError("每小时模式要写：加 每小时 8-20 0,30（小时段 分钟）")
        rule["hours"] = schedule.parse_hours(rest[0])
        rule["minutes"] = schedule.parse_minutes(rest[1]) if len(rest) > 1 else [0]

    elif kind_token in ("日出", "日落", "sunrise", "sunset"):
        rule["kind"] = "solar"
        rule["solar"] = "sunrise" if kind_token in ("日出", "sunrise") else "sunset"
        rule["offset"] = schedule.parse_offset(rest[0]) if rest else 0

    else:
        raise ValueError(f"不认识的触发方式：{kind_token}")

    content_text = kv.get("内容") or "now,daily"
    rule["content"] = schedule.parse_content(content_text)
    if kv.get("标题"):
        rule["title"] = kv["标题"][:20]
    return rule


class Commands:
    """所有对话指令的落地实现。"""

    def __init__(
        self,
        store: SubStore,
        keys: KeyStore,
        wizard: KeyWizard,
        client_factory: ClientFactory,
        resolve_place: PlaceResolver | None = None,
    ) -> None:
        self._store = store
        self._keys = keys
        self._wizard = wizard
        self._client = client_factory
        self._resolve = resolve_place

    # ================================================================ 地址

    async def _target(self, umo: str, ref: str = "") -> tuple[dict[str, Any] | None, str]:
        """本次操作的地址。``ref`` 为空取当前地址。"""
        if ref:
            loc = await self._store.location(umo, ref)
            if loc is None:
                return None, f"没找到地址「{ref}」，发「{cmd('地址')}」看列表。"
            return loc, ""
        loc = await self._store.active(umo)
        if loc is None:
            return None, f"⚠️ 还没有地址。先发「{cmd('地址 加 天津')}」建一个。"
        return loc, ""

    async def handle_address(self, umo: str, args: list[str]) -> str:
        if args and args[0] in ("帮助", "help", "?", "？"):
            return address_help()

        head = args[0] if args else ""
        rest = args[1:]

        if not head or head in ("列表", "ls", "list"):
            return await self._address_list(umo)

        if head in ("加", "添加", "add"):
            return await self._add_address(umo, rest)

        if head in ("用", "切换", "选", "use", "switch"):
            if not rest:
                return f"用法：{cmd('地址 用 2')}（也可以写别名）"
            loc = await self._store.set_active(umo, rest[0])
            if loc is None:
                return f"没找到地址「{rest[0]}」。发「{cmd('地址')}」看列表。"
            return f"✅ 当前地址已切到：{render.loc_label(loc)}"

        if head in ("删", "删除", "del"):
            if not rest:
                return f"用法：{cmd('地址 删 2')}"
            if len(await self._store.locations(umo)) <= 1:
                loc = await self._store.active(umo)
                if loc is not None:
                    return (
                        "只剩这一个地址，删了就没得查了。\n确实不要的话先加一个新地址，再删这个。"
                    )
            removed = await self._store.delete_location(umo, rest[0])
            if removed is None:
                return f"没找到地址「{rest[0]}」。"
            active = await self._store.active(umo)
            tail = f"\n当前地址：{render.loc_label(active)}" if active else ""
            return f"🗑 已删除：{render.loc_label(removed)}{tail}"

        if head in ("改", "修改", "edit", "rename"):
            if not rest:
                return f"用法：{cmd('地址 改 2 名称=家')}　　或　{cmd('地址 改 2 地点=武清')}"
            ref = rest[0]
            _, kv = _kv_split(rest[1:])
            patch: dict[str, Any] = {}
            if "名称" in kv:
                patch["name"] = kv["名称"][:20]
            if "地点" in kv:
                if self._resolve is None:
                    return "❌ 改绑地点暂不可用。"
                place, err = await self._resolve(umo, kv["地点"])
                if place is None:
                    return err
                patch["city"] = place
            if not patch:
                return "没看出要改什么。支持：名称= 地点="
            loc = await self._store.update_location(umo, ref, patch)
            if loc is None:
                return f"没找到地址「{ref}」。"
            hint = "\n（推送规则没动，仍挂在这个地址上）" if "city" in patch else ""
            return f"✅ 已更新：{render.loc_label(loc)}{hint}"

        if head in ("查", "看", "query"):
            ref = rest[0] if rest else ""
            loc, err = await self._target(umo, ref)
            if loc is None:
                return err
            return await self.render_for_location(umo, loc, preview=False)

        return await self._address_list(umo)

    async def _address_list(self, umo: str) -> str:
        locations = await self._store.locations(umo)
        if not locations:
            return "还没有地址。\n\n" + address_help()
        active = await self._store.active(umo)
        active_lid = (active or {}).get("lid")
        lines = ["地址列表（每个地址的推送方案各自独立）："]
        for idx, loc in enumerate(locations, 1):
            mark = "▶" if loc.get("lid") == active_lid else "　"
            tags: list[str] = []
            rules = loc.get("rules") or []
            if rules:
                tags.append(f"{len(rules)} 条推送")
            if (loc.get("precip") or {}).get("enabled"):
                tags.append("雨雪开")
            if (loc.get("alert") or {}).get("enabled"):
                tags.append("预警开")
            tail = f"（{'，'.join(tags)}）" if tags else ""
            lines.append(f"{mark} {idx}. {render.loc_label(loc)}{tail}")
        lines.append("")
        lines.append(f"▶ 是当前地址。「{cmd('地址 用 2')}」切换，「{cmd('地址 加 ...')}」新增。")
        return "\n".join(lines)

    async def _add_address(self, umo: str, rest: list[str]) -> str:
        if self._resolve is None:
            return "❌ 地址解析不可用。"
        positional, kv = _kv_split(rest)
        place_text = join_place(positional)
        if not place_text:
            return (
                f"用法：{cmd('地址 加 天津')}　或　{cmd('地址 加 117.21,39.13')}"
                f"　或　{cmd('地址 加 天津 名称=公司')}"
            )
        if len(await self._store.locations(umo)) >= MAX_LOCATIONS:
            return f"最多 {MAX_LOCATIONS} 个地址，先删掉不用的。"
        place, err = await self._resolve(umo, place_text)
        if place is None:
            return err
        loc, status = await self._store.add_location(umo, place, kv.get("名称") or "")
        if loc is None:
            return status or "添加失败。"
        if status == "already":
            return f"这个地点已经有了：{render.loc_label(loc)}（已切为当前地址）"
        hint = ""
        if place.get("coord"):
            hint = "\n坐标精度约 1 km（0.01°）。"
        return (
            f"✅ 已添加地址：{render.loc_label(loc)}{hint}\n"
            f"已设为当前地址，接下来「{cmd('推送')}」「{cmd('雨雪')}」「{cmd('预警')}」"
            "都作用在它上面。\n\n" + await self.render_for_location(umo, loc, preview=False)
        )

    # ================================================================ 推送规则

    async def handle_push(self, umo: str, args: list[str]) -> str:
        args, addr_ref = _pop_address(args)
        if not args or args[0] in ("帮助", "help", "?", "？"):
            return push_help()

        head = args[0]
        rest = args[1:]
        loc, err = await self._target(umo, addr_ref)
        if loc is None:
            return err

        if head in ("列表", "ls"):
            return await self._list_rules(umo, loc)

        if head in ("加", "添加", "add"):
            return await self._add_rule(umo, loc, rest)

        if head in ("改", "修改", "edit"):
            return await self._edit_rule(umo, loc, rest)

        if head in ("开", "启用", "on"):
            return await self._toggle_rule(umo, loc, rest, True)

        if head in ("关", "关闭", "停用", "off"):
            return await self._toggle_rule(umo, loc, rest, False)

        if head in ("删", "删除", "del"):
            return await self._delete_rule(umo, loc, rest)

        if head in ("试", "测试", "test"):
            return await self._try_rule(umo, loc, rest)

        return push_help()

    async def _list_rules(self, umo: str, loc: dict[str, Any]) -> str:
        rules = loc.get("rules") or []
        lines = [f"地址：{render.loc_label(loc)}", ""]
        if not rules:
            lines.append(f"还没有推送规则。用「{cmd('推送 加 每天 07:30 内容=now,daily')}」添加。")
            return "\n".join(lines)
        for idx, rule in enumerate(rules, 1):
            lines.append(schedule.describe_rule(rule, idx))
        lines.append("")
        lines.append("开/关/删/改/试 后面跟序号。别的地址加「地址=2」。")
        return "\n".join(lines)

    async def _add_rule(self, umo: str, loc: dict[str, Any], rest: list[str]) -> str:
        try:
            rule = parse_rule(rest)
        except ValueError as exc:
            return f"❌ {exc}\n\n{push_help()}"

        saved = await self._store.add_rule(umo, loc["lid"], rule)
        if saved is None:
            return f"❌ 地址已被删除，重发「{cmd('地址')}」看看。"
        rules = await self._store.list_rules(umo, loc["lid"])
        idx = next((i for i, r in enumerate(rules, 1) if r.get("id") == saved.get("id")), 0)
        lines = [
            f"✅ 已添加规则 {idx}（{render.loc_label(loc)}）",
            schedule.describe_rule(saved),
        ]
        preview = await self._preview_next(umo, loc, saved)
        if preview:
            lines.append(f"下次触发：{preview}")
        return "\n".join(lines)

    async def _edit_rule(self, umo: str, loc: dict[str, Any], rest: list[str]) -> str:
        if not rest:
            return f"用法：{cmd('推送 改 1 时间=08:00 内容=now,daily')}"
        rule = await self._store.find_rule(umo, loc["lid"], rest[0])
        if not rule:
            return f"没找到规则 {rest[0]}，发「{cmd('推送')}」看列表。"
        _, kv = _kv_split(rest[1:])
        patch: dict[str, Any] = {}
        try:
            if "时间" in kv:
                hour, minute = schedule.parse_clock(kv["时间"])
                patch["at"] = schedule.fmt_clock(hour, minute)
            if "内容" in kv:
                patch["content"] = schedule.parse_content(kv["内容"])
            if "标题" in kv:
                patch["title"] = kv["标题"][:20]
        except ValueError as exc:
            return f"❌ {exc}"
        if not patch:
            return "没看出要改什么。支持：时间= 内容= 标题="
        updated = await self._store.update_rule(umo, loc["lid"], rule["id"], patch)
        return f"✅ 已更新\n{schedule.describe_rule(updated or rule)}"

    async def _toggle_rule(
        self, umo: str, loc: dict[str, Any], rest: list[str], enabled: bool
    ) -> str:
        if not rest:
            return f"用法：{cmd('推送 开 1')}"
        rule = await self._store.find_rule(umo, loc["lid"], rest[0])
        if not rule:
            return f"没找到规则 {rest[0]}。"
        updated = await self._store.update_rule(umo, loc["lid"], rule["id"], {"enabled": enabled})
        return (
            f"{'✅ 已开启' if enabled else '⏸ 已关闭'}\n{schedule.describe_rule(updated or rule)}"
        )

    async def _delete_rule(self, umo: str, loc: dict[str, Any], rest: list[str]) -> str:
        if not rest:
            return f"用法：{cmd('推送 删 1')}"
        rule = await self._store.find_rule(umo, loc["lid"], rest[0])
        if not rule:
            return f"没找到规则 {rest[0]}。"
        await self._store.delete_rule(umo, loc["lid"], rule["id"])
        return f"🗑 已删除\n{schedule.describe_rule(rule)}"

    async def _try_rule(self, umo: str, loc: dict[str, Any], rest: list[str]) -> str:
        if not rest:
            return f"用法：{cmd('推送 试 1')}（立即按该规则渲染一次，不发到频道）"
        rule = await self._store.find_rule(umo, loc["lid"], rest[0])
        if not rule:
            return f"没找到规则 {rest[0]}。"
        return await self.render_for_rule(umo, loc, rule, preview=True)

    async def _preview_next(self, umo: str, loc: dict[str, Any], rule: dict[str, Any]) -> str:
        sun = await self._sun_times(umo, loc)
        runs = schedule.next_runs(rule, datetime.now(), sun, limit=1)
        return runs[0].strftime("%m-%d %H:%M") if runs else ""

    async def _sun_times(self, umo: str, loc: dict[str, Any]) -> dict[str, str] | None:
        """拿今天的日出日落。solar 规则要它，缓存 30 分钟避免每 30 秒查一次。"""
        city = loc.get("city") or {}
        if not city.get("lat"):
            return None
        cache = self.__dict__.setdefault("_sun_cache", {})
        key = f"{city.get('lat')},{city.get('lon')}"
        cached = cache.get(key)
        now_ts = datetime.now().timestamp()
        if cached and now_ts - cached[0] < 1800:
            return cached[1]
        try:
            client = await self._client(umo)
            days = await client.daily(float(city["lat"]), float(city["lon"]), 1)
        except (QWeatherError, KeyError, ValueError):
            return None
        if not days:
            return None
        sun = {"sunrise": days[0].get("sunrise") or "", "sunset": days[0].get("sunset") or ""}
        cache[key] = (now_ts, sun)
        return sun

    # ================================================================ 渲染一次推送

    async def render_for_rule(
        self, umo: str, loc: dict[str, Any], rule: dict[str, Any], *, preview: bool = False
    ) -> str:
        city = loc.get("city") or {}
        if not city.get("lat"):
            return "⚠️ 这个地址还没绑地点。"
        content = rule.get("content") or ["now"]
        try:
            bundle = await self.fetch_bundle(umo, city, content)
        except QWeatherError as exc:
            return f"❌ 取数据失败：{exc}"
        except Exception as exc:  # noqa: BLE001 推送链路不能因为未知异常整条断掉
            return f"❌ 取数据异常：{type(exc).__name__}: {exc}"
        text = render.render_report(
            city,
            content,
            bundle,
            title=rule.get("title") or "天气",
            alias=str(loc.get("name") or ""),
        )
        if not preview:
            return text
        return f"（预览｜{render.loc_label(loc)}）\n" + text

    async def render_for_location(
        self, umo: str, loc: dict[str, Any], *, preview: bool = False, extra: bool = True
    ) -> str:
        city = loc.get("city") or {}
        content = ["now", "daily", "sun"] + (["air"] if extra else [])
        try:
            bundle = await self.fetch_bundle(umo, city, content)
        except QWeatherError as exc:
            return f"查询失败：{exc}"
        return render.render_report(
            city, content, bundle, title="天气", alias=str(loc.get("name") or "")
        )

    async def fetch_bundle(
        self, umo: str, city: dict[str, Any], content: list[str]
    ) -> dict[str, Any]:
        """按内容项拉数据。同一个数据只拉一次（daily 被 sun 复用）。"""
        client = await self._client(umo)
        lat, lon = float(city["lat"]), float(city["lon"])
        need: set[str] = set()
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
                bundle["air"] = None  # 空气挂了不该让整条推送失败
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

    # ================================================================ 雨雪提醒

    async def handle_precip(self, umo: str, args: list[str]) -> str:
        args, addr_ref = _pop_address(args)
        loc, err = await self._target(umo, addr_ref)
        if loc is None:
            return err
        lid = loc["lid"]
        cfg = await self._store.get_section(umo, lid, "precip")
        if not args:
            return self._precip_view(loc, cfg)

        head = args[0]
        rest = args[1:]

        if head in ("开", "on"):
            await self._store.patch_section(umo, lid, "precip", {"enabled": True})
            return (
                "✅ 雨雪提醒已开启（"
                + render.loc_label(loc)
                + "）\n"
                + self._precip_view(loc, await self._store.get_section(umo, lid, "precip"))
            )
        if head in ("关", "off"):
            await self._store.patch_section(umo, lid, "precip", {"enabled": False})
            return "⏸ 雨雪提醒已关闭（" + render.loc_label(loc) + "）"

        if head in ("加时段", "加"):
            if not rest:
                return f"用法：{cmd('雨雪 加时段 08:00-12:00')}"
            window = self._parse_window(rest[0])
            windows = list(cfg.get("windows") or []) + [window]
            await self._store.patch_section(umo, lid, "precip", {"windows": windows})
            return f"✅ 已添加时段 {window['start']}-{window['end']}\n" + self._precip_view(
                loc, await self._store.get_section(umo, lid, "precip")
            )

        if head in ("列时段", "时段"):
            return self._window_list(cfg.get("windows") or [])

        if head in ("删时段", "删"):
            if not rest or not rest[0].isdigit():
                return f"用法：{cmd('雨雪 删时段 2')}"
            idx = int(rest[0]) - 1
            windows = list(cfg.get("windows") or [])
            if not (0 <= idx < len(windows)):
                return f"没有第 {rest[0]} 个时段。"
            removed = windows.pop(idx)
            await self._store.patch_section(umo, lid, "precip", {"windows": windows})
            return f"🗑 已删除时段 {removed['start']}-{removed['end']}\n" + self._window_list(
                windows
            )

        if head in ("间隔", "轮询"):
            if not rest or not rest[0].isdigit():
                return f"用法：{cmd('雨雪 间隔 10')}（分钟，范围 5-60）"
            value = max(5, min(int(rest[0]), 60))
            await self._store.patch_section(umo, lid, "precip", {"poll_interval": value})
            return f"✅ 轮询间隔改为 {value} 分钟"

        if head in ("提前",):
            if not rest or not rest[0].isdigit():
                return f"用法：{cmd('雨雪 提前 60')}（分钟）"
            value = max(10, min(int(rest[0]), 120))
            await self._store.patch_section(umo, lid, "precip", {"lead_minutes": value})
            return f"✅ 提前量改为 {value} 分钟"

        for key, labels in (("on_start", ("开始",)), ("on_stop", ("结束",))):
            if head in labels:
                if not rest or rest[0] not in ("开", "关", "on", "off"):
                    return f"用法：{cmd('雨雪 ' + head + ' 开|关')}"
                enabled = rest[0] in ("开", "on")
                await self._store.patch_section(umo, lid, "precip", {key: enabled})
                label = "降水开始提醒" if key == "on_start" else "降水结束提醒"
                return f"✅ {label}已{'开启' if enabled else '关闭'}"

        return self._precip_view(loc, cfg)

    def _precip_view(self, loc: dict[str, Any], cfg: dict[str, Any]) -> str:
        return "\n".join(
            [
                f"地址：{render.loc_label(loc)}",
                f"雨雪提醒：{'✅ 开启' if cfg.get('enabled') else '⏸ 关闭'}",
                f"可提醒时段：{schedule.window_text(cfg.get('windows') or [])}",
                f"降水开始：{'开' if cfg.get('on_start') else '关'}"
                f"　降水结束：{'开' if cfg.get('on_stop') else '关'}",
                f"提前量：{cfg.get('lead_minutes')} 分钟　轮询：{cfg.get('poll_interval')} 分钟",
                "",
                "本指令后接：加时段 08:00-12:00｜删时段 2｜间隔 10｜提前 60｜开始 开｜结束 关",
            ]
        )

    @staticmethod
    def _parse_window(text: str) -> dict[str, str]:
        raw = (text or "").replace("~", "-").replace("到", "-")
        parts = [p for p in re.split(r"[-—]", raw) if p.strip()]
        if len(parts) != 2:
            raise ValueError("时段格式：08:00-12:00")
        start = schedule.fmt_clock(*schedule.parse_clock(parts[0]))
        end = schedule.fmt_clock(*schedule.parse_clock(parts[1]))
        return {"start": start, "end": end}

    @staticmethod
    def _window_list(windows: list[dict[str, str]]) -> str:
        if not windows:
            return "没有配置时段（等于全天可提醒）。"
        lines = ["可提醒时段："]
        for idx, window in enumerate(windows, 1):
            lines.append(f"  {idx}. {window.get('start')}-{window.get('end')}")
        return "\n".join(lines)

    # ================================================================ 预警推送

    async def handle_alert(self, umo: str, args: list[str]) -> str:
        args, addr_ref = _pop_address(args)
        loc, err = await self._target(umo, addr_ref)
        if loc is None:
            return err
        lid = loc["lid"]
        cfg = await self._store.get_section(umo, lid, "alert")
        if not args:
            return self._alert_view(loc, cfg)

        head = args[0]
        rest = args[1:]

        if head in ("开", "on"):
            await self._store.patch_section(umo, lid, "alert", {"enabled": True})
            return (
                "✅ 灾害预警推送已开启（"
                + render.loc_label(loc)
                + "）\n"
                + self._alert_view(loc, await self._store.get_section(umo, lid, "alert"))
            )
        if head in ("关", "off"):
            await self._store.patch_section(umo, lid, "alert", {"enabled": False})
            return "⏸ 灾害预警推送已关闭（" + render.loc_label(loc) + "）"

        if head in ("间隔", "轮询"):
            if not rest or not rest[0].isdigit():
                return f"用法：{cmd('预警 间隔 60')}（分钟，范围 5-360）"
            value = max(5, min(int(rest[0]), 360))
            await self._store.patch_section(umo, lid, "alert", {"poll_interval": value})
            return f"✅ 轮询间隔改为 {value} 分钟"

        if head in ("等级", "级别"):
            if not rest:
                return f"用法：{cmd('预警 等级 橙')}（蓝/黄/橙/红）"
            level = normalize_level(rest[0])
            if not level:
                return f"不认识的等级：{rest[0]}（用 蓝/黄/橙/红）"
            await self._store.patch_section(umo, lid, "alert", {"min_level": level})
            return f"✅ 最低推送等级：{LEVEL_LABEL[level]}"

        if head in ("升级",):
            if not rest or rest[0] not in ("开", "关", "on", "off"):
                return f"用法：{cmd('预警 升级 开|关')}"
            enabled = rest[0] in ("开", "on")
            await self._store.patch_section(umo, lid, "alert", {"notify_on_upgrade": enabled})
            return f"✅ 等级升高再推送：{'开启' if enabled else '关闭'}"

        if head in ("加时段", "加"):
            if not rest:
                return f"用法：{cmd('预警 加时段 07:00-23:00')}"
            window = self._parse_window(rest[0])
            windows = list(cfg.get("windows") or []) + [window]
            await self._store.patch_section(umo, lid, "alert", {"windows": windows})
            return f"✅ 已添加时段 {window['start']}-{window['end']}"

        if head in ("删时段", "删"):
            if not rest or not rest[0].isdigit():
                return f"用法：{cmd('预警 删时段 1')}"
            idx = int(rest[0]) - 1
            windows = list(cfg.get("windows") or [])
            if not (0 <= idx < len(windows)):
                return f"没有第 {rest[0]} 个时段。"
            windows.pop(idx)
            await self._store.patch_section(umo, lid, "alert", {"windows": windows})
            return self._window_list(windows)

        if head in ("列时段", "时段"):
            return self._window_list(cfg.get("windows") or [])

        if head in ("测试", "试"):
            city = loc.get("city") or {}
            if not city.get("lat"):
                return "⚠️ 这个地址还没绑地点。"
            try:
                client = await self._client(umo)
                alerts = await client.warning_now(float(city["lat"]), float(city["lon"]))
            except QWeatherError as exc:
                return f"❌ 查询失败：{exc}"
            name = render.loc_short(loc)
            if not alerts:
                return f"{name} 当前没有生效预警。"
            lines = [f"{name} 当前 {len(alerts)} 条预警："]
            for alert in alerts:
                lines.append(
                    f"· {alert.get('level')}{alert.get('type_name')}　{alert.get('title')}"
                )
            return "\n".join(lines)

        return self._alert_view(loc, cfg)

    def _alert_view(self, loc: dict[str, Any], cfg: dict[str, Any]) -> str:
        level = cfg.get("min_level") or "yellow"
        return "\n".join(
            [
                f"地址：{render.loc_label(loc)}",
                f"灾害预警推送：{'✅ 开启' if cfg.get('enabled') else '⏸ 关闭'}",
                f"轮询间隔：{cfg.get('poll_interval')} 分钟（默认 60）",
                f"最低推送等级：{LEVEL_LABEL.get(level, level)}",
                f"可提醒时段：{schedule.window_text(cfg.get('windows') or [])}",
                f"等级升高再推：{'开' if cfg.get('notify_on_upgrade') else '关'}",
                "",
                "本指令后接：间隔 60｜等级 橙｜升级 开｜加时段 07:00-23:00｜删时段 1｜测试",
            ]
        )

    # ================================================================ key

    async def handle_key(self, umo: str, args: list[str]) -> tuple[str, bool]:
        """返回 ``(回复文本, 是否进入分步输入)``。"""
        if not args:
            return await self._keys.describe(umo), False

        head = args[0]
        if head in ("设置", "换", "set"):
            prompt = self._wizard.start(umo)
            return (
                "开始设置本会话专用 key（共 5 步，5 分钟内有效，发「取消」可中断）。\n\n" + prompt,
                True,
            )
        if head in ("默认", "恢复", "reset"):
            removed = await self._keys.clear_session(umo)
            return (
                "✅ 已恢复使用系统默认 key" if removed else "本会话本来就在用系统默认 key",
                False,
            )
        if head in ("校验", "验证", "check"):
            try:
                client = await self._client(umo)
                loc = await self._store.active(umo)
                city = (loc or {}).get("city") or {}
                lat = float(city.get("lat") or 39.13)
                lon = float(city.get("lon") or 117.21)
                data = await client.current(lat, lon)
                return (
                    f"✅ key 有效\n{data.get('text')} {data.get('temp')}°C，"
                    f"湿度 {data.get('humidity')}%",
                    False,
                )
            except QWeatherError as exc:
                return f"❌ {exc}", False
        if head in ("公钥", "pub"):
            pub = self._keys.default_public_key()
            return (
                f"系统私钥对应的公钥（控制台上传的就是这串）：\n{pub}"
                if pub
                else "系统默认私钥未配置。",
                False,
            )
        if head in ("取消", "cancel"):
            self._wizard.cancel(umo)
            return ("已取消设置流程。", False)

        return await self._keys.describe(umo), False

    async def finish_key_wizard(self, umo: str, data: dict[str, str]) -> str:
        """引导收齐后落库并验证。"""
        await self._keys.set_session(umo, data)
        try:
            client = await self._client(umo)
            await client.current(39.08, 117.20)
        except QWeatherError as exc:
            hint = ""
            if exc.is_auth:
                hint = "\n提示：iss/sub/kid 有一项不对，或私钥与上传的公钥不是同一对。"
            elif exc.is_permission:
                hint = (
                    "\n提示：host 用控制台-设置里那个专属 API Host"
                    "（形如 xxxx.re.qweatherapi.com）。"
                )
            return f"⚠️ 已保存，但校验未通过：{exc}{hint}\n发「{cmd('key 设置')}」可重配。"
        return (
            f"✅ 本会话专用 key 已保存并校验通过。发「{cmd('key')}」查看，"
            f"发「{cmd('key 默认')}」可恢复系统 key。"
        )

    # ================================================================ 额度

    async def handle_quota(self, umo: str) -> str:
        try:
            client = await self._client(umo)
        except QWeatherError as exc:
            return f"❌ {exc}"

        lines: list[str] = []
        errors: list[str] = []

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
            lines.append("")
            lines.append(
                f"最近 24 小时（截至 {as_of} UTC）：成功 {total_ok} 次，失败 {total_err} 次"
            )
            for item in sorted(success, key=lambda x: -sum(x.get("hours") or []))[:6]:
                count = sum(item.get("hours") or [])
                if count:
                    lines.append(f"  {item.get('api')}：{count}")

        if errors:
            lines.append("")
            lines.append("⚠️ 以下接口未取到：")
            lines.extend(f"  {e}" for e in errors)
            if any("403" in e for e in errors):
                lines.append(
                    "\n控制台 API 默认关闭。去「控制台 → 项目管理 → 点开凭据 → 下滑到控制台权限」，"
                    "勾选「允许访问财务汇总数据」和「允许访问请求量统计」，保存后再试。"
                )

        return "\n".join(lines) if lines else "没取到任何数据。"


# 供 main.py 判断某条消息是不是在等用户补输入
WIZARD_CANCEL_WORDS = ("取消", "cancel", "退出", "算了")
