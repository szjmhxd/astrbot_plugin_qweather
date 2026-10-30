"""和风天气订阅推送插件。

指令
----
所有指令都挂在一条主指令下，前缀可配（默认 ``qweather``，不允许为空）：

``/qweather 天气``　查当前地址的天气，也可以在这里改 / 清除地址
``/qweather 地址``　管理多个地址，每个地址有独立的推送方案
``/qweather 推送``　定时推送规则（每天、每周、每月、每小时、日出日落）
``/qweather 雨雪``　降水开始 / 结束提醒，可配多个可提醒时段
``/qweather 预警``　灾害预警轮询，同一条不重复推、等级升高再推
``/qweather 工作日`` 今天 / 指定日期是不是工作日（含节假日与调休）
``/qweather key``　 本会话专用凭据（分步引导），可随时恢复系统默认
``/qweather 额度``　账户余额与最近 24 小时请求量
``/qweather 额度推送`` 把额度报告按固定时间推送（本会话设过专用 key 就用专用 key）

为什么不做无前缀指令
--------------------
``/天气`` 这种裸指令太容易和别的天气插件撞名：AstrBot 会把同一条消息投给所有匹
配的 handler，用户会收到两份回答。所以整族指令收在一条主指令下，前缀写进配置，
改它有用、留空无效（空等于把撞名问题还回去）。

开发约定
--------
``@filter.command`` 的指令名在 import 时就定死了，配置项改不动它。这里改用「一条
消息处理器 + 自己解析前缀」：``@filter.event_message_type(ALL)`` 拿全部消息，前缀
不匹配立刻 return，不拦别的插件也不拦 LLM。已核对 AstrBot 的 WakingCheckStage /
ProcessStage：LLM 只在 ``is_at_or_wake_command`` 为真且没有发送过消息时才会被调用，
所以这个 ALL 处理器不会让机器人变得有问必答（pending 输入本来也是这么做的）。

另外：所有 ``@filter.*`` 必须写在 ``main.py`` 的插件类里。AstrBot 用
``handler_module_path`` 精确匹配 handler 归属，写在其它模块会被静默丢弃。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .qw import naming, render
from .qw.card import CardRenderer
from .qw.client import QWeatherClient, QWeatherError, coord_error, looks_like_coord, parse_latlon
from .qw.commands import Commands, join_place
from .qw.keys import KeyStore, KeyWizard
from .qw.pushers import Pusher
from .qw.store import SubStore
from .qw.usage import UsageLedger
from .qw.workday import (
    DEFAULT_CALENDAR_DATE,
    DEFAULT_CLEANUP_DATE,
    DEFAULT_FETCH_DATE,
    WorkdayCalendar,
    parse_month_day,
)

PLUGIN_NAME = "astrbot_plugin_qweather"

ASK_CITY_TEXT = (
    "你还没告诉我地点。平台不会把用户位置给机器人，所以这一步要你自己说：\n"
    "直接回复城市名（例如：天津）或坐标（39.13,117.21），90 秒内有效，我会记住。"
)

_SENTENCE_PUNCT = "？?。！!；;，,\n\r"
_STOPWORDS = ("天气", "怎么样", "如何", "多少", "什么", "吗", "呢", "查", "帮我", "今天", "明天")
_CMD_PREFIXES = "/～~!！#"
_WIZARD_CANCEL_WORDS = ("取消", "cancel", "退出", "算了")

# 子指令别名 -> 处理函数名（全部指向本类的方法）
_SUB_ALIASES = {
    "天气": "_cmd_weather",
    "weather": "_cmd_weather",
    "地址": "_cmd_address",
    "位置": "_cmd_address",
    "address": "_cmd_address",
    "loc": "_cmd_address",
    "推送": "_cmd_push",
    "订阅": "_cmd_push",
    "定时": "_cmd_push",
    "天气推送": "_cmd_push",
    "push": "_cmd_push",
    "雨雪": "_cmd_precip",
    "降水": "_cmd_precip",
    "雨雪提醒": "_cmd_precip",
    "precip": "_cmd_precip",
    "预警": "_cmd_alert",
    "警报": "_cmd_alert",
    "预警推送": "_cmd_alert",
    "alert": "_cmd_alert",
    "warning": "_cmd_alert",
    "key": "_cmd_key",
    "密钥": "_cmd_key",
    "凭据": "_cmd_key",
    "额度": "_cmd_quota",
    "余额": "_cmd_quota",
    "用量": "_cmd_quota",
    "quota": "_cmd_quota",
    "额度推送": "_cmd_quota_push",
    "额度订阅": "_cmd_quota_push",
    "quota_push": "_cmd_quota_push",
    "工作日": "_cmd_workday",
    "上班日": "_cmd_workday",
    "节假日": "_cmd_workday",
    "workday": "_cmd_workday",
    "设置": "_cmd_settings",
    "配置": "_cmd_settings",
    "settings": "_cmd_settings",
    "成本": "_cmd_cost",
    "费用": "_cmd_cost",
    "花钱": "_cmd_cost",
    "估算": "_cmd_cost",
    "cost": "_cmd_cost",
    "帮助": "_help_text",
    "help": "_help_text",
    "用法": "_help_text",
    "?": "_help_text",
    "？": "_help_text",
}


def help_text() -> str:
    """主帮助。指令写法全部由命名模块生成，前缀改了这里自动跟着变。"""
    c = naming.cmd
    p = naming.prefix()
    return f"""和风天气（指令前缀 /{p}，可在插件配置里改）

天气：
{c("天气")}　　　　　　　查当前地址的天气
{c("天气 杭州")}　　　　临时查另一个地点
{c("天气 设置 杭州")}　 把当前地址改成杭州
{c("天气 清除")}　　　　删掉当前地址

地址管理（一个会话可以多个地址，推送方案各自独立）：
{c("地址")}　　　　　　　列出所有地址
{c("地址 加 天津")}　　　新增地址
{c("地址 用 2")}　　　　 切换当前地址
{c("地址 删 2")}　　　　 删除地址
{c("地址 帮助")}　　　　 全部地址指令

地点可以写城市名或坐标：
· 城市名最多到区县（省 / 市 / 区县），街道乡镇查不到
· 坐标写作「纬度,经度」，例如 39.13,117.21（天津），精度约 1 km
· 度数分秒也认：39°13'53",117°8'2"（照样是纬度在前）
· 坐标会反查出所属区县，名字照常显示，预警按该区县匹配

推送：
{c("推送")}　　　　　　　定时推送规则（作用在当前地址）
{c("雨雪")}　　　　　　　降水开始 / 结束提醒
{c("预警")}　　　　　　　灾害预警提醒
　想指定别的地址，在命令里加「地址=2」
　推送规则加「日=工作日」，就只在上班日推（调休上班算工作日）
　内容写「全部」就是 8 项全要，「全部除now」是除实况以外全要

工具：
{c("工作日")}　　　　　　今天 / 指定日期是不是工作日（含节假日与调休）

配置：
{c("key")}　　　　　　　 查看凭据（支持本会话专用 key）
{c("额度")}　　　　　　　余额与当天 / 前一天请求量
{c("额度推送")}　　　　　把额度报告按固定时间推送（用本会话的 key）
{c("设置")}　　　　　　　导出本会话的全部设置
{c("成本")}　　　　　　　按当前设置算每月请求量和花多少钱"""


class QWeatherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        # AstrBot v4.29 的 Star.__init__ 不再把 config 存成 self.config，插件得自己留一份。
        # 以前漏了这步，self.config 直接抛 AttributeError，被 _raw 的兜底吃掉，所有配置项
        # 都退化成默认值——表现就是「面板里明明填了 key，插件一直说没配」。
        # 这里以 _conf 为准（同时补上 self.config，兼容仍会自己赋值的老版本框架）。
        self._conf: dict = config if isinstance(config, dict) else {}
        try:
            self.config = self._conf  # type: ignore[assignment]
        except Exception:  # noqa: BLE001 框架把 config 定义成只读属性时不影响主流程
            pass
        # 配置文件的 mtime，用来发现「WebUI 保存了但插件没重载」
        self._cfg_mtime: int = -1
        # 配置文件路径，第一次用到时再猜（框架给不出时按数据目录反推）
        self._cfg_file: str | None = None
        data_dir = self._resolve_data_dir()
        self._data_dir = data_dir

        # 前缀必须非空：空前缀等于允许「/天气」这种裸指令，会和别的插件撞名
        self._prefix = naming.set_prefix(self._cfg_str("command_prefix"))

        self._subs = SubStore(data_dir / "subs.json")
        self._keys = KeyStore(
            data_dir / "keys.json",
            default_iss=self._cfg_str("iss"),
            default_sub=self._cfg_str("sub"),
            default_kid=self._cfg_str("kid"),
            default_host=self._cfg_str("host"),
            default_pem=self._cfg_str("private_key"),
        )
        self._wizard = KeyWizard(timeout=300)
        # 工作日日历：节假日 / 调休落盘到 workdays.json，按配置的三个日期自动轮换
        self._calendar = WorkdayCalendar(
            data_dir / "workdays.json",
            fetch_date=parse_month_day(self._cfg_str("next_year_fetch_date"), DEFAULT_FETCH_DATE),
            calendar_date=parse_month_day(
                self._cfg_str("next_year_calendar_date"), DEFAULT_CALENDAR_DATE
            ),
            cleanup_date=parse_month_day(self._cfg_str("cleanup_date"), DEFAULT_CLEANUP_DATE),
            timeout=self._cfg_float("http_timeout", 12.0),
        )
        # 用量账本：接口只给最近 24 小时，切不出完整的「昨天」，所以自己按天攒
        self._ledger = UsageLedger(data_dir / "usage.json")
        # 图片卡片：Playwright 在容器里通常没有，检测不到就只发文字（不影响推送）
        self._cards = CardRenderer(
            data_dir / "icons",
            enabled=self._cfg_bool("image_cards", True),
        )
        self._commands = Commands(
            self._subs,
            self._keys,
            self._wizard,
            self._client_for,
            self._resolve_place,
            self._calendar,
            self._ledger,
        )
        self._pusher = Pusher(
            self._subs,
            self._keys,
            self._client_for,
            self._send,
            self._calendar,
            self._ledger,
            self._cards,
        )

        self._clients: dict[str, QWeatherClient] = {}
        self._pending_city: dict[str, float] = {}
        self._cards_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        await self._subs.load()
        await self._keys.load()
        await self._calendar.load()
        await self._ledger.load()
        # 启动时补一次：当年缺数据要拉，过了 11-01 还要顺手把次年的拉下来。
        # 后台跑，不阻塞 AstrBot 启动。
        self._calendar.kick(datetime.now(), startup=True)

        sessions = await self._subs.all_sessions()
        locations = [loc for s in sessions.values() for loc in s.get("locations") or []]
        rules = sum(len(loc.get("rules") or []) for loc in locations)
        quota_rules = sum(len((s.get("quota") or {}).get("rules") or []) for s in sessions.values())
        key_state = "就绪" if self._keys.default_ready else f"未配置（{self._keys.default_error}）"
        today = datetime.now().date()
        calendar_state = "已就绪" if self._calendar.has_data(today.year) else "待抓取"
        logger.info(
            f"[和风天气] 已启动：{len(locations)} 个地址（{len(sessions)} 个会话），"
            f"共 {rules} 条推送规则、{quota_rules} 条额度推送；"
            f"系统默认 key {key_state}；指令前缀 /{self._prefix}；"
            f"{today.year} 年工作日数据 {calendar_state}"
        )
        cfg_path = self._config_path
        if cfg_path:
            logger.info(f"[和风天气] 配置文件：{cfg_path}")
        else:
            cfg_dir = self._data_dir.parent.parent / "config"
            logger.info(
                f"[和风天气] 配置文件：没定位到（找不到 {cfg_dir} 下的 "
                f"{PLUGIN_NAME}_config.json，改配置后需要手动重载插件）"
            )
        logger.info(f"[和风天气] 配置读到 {len(self._conf)} 项")
        for line in self._keys.default_fingerprint():
            logger.info(f"[和风天气] 配置项 {line}")
        if not self._keys.default_ready:
            logger.warning(
                "[和风天气] 系统默认 key 还没配好。在 WebUI → 插件 → 本插件 → 配置里填 "
                "iss/sub/kid/host/私钥，点「保存并关闭」即可，插件会自动重读，不用重载；"
                "也可以让每个会话发「/qweather key 设置」用自己的 key。"
            )
        if self._cfg_bool("push_enabled", True):
            self._pusher.start()

        # 图片卡片：Chromium 启动要半秒到两秒，放后台探测，别拖慢 AstrBot 启动。
        # 探测结果（成功 / 缺 Playwright / 缺浏览器）由 CardRenderer 自己打日志。
        if self._cfg_bool("image_cards", True):
            self._cards_task = asyncio.create_task(self._cards.probe())

    async def terminate(self) -> None:
        await self._pusher.stop()
        try:
            await self._cards.close()  # 关掉 Chromium，否则进程退出前会挂住
        except Exception:  # noqa: BLE001
            pass
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001 关闭失败不该影响卸载
                pass
        self._clients.clear()
        self._pending_city.clear()

    # ---------------------------------------------------------------- 消息入口

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """全部消息的唯一入口：先按前缀认指令，认不出来再看是不是在等用户补输入。"""
        if not self._cfg_bool("enabled", True):
            return

        umo = self._umo(event)
        text = event.message_str or ""

        matched, sub, args = naming.split(text)
        if matched:
            # 认领这条消息：不再传给其它插件的 handler，也不让 LLM 接着回一遍。
            # 少了这一步，私聊里发「qweather 天气」（不带 /、不 @）会同时收到插件答案和
            # 模型的自由发挥。
            event.stop_event()
            self._pending_city.pop(umo, None)
            self._wizard.cancel(umo)
            async for result in self._dispatch(event, umo, sub, args):
                yield result
            return

        try:
            async for result in self._handle_pending_input(event, umo, text):
                yield result
        except Exception:  # noqa: BLE001 补输入路径也不能把异常抛回框架
            logger.exception("[和风天气] 处理待补输入时出错")

    async def _dispatch(self, event: AstrMessageEvent, umo: str, sub: str, args: list[str]):
        """子指令分发。写错子指令时给出帮助，而不是沉默。"""
        if not sub:
            yield event.plain_result(help_text())
            return

        handler_name = _SUB_ALIASES.get(sub.lower())
        if handler_name is None:
            yield event.plain_result(f"不认识的子指令「{sub}」。\n\n" + help_text())
            return

        try:
            result = await getattr(self, handler_name)(umo, args)
        except ValueError as exc:
            result = f"❌ {exc}"
        except QWeatherError as exc:
            result = f"❌ {exc}"
        except Exception as exc:  # noqa: BLE001 兜底：一个 handler 的 bug 不该让机器人失声
            logger.exception(f"[和风天气] 执行 {sub} 时出错")
            result = (
                f"❌ 这个操作出错了：{exc}\n可以发「{naming.cmd('帮助')}」看用法，或过会儿再试。"
            )
        if result:
            yield event.plain_result(result)

    async def _handle_pending_input(self, event: AstrMessageEvent, umo: str, text: str):
        """接管「等用户补输入」的消息：key 设置引导、或反问后的地点名。"""
        text = (text or "").strip()
        if not text:
            return
        if text[0] in _CMD_PREFIXES:  # 用户改主意发别的指令了，状态作废
            self._wizard.cancel(umo)
            self._pending_city.pop(umo, None)
            return

        # 1) key 设置分步引导
        state = self._wizard.get(umo)
        if state is not None:
            if text in _WIZARD_CANCEL_WORDS:
                self._wizard.cancel(umo)
                event.stop_event()
                yield event.plain_result("已取消 key 设置。")
                return
            try:
                prompt, finished = self._wizard.feed(umo, text)
            except ValueError as exc:
                event.stop_event()
                yield event.plain_result(f"❌ {exc}\n请重新输入，或发「取消」退出。")
                return
            event.stop_event()
            if finished:
                yield event.plain_result(await self._commands.finish_key_wizard(umo, finished))
            else:
                yield event.plain_result(prompt or "")
            return

        # 2) 反问后的下一条短文本
        deadline = self._pending_city.get(umo)
        if deadline is None:
            return
        if deadline < time.monotonic():
            self._pending_city.pop(umo, None)
            return
        if not self._looks_like_place(text):
            self._pending_city.pop(umo, None)
            return
        place, err = await self._resolve_place(umo, text)
        if place is None:
            self._pending_city.pop(umo, None)
            if err and not err.startswith("查询失败"):
                yield event.plain_result(err)
            return

        self._pending_city.pop(umo, None)
        event.stop_event()
        loc, status = await self._subs.add_location(umo, place)
        if loc is None:
            yield event.plain_result(status or "添加地址失败。")
            return
        yield event.plain_result(
            f"已记住：{render.loc_label(loc)}。以后直接发「{naming.cmd('天气')}」就行。\n\n"
            + await self._commands.render_for_location(umo, loc)
        )

    # ---------------------------------------------------------------- 子指令

    async def _help_text(self, umo: str, args: list[str]) -> str:  # noqa: ARG002 统一签名
        return help_text()

    async def _cmd_weather(self, umo: str, args: list[str]) -> str:
        """查天气 / 设置当前地址的地点"""
        head = args[0].lower() if args else ""

        if head in ("帮助", "help", "?", "？"):
            return help_text()

        if head in ("清除", "解绑", "删除", "clear"):
            return await self._drop_active(umo)

        if head in ("设置", "set", "绑定"):
            place_text = join_place(args[1:])
            if not place_text:
                return (
                    f"用法：{naming.cmd('天气 设置 杭州')}\n"
                    f"　　　{naming.cmd('天气 设置 39.13,117.21')}　（纬度,经度，精度约 1 km）"
                )
            return await self._set_active_place(umo, place_text)

        if args:
            return await self._query_place(umo, join_place(args))

        loc = await self._subs.active(umo)
        if loc is not None:
            return await self._commands.render_for_location(umo, loc)

        self._pending_city[umo] = time.monotonic() + max(15, self._cfg_int("pending_timeout", 90))
        return ASK_CITY_TEXT

    async def _cmd_address(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_address(umo, args)

    async def _cmd_push(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_push(umo, args)

    async def _cmd_precip(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_precip(umo, args)

    async def _cmd_alert(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_alert(umo, args)

    async def _cmd_key(self, umo: str, args: list[str]) -> str:
        text, _need_input = await self._commands.handle_key(umo, args)
        return text

    async def _cmd_quota(self, umo: str, args: list[str]) -> str:
        # 「/qweather 额度 推送 ...」与「/qweather 额度推送 ...」等价，两种写法都认
        if args and args[0].lower() in ("推送", "订阅", "push"):
            return await self._commands.handle_quota_push(umo, args[1:])
        return await self._commands.handle_quota(umo)

    async def _cmd_quota_push(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_quota_push(umo, args)

    async def _cmd_workday(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_workday(umo, args)

    async def _cmd_settings(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_settings(umo, args)

    async def _cmd_cost(self, umo: str, args: list[str]) -> str:
        return await self._commands.handle_cost(umo, args)

    # ---------------------------------------------------------------- 天气实现

    async def _client_for(self, umo: str) -> QWeatherClient:
        """按本会话解析出的凭据复用客户端（同一套 key 共用一个连接池与 JWT）。"""
        self._sync_config()  # 先确认配置是最新的，否则 WebUI 刚填的 key 可能还用不上
        resolved = await self._keys.resolve(umo)
        if resolved is None:
            raise QWeatherError(
                0,
                "没有可用的 key",
                f"{self._keys.default_error or '请先配置系统 key'}\n"
                "填法：WebUI → 插件 → 本插件 → 配置，填 iss/sub/kid/host/私钥，"
                "点「保存并关闭」即可（插件会自动重读，不用重载）；"
                f"也可以发 /{self._prefix} key 设置 用本会话自己的 key。",
            )
        fingerprint = resolved.key.fingerprint()
        client = self._clients.get(fingerprint)
        if client is None:
            client = QWeatherClient(resolved.key, timeout=self._cfg_float("http_timeout", 12.0))
            self._clients[fingerprint] = client
        return client

    async def _resolve_place(self, umo: str, text: str) -> tuple[dict[str, Any] | None, str]:
        """把用户输入变成可查询的地点。

        两种输入：坐标（``纬度,经度``，也接受空格分隔）和城市名。
        坐标会先反查最近的行政区——名字用来显示，LocationID 用来匹配预警列表，
        但 ``lat``/``lon`` 保留用户给的原值，不能换成行政区中心点，否则精细度就丢光了。
        """
        coord = parse_latlon(text)
        if coord is None:
            # 是「两个数字」但没解析成功，几乎都是顺序写反了，直接给出正确写法
            if looks_like_coord(text):
                return None, coord_error(text)
            try:
                client = await self._client_for(umo)
                places = await client.city_lookup(text, 1)
            except QWeatherError as exc:
                return None, f"查询失败：{exc}"
            if not places:
                return None, (
                    f"没找到「{text}」。\n"
                    "· 城市只写名字，例如「杭州」「武清」，越级写「天津 武清」查不到\n"
                    "· 要更精细就发坐标：「39.13,117.21」（纬度,经度）"
                )
            place = dict(places[0])
            place["coord"] = False
            return place, ""

        lat, lon = coord
        try:
            client = await self._client_for(umo)
            near = await client.reverse_lookup(lat, lon) or {}
        except QWeatherError as exc:
            return None, f"查询失败：{exc}"
        return {
            "name": near.get("name") or "坐标点",
            "id": near.get("id") or "",
            "adm1": near.get("adm1") or "",
            "adm2": near.get("adm2") or "",
            "lat": lat,
            "lon": lon,
            "coord": True,
            # 坐标落在区县里，预警按该区县精确匹配，不要退化成全市
            "level": "district",
        }, ""

    async def _set_active_place(self, umo: str, text: str) -> str:
        """把当前地址的地点改掉；没有地址就新建一个。"""
        place, err = await self._resolve_place(umo, text)
        if place is None:
            return err

        current = await self._subs.active(umo)
        if current is None:
            loc, status = await self._subs.add_location(umo, place)
            if loc is None:
                return status or "添加地址失败。"
            hint = "\n坐标精度约 1 km（0.01°）。" if place.get("coord") else ""
            return f"已设为当前位置：{render.loc_label(loc)}{hint}\n\n" + (
                await self._commands.render_for_location(umo, loc)
            )

        updated = await self._subs.update_location(umo, current["lid"], {"city": place})
        if updated is None:
            return f"❌ 地址已被删除，重发「{naming.cmd('地址')}」看看。"
        hint = ""
        if place.get("coord"):
            named = naming.cmd(f"天气 设置 {place['name']}")
            hint = f"\n坐标精度约 1 km（0.01°）。想回到行政区中心就发「{named}」。"
        return f"已更新当前位置：{render.loc_label(updated)}{hint}\n\n" + (
            await self._commands.render_for_location(umo, updated)
        )

    async def _query_place(self, umo: str, text: str) -> str:
        place, err = await self._resolve_place(umo, text)
        if place is None:
            return err
        temp = {"lid": "", "name": "", "city": place}
        text_out = await self._commands.render_for_location(umo, temp)
        current = await self._subs.active(umo)
        if current and render.loc_label(current) != render.loc_label(temp):
            text_out += (
                f"\n\n（临时查询。当前地址还是 {render.loc_label(current)}，"
                f"要改发「{naming.cmd('天气 设置 ...')}」）"
            )
        return text_out

    async def _drop_active(self, umo: str) -> str:
        current = await self._subs.active(umo)
        if current is None:
            return f"你还没有地址，直接发「{naming.cmd('天气 城市名')}」即可"
        locations = await self._subs.locations(umo)
        if len(locations) > 1:
            removed = await self._subs.delete_location(umo, current["lid"])
            still = await self._subs.active(umo)
            tail = f"\n当前地址：{render.loc_label(still)}" if still else ""
            return (
                f"已删除地址：{render.loc_label(removed or current)}{tail}"
                if removed
                else "删除失败。"
            )
        removed = await self._subs.delete_location(umo, current["lid"])
        return (
            f"已删除地址：{render.loc_label(removed or current)}\n"
            f"顺便说一句：只剩一个地址时删掉就等于重置，下次发「{naming.cmd('天气')}」"
            "会重新问你地点。"
            if removed
            else "删除失败。"
        )

    # ---------------------------------------------------------------- 发送

    async def _send(self, umo: str, text: str, image: bytes | None = None) -> bool:
        """主动发消息：先文字，再图片。

        分成两条消息发是有意的——图片在有些平台上会失败（大小限制、适配器不带媒体上传），
        文字先落地至少保证用户拿到了信息。图片发不出去只记日志，不影响返回值。

        两条路：优先把 umo 直接交给框架；不行再拆成 ``MessageSesion``。
        拆解路径依赖 ``umo`` 的 ``平台:类型:会话ID`` 结构，AstrBot 各版本稳定，但字段名
        有差异，所以外面套一层 try，失败只记日志——一条推送发不出去不该拖垮循环。
        """
        if not text.strip():
            return False
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain
        except ImportError as exc:
            logger.warning(f"[和风天气] 无法导入消息链：{exc}")
            return False

        ok = await self._dispatch_chain(umo, MessageChain([Plain(text)]))
        if ok and image:
            await self._send_image(umo, image)
        return ok

    async def _send_image(self, umo: str, png: bytes) -> bool:
        """发一张 PNG。失败只记日志，绝不影响文字那条。"""
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Image
        except ImportError:
            return False
        path = self._data_dir / "cards"
        name = f"card_{int(time.time() * 1000)}.png"
        target = path / name
        try:
            path.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
        except OSError as exc:
            logger.warning(f"[和风天气] 卡片写入失败：{exc}")
            return False
        try:
            try:
                component = Image.fromFileSystem(str(target))
            except Exception:  # noqa: BLE001 老版本没有这个类方法，退回 base64
                component = Image.fromBytes(png)
            sent = await self._dispatch_chain(umo, MessageChain([component]))
            if not sent:
                logger.info(f"[和风天气] 图片发送失败（{umo}），本轮只发文字")
            return sent
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[和风天气] 图片发送异常（{umo}）：{exc}")
            return False
        finally:
            try:
                target.unlink(missing_ok=True)  # 发完就删，别把 data 目录塞满
            except OSError:
                pass

    async def _dispatch_chain(self, umo: str, chain) -> bool:
        """把一条消息链交给框架，两条路都试。"""
        # 路 1：直接传 umo 字符串（新版框架支持）
        try:
            if await self.context.send_message(umo, chain):
                return True
        except Exception:  # noqa: BLE001 参数类型不匹配时走下面的拆解路径
            pass

        # 路 2：拆成 MessageSesion
        try:
            from astrbot.core.platform.message_session import MessageSesion

            session = self._build_session(umo, MessageSesion)
            if session is None:
                return False
            return bool(await self.context.send_message(session, chain))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[和风天气] 推送发送失败（{umo}）：{exc}")
            return False

    @staticmethod
    def _build_session(umo: str, session_cls):
        from_str = getattr(session_cls, "from_str", None)
        if callable(from_str):
            try:
                return from_str(umo)
            except Exception:  # noqa: BLE001 格式不符就走手工拆
                pass
        parts = (umo or "").split(":", 2)
        if len(parts) != 3:
            return None
        platform_name, message_type, session_id = parts
        try:
            from astrbot.api.platform import MessageType

            mapping = {
                "groupmessage": MessageType.GROUP_MESSAGE,
                "friendmessage": MessageType.FRIEND_MESSAGE,
                "group": MessageType.GROUP_MESSAGE,
                "friend": MessageType.FRIEND_MESSAGE,
            }
            mtype = mapping.get(message_type.lower().replace("_", ""), message_type)
        except ImportError:
            mtype = message_type
        return session_cls(platform_name=platform_name, message_type=mtype, session_id=session_id)

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _umo(event: AstrMessageEvent) -> str:
        """会话唯一标识。AstrBot 原生字段，规则和 key 都挂在它上面。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        if umo:
            return str(umo)
        platform = ""
        for attr in ("get_platform_id", "get_platform_name"):
            getter = getattr(event, attr, None)
            if callable(getter):
                try:
                    platform = str(getter() or "")
                except Exception:  # noqa: BLE001
                    platform = ""
                if platform:
                    break
        sender = ""
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                sender = str(getter() or "")
            except Exception:  # noqa: BLE001
                sender = ""
        sid = ""
        for attr in ("get_group_id", "session_id"):
            value = getattr(event, attr, None)
            sid = str(value() or "") if callable(value) else str(value or "")
            if sid:
                break
        return (
            f"{platform or 'unknown'}:{'GroupMessage' if sid else 'FriendMessage'}:{sid or sender}"
        )

    @staticmethod
    def _looks_like_place(text: str) -> bool:
        if not text:
            return False
        # 坐标里带逗号/空格，会被下面的标点规则挡掉，先单独放行
        if looks_like_coord(text) or parse_latlon(text):
            return True
        if len(text) > 12:
            return False
        if text[0] in _CMD_PREFIXES:
            return False
        if any(ch in text for ch in _SENTENCE_PUNCT):
            return False
        return not any(word in text for word in _STOPWORDS)

    def _resolve_data_dir(self) -> Path:
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001 仅在框架异常时触发
            fallback = Path("data") / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(f"[和风天气] 取不到数据目录（{exc}），回退到 {fallback}")
            return fallback

    @property
    def _config_path(self) -> str:
        """插件配置文件的落盘路径（找不到就空串）。

        框架给的 ``AstrBotConfig`` 带 ``config_path``，直接用；万一拿不到（配置对象是被
        替换成普通 dict 的场合），按数据目录反推：数据目录是 ``data/plugin_data/<插件名>``，
        配置文件在同级的 ``data/config/<插件名>_config.json``。
        """
        if self._cfg_file is not None:
            return self._cfg_file
        path = ""
        try:
            path = str(getattr(self._conf, "config_path", "") or "")
        except Exception:  # noqa: BLE001 配置对象奇形怪状时按没有处理
            path = ""
        if not path:
            try:
                guess = self._data_dir.parent.parent / "config" / f"{PLUGIN_NAME}_config.json"
                if guess.is_file():
                    path = str(guess)
            except Exception:  # noqa: BLE001
                path = ""
        self._cfg_file = path
        return path

    def _sync_config(self) -> None:
        """WebUI 保存配置不会重载插件，这里按文件 mtime 自动重读一次。

        AstrBot 的保存接口只写 ``data/config/<插件名>_config.json``，正在跑的这个实例仍
        持有旧 dict，于是出现「面板里明明填了 key，插件却说没配」。每次读配置前 stat 一下
        文件，mtime 变了就把新值合并进来，顺手刷新系统默认 key，省掉手动重载。
        """
        path = self._config_path
        if not path:
            return
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return
        if self._cfg_mtime < 0:  # 首次只是记下时间戳，值已经由框架加载好了
            self._cfg_mtime = mtime
            return
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            self._conf.update(data)
        except Exception:  # noqa: BLE001 配置对象只读时放弃热更新，不影响主流程
            return
        keys = getattr(self, "_keys", None)
        if keys is not None:
            keys.set_defaults(
                iss=str(data.get("iss") or ""),
                sub=str(data.get("sub") or ""),
                kid=str(data.get("kid") or ""),
                host=str(data.get("host") or ""),
                pem=str(data.get("private_key") or ""),
            )
            logger.info(f"[和风天气] 配置已保存，自动重读：系统默认 key {keys.default_state()}")
        # 前缀是构造时定死的，不跟着换会让用户以为「改了没用」
        new_prefix = naming.set_prefix(str(data.get("command_prefix") or ""))
        old_prefix = getattr(self, "_prefix", None)
        if old_prefix is not None and new_prefix != old_prefix:
            logger.info(f"[和风天气] 指令前缀 {old_prefix} → {new_prefix}")
            self._prefix = new_prefix

    def _raw(self, key: str, default: Any = None) -> Any:
        self._sync_config()
        try:
            value = self._conf.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self._raw(key, default)).strip()

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._raw(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(float(self._raw(key, default)))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._raw(key, default))
        except (TypeError, ValueError):
            return default
