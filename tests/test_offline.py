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
    format_coord,
    geo_location,
    parse_latlon,
)
from astrbot_plugin_qweather.qw.commands import parse_rule  # noqa: E402
from astrbot_plugin_qweather.qw.keys import KeyStore, KeyWizard  # noqa: E402
from astrbot_plugin_qweather.qw.pushers import location_matches  # noqa: E402
from astrbot_plugin_qweather.qw.render import place_label, short_label  # noqa: E402
from astrbot_plugin_qweather.qw.store import SubStore  # noqa: E402

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
    section("存储：规则增删改")

    async def run() -> None:
        path = HERE / "_test_data" / "subs_test.json"
        if path.exists():
            path.unlink()
        store = SubStore(path)
        await store.load()

        umo = "test:GroupMessage:1"
        await store.set_city(umo, {"name": "天津", "id": "101030100", "lat": 39.08, "lon": 117.2})
        city = await store.get_city(umo)
        check("城市写入读出", city and city["name"] == "天津")

        rule = await store.add_rule(umo, {"kind": "clock", "at": "07:30", "content": ["now"]})
        check("规则有 id", bool(rule.get("id")))
        check("默认字段补齐", "enabled" in rule and "minutes" in rule)

        found = await store.find_rule(umo, "1")
        check("按序号 1 能找到", found and found["id"] == rule["id"])
        found_by_id = await store.find_rule(umo, rule["id"])
        check("按 id 能找到", found_by_id and found_by_id["id"] == rule["id"])

        await store.update_rule(umo, rule["id"], {"at": "08:00", "enabled": False})
        updated = await store.find_rule(umo, rule["id"])
        check("改时间与开关", updated["at"] == "08:00" and updated["enabled"] is False)

        await store.mark_fired(umo, rule["id"], "2026-10-29 08:00")
        state = await store.state(umo)
        check("fired 戳落盘", state["fired"].get(rule["id"]) == "2026-10-29 08:00")

        await store.mark_precip_state(umo, "101030100", "wet")
        state = await store.state(umo)
        check("降水状态落盘", state["precip"].get("101030100") == "wet")

        await store.remember_alert(umo, "101030100", "aid-1", {"level": "黄色", "at": "now"})
        state = await store.state(umo)
        check("预警去重记录", "aid-1" in (state["alerts"].get("101030100") or {}))

        # 重新加载：验证持久化
        store2 = SubStore(path)
        await store2.load()
        reloaded = await store2.find_rule(umo, rule["id"])
        check("重启后规则还在", reloaded is not None and reloaded["at"] == "08:00")
        state2 = await store2.state(umo)
        check("重启后去重状态还在", state2["fired"].get(rule["id"]) == "2026-10-29 08:00")

        ok = await store.delete_rule(umo, rule["id"])
        check("删除规则", ok and not await store.list_rules(umo))
        check("删除不解绑城市", (await store.get_city(umo)) is not None)

        cleared = await store.clear_city(umo)
        check("解除城市绑定", cleared and (await store.get_city(umo)) is None)

        # 脏文件容错
        path.write_text(
            '{"sessions": {"x": {"rules": [{"kind":"clock","id":"abc"}, "junk"]}}}',
            encoding="utf-8",
        )
        store3 = SubStore(path)
        await store3.load()
        rules = await store3.list_rules("x")
        check("脏规则被清理", len(rules) == 1 and rules[0]["kind"] == "clock")

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
    section("坐标解析")
    check("经纬度顺序：纬度在前", parse_latlon("39.13,117.21") == (39.13, 117.21))
    check("经纬度顺序：经度在前", parse_latlon("117.21,39.13") == (39.13, 117.21))
    check("中文逗号", parse_latlon("39.13，117.21") == (39.13, 117.21))
    check("空格分隔", parse_latlon("39.13 117.21") == (39.13, 117.21))
    check("多余空格", parse_latlon("  39.13 ,  117.21 ") == (39.13, 117.21))
    check("西部经度也能判对", parse_latlon("87.62,43.82") == (43.82, 87.62))
    check("负数坐标", parse_latlon("-33.87,151.21") == (-33.87, 151.21))
    check("小数位截到 2 位", parse_latlon("39.12345,117.21999") == (39.12, 117.22))
    check("城市名不是坐标", parse_latlon("杭州") is None)
    check("单个数字不是坐标", parse_latlon("39.13") is None)
    check("三个数字不是坐标", parse_latlon("39.13,117.21,5") is None)
    check("超范围纬度", parse_latlon("95.0,117.21") is None)
    check("空串", parse_latlon("") is None)

    # 输出格式：给人看用「纬度,经度」，给和风用「经度,纬度」
    check("format_coord 给人看", format_coord(39.13, 117.21) == "39.13,117.21")
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
    check("坐标带坐标后缀", place_label(coord) == "天津市·和平（39.13,117.21）", place_label(coord))
    check("推送短名带坐标", short_label(coord) == "和平(39.13,117.21)", short_label(coord))
    check("推送短名不带省", short_label(district) == "武清", short_label(district))


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


def test_plugin_class() -> None:
    section("插件类：指令字符串处理")
    from astrbot_plugin_qweather.main import QWeatherPlugin

    check("/天气 杭州 → ['杭州']", QWeatherPlugin._strip_command("/天气 杭州", "天气") == ["杭州"])
    check(
        "/天气推送 加 每天 07:30 → 4 段",
        QWeatherPlugin._strip_command("/天气推送 加 每天 07:30", "天气推送")
        == ["加", "每天", "07:30"],
    )
    check(
        "全角空格也能切",
        QWeatherPlugin._strip_command("/天气　设置　天津", "天气") == ["设置", "天津"],
    )
    check(
        "@机器人 前缀被去掉", QWeatherPlugin._strip_command("@bot /天气 杭州", "天气") == ["杭州"]
    )
    check("城市粗筛：天津", QWeatherPlugin._looks_like_city("天津"))
    check("城市粗筛：挡掉问句", not QWeatherPlugin._looks_like_city("天气怎么样？"))
    check("城市粗筛：挡掉指令", not QWeatherPlugin._looks_like_city("/天气"))
    check("城市粗筛：挡掉长句", not QWeatherPlugin._looks_like_city("今天天津的天气情况怎么样呢"))
    check("城市粗筛：放行坐标", QWeatherPlugin._looks_like_city("39.13,117.21"))
    check(
        "设置坐标时参数不被空格切坏",
        QWeatherPlugin._strip_command("/天气 设置 39.13 117.21", "天气")
        == ["设置", "39.13", "117.21"],
    )

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


# ---------------------------------------------------------------- 主流程


def main() -> int:
    test_jwt()
    test_schedule()
    test_store()
    test_commands()
    test_location()
    test_coord()
    test_keys()
    test_render()
    test_plugin_class()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
