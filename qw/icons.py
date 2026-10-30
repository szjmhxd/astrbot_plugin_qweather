"""和风官方图标（https://icons.qweather.com/）的本地缓存。

为什么要缓存
------------
卡片渲染时，如果 HTML 里直接写 ``<img src="https://icons.qweather.com/...">``，每次截图
都要等网络，而且 Chromium 在容器里可能因为 DNS / 代理直接挂图。更关键的是图标站对
非浏览器 UA 返回 **403**（``curl`` 拿不到，浏览器能拿），所以下载这一步必须自己带 UA。

SVG 是 ``fill="currentColor"`` 的，内联进 HTML 后由 CSS ``color`` 上色，一套图标能配
深色卡片也能配浅色。

缓存策略
--------
图标内容不会变，按 code 存成 ``<data>/icons/<code>.svg``，命中直接读文件。下载失败返回
空串，卡片降级成「没有图标」——图标缺失不该让整张图渲不出来。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from astrbot.api import logger

ICON_URL = "https://icons.qweather.com/assets/icons/{code}.svg"
# 站点对非浏览器 UA 直接 403，必须伪装
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 8.0


class IconCache:
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, code: str) -> Path:
        return self._dir / f"{code}.svg"

    async def get(self, code: str) -> str:
        """返回 SVG 源码；拿不到返回空串（调用方按无图标处理）。"""
        key = "".join(ch for ch in str(code or "") if ch.isalnum())
        if not key:
            return ""
        path = self._path(key)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # 同一张图并发只下载一次
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError:
                    return ""
            svg = await self._download(key)
            if svg:
                try:
                    self._dir.mkdir(parents=True, exist_ok=True)
                    path.write_text(svg, encoding="utf-8")
                except OSError as exc:  # 目录只读时照样能用，只是每次都重下
                    logger.debug(f"[和风天气] 图标缓存写入失败：{exc}")
            return svg

    @staticmethod
    async def _download(code: str) -> str:
        url = ICON_URL.format(code=code)
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": UA}) as resp:
                    if resp.status != 200:
                        logger.debug(f"[和风天气] 图标 {code} 下载失败：HTTP {resp.status}")
                        return ""
                    text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.debug(f"[和风天气] 图标 {code} 下载异常：{exc}")
            return ""
        return text if "<svg" in text else ""

    async def inline(self, code: str, size: int = 28, color: str = "currentColor") -> str:
        """内联用的 ``<svg>`` 片段。宽高写死，避免踩到原图 16×16 的尺寸。"""
        svg = await self.get(code)
        if not svg:
            return ""
        body = _strip_svg_wrapper(svg)
        return (
            f'<svg viewBox="0 0 16 16" width="{size}" height="{size}" '
            f'fill="{color}" xmlns="http://www.w3.org/2000/svg">{body}</svg>'
        )


def _strip_svg_wrapper(svg: str) -> str:
    """去掉外层 ``<svg ...>``，只留路径，方便重新包一层控制尺寸与颜色。"""
    text = svg.strip()
    start = text.find(">")
    end = text.rfind("</svg>")
    if start < 0 or end < 0 or end <= start:
        return text
    return text[start + 1 : end].strip()
