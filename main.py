"""和风天气订阅推送插件。

功能
----
``/天气``　　　查天气 / 绑定城市
``/天气推送``　定时推送规则（每天、每周、每月、每小时、日出日落）
``/雨雪提醒``　降水开始 / 结束提醒，可配多个可提醒时段
``/预警推送``　灾害预警轮询，同一条不重复推、等级升高再推
``/key``　　　本会话专用凭据（分步引导），可随时恢复系统默认
``/额度``　　　账户余额与最近 24 小时请求量

开发约定
--------
所有 ``@filter.*`` 必须写在 ``main.py`` 的插件类里。AstrBot 用 ``handler_module_path``
精确匹配 handler 归属，写在其它模块会被静默丢弃——这个坑在本地天气插件上踩过。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .qw.client import QWeatherClient, QWeatherError
from .qw.commands import Commands
from .qw.keys import KeyStore, KeyWizard
from .qw.pushers import Pusher
from .qw.store import SubStore

PLUGIN_NAME = "astrbot_plugin_qweather"

HELP_TEXT = """和风天气：
/天气　　　　　　　查你绑定的城市
/天气 杭州　　　　临时查另一个城市
/天气 设置 杭州　 绑定 / 更换城市
/天气 清除　　　　解除绑定

推送：
/天气推送　　　　 定时推送规则
/雨雪提醒　　　　 降水开始 / 结束提醒
/预警推送　　　　 灾害预警提醒

配置：
/key　　　　　　　查看凭据（支持本会话专用 key）
/额度　　　　　　 余额与请求量"""

ASK_CITY_TEXT = (
    "你还没告诉我城市。平台不会把用户位置给机器人，所以这一步要你自己说：\n"
    "直接回复城市名（例如：天津），90 秒内有效，我会记住。"
)

_SENTENCE_PUNCT = "？?。！!；;，,\n\r"
_STOPWORDS = ("天气", "怎么样", "如何", "多少", "什么", "吗", "呢", "查", "帮我", "今天", "明天")
_CMD_PREFIXES = "/~!#！"


class QWeatherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        data_dir = self._resolve_data_dir()

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
        self._commands = Commands(self._subs, self._keys, self._wizard, self._client_for)
        self._pusher = Pusher(self._subs, self._keys, self._client_for, self._send)

        self._clients: dict[str, QWeatherClient] = {}
        self._pending_city: dict[str, float] = {}

    # ---------------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        await self._subs.load()
        await self._keys.load()
        sessions = await self._subs.all_sessions()
        with_city = sum(1 for s in sessions.values() if s.get("city"))
        rules = sum(len(s.get("rules") or []) for s in sessions.values())
        key_state = "就绪" if self._keys.default_ready else f"未配置（{self._keys.default_error}）"
        logger.info(
            f"[和风天气] 已启动：{with_city} 个会话绑定了城市，共 {rules} 条推送规则；"
            f"系统默认 key {key_state}"
        )
        if self._cfg_bool("push_enabled", True):
            self._pusher.start()

    async def terminate(self) -> None:
        await self._pusher.stop()
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001 关闭失败不该影响卸载
                pass
        self._clients.clear()
        self._pending_city.clear()

    # ---------------------------------------------------------------- 指令：天气

    @filter.command("天气")
    async def cmd_weather(self, event: AstrMessageEvent):
        """查询天气 / 绑定城市"""
        if not self._cfg_bool("enabled", True):
            yield event.plain_result("和风天气插件已关闭")
            return

        # /天气推送 会被 AstrBot 当成 /天气 的前缀，先挡掉，交给专门的 handler
        raw = (event.message_str or "").lstrip("~/!#！ ")
        if re.match(r"^天气(推送|订阅|提醒)", raw):
            return

        umo = self._umo(event)
        self._pending_city.pop(umo, None)
        self._wizard.cancel(umo)

        args = self._strip_command(event.message_str, "天气")
        head = args[0].lower() if args else ""

        if head in ("帮助", "help", "?", "？"):
            yield event.plain_result(HELP_TEXT)
            return

        if head in ("清除", "解绑", "删除", "clear"):
            cleared = await self._subs.clear_city(umo)
            yield event.plain_result(
                "已解除城市绑定，推送规则保留（但没有城市不会推送）"
                if cleared
                else "你还没有绑定城市，直接发「/天气 城市名」即可"
            )
            return

        if head in ("设置", "set", "绑定"):
            city_name = " ".join(args[1:]).strip()
            if not city_name:
                yield event.plain_result("用法：/天气 设置 杭州")
                return
            yield event.plain_result(await self._bind_city(umo, city_name))
            return

        if args:
            yield event.plain_result(await self._query_city(umo, " ".join(args).strip()))
            return

        city = await self._subs.get_city(umo)
        if city:
            yield event.plain_result(await self._render_now(umo, city))
            return

        self._pending_city[umo] = time.monotonic() + max(15, self._cfg_int("pending_timeout", 90))
        yield event.plain_result(ASK_CITY_TEXT)

    @filter.command("天气推送")
    async def cmd_push(self, event: AstrMessageEvent):
        """定时推送规则"""
        umo = self._umo(event)
        args = self._strip_command(event.message_str, "天气推送")
        yield event.plain_result(await self._commands.handle_push(umo, args))

    @filter.command("雨雪提醒")
    async def cmd_precip(self, event: AstrMessageEvent):
        """降水开始 / 结束提醒"""
        umo = self._umo(event)
        args = self._strip_command(event.message_str, "雨雪提醒")
        try:
            yield event.plain_result(await self._commands.handle_precip(umo, args))
        except ValueError as exc:
            yield event.plain_result(f"❌ {exc}")

    @filter.command("预警推送")
    async def cmd_alert(self, event: AstrMessageEvent):
        """灾害预警提醒"""
        umo = self._umo(event)
        args = self._strip_command(event.message_str, "预警推送")
        try:
            yield event.plain_result(await self._commands.handle_alert(umo, args))
        except ValueError as exc:
            yield event.plain_result(f"❌ {exc}")

    @filter.command("key")
    async def cmd_key(self, event: AstrMessageEvent):
        """凭据管理（支持本会话专用 key）"""
        umo = self._umo(event)
        args = self._strip_command(event.message_str, "key")
        text, _need_input = await self._commands.handle_key(umo, args)
        yield event.plain_result(text)

    @filter.command("额度")
    async def cmd_quota(self, event: AstrMessageEvent):
        """账户余额与请求量"""
        umo = self._umo(event)
        yield event.plain_result(await self._commands.handle_quota(umo))

    # ---------------------------------------------------------------- 补输入

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_pending_input(self, event: AstrMessageEvent):
        """接管「等用户补输入」的消息：key 设置引导、或反问后的城市名。"""
        if not self._cfg_bool("enabled", True):
            return

        umo = self._umo(event)
        text = (event.message_str or "").strip()
        if not text:
            return
        if text[0] in _CMD_PREFIXES:  # 用户改主意发指令了，状态作废
            self._wizard.cancel(umo)
            self._pending_city.pop(umo, None)
            return

        # 1) key 设置分步引导
        state = self._wizard.get(umo)
        if state is not None:
            if text in ("取消", "cancel", "退出", "算了"):
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

        # 2) 反问城市后的下一条短文本
        deadline = self._pending_city.get(umo)
        if deadline is None:
            return
        if deadline < time.monotonic():
            self._pending_city.pop(umo, None)
            return
        if not self._looks_like_city(text):
            self._pending_city.pop(umo, None)
            return
        try:
            client = await self._client_for(umo)
            places = await client.city_lookup(text, 1)
        except QWeatherError:
            self._pending_city.pop(umo, None)
            return
        if not places:
            self._pending_city.pop(umo, None)
            return

        self._pending_city.pop(umo, None)
        event.stop_event()
        await self._subs.set_city(umo, places[0])
        yield event.plain_result(
            f"已记住：{places[0]['name']}。以后直接发「/天气」就行。\n\n"
            + await self._render_now(umo, places[0])
        )

    # ---------------------------------------------------------------- 天气实现

    async def _client_for(self, umo: str) -> QWeatherClient:
        """按本会话解析出的凭据复用客户端（同一套 key 共用一个连接池与 JWT）。"""
        resolved = await self._keys.resolve(umo)
        if resolved is None:
            raise QWeatherError(0, "没有可用的 key", self._keys.default_error or "请先配置系统 key")
        fingerprint = resolved.key.fingerprint()
        client = self._clients.get(fingerprint)
        if client is None:
            client = QWeatherClient(resolved.key, timeout=self._cfg_float("http_timeout", 12.0))
            self._clients[fingerprint] = client
        return client

    async def _bind_city(self, umo: str, name: str) -> str:
        try:
            client = await self._client_for(umo)
            places = await client.city_lookup(name, 1)
        except QWeatherError as exc:
            return f"查询失败：{exc}"
        if not places:
            return f"没找到「{name}」。只写城市名试试（例如「杭州」），不要带区县或街道。"
        await self._subs.set_city(umo, places[0])
        return (
            f"已绑定：{places[0]['name']}（{places[0].get('adm1') or '—'}）\n\n"
            + await self._render_now(umo, places[0])
        )

    async def _query_city(self, umo: str, name: str) -> str:
        try:
            client = await self._client_for(umo)
            places = await client.city_lookup(name, 1)
        except QWeatherError as exc:
            return f"查询失败：{exc}"
        if not places:
            return f"没找到「{name}」。"
        text = await self._render_now(umo, places[0])
        bound = await self._subs.get_city(umo)
        if bound and bound.get("name") != places[0]["name"]:
            text += (
                f"\n\n（临时查询，已绑定的还是 {bound['name']}。"
                f"要改发「/天气 设置 {places[0]['name']}」）"
            )
        return text

    async def _render_now(self, umo: str, city: dict[str, Any]) -> str:
        content = ["now", "daily", "sun"]
        if self._cfg_bool("include_air", True):
            content.append("air")
        try:
            bundle = await self._commands.fetch_bundle(umo, city, content)
        except QWeatherError as exc:
            return f"查询失败：{exc}"
        from .qw import render

        return render.render_report(city, content, bundle, title="天气")

    async def _clear_city(self, umo: str) -> bool:
        return await self._subs.clear_city(umo)

    # ---------------------------------------------------------------- 发送

    async def _send(self, umo: str, text: str) -> bool:
        """主动发消息到配置规则的那个会话。

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

        chain = MessageChain([Plain(text)])

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
    def _looks_like_city(text: str) -> bool:
        if not text or len(text) > 12:
            return False
        if text[0] in _CMD_PREFIXES:
            return False
        if any(ch in text for ch in _SENTENCE_PUNCT):
            return False
        return not any(word in text for word in _STOPWORDS)

    @staticmethod
    def _strip_command(message_str: str, command: str) -> list[str]:
        text = (message_str or "").replace("\u3000", " ").strip()
        text = re.sub(r"^(?:@\S+\s+)+", "", text)
        text = re.sub(
            rf"^[~/!#！]{{0,2}}\s*{re.escape(command)}(?=\s|$)\s*", "", text, flags=re.IGNORECASE
        )
        return text.split() if text else []

    def _resolve_data_dir(self) -> Path:
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001 仅在框架异常时触发
            fallback = Path("data") / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(f"[和风天气] 取不到数据目录（{exc}），回退到 {fallback}")
            return fallback

    def _raw(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
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
