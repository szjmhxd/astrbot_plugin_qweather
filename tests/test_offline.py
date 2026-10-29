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
import sys
import types
from datetime import datetime
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
            self.context = context
            self.config = config or {}

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
from astrbot_plugin_qweather.qw.store import SubStore, blank_location  # noqa: E402

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
    section("坐标解析：固定 经度,纬度")
    check("经度在前", parse_latlon("117.21,39.13") == (39.13, 117.21))
    check("中文逗号", parse_latlon("117.21，39.13") == (39.13, 117.21))
    check("空格分隔", parse_latlon("117.21 39.13") == (39.13, 117.21))
    check("多余空格", parse_latlon("  117.21 ,  39.13 ") == (39.13, 117.21))
    check("西部经度照写不猜", parse_latlon("87.62,43.82") == (43.82, 87.62))
    check("南半球负纬度", parse_latlon("151.21,-33.87") == (-33.87, 151.21))
    check("小数位截到 2 位", parse_latlon("117.21999,39.12345") == (39.12, 117.22))
    check("城市名不是坐标", parse_latlon("杭州") is None)
    check("单个数字不是坐标", parse_latlon("117.21") is None)
    check("三个数字不是坐标", parse_latlon("117.21,39.13,5") is None)
    check("写反了：纬度超 90 → 拒绝", parse_latlon("39.13,117.21") is None)
    check("经度超范围 → 拒绝", parse_latlon("200.0,39.13") is None)
    check("空串", parse_latlon("") is None)

    section("坐标写错的提示")
    hint = coord_error("39.13,117.21")
    check("指出是顺序问题", "经度,纬度" in hint)
    check("给出可直接照抄的正确写法", "117.21,39.13" in hint, hint)
    check("完全不是数字时给格式示例", "117.21,39.13" in coord_error("天津"))

    section("坐标形态判断")
    check("两个数字算坐标形态", looks_like_coord("39.13,117.21"))
    check("写反了也算坐标形态（好报错）", looks_like_coord("95.0,200.0"))
    check("城市名不是坐标形态", not looks_like_coord("天津"))

    # 输出格式：输入、展示、请求三处统一「经度,纬度」
    check("format_coord 经度在前", format_coord(39.13, 117.21) == "117.21,39.13")
    check("geo_location 给和风", geo_location(39.13, 117.21) == "117.21,39.13")

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
    check("坐标带坐标后缀", place_label(coord) == "天津市·和平（117.21,39.13）", place_label(coord))
    check("推送短名带坐标", short_label(coord) == "和平(117.21,39.13)", short_label(coord))
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


# ---------------------------------------------------------------- 6. 凭据与引导


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
    test_keys()
    test_render()
    test_naming()
    test_plugin_class()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
