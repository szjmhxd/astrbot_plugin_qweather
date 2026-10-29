"""指令前缀与指令文本。

为什么所有指令都挂在一条主指令下
--------------------------------
``/天气`` 这种裸指令最容易和别的天气插件撞名：两个插件注册同名指令，AstrBot 会把
一条消息同时投给它们，用户收到两份回答，还分不清哪份是对的。所以整族指令收进
``/qweather 天气`` 这种「主指令 + 子指令」的结构，同一个前缀可以被用户改，但永远
不会退化成空——空前缀就等于把撞名问题原样还回去。

前缀只在这里维护一份：所有面向用户的文案都用 :func:`cmd` 生成，配置改了文案自动跟着变，
不会出现文档写 ``/qweather`` 而提示语还写着 ``/天气`` 的情况。
"""

from __future__ import annotations

import re

DEFAULT_PREFIX = "qweather"

# 用户手打时常见的唤醒字符
_WAKE_CHARS = "/～~!！#"
# 群聊里 @机器人 时消息文本前面会带 @昵称
_AT_RE = re.compile(r"^(?:@\S+\s+)+")

_prefix = DEFAULT_PREFIX


def normalize(raw: object) -> str:
    """把配置里的前缀洗成安全值。空、纯空格、纯符号都回落成默认值。"""
    text = " ".join(str(raw or "").replace("\u3000", " ").split())
    if not text:
        return DEFAULT_PREFIX
    # 用户可能顺手把「/」也写进去，去掉，避免出现 //qweather
    text = text.lstrip(_WAKE_CHARS).strip()
    return text or DEFAULT_PREFIX


def set_prefix(raw: object) -> str:
    """设置当前实例的前缀，返回生效值。"""
    global _prefix
    _prefix = normalize(raw)
    return _prefix


def prefix() -> str:
    return _prefix


def cmd(rest: str = "") -> str:
    """拼出完整指令写法，例如 ``cmd("天气 设置 杭州")`` → ``/qweather 天气 设置 杭州``。"""
    tail = str(rest or "").strip()
    return f"/{_prefix} {tail}" if tail else f"/{_prefix}"


def split(text: str) -> tuple[bool, str, list[str]]:
    """拆一条消息：``(是不是本插件指令, 子指令, 参数)``。

    兼容几种写法：``/qweather 天气``、``qweather 天气``（唤醒前缀已被框架吃掉）、
    ``@机器人 qweather 天气``、``/qweather天气``（漏了空格）。
    """
    body = (text or "").replace("\u3000", " ").strip()
    body = _AT_RE.sub("", body).lstrip(_WAKE_CHARS).strip()
    if not body:
        return False, "", []

    tokens = body.split()
    parts = _prefix.split()
    count = len(parts)

    if len(tokens) >= count and [t.lower() for t in tokens[:count]] == [p.lower() for p in parts]:
        rest = tokens[count:]
    elif count == 1 and tokens[0].lower().startswith(parts[0].lower()):
        # 「/qweather天气」这种漏了空格的写法。只在紧跟的字符不是 ASCII 字母数字时认，
        # 否则 /qweatherly 也会被当成子指令「ly」。
        tail = tokens[0][len(parts[0]) :].strip()
        if not tail or tail[0].isascii():
            return False, "", []
        rest = [tail] + tokens[1:]
    else:
        return False, "", []

    if not rest:
        return True, "", []
    return True, rest[0], rest[1:]
