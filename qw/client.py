"""和风天气 HTTP 客户端。

路径分两套，别想当然
--------------------
和风改版后**没有**把老接口全迁到 ``/v1/``：实时天气、每日/逐时预报、空气质量、预警、
太阳辐射、GeoAPI 在 v1/v2；而**分钟级降水、生活指数、天文、时光机、台风、潮汐仍在
``/v7/``**。用 v1 的写法打 ``/indices/v1/...`` 会得到 HTTP 404 且**响应体为空**——这个
404 有歧义（路径错 / 接口不存在 / 无权限都是它），不能当"没订阅"的证据。

数值字段两套结构
----------------
v1 的数值是 ``{"value": 19.85, "unit": "°C"}`` 对象，且 ``humidity`` 是 0~1 小数；
v7 是扁平字符串（``"temp": "20"``）且湿度是 0~100 整数。``current()`` 把 v1 归一化成
扁平结构，上层只面对一种形状。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .jwtlite import make_jwt

DEFAULT_TIMEOUT = 12.0
TOKEN_TTL = 900  # 和风允许最长 24h，用 15 分钟足够且更安全
TOKEN_SAFETY = 120  # 提前 2 分钟换新 token


class QWeatherError(Exception):
    """接口返回的业务错误（4xx/5xx 或 code != 200）。"""

    def __init__(self, status: int, title: str, detail: str = "") -> None:
        self.status = status
        self.title = title
        self.detail = detail
        super().__init__(f"{status} {title}{('：' + detail) if detail else ''}")

    @property
    def is_auth(self) -> bool:
        return self.status == 401

    @property
    def is_permission(self) -> bool:
        return self.status in (403, 404)

    @property
    def is_quota(self) -> bool:
        return self.status in (402, 429)


@dataclass(slots=True)
class KeyConfig:
    """一套和风凭据。私钥是 Ed25519 的 32 字节种子。"""

    iss: str
    sub: str
    kid: str
    host: str
    seed: bytes | None = None

    @property
    def host_clean(self) -> str:
        host = (self.host or "").strip()
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix) :]
        return host.rstrip("/")

    def fingerprint(self) -> str:
        """用于缓存客户端：同一套凭据复用同一个连接池。"""
        return f"{self.iss}|{self.sub}|{self.kid}|{self.host_clean}"


def _num(obj: Any) -> float | int | None:
    """v1 的 ``{value, unit}`` → 裸数值。"""
    if isinstance(obj, dict):
        return obj.get("value")
    if isinstance(obj, (int, float)):
        return obj
    return None


# ---------------------------------------------------------------- 坐标解析

_COORD_SEP = re.compile(r"[,，;；/\\\s]+")
COORD_DECIMALS = 2

# 度分秒：``39°13'53"`` / ``39度13分53秒`` / ``117°8′2″`` 都认。
# 关键点：只有在「度」带了单位（° 或 度）时才继续吃分和秒，否则 ``39 13 53 117 8 2``
# 这种纯数字串会被贪婪地当成一个坐标，永远分不开两个点。
_DMS = re.compile(
    r"""
    (?P<sign>[+-])?
    (?P<deg>\d+(?:\.\d+)?)\s*(?:°|度|º)
    (?:\s*(?P<min>\d+(?:\.\d+)?)\s*(?:['′’分]))?
    (?:\s*(?P<sec>\d+(?:\.\d+)?)\s*["″“”秒']{1,2})?
    """,
    re.VERBOSE,
)

# 十进制的裸数字（可能带 N/S/E/W 方位词，前缀或后缀都行）
_PLAIN = re.compile(r"^(?P<pre>[NSEWnsew])?\s*(?P<num>[+-]?\d+(?:\.\d+)?)\s*(?P<dir>[NSEWnsew])?$")


def _dms_atoms(text: str) -> list[float]:
    """把文本里所有「度分秒」块转成十进制度，按出现顺序返回。"""
    out: list[float] = []
    for match in _DMS.finditer(text or ""):
        deg = float(match.group("deg"))
        minutes = float(match.group("min") or 0)
        seconds = float(match.group("sec") or 0)
        # 分秒必须 < 60，否则大概率是写错了（例如把 61 分当成了分）
        if minutes >= 60 or seconds >= 60:
            return []
        value = deg + minutes / 60 + seconds / 3600
        if match.group("sign") == "-":
            value = -value
        out.append(value)
    return out


def _plain_atom(text: str) -> float | None:
    """``"117.21"`` / ``"39.23N"`` / ``"E117.21"`` → float；带方位词时按方位定符号。"""
    raw = (text or "").strip()
    if not raw:
        return None
    match = _PLAIN.match(raw)
    if match is None:
        return None
    value = float(match.group("num"))
    direction = (match.group("dir") or match.group("pre") or "").upper()
    if direction in ("S", "W") and value > 0:
        value = -value
    return value


def _number_pair(text: str) -> tuple[float, float] | None:
    """把``"117.21,39.13"`` 或 ``"117°8'2\",39°13'53\""`` 拆成两个 float。

    度分秒只要出现过一次，就走度分秒解析（两个点都得写符号）；一个都没出现才按
    普通数字切。混着写（``117°8'2",39.13``）也支持——先取到几个度分秒块，剩下一段
    按裸数字解析。
    """
    if not text:
        return None
    raw = text.strip()

    atoms = _dms_atoms(raw)
    if atoms:
        # 把每个度分秒块换成一个不含分隔符的占位符，再按原顺序分词，
        # 这样「度分秒 + 裸数字」混着写也能各归各位。
        masked = raw
        pieces: list[str] = []
        last_end = 0
        for idx, match in enumerate(_DMS.finditer(raw)):
            pieces.append(raw[last_end : match.start()])
            pieces.append(f"@@{idx}@@ ")
            last_end = match.end()
        pieces.append(raw[last_end:])
        masked = "".join(pieces)
        tokens = [p for p in _COORD_SEP.split(masked) if p]
        if len(tokens) != 2:
            return None
        out: list[float | None] = []
        for token in tokens:
            slot = re.fullmatch(r"@@(\d+)@@", token)
            out.append(atoms[int(slot.group(1))] if slot else _plain_atom(token))
        if out[0] is None or out[1] is None:
            return None
        return out[0], out[1]

    parts = [p for p in _COORD_SEP.split(raw) if p]
    if len(parts) != 2:
        return None
    first, second = _plain_atom(parts[0]), _plain_atom(parts[1])
    if first is None or second is None:
        return None
    return first, second


def looks_like_coord(text: str) -> bool:
    """是不是「两个数字」的形态。用来区分「坐标写错了」和「这是个城市名」。"""
    return _number_pair(text) is not None


def parse_latlon(text: str) -> tuple[float, float] | None:
    """解析坐标，返回 ``(lat, lon)``；不是合法坐标就返回 ``None``。

    顺序**固定为「经度,纬度」**，与和风 API 的 ``location`` 参数一致，也和国际通用写法
    一致（GeoJSON、Google Maps 都是 lon,lat）。不做自动判序：自动判序看起来聪明，实际
    会在经度 < 90 的地方（例如乌鲁木齐 87.62）猜错，而且用户永远不知道该按哪个顺序写。
    写反了会有明确报错，见 :func:`coord_error`。

    小数位截到 2 位：和风文档要求最多两位，且 0.01° ≈ 1.1 km，与后端网格分辨率相当，
    再多位没有实际意义。
    """
    pair = _number_pair(text)
    if pair is None:
        return None
    lon, lat = pair
    if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
        return None
    return round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS)


def coord_error(text: str) -> str:
    """坐标写错时给一句能直接照抄的提示。"""
    pair = _number_pair(text)
    if pair is None:
        if _dms_atoms(text) and len(list(_DMS.finditer(text))) != 2:
            return (
                "度分秒要两个点都写符号，例如 117°8'2\",39°13'53\"（经度在前）。\n"
                "只有度分秒、没有 ° 和 ' 的数字串分不开哪个是哪个。"
            )
        return "坐标格式是「经度,纬度」，例如 117.21,39.13（天津）。"
    lon, lat = pair
    head = "坐标顺序是「经度,纬度」——经度在前，纬度在后。"
    if abs(lat) > 90 >= abs(lon):
        raw = [m.group(0).strip() for m in _DMS.finditer(text)]
        fixed = f"{raw[1]},{raw[0]}" if len(raw) == 2 else f"{lat:g},{lon:g}"
        return (
            f"{head}\n你写的是「纬度,经度」，纬度 117° 那种值超不过 ±90 的只有经度。\n改成：{fixed}"
        )
    return f"{head}\n经度范围 ±180、纬度范围 ±90，你写的是 {lon:g},{lat:g}。"


def format_coord(lat: float, lon: float) -> str:
    """``(lat, lon)`` → ``"117.21,39.13"``（经度在前，与输入顺序一致）。"""
    return f"{lon:.{COORD_DECIMALS}f},{lat:.{COORD_DECIMALS}f}"


def geo_location(lat: float, lon: float) -> str:
    """``(lat, lon)`` → 和风要求的 ``"经度,纬度"``。"""
    return f"{lon:.{COORD_DECIMALS}f},{lat:.{COORD_DECIMALS}f}"


class QWeatherClient:
    """一个 KeyConfig 对应一个客户端，内部复用 aiohttp 连接池与 JWT。"""

    def __init__(self, key: KeyConfig, timeout: float = DEFAULT_TIMEOUT) -> None:
        if key.seed is None:
            raise QWeatherError(0, "缺少私钥", "Ed25519 私钥未配置")
        self._key = key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._token = ""
        self._token_exp = 0.0
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _get_token(self) -> str:
        """带缓存的 JWT，提前 2 分钟换新，避免临界点踩空。"""
        now = time.time()
        if self._token and now < self._token_exp - TOKEN_SAFETY:
            return self._token
        async with self._lock:
            now = time.time()
            if self._token and now < self._token_exp - TOKEN_SAFETY:
                return self._token
            self._token = await asyncio.to_thread(
                make_jwt, self._key.seed, self._key.iss, self._key.sub, self._key.kid, TOKEN_TTL
            )
            self._token_exp = now + TOKEN_TTL
            return self._token

    # ---------------------------------------------------------------- 请求

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        tries: int = 3,
        refresh_on_401: bool = True,
    ) -> dict[str, Any]:
        """GET 一个接口。网络错误重试，业务错误直接抛。"""
        session = await self._ensure_session()
        url = f"https://{self._key.host_clean}{path}"
        last_exc: Exception | None = None

        for attempt in range(tries):
            token = await self._get_token()
            # 不手动设 Accept-Encoding：交给 aiohttp 自动协商与解压。手动设了它仍会解压，
            # 但响应头还留着 Content-Encoding: gzip，再解一次就会 BadGzipFile。
            headers = {"Authorization": f"Bearer {token}"}
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    raw = await resp.read()
                    if raw[:2] == b"\x1f\x8b":  # 万一框架没解压，自己兜一下
                        import gzip

                        raw = gzip.decompress(raw)
                    payload = self._decode(raw, resp.status)
                    if resp.status == 200:
                        code = payload.get("code") if isinstance(payload, dict) else None
                        if code in (None, "200"):
                            return payload
                        raise QWeatherError(200, f"业务错误 code={code}", str(payload)[:200])
                    err = self._error_from(resp.status, payload)
                    # 401 可能是 token 过期（时钟漂移），换一次 token 重试
                    if resp.status == 401 and refresh_on_401:
                        self._token_exp = 0
                        refresh_on_401 = False
                        continue
                    raise err
            except QWeatherError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < tries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise QWeatherError(0, "网络错误", f"{type(exc).__name__}: {exc}") from exc

        raise QWeatherError(0, "请求失败", str(last_exc) if last_exc else "未知原因")

    @staticmethod
    def _decode(raw: bytes, status: int) -> dict[str, Any]:
        import json

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"raw": raw[:200].decode("utf-8", "replace")}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    @staticmethod
    def _error_from(status: int, payload: dict[str, Any]) -> QWeatherError:
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return QWeatherError(
                int(err.get("status") or status),
                str(err.get("title") or ""),
                str(err.get("detail") or ""),
            )
        if status == 404 and not payload:
            return QWeatherError(
                404, "接口不可用", "路径不存在或该接口未在凭据中启用（和风的空响应 404 有歧义）"
            )
        return QWeatherError(status, f"HTTP {status}", str(payload)[:200])

    # ---------------------------------------------------------------- 天气

    async def current(self, lat: float, lon: float) -> dict[str, Any]:
        """实时天气，归一化成扁平字段（温度是数字、湿度是 0~100 整数）。"""
        data = await self._get(f"/weather/v1/current/{lat:.2f}/{lon:.2f}")
        humidity = data.get("humidity")
        wind = data.get("wind") or {}
        precip = data.get("precipitation") or {}
        return {
            "temp": _num(data.get("temperature")),
            "feels_like": _num(data.get("feelsLike")),
            "text": (data.get("condition") or {}).get("text") or "",
            "code": (data.get("condition") or {}).get("code") or "",
            "humidity": round(humidity * 100) if isinstance(humidity, (int, float)) else None,
            "wind_dir": (wind.get("direction") or {}).get("compass") or "",
            "wind_scale": wind.get("scale"),
            "wind_speed": _num(wind.get("speed")),
            "pressure": _num(data.get("pressure")),
            "visibility": _num(data.get("visibility")),
            "cloud": data.get("cloudCover"),
            "uv": data.get("uvIndex"),
            "precip": _num(precip.get("amount") or {}),
            "precip_type": precip.get("type"),
            "dew_point": _num(data.get("dewPoint")),
        }

    async def daily(self, lat: float, lon: float, days: int = 4) -> list[dict[str, Any]]:
        """每日预报。v1 上限 10 天，超过会 400。"""
        days = max(1, min(int(days), 10))
        data = await self._get(
            f"/weather/v1/daily/{lat:.2f}/{lon:.2f}",
            {"days": days, "localTime": "true"},
        )
        out: list[dict[str, Any]] = []
        for item in data.get("days") or []:
            # 注意：v1 的高低温在 days[] 顶层，不在 daytime/nighttime 里；
            # 日出日落等天文数据在 days[].astro 下。这两处极易写错。
            day = item.get("daytime") or {}
            night = item.get("nighttime") or {}
            astro = item.get("astro") or {}
            humidity = day.get("humidity")
            out.append(
                {
                    "date": (item.get("forecastStartTime") or "")[:10],
                    "temp_max": _num(item.get("temperatureMax")),
                    "temp_min": _num(item.get("temperatureMin")),
                    "temp_avg": _num(item.get("temperatureAvg")),
                    "text_day": (day.get("condition") or {}).get("text") or "",
                    "text_night": (night.get("condition") or {}).get("text") or "",
                    "precip_prob": _num((day.get("precipitation") or {}).get("probability")),
                    "precip": _num(((day.get("precipitation") or {}).get("amount")) or {}),
                    "humidity": round(humidity * 100)
                    if isinstance(humidity, (int, float))
                    else None,
                    "wind_dir": ((day.get("wind") or {}).get("direction") or {}).get("compass")
                    or "",
                    "wind_scale": (day.get("wind") or {}).get("scale"),
                    "uv": item.get("uvIndexMax"),
                    "sunrise": (astro.get("sunrise") or "")[11:16],
                    "sunset": (astro.get("sunset") or "")[11:16],
                    "moonrise": (astro.get("moonrise") or "")[11:16],
                    "moon_phase": astro.get("moonPhase") or "",
                }
            )
        return out

    async def hourly(self, lat: float, lon: float, hours: int = 12) -> list[dict[str, Any]]:
        hours = max(1, min(int(hours), 240))
        data = await self._get(
            f"/weather/v1/hourly/{lat:.2f}/{lon:.2f}",
            {"hours": hours, "localTime": "true"},
        )
        out: list[dict[str, Any]] = []
        for item in data.get("hours") or []:
            precip = item.get("precipitation") or {}
            humidity = item.get("humidity")
            out.append(
                {
                    "time": (item.get("forecastTime") or "")[11:16],
                    "temp": _num(item.get("temperature")),
                    "text": (item.get("condition") or {}).get("text") or "",
                    "precip_prob": _num(precip.get("probability")),
                    "precip": _num(precip.get("amount") or {}),
                    "wind_scale": (item.get("wind") or {}).get("scale"),
                    "humidity": round(humidity * 100)
                    if isinstance(humidity, (int, float))
                    else None,
                }
            )
        return out

    async def minutely(self, lat: float, lon: float) -> dict[str, Any]:
        """未来 2 小时分钟级降水。只有中国区有数据。"""
        data = await self._get("/v7/minutely/5m", {"location": f"{lon:.2f},{lat:.2f}"})
        points = [
            {
                "time": (p.get("fxTime") or "")[11:16],
                "precip": float(p.get("precip") or 0),
                "type": p.get("type") or "",
            }
            for p in (data.get("minutely") or [])
        ]
        return {"summary": data.get("summary") or "", "points": points}

    async def air_now(self, lat: float, lon: float) -> dict[str, Any] | None:
        data = await self._get(f"/airquality/v1/current/{lat:.2f}/{lon:.2f}")
        indexes = data.get("indexes") or []
        chosen = next(
            (i for i in indexes if str(i.get("code")) == "cn-mee"),
            indexes[0] if indexes else None,
        )
        if not chosen:
            return None
        pollutant = chosen.get("primaryPollutant") or {}
        return {
            "aqi": chosen.get("aqi"),
            "category": chosen.get("category") or "",
            "level": chosen.get("level"),
            "primary": pollutant.get("name") or "",
            "advice": ((chosen.get("health") or {}).get("advice") or {}).get("generalPopulation")
            or "",
        }

    async def indices(self, lat: float, lon: float, types: str = "0") -> list[dict[str, str]]:
        """生活指数。在 v7，不在 v1。``types="0"`` 取全部（16 项）。"""
        data = await self._get(
            "/v7/indices/1d", {"location": f"{lon:.2f},{lat:.2f}", "type": types}
        )
        return [
            {
                "type": str(d.get("type") or ""),
                "name": d.get("name") or "",
                "level": str(d.get("level") or ""),
                "category": d.get("category") or "",
                "text": d.get("text") or "",
            }
            for d in (data.get("daily") or [])
        ]

    # ---------------------------------------------------------------- 预警

    async def warning_now(self, lat: float, lon: float) -> list[dict[str, Any]]:
        data = await self._get("/v7/warning/now", {"location": f"{lon:.2f},{lat:.2f}"})
        return [self._norm_warning(w) for w in (data.get("warning") or [])]

    @staticmethod
    def _norm_warning(w: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(w.get("id") or ""),
            "title": w.get("title") or "",
            "sender": w.get("sender") or "",
            "level": w.get("level") or "",
            "type": str(w.get("type") or ""),
            "type_name": w.get("typeName") or "",
            "status": w.get("status") or "",
            "related": str(w.get("related") or ""),
            "start": (w.get("startTime") or "")[5:16].replace("T", " "),
            "end": (w.get("endTime") or "")[5:16].replace("T", " "),
            "text": w.get("text") or "",
        }

    async def warning_list(self, region: str = "cn") -> list[str]:
        """全国有生效预警的地区 ID 列表。一次调用覆盖所有城市，轮询预警只该用它。"""
        data = await self._get("/v7/warning/list", {"range": region})
        return [str(x.get("locationId") or "") for x in (data.get("warningLocList") or [])]

    async def warning_detail(self, lat: float, lon: float) -> list[dict[str, Any]]:
        """v1 预警，比 v7 多 ``criteria``（预警标准）与 ``instruction``（防御指南）。"""
        data = await self._get(f"/weatheralert/v1/current/{lat:.2f}/{lon:.2f}")
        out: list[dict[str, Any]] = []
        for a in data.get("alerts") or []:
            event = a.get("eventType") or {}
            color = a.get("color") or {}
            out.append(
                {
                    "id": str(a.get("id") or ""),
                    "headline": a.get("headline") or "",
                    "type_name": event.get("name") or "",
                    "type": str(event.get("code") or ""),
                    "severity": str(a.get("severity") or ""),
                    "color": str(color.get("code") or ""),
                    "criteria": a.get("criteria") or "",
                    "instruction": a.get("instruction") or "",
                    "description": a.get("description") or "",
                }
            )
        return out

    # ---------------------------------------------------------------- 地理 / 账户

    async def city_lookup(self, name: str, number: int = 5) -> list[dict[str, Any]]:
        data = await self._get(
            "/geo/v2/city/lookup", {"location": name, "number": max(1, min(number, 20))}
        )
        out: list[dict[str, Any]] = []
        for x in data.get("location") or []:
            item = {
                "name": x.get("name") or "",
                "id": str(x.get("id") or ""),
                "adm1": x.get("adm1") or "",
                "adm2": x.get("adm2") or "",
                "lat": float(x.get("lat") or 0),
                "lon": float(x.get("lon") or 0),
                "country": x.get("country") or "",
            }
            # 「市」的 name 和 adm2 相同（杭州/杭州）；「区县」不同（萧山/杭州）。
            # 这个区别决定预警怎么匹配，见 pushers.location_matches。
            item["level"] = "city" if item["name"] == item["adm2"] else "district"
            out.append(item)
        return out

    async def reverse_lookup(self, lat: float, lon: float) -> dict[str, Any] | None:
        """坐标反查最近的行政区（区县级）。

        用途有两个：给坐标点一个可读的名字；拿到 LocationID 用于预警列表的前缀匹配。
        注意返回的 ``lat``/``lon`` 是**该行政区中心点**，不是用户给的坐标，别拿来查天气。
        """
        places = await self.city_lookup(geo_location(lat, lon), 1)
        return places[0] if places else None

    async def finance_summary(self) -> dict[str, Any]:
        """需在控制台给凭据勾选「允许访问财务汇总数据」，否则 403。"""
        return await self._get("/finance/v1/summary")

    async def metrics_stats(self) -> dict[str, Any]:
        """最近 24 小时请求量。需勾选「允许访问请求量统计」。"""
        return await self._get("/metrics/v1/stats")
