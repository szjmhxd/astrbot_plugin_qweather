"""把推送内容渲染成图片卡片。

为什么用 Playwright 而不是 Pillow
---------------------------------
Pillow 画不了排版：折行、字重、圆角阴影、图标对齐全要手算，中文字体还得自己找路径。
HTML + Chromium 截图把这些交给 CSS，改样式不用改代码。

代价是依赖：Playwright 和 Chromium 加起来几百 MB，容器里通常没有。所以**检测不到就静默
降级成纯文字**——推送是高频功能，不能因为渲染不了图就收不到天气。检测结果缓存，启动时
试一次，失败就这轮不再试。

图标全部来自和风官方 https://icons.qweather.com/ ，见 :mod:`qw.icons`。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape
from typing import Any

from astrbot.api import logger

from . import render, zh
from .icons import IconCache

# 卡片配色：浅色系，微信 / QQ 的浅色聊天背景和深色背景都能看清
INK = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2f6df6"
CARD = "#ffffff"
PAGE_BG = "linear-gradient(160deg, #e8f0ff 0%, #f7faff 45%, #eef6f0 100%)"

LEVEL_COLOR = {
    "blue": "#2f6df6",
    "yellow": "#e0a800",
    "orange": "#f58319",
    "red": "#e5484d",
}

FONT = (
    '-apple-system, BlinkMacSystemFont, "PingFang SC", "Noto Sans CJK SC", '
    '"Source Han Sans SC", "WenQuanYi Micro Hei", "Microsoft YaHei", sans-serif'
)


def _t(value: Any, digits: int = 0) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _level_color(text: str) -> str:
    raw = (text or "").lower()
    for key in ("red", "orange", "yellow", "blue"):
        if key in raw:
            return LEVEL_COLOR[key]
    for key, value in (("红", "red"), ("橙", "orange"), ("黄", "yellow"), ("蓝", "blue")):
        if key in raw:
            return LEVEL_COLOR[value]
    return LEVEL_COLOR["blue"]


class CardRenderer:
    """Playwright 渲染器。不可用时所有方法返回 ``None``。"""

    def __init__(self, icon_dir, *, enabled: bool = True, width: int = 720) -> None:
        self._icons = IconCache(icon_dir)
        self._enabled = enabled
        self._width = width
        self._checked = False
        self._ok = False
        self._browser = None
        self._playwright = None
        self._lock = asyncio.Lock()
        self._warned = False

    # ---------------------------------------------------------------- 生命周期

    @property
    def available(self) -> bool:
        return self._ok

    async def probe(self) -> bool:
        """试启动一次 Chromium。成功就一直复用，失败永久关闭。

        调用方在插件启动时调一次即可；渲染时如果还没试过也会再试，但只试一次。
        """
        if not self._enabled:
            return False
        if self._checked:
            return self._ok
        async with self._lock:
            if self._checked:
                return self._ok
            self._checked = True
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                logger.info(
                    "[和风天气] 未安装 Playwright，图片卡片关闭（只发文字）。"
                    "需要图片：pip install playwright && playwright install chromium"
                )
                self._ok = False
                return False
            try:
                self._playwright = await async_playwright().start()
                self._browser, how = await self._launch()
                self._ok = True
                logger.info(f"[和风天气] Playwright 就绪（{how}），推送会附一张图片卡片")
            except Exception as exc:  # noqa: BLE001 浏览器没装 / 缺依赖都归这里
                logger.info(f"[和风天气] Playwright 启动失败，图片卡片关闭：{exc}")
                await self._cleanup()
                self._ok = False
            return self._ok

    async def _launch(self):
        """依次试：自带 Chromium → 系统 Chrome → 系统 Edge。

        很多人只 `pip install playwright` 忘了 `playwright install chromium`，但机器上
        本来就有 Chrome。能用现成的就别再让人下一个 150 MB。
        """
        args = ["--no-sandbox", "--disable-dev-shm-usage"]
        errors: list[str] = []
        for kwargs, label in (
            ({}, "Playwright Chromium"),
            ({"channel": "chrome"}, "系统 Chrome"),
            ({"channel": "msedge"}, "系统 Edge"),
        ):
            try:
                browser = await self._playwright.chromium.launch(args=args, **kwargs)
                return browser, label
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {type(exc).__name__}")
        raise RuntimeError("找不到可用的浏览器（" + "；".join(errors) + "）")

    async def close(self) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._playwright = None

    # ---------------------------------------------------------------- 渲染

    async def _shot(self, body_html: str, *, height: int = 260) -> bytes | None:
        if not await self.probe():
            return None
        if self._browser is None:
            return None
        html = _page(body_html, self._width)
        try:
            page = await self._browser.new_page(
                viewport={"width": self._width, "height": height}, device_scale_factor=2
            )
            try:
                await page.set_content(html, wait_until="load")
                await page.wait_for_timeout(120)  # 等字体与行内 SVG 布局稳定
                png = await page.screenshot(full_page=True, omit_background=True)
            finally:
                await page.close()
            return png
        except Exception as exc:  # noqa: BLE001 渲染失败不该影响推送
            if not self._warned:
                self._warned = True
                logger.warning(f"[和风天气] 图片渲染失败，降级为纯文字：{exc}")
            return None

    async def weather(
        self,
        place: dict[str, Any],
        content: list[str],
        bundle: dict[str, Any],
        *,
        moment: datetime | None = None,
        title: str = "天气",
        alias: str = "",
    ) -> bytes | None:
        """主卡片：当前天气 + 逐日 + 逐时 + 空气 + 预警。"""
        if not await self.probe():
            return None
        moment = moment or datetime.now()
        body = await self._weather_html(place, content, bundle, moment, title, alias)
        return await self._shot(body, height=300)

    async def _weather_html(
        self,
        place: dict[str, Any],
        content: list[str],
        bundle: dict[str, Any],
        moment: datetime,
        title: str,
        alias: str,
    ) -> str:
        want = set(content or [])
        now = bundle.get("now") or {}
        days = bundle.get("daily") or []
        today = days[0] if days else {}
        head_name = alias or place.get("name") or "天气"
        sub = render.place_label(place)

        parts: list[str] = [
            '<div class="top">'
            f'<div><div class="place">{escape(head_name)}</div>'
            f'<div class="sub">{escape(sub)}</div></div>'
            f'<div class="stamp">{moment.strftime("%m-%d %H:%M")}</div></div>'
        ]

        # 当前天气：一个大图标 + 温度
        if now:
            icon = await self._icons.inline(str(now.get("code") or ""), 92, INK)
            parts.append(
                '<div class="now">'
                f'<div class="now-icon">{icon or ""}</div>'
                '<div class="now-text">'
                f'<div class="now-desc">{escape(str(now.get("text") or "—"))}</div>'
                f'<div class="now-temp">{_t(now.get("temp"), 1)}<span class="unit">°C</span></div>'
                '<div class="now-meta">'
                f"体感 {_t(now.get('feels_like'), 0)}°"
                f"　{escape(str(now.get('wind_dir') or ''))} {now.get('wind_scale') or '—'} 级"
                f"　湿度 {_t(now.get('humidity'), 0)}%"
                "</div></div></div>"
            )
        else:
            icon = await self._icons.inline(str(today.get("code_day") or ""), 92, INK)
            if icon:
                parts.append(f'<div class="now"><div class="now-icon">{icon}</div></div>')

        # 逐日
        if days and "daily" in want:
            rows: list[str] = []
            for idx, day in enumerate(days[:4]):
                label = (
                    ["今天", "明天", "后天"][idx]
                    if idx < 3
                    else render._date_label(str(day.get("date") or ""))
                )
                icon = await self._icons.inline(str(day.get("code_day") or ""), 30, INK)
                text = str(day.get("text_day") or "")
                if day.get("text_night") and day.get("text_night") != text:
                    text = f"{text}转{day['text_night']}"
                rows.append(
                    '<div class="day">'
                    f'<span class="day-label">{escape(label)}</span>'
                    f'<span class="day-icon">{icon or ""}</span>'
                    f'<span class="day-text">{escape(text)}</span>'
                    f'<span class="day-temp">{_t(day.get("temp_min"))}° / '
                    f"<b>{_t(day.get('temp_max'))}°</b></span>"
                    "</div>"
                )
            parts.append('<div class="card days">' + "".join(rows) + "</div>")

        # 逐时
        hours = bundle.get("hourly") or []
        if hours and "hourly" in want:
            cells: list[str] = []
            for item in hours[:8]:
                icon = await self._icons.inline(str(item.get("code") or ""), 26, INK)
                cells.append(
                    '<div class="hour">'
                    f'<div class="hour-time">{escape(str(item.get("time") or ""))}</div>'
                    f'<div class="hour-icon">{icon or ""}</div>'
                    f'<div class="hour-temp">{_t(item.get("temp"))}°</div>'
                    "</div>"
                )
            parts.append('<div class="card hours">' + "".join(cells) + "</div>")

        # 底部信息条
        chips: list[str] = []
        air = bundle.get("air") or {}
        if air and "air" in want:
            chips.append(
                f'<span class="chip">空气 <b>{escape(str(air.get("category") or "—"))}</b> '
                f"AQI {_t(air.get('aqi'))}</span>"
            )
        if today.get("sunrise") and ("sun" in want or "daily" in want):
            chips.append(
                f'<span class="chip">日出 {escape(str(today.get("sunrise")))}'
                f"　日落 {escape(str(today.get('sunset')))}</span>"
            )
        minutely = bundle.get("minutely") or {}
        if minutely.get("summary") and "minutely" in want:
            chips.append(f'<span class="chip">{escape(str(minutely["summary"]))}</span>')
        indices = bundle.get("indices") or []
        if indices and "indices" in want:
            picked = [i for i in indices if i.get("type") in ("1", "2", "3", "5")][:4]
            if picked:
                text = "　".join(
                    f"{zh.index_name_zh(str(i.get('name') or ''))} {i.get('category') or ''}"
                    for i in picked
                )
                chips.append(f'<span class="chip">{escape(text)}</span>')
        if chips:
            parts.append('<div class="chips">' + "".join(chips) + "</div>")

        # 预警
        alerts = bundle.get("alerts") or []
        if alerts and "alert" in want:
            parts.append(self._alert_block(alerts[:2]))

        return "".join(parts)

    def _alert_block(self, alerts: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        for alert in alerts:
            color = _level_color(str(alert.get("level") or ""))
            name = alert.get("title") or alert.get("type_name") or "预警"
            body = "<br>".join(
                escape(line) for line in render.wrap(str(alert.get("text") or ""), 38, 3)
            )
            rows.append(
                f'<div class="alert" style="border-left-color:{color}">'
                f'<div class="alert-title" style="color:{color}">{escape(str(name))}</div>'
                f'<div class="alert-text">{body}</div>'
                "</div>"
            )
        return '<div class="alerts">' + "".join(rows) + "</div>"

    async def notice(
        self,
        title: str,
        lines: list[tuple[str, str]],
        *,
        footer: str = "",
        accent: str = ACCENT,
        icon_code: str = "",
    ) -> bytes | None:
        """通用卡片：降水提醒、额度报告这类「标题 + 若干行」的内容。"""
        if not await self.probe():
            return None
        icon = await self._icons.inline(icon_code, 40, accent) if icon_code else ""
        rows = "".join(
            (
                f'<div class="row"><span class="k">{escape(k)}</span>'
                f'<span class="v">{escape(v)}</span></div>'
                if k
                else f'<div class="row wide"><span class="v">{escape(v)}</span></div>'
            )
            for k, v in lines
            if v
        )
        body = (
            f'<div class="top end"><div class="stamp">'
            f"{datetime.now().strftime('%m-%d %H:%M')}</div></div>"
            f'<div class="card notice" style="border-top:4px solid {accent}">'
            f'<div class="notice-head">{icon}'
            f'<span class="notice-title" style="color:{accent}">{escape(title)}</span></div>'
            f"{rows}</div>" + (f'<div class="footer">{escape(footer)}</div>' if footer else "")
        )
        return await self._shot(body, height=240)


def _page(body: str, width: int) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:{width}px;font-family:{FONT};background:{PAGE_BG};padding:22px;color:{INK}}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px}}
.top.end{{justify-content:flex-end}}
.place{{font-size:30px;font-weight:700;letter-spacing:.5px}}
.sub{{font-size:15px;color:{MUTED};margin-top:4px}}
.stamp{{font-size:15px;color:{MUTED}}}
.now{{display:flex;align-items:center;gap:18px;margin-bottom:18px}}
.now-icon{{width:100px;height:100px;display:flex;align-items:center;justify-content:center}}
.now-desc{{font-size:22px;color:{MUTED}}}
.now-temp{{font-size:60px;font-weight:700;line-height:1.05}}
.unit{{font-size:26px;font-weight:500;color:{MUTED};margin-left:4px}}
.now-meta{{font-size:16px;color:{MUTED};margin-top:6px}}
.card{{background:{CARD};border-radius:20px;padding:14px 18px;
 box-shadow:0 6px 20px rgba(31,41,55,.07);margin-bottom:14px}}
.days{{padding:6px 18px}}
.day{{display:flex;align-items:center;gap:12px;padding:7px 0;font-size:18px}}
.day+.day{{border-top:1px solid #f0f2f6}}
.day-label{{width:96px;color:{MUTED}}}
.day-icon{{width:32px;display:flex;justify-content:center}}
.day-text{{flex:1}}
.day-temp{{color:{MUTED};font-variant-numeric:tabular-nums}}
.day-temp b{{color:{INK}}}
.hours{{display:flex;justify-content:space-between;padding:12px 10px}}
.hour{{display:flex;flex-direction:column;align-items:center;gap:4px;font-size:15px}}
.hour-time{{color:{MUTED}}}
.hour-temp{{font-weight:600}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}}
.chip{{background:rgba(255,255,255,.85);border-radius:999px;padding:7px 14px;
 font-size:15px;color:{MUTED}}}
.chip b{{color:{INK}}}
.alerts{{display:flex;flex-direction:column;gap:8px}}
.alert{{background:{CARD};border-left:6px solid;border-radius:12px;padding:10px 14px;
 box-shadow:0 4px 14px rgba(31,41,55,.06)}}
.alert-title{{font-size:17px;font-weight:600;margin-bottom:3px}}
.alert-text{{font-size:14px;color:{MUTED};line-height:1.5}}
.notice{{padding:18px 20px}}
.notice-head{{display:flex;align-items:center;gap:10px;margin-bottom:12px}}
.notice-title{{font-size:24px;font-weight:700}}
.row{{display:flex;justify-content:space-between;font-size:17px;padding:7px 0}}
.row+.row{{border-top:1px solid #f0f2f6}}
.row.wide{{justify-content:flex-start}}
.row.wide .v{{font-weight:400;color:{MUTED}}}
.k{{color:{MUTED}}}
.v{{font-weight:600;font-variant-numeric:tabular-nums}}
.footer{{font-size:13px;color:{MUTED};text-align:center;margin-top:4px}}
</style></head><body>{body}</body></html>"""
