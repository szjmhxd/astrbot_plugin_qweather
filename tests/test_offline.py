#!/usr/bin/env python3
"""和风天气插件的离线冒烟测试。

不联网、不需要 AstrBot：用桩模块替换 ``astrbot.*``，只验纯逻辑（签名、规则引擎、
存储、指令解析、渲染）。

跑法（在仓库根目录执行，依赖 aiohttp）：
    python tests/test_offline.py

需要真实接口验证时，在 AstrBot 里实际跑一遍指令即可。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types
from datetime import date, datetime
from pathlib import Path

# 仓库根目录本身就是一个 Python 包（目录名 astrbot_plugin_qweather），
# 所以把它的父目录放进 sys.path，才能以包的形式导入用来测相对导入。
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE.parent))


# ---------------------------------------------------------------- 桩模块


def install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    class _Logger:
        def info(self, msg, *a, **k):
            pass

        warning = info
        error = info
        debug = info

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

    class AstrMessageEvent:  # noqa: N801
        pass

    class _Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def command(*a, **k):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def event_message_type(*a, **k):
            def deco(fn):
                return fn

            return deco

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    event_mod.MessageChain = lambda components: components
    sys.modules["astrbot.api.event"] = event_mod

    class Star:
        def __init__(self, context=None, config=None):
            # 和 AstrBot v4.29 一致：基类只存 context，**不存 config**。
            # 之前这里贴心地写成了 self.config = config，正好把「插件忘了自己留一份配置」
            # 的 bug 盖住了，线上表现为「面板里填了 key，插件一直说没配」。
            self.context = context
            del config

    class Context:
        async def send_message(self, *a, **k):
            return True

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            path = HERE / "_test_data" / name
            path.mkdir(parents=True, exist_ok=True)
            return path

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.StarTools = StarTools
    sys.modules["astrbot.api.star"] = star_mod

    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Plain = lambda text: text
    sys.modules["astrbot.api.message_components"] = comp_mod

    platform_mod = types.ModuleType("astrbot.api.platform")
    platform_mod.MessageType = types.SimpleNamespace(
        GROUP_MESSAGE="GroupMessage", FRIEND_MESSAGE="FriendMessage"
    )
    sys.modules["astrbot.api.platform"] = platform_mod


install_stubs()

from astrbot_plugin_qweather.qw import jwtlite, render, schedule  # noqa: E402
from astrbot_plugin_qweather.qw.client import (  # noqa: E402
    coord_error,
    format_coord,
    geo_location,
    looks_like_coord,
    parse_latlon,
)
from astrbot_plugin_qweather.qw.commands import join_place, parse_rule  # noqa: E402
from astrbot_plugin_qweather.qw.keys import KeyStore, KeyWizard  # noqa: E402
from astrbot_plugin_qweather.qw.pushers import location_matches  # noqa: E402
from astrbot_plugin_qweather.qw.render import (  # noqa: E402
    loc_label,
    loc_short,
    place_label,
    short_label,
)
from astrbot_plugin_qweather.qw.store import (  # noqa: E402
    SubStore,
    blank_location,
    level_rank,
    normalize_level,
)  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 1. 签名


def test_jwt() -> None:
    section("Ed25519 签名与 JWT")
    pem = """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIL7RbFsq4W6J+5sAlWZyL6lRieZN/+DXu+vn0vNjNzF8
-----END PRIVATE KEY-----"""
    seed = jwtlite.seed_from_pem(pem)
    check("私钥解出 32 字节种子", len(seed) == 32, f"got {len(seed)}")

    pub = jwtlite.public_key_b64(seed)
    check("公钥稳定可复现", pub == jwtlite.public_key_b64(seed) and len(pub) == 44, pub)

    jwt = jwtlite.make_jwt(seed, "Q1A2B3C4D5", "SUBTEST01", "KIDTEST1")
    check("JWT 三段结构", jwt.count(".") == 2, jwt[:40])

    import base64
    import json

    def decode(part: str) -> dict:
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    header = decode(jwt.split(".")[0])
    payload = decode(jwt.split(".")[1])
    check("header alg=EdDSA", header.get("alg") == "EdDSA", str(header))
    check("header 带 kid", header.get("kid") == "KIDTEST1", str(header))
    check(
        "payload 含 iss/sub/iat/exp",
        payload.get("iss") == "Q1A2B3C4D5"
        and payload.get("sub") == "SUBTEST01"
        and "iat" in payload
        and payload["exp"] > payload["iat"],
        str(payload),
    )
    try:
        jwtlite.seed_from_pem("not-base64-@@@")
        check("非法私钥应报错", False)
    except ValueError:
        check("非法私钥报错", True)


# ---------------------------------------------------------------- 2. 规则引擎


def test_schedule() -> None:
    section("规则引擎：解析")
    check("07:30 → (7,30)", schedule.parse_clock("07:30") == (7, 30))
    check("7:05 → (7,5)", schedule.parse_clock("7:05") == (7, 5))
    check("7点 → (7,0)", schedule.parse_clock("7点") == (7, 0))
    check("0730 → (7,30)", schedule.parse_clock("0730") == (7, 30))
    check("8-20 展开 13 小时", schedule.parse_hours("8-20") == list(range(8, 21)))
    check("混合区间 0,30", schedule.parse_minutes("0,30") == [0, 30])
    check("一,三,五 → 1,3,5", schedule.parse_weekdays("一,三,五") == [1, 3, 5])
    check("周一到周日", schedule.parse_weekdays("周一,周日") == [1, 7])
    check("1,15 号", schedule.parse_monthdays("1,15") == [1, 15])
    check("日出 → sunrise", schedule.parse_solar("日出") == "sunrise")
    check("-30 偏移", schedule.parse_offset("-30") == -30)

    for bad, why in [("25:00", "越界"), ("abc", "非时间"), ("8-3", "区间反了")]:
        try:
            schedule.parse_clock(bad) if ":" in bad or "abc" in bad else schedule.parse_hours(bad)
            check(f"非法输入 {bad} 应报错", False)
        except ValueError:
            check(f"非法输入 {bad} 报错（{why}）", True)

    section("规则引擎：触发判定")
    now = datetime(2026, 10, 29, 7, 30)  # 周四
    clock_rule = {"kind": "clock", "at": "07:30", "enabled": True}
    check("每天 07:30 命中", schedule.is_due(clock_rule, now))
    check("同一分钟已推过则跳过", not schedule.is_due(clock_rule, now, "2026-10-29 07:30"))
    check("07:31 不命中", not schedule.is_due(clock_rule, datetime(2026, 10, 29, 7, 31)))

    weekday_rule = {"kind": "weekday", "weekdays": [4], "at": "07:30", "enabled": True}
    check("周四命中", schedule.is_due(weekday_rule, now))
    wednesday_rule = {"kind": "weekday", "weekdays": [3], "at": "07:30", "enabled": True}
    check("周三不命中", not schedule.is_due(wednesday_rule, now))

    monthday_rule = {"kind": "monthday", "monthdays": [29], "at": "07:30", "enabled": True}
    check("29 号命中", schedule.is_due(monthday_rule, now))

    hourly_rule = {
        "kind": "hourly",
        "hours": [7, 8],
        "minutes": [0, 30],
        "enabled": True,
    }
    check("每小时 07:30 命中", schedule.is_due(hourly_rule, now))
    check("07:31 不命中", not schedule.is_due(hourly_rule, datetime(2026, 10, 29, 7, 31)))
    check("07:00 命中", schedule.is_due(hourly_rule, datetime(2026, 10, 29, 7, 0)))
    check("09:00 不在小时段", not schedule.is_due(hourly_rule, datetime(2026, 10, 29, 9, 0)))

    sun = {"sunrise": "06:34", "sunset": "17:14"}
    solar_rule = {"kind": "solar", "solar": "sunrise", "offset": -30, "enabled": True}
    check(
        "日出前 30 分 = 06:04", schedule.is_due(solar_rule, datetime(2026, 10, 29, 6, 4), "", sun)
    )
    check(
        "日出时刻本身不命中（offset 生效）",
        not schedule.is_due(solar_rule, datetime(2026, 10, 29, 6, 34), "", sun),
    )
    check(
        "没有天文数据时 solar 不触发", not schedule.is_due(solar_rule, datetime(2026, 10, 29, 6, 4))
    )

    off_rule = {"kind": "clock", "at": "07:30", "enabled": False}
    check("关闭的规则不触发", not schedule.is_due(off_rule, now))

    section("规则引擎：下次触发")
    runs = schedule.next_runs(clock_rule, datetime(2026, 10, 29, 8, 0), None, limit=2)
    check(
        "下次是明天 07:30",
        len(runs) == 2 and runs[0].strftime("%m-%d %H:%M") == "10-30 07:30",
        str(runs),
    )

    section("规则引擎：可提醒时段")
    windows = [{"start": "08:00", "end": "12:00"}, {"start": "11:00", "end": "20:00"}]
    check("09:00 在窗口内", schedule.in_windows(datetime(2026, 10, 29, 9, 0), windows))
    check("13:00 在窗口内", schedule.in_windows(datetime(2026, 10, 29, 13, 0), windows))
    check("21:00 不在窗口内", not schedule.in_windows(datetime(2026, 10, 29, 21, 0), windows))
    cross = [{"start": "22:00", "end": "06:00"}]
    check("跨零点 23:00 命中", schedule.in_windows(datetime(2026, 10, 29, 23, 0), cross))
    check("跨零点 05:00 命中", schedule.in_windows(datetime(2026, 10, 29, 5, 0), cross))
    check("跨零点 12:00 不命中", not schedule.in_windows(datetime(2026, 10, 29, 12, 0), cross))
    check("空窗口 = 全天", schedule.in_windows(datetime(2026, 10, 29, 3, 0), []))


# ---------------------------------------------------------------- 3. 存储


def test_store() -> None:
    section("存储：多地址与规则隔离")

    async def run() -> None:
        path = HERE / "_test_data" / "subs_test.json"
        if path.exists():
            path.unlink()
        store = SubStore(path)
        await store.load()
        umo = "test:GroupMessage:1"

        check("初始没有地址", await store.active(umo) is None)

        tj = {
            "name": "天津",
            "id": "101030100",
            "adm1": "天津市",
            "adm2": "天津",
            "lat": 39.08,
            "lon": 117.2,
        }
        loc, status = await store.add_location(umo, tj)
        check("添加地址", loc is not None and status == "" and loc["city"]["name"] == "天津")
        check("地址 id 带 L 前缀", loc["lid"].startswith("L"), loc["lid"])
        check("新地址自动设为当前", (await store.active(umo))["lid"] == loc["lid"])

        dup, status = await store.add_location(umo, tj)
        check("同一地点不重复添加", status == "already" and len(await store.locations(umo)) == 1)
        check("重复添加返回已有地址", dup["lid"] == loc["lid"])

        wq = {
            "name": "武清",
            "id": "101030200",
            "adm1": "天津市",
            "adm2": "天津",
            "lat": 39.38,
            "lon": 117.06,
        }
        loc2, _ = await store.add_location(umo, wq, "公司")
        check("多地址", len(await store.locations(umo)) == 2)
        check("别名保存", loc2["name"] == "公司")
        check("按 id 找", (await store.location(umo, loc2["lid"]))["lid"] == loc2["lid"])
        check("按别名找", (await store.location(umo, "公司"))["lid"] == loc2["lid"])
        check("按序号找", (await store.location(umo, "1"))["lid"] == loc["lid"])
        check("找不到返回 None", await store.location(umo, "99") is None)
        check("切换当前", (await store.set_active(umo, "1"))["lid"] == loc["lid"])

        # 规则挂在地址上，互不影响
        r1 = await store.add_rule(
            umo, loc["lid"], {"kind": "clock", "at": "07:30", "content": ["now"]}
        )
        r2 = await store.add_rule(
            umo, loc2["lid"], {"kind": "clock", "at": "09:00", "content": ["daily"]}
        )
        check("规则有 id", bool(r1.get("id")) and bool(r2.get("id")))
        check("默认字段补齐", "enabled" in r1 and "minutes" in r1)
        check(
            "规则按地址隔离",
            len(await store.list_rules(umo, loc["lid"])) == 1
            and len(await store.list_rules(umo, loc2["lid"])) == 1,
        )
        check("按序号找规则", (await store.find_rule(umo, loc["lid"], "1"))["id"] == r1["id"])
        check("按 id 找规则", (await store.find_rule(umo, loc["lid"], r1["id"]))["id"] == r1["id"])
        check("别的地址查不到这条规则", await store.find_rule(umo, loc2["lid"], r1["id"]) is None)

        await store.update_rule(umo, loc["lid"], r1["id"], {"at": "08:00", "enabled": False})
        updated = await store.find_rule(umo, loc["lid"], r1["id"])
        check("改时间与开关", updated["at"] == "08:00" and updated["enabled"] is False)

        # 各地址独立的雨雪 / 预警配置
        await store.patch_section(umo, loc["lid"], "precip", {"enabled": True, "poll_interval": 5})
        p1 = await store.get_section(umo, loc["lid"], "precip")
        p2 = await store.get_section(umo, loc2["lid"], "precip")
        check("雨雪配置按地址隔离", p1["enabled"] and not p2["enabled"], f"{p1} / {p2}")
        check("轮询间隔按地址隔离", p1["poll_interval"] == 5 and p2["poll_interval"] == 10)
        await store.patch_section(umo, loc2["lid"], "alert", {"min_level": "orange"})
        check(
            "预警阈值按地址隔离",
            (await store.get_section(umo, loc2["lid"], "alert"))["min_level"] == "orange"
            and (await store.get_section(umo, loc["lid"], "alert"))["min_level"] == "yellow",
        )

        # 去重状态也按地址隔离
        await store.mark_fired(umo, loc["lid"], r1["id"], "2026-10-29 08:00")
        await store.mark_precip_state(umo, loc["lid"], "wet")
        await store.remember_alert(umo, loc["lid"], "aid-1", {"level": "黄色", "at": "now"})
        s1 = await store.state(umo, loc["lid"])
        s2 = await store.state(umo, loc2["lid"])
        check("fired 落盘", s1["fired"].get(r1["id"]) == "2026-10-29 08:00")
        check("降水状态落盘", s1["precip"] == "wet")
        check("预警去重记录", "aid-1" in s1["alerts"])
        check(
            "另一个地址状态没被污染",
            s2["fired"] == {} and s2["precip"] == "dry" and s2["alerts"] == {},
            str(s2),
        )

        # 重新加载：验证持久化
        store2 = SubStore(path)
        await store2.load()
        check("重启后地址还在", len(await store2.locations(umo)) == 2)
        check("重启后别名还在", (await store2.location(umo, "公司"))["name"] == "公司")
        reloaded = await store2.find_rule(umo, loc["lid"], r1["id"])
        check("重启后规则还在", reloaded is not None and reloaded["at"] == "08:00")
        check(
            "重启后去重状态还在",
            (await store2.state(umo, loc["lid"]))["fired"].get(r1["id"]) == "2026-10-29 08:00",
        )

        # 删除
        ok = await store.delete_rule(umo, loc["lid"], r1["id"])
        check("删除规则", ok and not await store.list_rules(umo, loc["lid"]))
        check("删规则不动地址", len(await store.locations(umo)) == 2)
        check(
            "删规则顺手清 fired",
            (await store.state(umo, loc["lid"]))["fired"].get(r1["id"]) is None,
        )

        removed = await store.delete_location(umo, "公司")
        check("按别名删地址", removed is not None and len(await store.locations(umo)) == 1)
        check("剩下的地址成为当前", (await store.active(umo))["lid"] == loc["lid"])

        renamed = await store.update_location(umo, "1", {"name": "家"})
        check("改名", renamed["name"] == "家")
        moved = await store.update_location(umo, "家", {"city": wq})
        check("改绑地点", moved["city"]["name"] == "武清")

        # 旧结构（v1.1 及以前：一个会话一个 city）自动迁移
        old = {
            "sessions": {
                "old": {
                    "city": {"name": "天津", "id": "101030100", "lat": 39.08, "lon": 117.2},
                    "rules": [{"kind": "clock", "at": "07:30", "content": ["now"], "id": "rold01"}],
                    "precip": {"enabled": True},
                    "alert": {"enabled": True, "min_level": "orange"},
                    "state": {
                        "alerts": {"101030100": {"a1": {"level": "黄色"}}},
                        "precip": {"101030100": "wet"},
                        "fired": {"rold01": "2026-10-29 07:30"},
                    },
                }
            }
        }
        path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        store4 = SubStore(path)
        await store4.load()
        migrated = await store4.active("old")
        check("旧数据迁移成一个地址", migrated is not None and migrated["city"]["name"] == "天津")
        check(
            "迁移保留规则",
            len(migrated["rules"]) == 1 and migrated["rules"][0]["id"] == "rold01",
            str(migrated["rules"]),
        )
        check("迁移保留开关", migrated["precip"]["enabled"] and migrated["alert"]["enabled"])
        check("迁移保留预警阈值", migrated["alert"]["min_level"] == "orange")
        check(
            "迁移保留去重状态",
            migrated["state"]["fired"].get("rold01") == "2026-10-29 07:30"
            and migrated["state"]["precip"] == "wet"
            and "a1" in migrated["state"]["alerts"],
            str(migrated["state"]),
        )

        # 脏文件容错
        path.write_text(
            '{"sessions": {"x": {"locations": [{"city": {"name": "天津", "lat": 39.08,'
            ' "lon": 117.2}, "rules": [{"kind":"clock","id":"abc"}, "junk"]}]}}}',
            encoding="utf-8",
        )
        store3 = SubStore(path)
        await store3.load()
        loc_x = await store3.active("x")
        check("脏规则被清理", loc_x is not None and len(loc_x["rules"]) == 1)
        check("脏数据不影响解析", loc_x["rules"][0]["kind"] == "clock")

    asyncio.run(run())


# ---------------------------------------------------------------- 4. 指令解析


def test_commands() -> None:
    section("指令解析：/天气推送 加")

    rule = parse_rule(["每天", "07:30", "内容=now,daily,sun"])
    check("每天", rule["kind"] == "clock" and rule["at"] == "07:30")

    rule = parse_rule(["每周", "一,三,五", "07:30", "内容=daily,air"])
    check("每周", rule["weekdays"] == [1, 3, 5] and rule["at"] == "07:30")

    rule = parse_rule(["每月", "1,15", "09:00"])
    check("每月", rule["monthdays"] == [1, 15] and rule["content"] == ["now", "daily"])

    rule = parse_rule(["每小时", "8-20", "0,30", "内容=now,air"])
    check("每小时", rule["hours"] == list(range(8, 21)) and rule["minutes"] == [0, 30])

    rule = parse_rule(["日出", "-30", "内容=now,daily,sun"])
    check("日出前 30 分", rule["kind"] == "solar" and rule["offset"] == -30)

    rule = parse_rule(["日落", "+15", "标题=晚安"])
    check("日落后 15 分 + 标题", rule["offset"] == 15 and rule["title"] == "晚安")

    for bad, why in [
        (["每天"], "缺时间"),
        (["每周", "一"], "缺时间"),
        (["乱七八糟"], "未知触发方式"),
        (["每天", "07:30", "内容=abc"], "未知内容项"),
    ]:
        try:
            parse_rule(bad)
            check(f"{why} 应报错", False, str(bad))
        except ValueError:
            check(f"{why} 报错", True)

    section("指令解析：摘要")
    text = schedule.describe_rule(
        {
            "kind": "weekday",
            "weekdays": [1, 3],
            "at": "07:30",
            "content": ["now", "daily"],
            "enabled": True,
            "title": "早报",
        }
    )
    check("每周摘要含星期与内容", "每周一三" in text and "实况" in text and "早报" in text, text)
    text = schedule.describe_rule(
        {
            "kind": "hourly",
            "hours": [8, 9],
            "minutes": [0, 30],
            "content": ["now"],
            "enabled": False,
        }
    )
    check("关闭标记", "已关闭" in text, text)


# ---------------------------------------------------------------- 5. 预警匹配


def test_location() -> None:
    section("预警地区匹配")
    tianjin_city = {"id": "101030100"}
    ids = {"101030200", "101030300", "101050302"}
    check("市级 ID 靠前缀命中区县预警", location_matches(tianjin_city, ids))
    check("不在列表的城市不命中", not location_matches({"id": "101010100"}, ids))
    check("5 位 ID 精确匹配", location_matches({"id": "77B7"}, {"77B7", "X"}))
    check("5 位 ID 不做前缀匹配", not location_matches({"id": "77B7"}, {"77B77"}))
    check("空 ID 不命中", not location_matches({}, ids))

    # 区县 / 坐标绑定只能精确匹配，否则会把全市预警都推过来
    check(
        "区县绑定命中本区预警",
        location_matches({"id": "101030200", "name": "武清", "adm2": "天津"}, ids),
    )
    check(
        "区县绑定不吃邻区预警",
        not location_matches({"id": "101030800", "name": "和平", "adm2": "天津"}, ids),
    )
    check(
        "旧数据缺 level 时按 name/adm2 现推",
        not location_matches({"id": "101030800", "name": "和平", "adm2": "天津"}, {"101030200"}),
    )
    check(
        "旧数据缺 adm2 时退回前缀匹配",
        location_matches({"id": "101030100", "name": "天津"}, {"101030200"}),
    )
    check(
        "坐标绑定 level 为 district 时不吃全市",
        not location_matches(
            {"id": "101030800", "coord": True, "level": "district"}, {"101030200"}
        ),
    )


def test_coord() -> None:
    section("坐标解析：固定 纬度,经度")
    check("纬度在前", parse_latlon("39.13,117.21") == (39.13, 117.21))
    check("中文逗号", parse_latlon("39.13，117.21") == (39.13, 117.21))
    check("空格分隔", parse_latlon("39.13 117.21") == (39.13, 117.21))
    check("多余空格", parse_latlon("  39.13 ,  117.21 ") == (39.13, 117.21))
    check("西部经度照写不猜", parse_latlon("43.82,87.62") == (43.82, 87.62))
    check("南半球负纬度", parse_latlon("-33.87,151.21") == (-33.87, 151.21))
    check("小数位截到 2 位", parse_latlon("39.12345,117.21999") == (39.12, 117.22))
    check("城市名不是坐标", parse_latlon("杭州") is None)
    check("单个数字不是坐标", parse_latlon("117.21") is None)
    check("三个数字不是坐标", parse_latlon("39.13,117.21,5") is None)
    check("写反了：纬度超 90 → 拒绝", parse_latlon("117.21,39.13") is None)
    check("经度超范围 → 拒绝", parse_latlon("39.13,200.0") is None)
    check("空串", parse_latlon("") is None)

    section("坐标写错的提示")
    hint = coord_error("117.21,39.13")
    check("指出是顺序问题", "纬度,经度" in hint, hint)
    check("给出可直接照抄的正确写法", "39.13,117.21" in hint, hint)
    check("完全不是数字时给格式示例", "39.13,117.21" in coord_error("天津"))

    section("坐标形态判断")
    check("两个数字算坐标形态", looks_like_coord("39.13,117.21"))
    check("写反了也算坐标形态（好报错）", looks_like_coord("95.0,200.0"))
    check("城市名不是坐标形态", not looks_like_coord("天津"))

    # 输入、展示用「纬度,经度」；只有请求和风时才翻成「经度,纬度」
    check("format_coord 纬度在前", format_coord(39.13, 117.21) == "39.13,117.21")
    check("geo_location 给和风", geo_location(39.13, 117.21) == "117.21,39.13")

    section("坐标：度分秒写法")
    # 39°13'53" = 39 + 13/60 + 53/3600 = 39.2314 → 截两位 39.23
    # 117°8'2" = 117 + 8/60 + 2/3600 = 117.1339 → 截两位 117.13
    dms = "39°13'53\",117°8'2\""
    check("度分秒：标准写法", parse_latlon(dms) == (39.23, 117.13), str(parse_latlon(dms)))
    check("度分秒：单引号当秒", parse_latlon("39°13'53',117°8'2'") == (39.23, 117.13))
    check("度分秒：中文单位", parse_latlon("39度13分53秒,117度8分2秒") == (39.23, 117.13))
    check("度分秒：全角撇号", parse_latlon("39°13′53″,117°8′2″") == (39.23, 117.13))
    check("度分秒：只有度分", parse_latlon("39°13',117°8'") == (39.22, 117.13))
    check("度分秒：和十进制混写", parse_latlon("39°13'53\",117.13") == (39.23, 117.13))
    check("度分秒：空格分隔两个点", parse_latlon("39°13'53\" 117°8'2\"") == (39.23, 117.13))
    check("度分秒：经度在前要拒绝", parse_latlon("117°8'2\",39°13'53\"") is None)
    check("度分秒：分钟超 60 不算坐标", parse_latlon("39°88'0\",117°13'0\"") is None)
    check(
        "度分秒：写反了给出可复制的正确写法",
        "39°13'53\",117°8'2\"" in coord_error("117°8'2\",39°13'53\""),
        coord_error("117°8'2\",39°13'53\""),
    )
    check("方位词：后缀", parse_latlon("39.23N,117.13E") == (39.23, 117.13))
    check("方位词：前缀", parse_latlon("N39.23,E117.13") == (39.23, 117.13))
    check("方位词：S 为负", parse_latlon("33.87S,151.21E") == (-33.87, 151.21))
    check("裸数字串分不开就拒绝", parse_latlon("39 13 53, 117 8 2") is None)

    section("地点标签")
    city = {"name": "杭州", "adm1": "浙江省", "adm2": "杭州"}
    check("市：省·市去重", place_label(city) == "浙江省·杭州", place_label(city))
    district = {"name": "武清", "adm1": "天津市", "adm2": "天津"}
    check("区县：省·区县", place_label(district) == "天津市·武清", place_label(district))
    coord = {
        "name": "和平",
        "adm1": "天津市",
        "adm2": "天津",
        "coord": True,
        "lat": 39.13,
        "lon": 117.21,
    }
    check("坐标带坐标后缀", place_label(coord) == "天津市·和平（39.13,117.21）", place_label(coord))
    check("推送短名带坐标", short_label(coord) == "和平(39.13,117.21)", short_label(coord))
    check("推送短名不带省", short_label(district) == "武清", short_label(district))

    section("地址别名")
    plain = {"lid": "L1", "name": "", "city": district}
    aliased = {"lid": "L2", "name": "公司", "city": district}
    check("没别名就用行政区名", loc_label(plain) == "天津市·武清", loc_label(plain))
    check("有别名时别名在前", loc_label(aliased) == "公司｜天津市·武清", loc_label(aliased))
    check("推送短名用别名区分地址", loc_short(aliased) == "公司·武清", loc_short(aliased))
    check("别名和地名相同时不重复", loc_short({"name": "武清", "city": district}) == "武清")


# ---------------------------------------------------------------- 5b. 地址指令


def test_address_cmds() -> None:
    section("地址指令：增删改切换")

    from astrbot_plugin_qweather.qw.client import QWeatherError
    from astrbot_plugin_qweather.qw.commands import Commands

    async def no_client(_umo: str):
        raise QWeatherError(0, "测试不联网", "stub")

    seen: dict[str, int] = {}

    async def resolve(_umo: str, text: str):
        """假的地点解析：名字 → 递增 id，保证确定性（不能用 hash，每个进程都变）。"""
        if text == "火星":
            return None, "没找到「火星」。"
        if text.replace(",", "").replace(".", "").isdigit():
            lon, lat = (float(x) for x in text.split(","))
            return {
                "name": "和平",
                "id": "101030800",
                "adm1": "天津市",
                "adm2": "天津",
                "lat": lat,
                "lon": lon,
                "coord": True,
            }, ""
        idx = seen.setdefault(text, len(seen) + 1)
        return {
            "name": text,
            "id": f"101030{idx:03d}",
            "adm1": "天津市",
            "adm2": text,
            "lat": 39.0 + idx / 1000,  # 每个名字经纬度都不同，避免被判定为重复地点
            "lon": 117.0 + idx / 1000,
        }, ""

    async def run() -> None:
        path = HERE / "_test_data" / "cmd_test.json"
        if path.exists():
            path.unlink()
        store = SubStore(path)
        await store.load()
        cmd = Commands(store, None, None, no_client, resolve)
        umo = "test:GroupMessage:addr"

        out = await cmd.handle_address(umo, [])
        check("没地址时提示加地址", "还没有地址" in out, out[:60])

        out = await cmd.handle_push(umo, ["加", "每天", "07:30"])
        check("没地址时拒绝加推送规则", "还没有地址" in out, out[:60])

        out = await cmd.handle_address(umo, ["加", "天津"])
        check("加地址成功", "已添加地址" in out, out[:80])
        check("加完自动成为当前", len(await store.locations(umo)) == 1)

        out = await cmd.handle_address(umo, ["加", "武清", "名称=公司"])
        check("加第二个地址带别名", "公司" in out, out[:80])
        check("别名写进存储", (await store.location(umo, "公司")) is not None)

        out = await cmd.handle_address(umo, ["加", "天津"])
        check("重复地点提示已存在", "已经有了" in out, out[:80])
        check("重复地点不新增", len(await store.locations(umo)) == 2)

        out = await cmd.handle_address(umo, ["加", "火星"])
        check("解析失败时原样报错", "没找到" in out, out[:80])

        out = await cmd.handle_address(umo, [])
        check("列表标出当前地址", "▶" in out, out[:120])
        check("列表显示规则数或开关", "地址列表" in out)

        out = await cmd.handle_address(umo, ["用", "1"])
        check("切换到 1", "已切到" in out and (await store.active(umo))["city"]["name"] == "天津")

        # 规则落到当前地址
        out = await cmd.handle_push(umo, ["加", "每天", "07:30", "内容=now"])
        check("规则加到当前地址", "已添加规则" in out, out[:80])
        check(
            "当前地址有 1 条规则",
            len(await store.list_rules(umo, (await store.active(umo))["lid"])) == 1,
        )

        # 地址= 指定别的地址，不切换当前
        out = await cmd.handle_push(umo, ["加", "每天", "09:00", "地址=公司"])
        check("地址= 加到别名地址", "已添加规则" in out, out[:80])
        check("当前地址没变", (await store.active(umo))["city"]["name"] == "天津")
        check(
            "指定的地址拿到了规则",
            len(await store.list_rules(umo, (await store.location(umo, "公司"))["lid"])) == 1,
        )

        out = await cmd.handle_push(umo, ["加", "每天", "09:00", "地址=99"])
        check("地址不存在时报错", "没找到地址" in out, out[:80])

        out = await cmd.handle_precip(umo, ["开"])
        check("雨雪开关落到当前地址", "已开启" in out and "天津" in out, out[:60])

        out = await cmd.handle_alert(umo, ["等级", "橙"])
        check("预警等级落到当前地址", "橙色" in out, out[:60])

        out = await cmd.handle_address(umo, ["改", "2", "名称=办公室"])
        check("改别名", "办公室" in out, out[:80])

        out = await cmd.handle_address(umo, ["删", "办公室"])
        check("删地址", "已删除" in out and len(await store.locations(umo)) == 1, out[:80])

        out = await cmd.handle_address(umo, ["删", "1"])
        check("最后一个地址不许删", "只剩这一个地址" in out, out[:80])
        check("确实没被删掉", len(await store.locations(umo)) == 1)

        out = await cmd.handle_address(umo, ["用", "99"])
        check("切到不存在的地址报错", "没找到地址" in out, out[:60])

        out = await cmd.handle_address(umo, ["帮助"])
        check("地址帮助可读", "/qweather 地址 加" in out, out[:80])

    asyncio.run(run())


# ---------------------------------------------------------------- 5c. 推送循环


def test_pusher() -> None:
    section("推送循环：多地址各自触发、不重推")

    from astrbot_plugin_qweather.qw.pushers import Pusher

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def current(self, lat, lon):
            self.calls.append("current")
            return {
                "temp": 20.1,
                "feels_like": 19.4,
                "text": "晴",
                "humidity": 30,
                "wind_dir": "西",
                "wind_scale": 3,
            }

        async def daily(self, lat, lon, days=4):
            self.calls.append("daily")
            return [
                {
                    "date": "2026-10-30",
                    "temp_max": 22,
                    "temp_min": 12,
                    "text_day": "晴",
                    "text_night": "多云",
                    "sunrise": "06:34",
                    "sunset": "17:14",
                }
            ]

        async def hourly(self, lat, lon, hours=12):
            self.calls.append("hourly")
            return []

        async def air_now(self, lat, lon):
            raise AssertionError("这条规则没要 air，不该调用")

        async def indices(self, lat, lon, types):
            raise AssertionError("这条规则没要 indices，不该调用")

        async def warning_now(self, lat, lon):
            self.calls.append("warning_now")
            return []

        async def warning_list(self):
            self.calls.append("warning_list")
            return []

        async def minutely(self, lat, lon):
            self.calls.append("minutely")
            return {"points": [{"precip": 0}], "summary": ""}

    async def run() -> None:
        path = HERE / "_test_data" / "pusher_test.json"
        if path.exists():
            path.unlink()
        store = SubStore(path)
        await store.load()
        umo = "test:GroupMessage:push"

        at = datetime.now().strftime("%H:%M")
        home, _ = await store.add_location(
            umo, {"name": "和平", "id": "101030800", "lat": 39.13, "lon": 117.21}, "家"
        )
        work, _ = await store.add_location(
            umo, {"name": "武清", "id": "101030200", "lat": 39.38, "lon": 117.06}, "公司"
        )
        for lid in (home["lid"], work["lid"]):
            await store.add_rule(umo, lid, {"kind": "clock", "at": at, "content": ["now"]})

        sent: list[tuple[str, str]] = []

        async def send(u: str, text: str) -> bool:
            sent.append((u, text))
            return True

        client = FakeClient()

        async def factory(_umo: str):
            return client

        pusher = Pusher(store, None, factory, send)
        await pusher._tick()

        check("两个地址各推一次", len(sent) == 2, f"实际 {len(sent)} 条")
        bodies = "\n".join(t for _, t in sent)
        check("推送带地址别名", "家" in bodies and "公司" in bodies, bodies[:120])
        check("推送带实况温度", "20.1" in bodies)
        check("两条推送内容不同", sent[0][1] != sent[1][1])

        # 同一分钟再 tick 不应该重推
        await pusher._tick()
        check("同一分钟不重推", len(sent) == 2, f"实际 {len(sent)} 条")

        # 状态落盘：换一个 store 实例（模拟重启）也不重推
        store2 = SubStore(path)
        await store2.load()
        sent.clear()
        pusher2 = Pusher(store2, None, factory, send)
        await pusher2._tick()
        check("重启后同一分钟不重推", len(sent) == 0, f"实际 {len(sent)} 条")

        # 关掉一条规则只推另一条
        rules = await store.list_rules(umo, home["lid"])
        await store.update_rule(umo, home["lid"], rules[0]["id"], {"enabled": False})
        await store.patch_section(umo, home["lid"], "precip", {"enabled": False})
        sent.clear()
        # 清掉 fired 以便重测
        raw = path.read_text(encoding="utf-8")
        path.write_text(
            raw.replace(datetime.now().strftime("%Y-%m-%d %H:%M"), ""), encoding="utf-8"
        )
        store4 = SubStore(path)
        await store4.load()
        pusher4 = Pusher(store4, None, factory, send)
        await pusher4._tick()
        check("关掉的规则不推", len(sent) == 1, f"实际 {len(sent)} 条")
        check("推的是没关的那个", "公司" in sent[0][1] if sent else False)

        # 没绑地点的地址直接跳过，不该报错
        broken = blank_location({"name": "", "lat": 0, "lon": 0})
        check("空地址构造正常", broken["lid"].startswith("L"))

    asyncio.run(run())


# ---------------------------------------------------------------- 5.5 工作日日历


def test_workday() -> None:
    section("工作日日历：节假日 / 调休 / 逐年轮换 / 清理")

    from astrbot_plugin_qweather.qw.workday import WorkdayCalendar, parse_month_day

    check("11-1 解析成 (11,1)", parse_month_day("11-1", (2, 1)) == (11, 1))
    check("11月1日 也能解析", parse_month_day("11月1日", (2, 1)) == (11, 1))
    check("越界回落默认值", parse_month_day("13-40", (2, 1)) == (2, 1))
    check("空值回落默认值", parse_month_day("", (2, 1)) == (2, 1))

    fixture = {
        "year": 2026,
        "days": [
            {"name": "元旦", "date": "2026-01-01", "isOffDay": True},
            {"name": "元旦", "date": "2026-01-02", "isOffDay": True},
            {"name": "元旦", "date": "2026-01-03", "isOffDay": True},
            {"name": "元旦", "date": "2026-01-04", "isOffDay": False},
            {"name": "国庆节", "date": "2026-10-01", "isOffDay": True},
            {"name": "国庆节", "date": "2026-10-10", "isOffDay": False},
        ],
    }

    async def run() -> None:
        path = HERE / "_test_data" / "workdays_test.json"
        if path.exists():
            path.unlink()
        cal = WorkdayCalendar(path)
        calls: list[int] = []

        async def fake_get_json(url: str):
            year = int(url.rsplit("/", 1)[-1].split(".")[0])
            calls.append(year)
            # 2027 的 JSON 是「存在但 days 为空」，等同于还没公布
            return fixture if year == 2026 else {"year": year, "days": []}

        cal._get_json = fake_get_json  # type: ignore[method-assign]
        await cal.load()

        events = await cal.maintain(datetime(2026, 3, 15, 9, 0), startup=True)
        check("加载时抓到当年数据", cal.has_data(2026), str(events))
        check("当年周末日历生成了 104 天", len(cal.year_data(2026)["weekends"]) == 104)
        check("元旦放假算休息日", not cal.is_workday(date(2026, 1, 1)))
        check("调休上班的周日算工作日", cal.is_workday(date(2026, 1, 4)))
        check("普通周六算休息日", not cal.is_workday(date(2026, 10, 31)))
        check("普通周五算工作日", cal.is_workday(date(2026, 10, 30)))
        check("国庆调休的周六算工作日", cal.is_workday(date(2026, 10, 10)))
        check("3 月还没到窗口，不抓次年", not cal.has_data(2027))
        check("次年日历也还没生成", cal.year_data(2027)["weekends"] == [])
        check("没到 11-01 不会碰次年接口", 2027 not in calls, str(calls))

        # 11-01：开始每天试一次，同时生成次年周末日历
        calls.clear()
        events = await cal.maintain(datetime(2026, 11, 1, 0, 0))
        check("11-01 开始尝试次年数据", calls == [2027], str(calls))
        check("11-01 生成次年周末日历", len(cal.year_data(2027)["weekends"]) == 104)
        check("没公布时不算成功", not cal.has_data(2027))
        check("事件里说清「明天再试」", any("明天再试" in e for e in events), str(events))

        # 同一天再 tick 不能反复打接口
        calls.clear()
        await cal.maintain(datetime(2026, 11, 1, 0, 30))
        check("同一天只尝试一次", calls == [], str(calls))
        calls.clear()
        await cal.maintain(datetime(2026, 11, 2, 0, 0))
        check("第二天再试一次", calls == [2027], str(calls))

        # 落盘：换实例重读，判断结果不变
        cal2 = WorkdayCalendar(path)
        await cal2.load()
        check("重启后照样能判断调休", cal2.is_workday(date(2026, 1, 4)))
        check("重启后当年数据仍在", cal2.has_data(2026))

        # 次年真拿到数据后就不再请求
        cal._years[2027] = cal._blank(2027)
        cal._years[2027].update(
            {"status": "ready", "holidays": {"2027-01-01": "元旦"}, "makeup": {}}
        )
        calls.clear()
        await cal.maintain(datetime(2026, 11, 3, 0, 0))
        check("已有次年数据就不再请求", calls == [], str(calls))

        # 清理：2027-02-01 删掉 2026 及更早，保留当前年与次年
        cal._years[2025] = cal._blank(2025)
        cal._years[2025]["weekends"] = ["2025-01-04"]
        await cal.maintain(datetime(2027, 2, 1, 0, 0))
        check("清理掉旧年份", 2026 not in cal.years() and 2025 not in cal.years(), str(cal.years()))
        check("当前年保留", 2027 in cal.years())

        # 清理日之前不删
        cal3 = WorkdayCalendar(path, cleanup_date=(2, 1))
        await cal3.load()
        cal3._years[2025] = cal3._blank(2025)
        await cal3.maintain(datetime(2027, 1, 20, 0, 0))
        check("没到清理日不删", 2025 in cal3.years())

        check(
            "状态行能说人话",
            "未就绪" in cal3.status_line(2030) or "已就绪" in cal3.status_line(2030),
        )
        check("轮换配置能回显", "11-01" in cal3.schedule_text() and "02-01" in cal3.schedule_text())

    asyncio.run(run())


# ---------------------------------------------------------------- 5.6 日条件与额度推送


def test_day_rules() -> None:
    section("日条件：规则与可提醒时段")

    from astrbot_plugin_qweather.qw import schedule
    from astrbot_plugin_qweather.qw.commands import parse_rule

    check("工作日写法", schedule.parse_days("工作日") == "workday")
    check("休息日写法", schedule.parse_days("休息日") == "restday")
    check("不限写法", schedule.parse_days("不限") == "all")
    check("脏值按不限处理", schedule.normalize_days("???") == "all")
    check("工作日规则在休息日不生效", not schedule.day_allowed("workday", False))
    check("工作日规则在工作日生效", schedule.day_allowed("workday", True))
    check("休息日规则反过来", schedule.day_allowed("restday", True) is False)
    check("拿不到日历时带条件的规则不生效", not schedule.day_allowed("workday", None))

    rule = parse_rule(["每天", "07:30", "内容=now", "日=工作日"])
    check("规则能解析 日=工作日", rule.get("days") == "workday", str(rule))
    check(
        "描述里带日条件", "仅工作日" in schedule.describe_rule(rule), schedule.describe_rule(rule)
    )

    window = schedule.parse_window(["工作日", "08:00-18:00"])
    check(
        "时段能带日条件",
        window == {"start": "08:00", "end": "18:00", "days": "workday"},
        str(window),
    )
    glued = schedule.parse_window(["工作日08:00-18:00"])
    check("漏空格也能认", glued["days"] == "workday" and glued["start"] == "08:00", str(glued))
    check("后缀写法也认", schedule.parse_window(["09:00-18:00", "休息日"])["days"] == "restday")
    check("时段描述带前缀", schedule.window_text([window]) == "仅工作日 08:00-18:00")
    now = datetime(2026, 10, 31, 10, 0)  # 周六
    check("休息日不落在工作日时段", not schedule.in_windows(now, [window], False))
    check("工作日落在时段内", schedule.in_windows(now.replace(day=30), [window], True))
    check(
        "老时段（无 days）照旧全天有效",
        schedule.in_windows(now, [{"start": "00:00", "end": "23:59"}], False),
    )
    check("空时段等于不限制", schedule.in_windows(now, [], False))

    # 下次触发不能算到休息日
    quiet = {"kind": "clock", "at": "07:30", "content": ["now"], "days": "workday"}
    runs = schedule.next_runs(
        quiet, datetime(2026, 10, 30, 8, 0), None, limit=1, day_filter=lambda d: d.weekday() < 5
    )
    check("仅工作日的规则跳到周一", bool(runs) and runs[0].weekday() == 0, str(runs))

    from astrbot_plugin_qweather.qw.commands import _parse_day_text

    base = date(2026, 10, 30)
    check("日期：明天", _parse_day_text("明天", base) == date(2026, 10, 31))
    check("日期：MM-DD", _parse_day_text("11-01", base) == date(2026, 11, 1))
    check("日期：YYYY-MM-DD", _parse_day_text("2027-01-01", base) == date(2027, 1, 1))
    check("日期：斜杠也认", _parse_day_text("2027/1/1", base) == date(2027, 1, 1))
    check("日期：乱写返回 None", _parse_day_text("下下周", base) is None)


def test_quota_push() -> None:
    section("额度推送：按会话走、用会话 key、支持日条件")

    from astrbot_plugin_qweather.qw.pushers import Pusher
    from astrbot_plugin_qweather.qw.store import SubStore

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def finance_summary(self):
            self.calls += 1
            return {
                "balance": "12.34",
                "currency": "CNY",
                "asOf": "2026-10-30T01:00:00Z",
                "accruedCharges": {"thisMonth": "0.50", "previousDay": "0.02"},
                "availableResourcePlans": [],
                "pendingBills": [],
            }

        async def metrics_stats(self):
            return {
                "asOf": "2026-10-30T01:00:00Z",
                "success": [{"api": "weather", "hours": [1, 2, 3]}],
                "errors": [],
            }

    class FakeKeys:
        def __init__(self, source: str) -> None:
            self.source = source
            self.asked: list[str] = []

        async def resolve(self, umo: str):
            self.asked.append(umo)
            return types.SimpleNamespace(source=self.source)

    class FakeCal:
        def __init__(self, work: bool) -> None:
            self.work = work

        def is_workday(self, day) -> bool:
            return self.work

        def kick(self, *a, **k) -> None:
            return None

    async def run() -> None:
        path = HERE / "_test_data" / "quota_push_test.json"
        if path.exists():
            path.unlink()
        store = SubStore(path)
        await store.load()
        at = datetime.now().strftime("%H:%M")

        # 只配额度推送、没有任何地址的会话也要能正常工作
        await store.add_quota_rule("test:GroupMessage:q1", {"kind": "clock", "at": at})
        await store.add_quota_rule(
            "test:GroupMessage:q2", {"kind": "clock", "at": at, "days": "workday"}
        )

        sent: list[tuple[str, str]] = []

        async def send(umo: str, text: str) -> bool:
            sent.append((umo, text))
            return True

        client = FakeClient()

        async def factory(_umo: str):
            return client

        keys = FakeKeys("session")
        pusher = Pusher(store, keys, factory, send, FakeCal(False))

        await pusher._tick()
        check("休息日只推不限日条件的那条", len(sent) == 1, f"实际 {len(sent)} 条")
        check("推给对了会话", sent[0][0] == "test:GroupMessage:q1")
        check("内容是额度报告", "账户余额" in sent[0][1] and "12.34" in sent[0][1], sent[0][1][:80])
        check("标明用的是会话 key", "本会话专用 key" in sent[0][1], sent[0][1][:80])
        check("确实查了会话 key", keys.asked == ["test:GroupMessage:q1"], str(keys.asked))

        await pusher._tick()
        check("同一分钟不重推", len(sent) == 1, f"实际 {len(sent)} 条")

        # 换到工作日：剩下那条才会推
        sent.clear()
        pusher2 = Pusher(store, keys, factory, send, FakeCal(True))
        await pusher2._tick()
        check("工作日推工作日那条", len(sent) == 1 and sent[0][0] == "test:GroupMessage:q2")
        check("普通那条不重复推", all("q1" not in u for u, _ in sent))

        # 默认 key 时说「系统默认 key」
        keys2 = FakeKeys("default")
        sent.clear()
        store2 = SubStore(path)
        await store2.load()
        await store2.add_quota_rule("test:GroupMessage:q3", {"kind": "clock", "at": at})
        pusher3 = Pusher(store2, keys2, factory, send, FakeCal(True))
        await pusher3._tick()
        check("默认 key 也能推", len(sent) == 1, f"实际 {len(sent)} 条")
        check("标明用的是系统默认 key", "系统默认 key" in sent[0][1], sent[0][1][:80])

        # 增删改开
        rules = await store.quota_rules("test:GroupMessage:q2")
        check("规则有 id 且不是纯数字", rules and not rules[0]["id"].isdigit())
        await store.update_quota_rule("test:GroupMessage:q2", rules[0]["id"], {"enabled": False})
        check("能关掉", (await store.quota_rules("test:GroupMessage:q2"))[0]["enabled"] is False)
        check(
            "按序号也能找到", (await store.find_quota_rule("test:GroupMessage:q2", "1")) is not None
        )
        check("删掉", await store.delete_quota_rule("test:GroupMessage:q2", rules[0]["id"]))
        check("删完为空", await store.quota_rules("test:GroupMessage:q2") == [])

        # 老数据没有 quota 字段也不能炸
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["sessions"]["test:GroupMessage:old"] = {"active": "", "locations": []}
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        store3 = SubStore(path)
        await store3.load()
        check("老会话补上额度结构", (await store3.quota("test:GroupMessage:old"))["rules"] == [])

    asyncio.run(run())


def test_keys() -> None:
    section("凭据：会话级 key 与分步引导")

    async def run() -> None:
        path = HERE / "_test_data" / "keys_test.json"
        if path.exists():
            path.unlink()
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MC4CAQAwBQYDK2VwBCIEIL7RbFsq4W6J+5sAlWZyL6lRieZN/+DXu+vn0vNjNzF8\n"
            "-----END PRIVATE KEY-----"
        )
        keys = KeyStore(path, "Q1", "SUB1", "KID1", "h.example.com", pem)
        await keys.load()
        umo = "test:GroupMessage:9"
        check("默认 key 就绪", keys.default_ready)
        resolved = await keys.resolve(umo)
        check("无会话 key 时回落默认", resolved and resolved.source == "default")

        await keys.set_session(
            umo, {"iss": "Q2", "sub": "S2", "kid": "K2", "host": "h2.example.com", "pem": ""}
        )
        resolved = await keys.resolve(umo)
        check(
            "会话 key 覆盖默认",
            resolved and resolved.source == "session" and resolved.key.iss == "Q2",
        )
        check("空私钥复用默认私钥", resolved.key.seed is not None)

        removed = await keys.clear_session(umo)
        resolved = await keys.resolve(umo)
        check("恢复默认", removed and resolved.source == "default")

        # 热更新：WebUI 保存配置后不用重载，set_defaults 直接换掉默认 key
        check(
            "换一组相同的默认 key 不算变化",
            keys.set_defaults("Q1", "SUB1", "KID1", "h.example.com", pem) is False,
        )
        check(
            "换默认 key 会被识别",
            keys.set_defaults("Q9", "SUB9", "KID9", "h9.example.com", pem) is True,
        )
        check("换完仍然就绪", keys.default_ready and keys.default_state() == "就绪")
        check(
            "清空默认 key 后报缺少项",
            keys.set_defaults("", "", "", "", "") is True
            and "iss" in keys.default_error
            and "私钥" in keys.default_error,
        )
        check("清空后不再就绪", not keys.default_ready)
        fingerprint = keys.default_fingerprint()
        check(
            "指纹逐项给出",
            len(fingerprint) == 5 and fingerprint[0] == "iss=空",
            str(fingerprint),
        )
        keys.set_defaults("Q9ABCDEFGH", "SUB1234567", "KID12345678", "ab.re.qweatherapi.com", pem)
        masked = keys.default_fingerprint()
        check(
            "指纹只露前 4 位",
            masked[0] == "iss=Q9AB…（10 位）" and "SUB1234567" not in " ".join(masked),
            str(masked),
        )
        check("指纹标注私钥已载入", "已载入" in masked[4], masked[4])
        check(
            "坏私钥只记错误不抛异常",
            keys.set_defaults(
                "Q9",
                "SUB9",
                "KID9",
                "h9",
                "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
            )
            is True
            and not keys.default_ready
            and "解析" in keys.default_error,
            keys.default_error,
        )

        # 引导流程
        wizard = KeyWizard(timeout=60)
        prompt = wizard.start(umo)
        check("引导第一步问 iss", "开发者 ID" in prompt)
        prompt, done = wizard.feed(umo, "Q9")
        check("第二步问 sub", "项目 ID" in prompt and done is None)
        wizard.feed(umo, "S9")
        wizard.feed(umo, "K9")
        wizard.feed(umo, "https://h9.example.com/")
        prompt, done = wizard.feed(umo, "跳过")
        check(
            "四步收齐后返回数据", done is not None and done["host"] == "h9.example.com", str(done)
        )
        check("跳过私钥时 pem 为空", done["pem"] == "")
        check("收齐后状态清除", wizard.get(umo) is None)

        wizard.start(umo)
        check("取消后状态清除", (wizard.cancel(umo), wizard.get(umo) is None)[1])

    asyncio.run(run())


def test_config_reload() -> None:
    """WebUI 保存配置不会重载插件，插件得自己按文件 mtime 重读。"""
    from astrbot_plugin_qweather.main import QWeatherPlugin
    from astrbot_plugin_qweather.qw.client import QWeatherError

    section("配置热更新：保存即生效")

    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MC4CAQAwBQYDK2VwBCIEIL7RbFsq4W6J+5sAlWZyL6lRieZN/+DXu+vn0vNjNzF8\n"
        "-----END PRIVATE KEY-----"
    )
    tmp = Path(tempfile.mkdtemp(prefix="qwcfg_"))
    cfg_path = tmp / "astrbot_plugin_qweather_config.json"
    payload: dict = {
        "iss": "",
        "sub": "",
        "kid": "",
        "host": "",
        "private_key": "",
        "command_prefix": "qw",
        "enabled": True,
        "push_enabled": False,
    }
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")

    class _Cfg(dict):
        # AstrBot 的配置对象带 config_path，热更新靠它定位文件
        config_path = str(cfg_path)

    try:
        plugin = QWeatherPlugin(None, _Cfg(payload))
        check("空配置时默认 key 未就绪", not plugin._keys.default_ready)
        check("空配置时报缺少项", "iss" in plugin._keys.default_error)
        try:
            asyncio.run(plugin._client_for("test:FriendMessage:1"))
        except QWeatherError as exc:
            check("空配置时取客户端报缺 key", "没有可用的 key" in str(exc), str(exc))
        else:
            check("空配置时取客户端报缺 key", False, "居然拿到了客户端")

        payload.update(
            {
                "iss": "Q9ABCDEFGH",
                "sub": "3CTMP3UCG4",
                "kid": "TDPPRGVSGQC",
                "host": "ne6yvtne25.re.qweatherapi.com",
                "private_key": pem,
            }
        )
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        stamp = time.time_ns() + 10**9  # 保证 mtime 一定变
        os.utime(cfg_path, ns=(stamp, stamp))

        check(
            "保存后立刻读到新 iss",
            plugin._cfg_str("iss") == "Q9ABCDEFGH",
            plugin._cfg_str("iss"),
        )
        check("保存后默认 key 自动就绪", plugin._keys.default_ready, plugin._keys.default_error)
        check("前缀也跟着热更新", plugin._cfg_str("command_prefix") == "qw")
        check(
            "保存后不用重载就能取到客户端",
            asyncio.run(plugin._client_for("test:FriendMessage:1")) is not None,
        )

        payload["iss"] = ""
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        stamp += 10**9
        os.utime(cfg_path, ns=(stamp, stamp))
        check(
            "清空后再读就回落",
            plugin._cfg_str("iss") == "" and not plugin._keys.default_ready,
        )

        check(
            "本地错误不显示状态码 0",
            str(QWeatherError(0, "没有可用的 key", "缺 iss")).startswith("没有可用的 key"),
        )
        check(
            "接口错误仍带状态码",
            str(QWeatherError(401, "Unauthorized", "bad signature")).startswith("401 "),
        )

        # 真实框架（v4.29）的 Star 不存 config，插件必须自己留一份，否则所有配置项都
        # 退化成默认值。这里直接断言插件手里确实握着那份配置。
        section("配置热更新：插件自己留住了配置")
        check("插件自己存了 config", getattr(plugin, "config", None) is plugin._conf)
        check(
            "构造时就读到传入的值",
            plugin._conf["command_prefix"] == "qw" and "iss" in plugin._conf,
            str(plugin._conf)[:60],
        )

        section("配置热更新：框架不给路径时按目录反推")
        root = tmp / "data"
        data_dir = root / "plugin_data" / "astrbot_plugin_qweather"
        data_dir.mkdir(parents=True, exist_ok=True)
        cfg_dir = root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        guess = cfg_dir / "astrbot_plugin_qweather_config.json"
        guess.write_text(json.dumps(payload), encoding="utf-8")

        bare = QWeatherPlugin(None, dict(payload))  # 普通 dict，没有 config_path
        bare._data_dir = data_dir
        bare._cfg_file = None
        check("反推到 data/config 下的同名文件", bare._config_path == str(guess), bare._config_path)
        check("首次读取只是记下时间戳", bare._cfg_str("iss") == "", bare._cfg_str("iss"))

        payload["iss"] = "Q9FALLBACK"
        guess.write_text(json.dumps(payload), encoding="utf-8")
        stamp += 10**9
        os.utime(guess, ns=(stamp, stamp))
        check("按反推路径也能热更新", bare._cfg_str("iss") == "Q9FALLBACK", bare._cfg_str("iss"))

        section("配置热更新：路径彻底找不到时不崩")
        lost = QWeatherPlugin(None, {})
        lost._data_dir = tmp / "nowhere" / "plugin_data" / "x"
        lost._cfg_file = None
        check("没有配置文件路径就返回空串", lost._config_path == "", lost._config_path)
        check("照样能读默认值", lost._cfg_bool("enabled", True) is True)

        section("配置热更新：前缀改了就换")
        from astrbot_plugin_qweather.qw import naming

        payload["command_prefix"] = "wxtest"
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        stamp += 10**9
        os.utime(cfg_path, ns=(stamp, stamp))
        check("插件实例的前缀跟着换", plugin._cfg_str("command_prefix") == "wxtest")
        check("实例前缀已更新", plugin._prefix == "wxtest", plugin._prefix)
        check("按新前缀能路由", naming.split("/wxtest 天气")[0] is True)
        check("老前缀不再接管", naming.split("/qweather 天气")[0] is False)
    finally:
        naming.set_prefix("qweather")  # 前缀是全局的，别污染后面的用例
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 7. 渲染


def test_render() -> None:
    section("渲染：纯文本")
    city = {"name": "天津", "adm1": "天津市"}
    bundle = {
        "now": {
            "temp": 19.85,
            "feels_like": 14.69,
            "text": "晴",
            "humidity": 18,
            "wind_dir": "w",
            "wind_scale": 4,
            "pressure": 1015.17,
            "visibility": 17980,
        },
        "daily": [
            {
                "date": "2026-10-29",
                "temp_min": 7.38,
                "temp_max": 20.03,
                "text_day": "晴",
                "text_night": "晴",
                "precip_prob": 0,
                "sunrise": "06:34",
                "sunset": "17:14",
            },
            {
                "date": "2026-10-30",
                "temp_min": 8.0,
                "temp_max": 21.0,
                "text_day": "多云",
                "text_night": "阴",
                "precip_prob": 10,
                "sunrise": "06:35",
                "sunset": "17:13",
            },
        ],
        "hourly": [{"time": "08:00", "temp": 15.2, "text": "晴"}],
        "air": {"aqi": 62, "category": "良", "primary": "PM10"},
        "indices": [
            {"type": "1", "name": "运动指数", "category": "较不宜"},
            {"type": "5", "name": "紫外线指数", "category": "弱"},
        ],
        "alerts": [{"title": "巴南区气象台发布大雾黄色预警", "text": "能见度小于500米"}],
        "minutely": {"summary": "未来两小时无降水"},
    }
    text = render.render_report(
        city,
        ["now", "daily", "sun", "air", "indices", "alert"],
        bundle,
        moment=datetime(2026, 10, 29, 7, 30),
        title="早报",
    )
    for expected in (
        "早报",
        "天津",
        "07:30",
        "晴",
        "19.9°C",
        "西4级",
        "18%",
        "AQI 62",
        "日出 06:34",
        "运动指数",
        "大雾黄色预警",
    ):
        check(f"渲染含「{expected}」", expected in text, text[:200])
    check("未选中的 hourly 不出现", "逐时" not in text)
    check("缺失数据不炸", render.render_report(city, ["now", "air"], {"now": {}}) != "")

    text = render.render_report(city, ["now"], {"now": bundle["now"]}, title="")
    check("标题为空时用默认「天气」", "天气" in text)


# ---------------------------------------------------------------- 8. 插件类


def test_naming() -> None:
    section("指令前缀：拆分、回落、文案联动")
    from astrbot_plugin_qweather.qw import commands as cmds
    from astrbot_plugin_qweather.qw import naming

    naming.set_prefix("qweather")
    check("默认前缀", naming.prefix() == "qweather")

    ok, sub, args = naming.split("/qweather 天气 杭州")
    check("带唤醒符能认", ok and sub == "天气" and args == ["杭州"], f"{ok}/{sub}/{args}")
    ok, sub, args = naming.split("qweather 天气")
    check("唤醒符已被框架吃掉也能认", ok and sub == "天气" and args == [], f"{ok}/{sub}")
    ok, sub, args = naming.split("qweather天气 杭州")
    check("漏空格也能认", ok and sub == "天气" and args == ["杭州"], f"{ok}/{sub}/{args}")
    ok, sub, args = naming.split("/qweather　地址　用　2")
    check("全角空格也能切", ok and sub == "地址" and args == ["用", "2"], f"{ok}/{sub}/{args}")
    ok, sub, args = naming.split("@bot /qweather 推送 加 每天 07:30")
    check(
        "@机器人 前缀被去掉",
        ok and sub == "推送" and args == ["加", "每天", "07:30"],
        f"{ok}/{sub}/{args}",
    )
    check("只发主指令", naming.split("/qweather") == (True, "", []))
    check("裸指令不再接管", naming.split("/天气 杭州")[0] is False)
    check("英文前缀只认整词", naming.split("/qweatherly 天气")[0] is False)

    for raw in ("", "   ", "/", "　", None):
        check(f"空前缀回落成默认值：{raw!r}", naming.normalize(raw) == "qweather")
    check("前缀自带斜杠会被洗掉", naming.normalize("/qw") == "qw")

    naming.set_prefix("和风")
    check("自定义前缀生效", naming.cmd("天气") == "/和风 天气")
    check("自定义前缀能认", naming.split("/和风 天气")[0])
    check("自定义前缀漏空格也能认", naming.split("/和风天气")[1] == "天气")
    check("地址帮助跟着前缀变", "/和风 地址" in cmds.address_help())
    check("推送帮助跟着前缀变", "/和风 推送" in cmds.push_help())
    naming.set_prefix("qweather")
    check("恢复默认后文案跟着回", "/qweather 地址" in cmds.address_help())

    from astrbot_plugin_qweather.main import help_text

    check("主帮助用的是前缀指令", "/qweather 天气" in help_text())
    check("主帮助里没有裸指令", "/天气" not in help_text())


def test_plugin_class() -> None:
    section("插件类：指令字符串处理")
    from astrbot_plugin_qweather.main import QWeatherPlugin
    from astrbot_plugin_qweather.qw import naming

    # 插件实例会往 _test_data 里写 subs.json。上一次运行留下的规则会让「列规则」的断言
    # 时好时坏，先清干净，保证这套测试可以反复跑。
    for stale_name in ("subs.json", "keys.json", "workdays.json"):
        stale = HERE / "_test_data" / "astrbot_plugin_qweather" / stale_name
        if stale.exists():
            stale.unlink()

    naming.set_prefix("qweather")
    check("地点粗筛：天津", QWeatherPlugin._looks_like_place("天津"))
    check("地点粗筛：挡掉问句", not QWeatherPlugin._looks_like_place("天气怎么样？"))
    check("地点粗筛：挡掉指令", not QWeatherPlugin._looks_like_place("/天气"))
    check("地点粗筛：挡掉长句", not QWeatherPlugin._looks_like_place("今天天津的天气情况怎么样呢"))
    check("地点粗筛：放行坐标", QWeatherPlugin._looks_like_place("117.21,39.13"))
    check("地点粗筛：坐标写反也放行（才能报错）", QWeatherPlugin._looks_like_place("39.13,117.21"))

    section("地点参数拼合")
    check("单个城市名", join_place(["天津"]) == "天津")
    check("空格分隔的坐标拼回一个串", join_place(["117.21", "39.13"]) == "117.21,39.13")
    check("负数坐标也能拼", join_place(["-33.87", "151.21"]) == "-33.87,151.21")
    check("两个词的城市名不被当成坐标", join_place(["天津", "武清"]) == "天津 武清")
    check("空参数", join_place([]) == "")

    section("地址= 修饰解析")
    from astrbot_plugin_qweather.qw.commands import _pop_address

    rest, ref = _pop_address(["加", "每天", "07:30", "地址=2"])
    check("摘出地址参数", ref == "2" and rest == ["加", "每天", "07:30"], f"{rest} / {ref}")
    rest, ref = _pop_address(["开", "地址=公司"])
    check("地址可以写别名", ref == "公司" and rest == ["开"])
    rest, ref = _pop_address(["加", "每天", "07:30"])
    check("没有地址参数时为空", ref == "" and rest == ["加", "每天", "07:30"])
    rest, ref = _pop_address(["加", "每天", "07:30", "地址=2", "内容=now"])
    check("和内容= 共存", ref == "2" and "内容=now" in rest, f"{rest} / {ref}")

    class _Cfg(dict):
        pass

    try:
        plugin = QWeatherPlugin(
            None,
            _Cfg(
                iss="Q1",
                sub="S1",
                kid="K1",
                host="h.example.com",
                private_key="",
                enabled=True,
                push_enabled=False,
                command_prefix="qw",
            ),
        )
        check("插件可实例化", plugin._subs is not None and plugin._commands is not None)
        check("地点解析已注入指令层", plugin._commands._resolve is not None)
        check("地址指令已就绪", hasattr(plugin._commands, "handle_address"))
        check("配置里的前缀生效", plugin._prefix == "qw" and naming.prefix() == "qw")

        section("消息入口：前缀路由")

        async def run_message(text: str) -> list[str]:
            await plugin._subs.load()
            event = _FakeEvent(text)
            return [item async for item in plugin.on_message(event)]

        check("配了前缀就只认它", "地址" in "".join(asyncio.run(run_message("/qw 帮助"))))
        check("默认前缀此时不响应", asyncio.run(run_message("/qweather 帮助")) == [])
        check("裸指令彻底不接管", asyncio.run(run_message("/天气 杭州")) == [])
        out = "".join(asyncio.run(run_message("/qw 瞎写")))
        check("写错子指令给出帮助而不是沉默", "不认识的子指令" in out and "地址" in out, out[:80])
        out = "".join(asyncio.run(run_message("/qw 地址")))
        check("子指令能落到具体实现", "地址" in out, out[:80])
        out = "".join(asyncio.run(run_message("/qw 额度")))
        check("额度查询不崩（无网络时给错误提示）", bool(out), out[:80])
        out = "".join(asyncio.run(run_message("/qw 工作日")))
        check("工作日子指令能响应", "工作日" in out, out[:80])
        out = "".join(asyncio.run(run_message("/qw 额度推送")))
        check("额度推送子指令给帮助", "额度推送 加" in out, out[:80])
        out = "".join(asyncio.run(run_message("/qw 额度 推送 加 每天 09:00")))
        check("额度 推送 也能转发", "已添加" in out, out[:80])
    except Exception as exc:  # noqa: BLE001 构造失败就是部署会炸，必须暴露
        check("插件可实例化", False, f"{type(exc).__name__}: {exc}")
    finally:
        naming.set_prefix("qweather")

    session = QWeatherPlugin._build_session("kook:GroupMessage:123", _FakeSession)
    check(
        "umo 拆成会话",
        session.platform_name == "kook" and session.session_id == "123",
        str(getattr(session, "__dict__", {})),
    )


class _FakeSession:
    def __init__(self, platform_name=None, message_type=None, session_id=None):
        self.platform_name = platform_name
        self.message_type = message_type
        self.session_id = session_id


class _FakeEvent:
    """够用就行的假事件：只实现插件真正读的字段。"""

    def __init__(self, text: str, umo: str = "test:GroupMessage:1") -> None:
        self.message_str = text
        self.unified_msg_origin = umo
        self.stopped = False

    def plain_result(self, text: str):
        return text

    def stop_event(self) -> None:
        self.stopped = True


def test_content_all() -> None:
    section("内容项：全部 / 全部除 xx")
    full = schedule.parse_content("全部")
    check("全部 = 8 项", len(full) == 8, str(full))
    check("全部包含 now", "now" in full and "minutely" in full)
    check("全部除 now", "now" not in schedule.parse_content("全部除now"))
    check("全部-now 也认", "now" not in schedule.parse_content("全部-now"))
    drop = schedule.parse_content("全部除now,sun")
    check("一次除多项", "now" not in drop and "sun" not in drop and len(drop) == 6, str(drop))
    check("空格分隔扣除项", len(schedule.parse_content("全部除 now sun")) == 6)
    check("逐项写法照旧", schedule.parse_content("now,daily") == ["now", "daily"])
    check("去重保序", schedule.parse_content("daily,now,daily") == ["daily", "now"])

    for bad, label in (
        ("全部除xyz", "除未知项"),
        ("全部除now,daily,hourly,air,indices,alert,sun,minutely", "全除光"),
    ):
        try:
            schedule.parse_content(bad)
            check(f"{label}要报错", False)
        except ValueError:
            check(f"{label}要报错", True)

    section("规则摘要：内容多时不糊屏")
    rule = {"kind": "clock", "at": "08:00", "content": full}
    check(
        "8 项写「全部内容」",
        "全部内容" in schedule.describe_rule(rule),
        schedule.describe_rule(rule),
    )
    rule7 = {"kind": "clock", "at": "21:30", "content": [c for c in full if c != "now"]}
    text = schedule.describe_rule(rule7)
    check("7 项写「全部除实况」", "全部除实况" in text, text)


def test_alert_change() -> None:
    section("预警：等级升高 / 降低都要能配")

    from astrbot_plugin_qweather.qw.commands import Commands
    from astrbot_plugin_qweather.qw.pushers import Pusher

    async def run() -> None:
        path = HERE / "_test_data" / "alert_change_test.json"
        if path.exists():
            path.unlink()
        store = SubStore(path)
        await store.load()
        umo = "test:GroupMessage:alert"
        loc, _ = await store.add_location(
            umo, {"name": "北辰", "id": "101030200", "lat": 39.23, "lon": 117.13}, ""
        )
        assert loc is not None
        cmds = Commands(store, None, KeyWizard(), lambda u: None, None, None)
        lid = loc["lid"]

        await cmds.handle_alert(umo, ["降级", "关"])
        cfg = await store.get_section(umo, lid, "alert")
        check("降级能单独关", cfg.get("notify_on_downgrade") is False)
        check("关降级不影响升级", cfg.get("notify_on_upgrade") is True)

        await cmds.handle_alert(umo, ["变化", "开"])
        cfg = await store.get_section(umo, lid, "alert")
        check(
            "变化 开 = 升降都开",
            cfg.get("notify_on_upgrade") is True and cfg.get("notify_on_downgrade") is True,
        )

        await cmds.handle_alert(umo, ["变化", "关"])
        cfg = await store.get_section(umo, lid, "alert")
        check(
            "变化 关 = 升降都关",
            cfg.get("notify_on_upgrade") is False and cfg.get("notify_on_downgrade") is False,
        )

        await cmds.handle_alert(umo, ["升级", "开"])
        await cmds.handle_alert(umo, ["降级", "开"])
        view = await cmds.handle_alert(umo, [])
        check("视图里写明升降两档", "降低 开" in view, view)

    asyncio.run(run())

    loc = {"name": "公司", "city": {"name": "北辰"}}
    # 和风 /v7/warning/now 的 level 就是这种两个汉字，单字、英文也得认
    hot = {"level": "黄色", "type_name": "大风", "title": "T"}
    cool = {"level": "蓝色", "type_name": "大风", "title": "T"}
    up = Pusher._alert_text(loc, hot, cool)
    down = Pusher._alert_text(loc, cool, hot)
    check("升级标 ↑", "↑" in up and "蓝色→黄色" in up, up)
    check("降级标 ↓", "↓" in down and "黄色→蓝色" in down, down)
    check("首次推送不带箭头", "↑" not in Pusher._alert_text(loc, hot, None))
    check("等级没变不带箭头", "→" not in Pusher._alert_text(loc, hot, {"level": "黄色"}))
    check("英文等级也显示成中文", "黄色" in Pusher._alert_text(loc, {"level": "yellow"}))

    section("预警等级：双字 / 单字 / 英文都要认")
    check("双字「黄色」= 2 级", level_rank("黄色") == 2)
    check("单字「黄」= 2 级", level_rank("黄") == 2)
    check("双字「橙色」= 3 级", level_rank("橙色") == 3)
    check("英文 red = 4 级", level_rank("red") == 4)
    check("大写也认", level_rank("ORANGE") == 3)
    check("认不出的不猜", level_rank("紫色") == 0)
    check("normalize 双字", normalize_level("黄色") == "yellow")
    check("normalize 白色归蓝色", normalize_level("白色") == "blue")


def test_usage_ledger() -> None:
    section("用量账本：按本地日期分桶")
    from astrbot_plugin_qweather.qw.usage import UsageLedger, split_hours

    # asOf 02:59Z = 北京时间 10:59，hours 最后一条是 02:00-02:59Z = 本地 10 点档
    stats = {
        "asOf": "2026-10-30T02:59Z",
        "success": [{"api": "Weather", "hours": [3] + [0] * 22 + [5]}],
        "errors": [{"api": "Weather", "hours": [0] * 23 + [1]}],
    }
    rows = split_hours(stats)
    check("跨了两个本地日期", set(rows) == {"2026-10-29", "2026-10-30"}, str(sorted(rows)))
    check("前一天 3 次", rows["2026-10-29"]["total"] == 3, str(rows["2026-10-29"]))
    check("当天成功 + 失败都算", rows["2026-10-30"]["total"] == 6, str(rows["2026-10-30"]))
    check("失败单独记", rows["2026-10-30"]["err"].get("Weather") == 1)
    check("空数据不进账", split_hours({"asOf": "2026-10-30T02:59Z"}) == {})

    async def run() -> None:
        path = HERE / "_test_data" / "usage_test.json"
        if path.exists():
            path.unlink()
        ledger = UsageLedger(path)
        await ledger.load()
        small = {
            "asOf": "2026-10-30T02:59Z",
            "success": [{"api": "Weather", "hours": [0] * 23 + [5]}],
            "errors": [],
        }
        big = {
            "asOf": "2026-10-30T02:59Z",
            "success": [{"api": "Weather", "hours": [1] * 24}],
            "errors": [],
        }
        await ledger.merge(small)
        await ledger.merge(big)
        await ledger.merge(small)  # 回退的旧数据不能把已有的大值改小
        today = date(2026, 10, 30)
        check("同一天取较大值", ledger.day(today)["total"] == 11, str(ledger.day(today)))
        check("前一天按 13 条算", ledger.day(date(2026, 10, 29))["total"] == 13)
        check("账本落盘", path.exists())

        again = UsageLedger(path)
        await again.load()
        check("重新加载还在", again.day(today)["total"] == 11)
        check("没记录的日期返回空账", again.day(date(2020, 1, 1))["total"] == 0)

        # 先清掉今天的记录再测「昨天没有数据」这种情况
        for name in (path, path.with_suffix(".tmp")):
            if name.exists():
                name.unlink()

    asyncio.run(run())


def test_cost() -> None:
    section("成本：阶梯价")
    from astrbot_plugin_qweather.qw.cost import FREE_QUOTA, estimate_session, month_cost

    check("免费额度内为 0", month_cost(FREE_QUOTA) == 0)
    check("一分钱都不收", month_cost(0) == 0)
    # 官方示例：100 万次 = 50000×0 + 950000×0.0007 = 665 元
    check("100 万次 ≈ 665 元", abs(month_cost(1_000_000) - 665) < 0.01, str(month_cost(1_000_000)))
    check("超出 1 万次 = 7 元", abs(month_cost(60_000) - 7) < 0.01, str(month_cost(60_000)))

    section("成本：设置折成请求次数")
    session = {
        "locations": [
            {
                "lid": "L1",
                "name": "家",
                "city": {"lat": 39.23, "lon": 117.13},
                "rules": [
                    {
                        "id": "r1",
                        "kind": "clock",
                        "at": "08:00",
                        "content": ["now", "daily"],
                        "days": "all",
                        "enabled": True,
                    }
                ],
                "precip": {"enabled": False},
                "alert": {"enabled": False},
            }
        ],
        "quota": {"rules": []},
    }
    est = estimate_session(session, today=date(2026, 10, 30))
    # 10 月 31 天，每天一条规则 × 2 项 = 62 次
    check("每天规则按当月天数算", est.total == 62, str(est.total))
    check("当月在免费额度内", est.cost == 0)
    check("工作日数在合理范围", 18 <= est.workdays <= 23, str(est.workdays))

    # 加一条只在工作日推的，次数应该明显少于一整月
    session["locations"][0]["rules"].append(
        {
            "id": "r2",
            "kind": "clock",
            "at": "21:30",
            "content": ["now"],
            "days": "workday",
            "enabled": True,
        }
    )
    est2 = estimate_session(session, today=date(2026, 10, 30))
    check("工作日规则按工作日数算", est2.total == 62 + est2.workdays, str(est2.total))

    # sun 不额外计费：全部 8 项实际 7 次
    session["locations"][0]["rules"] = [
        {
            "id": "r3",
            "kind": "clock",
            "at": "08:00",
            "content": ["now", "daily", "hourly", "air", "indices", "alert", "sun", "minutely"],
            "days": "all",
            "enabled": True,
        }
    ]
    est3 = estimate_session(session, today=date(2026, 10, 30))
    check("全部 8 项按 7 次请求算", est3.total == 31 * 7, str(est3.total))

    session["locations"][0]["rules"] = [
        {"id": "r4", "kind": "clock", "at": "08:00", "content": ["sun"], "days": "all"}
    ]
    est4 = estimate_session(session, today=date(2026, 10, 30))
    check("只要日出日落仍拉一次 daily", est4.total == 31, str(est4.total))

    off = {
        "locations": [
            {"lid": "L1", "name": "家", "city": {}, "rules": [], "precip": {}, "alert": {}}
        ],
        "quota": {"rules": []},
    }
    check("没绑地点的地址不产生请求", estimate_session(off).total == 0)


# ---------------------------------------------------------------- 主流程


def main() -> int:
    test_jwt()
    test_schedule()
    test_store()
    test_commands()
    test_location()
    test_coord()
    test_address_cmds()
    test_pusher()
    test_workday()
    test_day_rules()
    test_content_all()
    test_alert_change()
    test_quota_push()
    test_usage_ledger()
    test_cost()
    test_keys()
    test_config_reload()
    test_render()
    test_naming()
    test_plugin_class()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
