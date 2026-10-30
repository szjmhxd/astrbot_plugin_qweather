"""凭据管理：系统默认 key + 会话级 key 覆盖。

优先级
------
1. 本会话自己设置的 key（``/qweather key 设置`` 走分步引导，存在 ``keys.json``）；
2. 系统默认 key（插件配置里的 iss/sub/kid/host + 私钥）；
3. 都没有 → 提示用户去配。

关键约束：**私钥和公钥必须成对**。不同和风账号的凭据里上传的是各自的公钥，所以一个会话
要用自己的账号，就必须同时提供自己的私钥。分步引导里私钥可以跳过——跳过就复用系统默认
私钥，只有在该账号的凭据里上传的正好是系统那把公钥时才有效。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import KeyConfig
from .jwtlite import public_key_b64, seed_from_pem
from .naming import cmd

MAX_SESSIONS = 2000


@dataclass(slots=True)
class WizardState:
    """分步引导的中间状态（只在内存里，重启即失效）。"""

    step: str
    data: dict[str, str] = field(default_factory=dict)
    deadline: float = 0.0

    def alive(self) -> bool:
        return time.monotonic() < self.deadline


# 引导步骤与提示语。顺序固定，避免用户填错位置。
WIZARD_STEPS: list[tuple[str, str]] = [
    ("iss", "① 开发者 ID（iss）：控制台 → 设置，Q 开头 10 位，例如 Q1A2B3C4D5"),
    ("sub", "② 项目 ID（sub）：控制台 → 项目管理，点进项目后可见"),
    ("kid", "③ 凭据 ID（kid）：创建凭据并上传公钥后显示的 ID"),
    ("host", "④ API Host：控制台 → 设置，形如 ne6yvtne25.re.qweatherapi.com（不要带 https://）"),
    ("pem", "⑤ 私钥 PEM（可选）：直接粘贴三行私钥；若想复用系统默认私钥，发「跳过」"),
]

STEP_KEYS = [s[0] for s in WIZARD_STEPS]

# 指纹要展示的字段顺序：(给人看的名字, 内部键)
_FINGERPRINT_FIELDS = [
    ("iss", "iss"),
    ("sub", "sub"),
    ("kid", "kid"),
    ("host", "host"),
]


def _mask(value: str, keep: int = 4) -> str:
    """打码成「前 4 位…（长度）」，够对照是哪一项，又不至于把凭据贴进聊天记录。"""
    text = (value or "").strip()
    if not text:
        return "空"
    if len(text) <= keep:
        return f"{'*' * len(text)}（{len(text)} 位）"
    return f"{text[:keep]}…（{len(text)} 位）"


@dataclass(slots=True)
class ResolvedKey:
    key: KeyConfig
    source: str  # "session" | "default"
    session_data: dict[str, Any] | None = None


class KeyStore:
    """会话级 key 的持久化 + 默认 key 的解析。"""

    def __init__(
        self,
        path: Path,
        default_iss: str = "",
        default_sub: str = "",
        default_kid: str = "",
        default_host: str = "",
        default_pem: str = "",
    ) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._default_raw: dict[str, str] = {}
        self._default_seed: bytes | None = None
        self._default_error = ""
        self.set_defaults(default_iss, default_sub, default_kid, default_host, default_pem)

    # ---------------------------------------------------------------- 默认 key

    def set_defaults(
        self,
        iss: str = "",
        sub: str = "",
        kid: str = "",
        host: str = "",
        pem: str = "",
    ) -> bool:
        """换一套系统默认 key（WebUI 保存配置后热更新用），返回是否有变化。

        私钥要重新解析：解析失败只记错误，不抛异常，免得一条坏配置把插件带崩。
        """
        raw = {
            "iss": (iss or "").strip(),
            "sub": (sub or "").strip(),
            "kid": (kid or "").strip(),
            "host": (host or "").strip(),
            "pem": (pem or "").strip(),
        }
        if raw == self._default_raw:
            return False
        self._default_raw = raw
        self._default_seed = None
        self._default_error = ""
        if raw["pem"]:
            try:
                self._default_seed = seed_from_pem(raw["pem"])
            except ValueError as exc:
                self._default_error = str(exc)
        return True

    def default_state(self) -> str:
        """一句话状态，日志和报错共用。"""
        return "就绪" if self.default_ready else self.default_error

    @property
    def default_ready(self) -> bool:
        raw = self._default_raw
        return bool(raw["iss"] and raw["sub"] and raw["kid"] and raw["host"] and self._default_seed)

    @property
    def default_error(self) -> str:
        if self._default_error:
            return self._default_error
        missing = [k for k in ("iss", "sub", "kid", "host") if not self._default_raw.get(k)]
        if not self._default_raw.get("pem"):
            missing.append("私钥")
        return f"系统默认 key 缺少：{', '.join(missing)}" if missing else ""

    def default_fingerprint(self) -> list[str]:
        """打码后的各项指纹，用来核对「插件到底读到了什么」。"""
        raw = self._default_raw
        lines = [f"{name}={_mask(raw.get(key, ''))}" for name, key in _FINGERPRINT_FIELDS]
        if self._default_error:
            lines.append(f"私钥=解析失败（{self._default_error}）")
        elif self._default_seed:
            lines.append(f"私钥=已载入（公钥 {self.default_public_key()[:8]}…）")
        else:
            lines.append("私钥=空")
        return lines

    def default_public_key(self) -> str:
        return public_key_b64(self._default_seed) if self._default_seed else ""

    # ---------------------------------------------------------------- 持久化

    async def load(self) -> None:
        async with self._lock:
            self._loaded = True
            if not self._path.exists():
                self._data = {}
                return
            try:
                raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                parsed = json.loads(raw)
                self._data = parsed if isinstance(parsed, dict) else {}
            except (OSError, ValueError, json.JSONDecodeError):
                self._data = {}

    async def _ensure(self) -> None:
        if not self._loaded:
            await self.load()

    async def _flush(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        tmp = self._path.with_suffix(".tmp")

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

        await asyncio.to_thread(_write)

    # ---------------------------------------------------------------- 会话 key

    async def set_session(self, umo: str, data: dict[str, str]) -> None:
        await self._ensure()
        async with self._lock:
            # 只存非空字段；私钥为空表示复用默认私钥
            clean = {k: (v or "").strip() for k, v in data.items() if (v or "").strip()}
            clean["updated"] = int(time.time())
            self._data[umo] = clean
            if len(self._data) > MAX_SESSIONS:
                ordered = sorted(self._data.items(), key=lambda kv: kv[1].get("updated", 0))
                for key, _ in ordered[: len(self._data) - MAX_SESSIONS]:
                    self._data.pop(key, None)
            await self._flush()

    async def clear_session(self, umo: str) -> bool:
        await self._ensure()
        async with self._lock:
            if umo not in self._data:
                return False
            del self._data[umo]
            await self._flush()
            return True

    async def get_session(self, umo: str) -> dict[str, Any] | None:
        await self._ensure()
        async with self._lock:
            record = self._data.get(umo)
            return dict(record) if record else None

    async def count_sessions(self) -> int:
        await self._ensure()
        async with self._lock:
            return len(self._data)

    # ---------------------------------------------------------------- 解析

    async def resolve(self, umo: str) -> ResolvedKey | None:
        """按优先级给出本会话该用哪套凭据。都没有返回 None。"""
        record = await self.get_session(umo)
        if record:
            pem = record.get("pem") or self._default_raw["pem"]
            try:
                seed = seed_from_pem(pem)
            except ValueError:
                seed = None
            if seed:
                key = KeyConfig(
                    iss=record.get("iss") or self._default_raw["iss"],
                    sub=record.get("sub") or self._default_raw["sub"],
                    kid=record.get("kid") or self._default_raw["kid"],
                    host=record.get("host") or self._default_raw["host"],
                    seed=seed,
                )
                if key.iss and key.sub and key.kid and key.host:
                    return ResolvedKey(key=key, source="session", session_data=record)
        if self.default_ready:
            key = KeyConfig(
                iss=self._default_raw["iss"],
                sub=self._default_raw["sub"],
                kid=self._default_raw["kid"],
                host=self._default_raw["host"],
                seed=self._default_seed,
            )
            return ResolvedKey(key=key, source="default")
        return None

    async def describe(self, umo: str) -> str:
        """给 ``/qweather key`` 查看用的一段人话。"""
        record = await self.get_session(umo)
        lines: list[str] = []
        if record:
            pem_note = "自带私钥" if record.get("pem") else "复用系统私钥"
            lines.append("本会话：自定义 key（覆盖系统默认）")
            lines.append(f"  iss={record.get('iss', '-')}")
            lines.append(f"  sub={record.get('sub', '-')}")
            lines.append(f"  kid={record.get('kid', '-')}")
            lines.append(f"  host={record.get('host', '-')}")
            lines.append(f"  私钥：{pem_note}")
        else:
            lines.append("本会话：使用系统默认 key")
        lines.append("")
        lines.append("系统默认：" + ("已就绪" if self.default_ready else "未配置"))
        for item in self.default_fingerprint():
            lines.append(f"  {item}")
        if self.default_ready:
            lines.append(f"系统公钥：{self.default_public_key()}")
        else:
            lines.append(f"⚠️ {self.default_error}")
            lines.append(
                "  在 WebUI → 插件 → 本插件 → 配置里填好这几项，点「保存并关闭」就生效"
                "（插件会自动重读，不用重载）"
            )
            lines.append(f"  只让本会话用自己的 key：发「{cmd('key 设置')}」")
        return "\n".join(lines)


class KeyWizard:
    """分步引导的状态机，纯内存。

    放在插件实例上而不是模块级，避免多实例互相污染；测试里可以直接 new 一个。
    """

    def __init__(self, timeout: float = 300.0) -> None:
        self._timeout = timeout
        self._states: dict[str, WizardState] = {}

    def start(self, umo: str) -> str:
        state = WizardState(
            step=WIZARD_STEPS[0][0], data={}, deadline=time.monotonic() + self._timeout
        )
        self._states[umo] = state
        return WIZARD_STEPS[0][1]

    def get(self, umo: str) -> WizardState | None:
        state = self._states.get(umo)
        if state is None:
            return None
        if not state.alive():
            self._states.pop(umo, None)
            return None
        return state

    def cancel(self, umo: str) -> None:
        self._states.pop(umo, None)

    def feed(self, umo: str, text: str) -> tuple[str | None, dict[str, str] | None]:
        """喂一条输入。

        返回 ``(下一步提示语, 收齐的数据)``。收齐时第二项非 None；中途出错抛 ValueError。
        """
        state = self.get(umo)
        if state is None:
            raise ValueError("没有正在进行的设置流程")
        text = (text or "").strip()
        if not text:
            raise ValueError("内容为空，请重新输入")

        step = state.step
        if step == "pem":
            if text in ("跳过", "skip", "-"):
                state.data["pem"] = ""
            else:
                # 私钥可能被消息平台折行，去掉空白后重新拼成 PEM
                body = "".join(ch for ch in text if ch not in " \t\r\n")
                if body.startswith("-----BEGIN"):
                    state.data["pem"] = body.replace(
                        "-----BEGINPRIVATEKEY-----", "-----BEGIN PRIVATE KEY-----\n"
                    ).replace("-----ENDPRIVATEKEY-----", "\n-----END PRIVATE KEY-----")
                else:
                    state.data["pem"] = (
                        "-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----"
                    )
                try:
                    seed_from_pem(state.data["pem"])
                except ValueError as exc:
                    raise ValueError(f"私钥解析失败：{exc}") from exc
        else:
            if step == "host":
                text = text.removeprefix("https://").removeprefix("http://").rstrip("/")
            state.data[step] = text

        idx = STEP_KEYS.index(step)
        if idx + 1 >= len(STEP_KEYS):
            self._states.pop(umo, None)
            return None, state.data
        state.step = STEP_KEYS[idx + 1]
        state.deadline = time.monotonic() + self._timeout
        prompt = WIZARD_STEPS[idx + 1][1]
        return prompt, None
