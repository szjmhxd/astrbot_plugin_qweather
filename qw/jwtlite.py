"""Ed25519（RFC 8032）签名 + 和风天气 JWT 签发，纯标准库、零依赖。

为什么自己实现
--------------
AstrBot 官方镜像里不一定装了 ``cryptography`` 或 ``PyJWT``，装依赖失败的插件在容器里
很难排查。Ed25519 的参考实现只有一百多行，签名结果与 ``openssl pkeyutl -sign -rawin``
逐字节一致，用标准库换掉一个依赖是划算的。

和风天气的 JWT 要求
------------------
``alg=EdDSA``，header 带 ``kid``（凭据 ID），payload 必须含 ``iss``（开发者 ID）、
``sub``（项目 ID）、``iat``、``exp``，有效期上限 24 小时。少 ``iss`` 是常见错误——
网上几个开源插件就是漏了这个字段导致 401。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

# ---------------------------------------------------------------- 曲线常量

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _sqrt(a: int) -> int | None:
    """模 P 开平方（P % 8 == 5）。"""
    x = pow(a, (P + 3) // 8, P)
    if (x * x - a) % P != 0:
        x = (x * pow(2, (P - 1) // 4, P)) % P
    if (x * x - a) % P != 0:
        return None
    return x


def _base_point() -> tuple[int, int]:
    by = (4 * _inv(5)) % P
    bx = _sqrt((by * by - 1) * _inv(D * by * by + 1) % P)
    if bx is None:  # pragma: no cover - 数学上不会发生
        raise ValueError("无法还原基点")
    if bx & 1:
        bx = P - bx
    return bx, by


BX, BY = _base_point()


def _add(p1: tuple[int, int], p2: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p1
    x2, y2 = p2
    t = D * x1 * x2 * y1 * y2 % P
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + t) % P
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - t) % P
    return x3, y3


def _mul(scalar: int, point: tuple[int, int]) -> tuple[int, int]:
    result = (0, 1)  # 单位元
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _clamp(h: bytes) -> int:
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a


# ---------------------------------------------------------------- 密钥与签名


def seed_from_pem(text: str) -> bytes:
    """从 PKCS#8 PEM 文本里取出 32 字节种子。

    Ed25519 私钥的 PEM 很短（119 字节、三行），可以直接让用户在对话里粘贴，
    不必上传文件。
    """
    body = "".join(
        line.strip()
        for line in (text or "").splitlines()
        if line.strip() and not line.strip().startswith("-----")
    )
    if not body:
        raise ValueError("私钥内容为空")
    try:
        der = base64.b64decode(body)
    except (ValueError, TypeError) as exc:
        raise ValueError("私钥不是合法 base64，请检查是否漏字符") from exc
    seed = der[-32:]
    if len(seed) != 32:
        raise ValueError("私钥解析失败：解出的种子不是 32 字节")
    return seed


def public_key_bytes(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    return _encode_point(_mul(_clamp(h), (BX, BY)))


def public_key_b64(seed: bytes) -> str:
    """公钥的 base64（和风控制台上传公钥时用的就是这串）。"""
    return base64.b64encode(public_key_bytes(seed)).decode()


def sign(seed: bytes, message: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = _clamp(h)
    prefix = h[32:]
    pub = _encode_point(_mul(a, (BX, BY)))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % L
    r_enc = _encode_point(_mul(r, (BX, BY)))
    k = int.from_bytes(hashlib.sha512(r_enc + pub + message).digest(), "little") % L
    s = (r + k * a) % L
    return r_enc + s.to_bytes(32, "little")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(seed: bytes, iss: str, sub: str, kid: str, ttl: int = 900) -> str:
    """签发和风天气用的 JWT。``ttl`` 建议 900 秒，别超过 86400。"""
    now = int(time.time())
    header = json.dumps({"alg": "EdDSA", "kid": kid}, separators=(",", ":")).encode()
    payload = json.dumps(
        {"iss": iss, "sub": sub, "iat": now - 30, "exp": now + ttl},
        separators=(",", ":"),
    ).encode()
    signing_input = f"{b64url(header)}.{b64url(payload)}"
    sig = sign(seed, signing_input.encode())
    return f"{signing_input}.{b64url(sig)}"
