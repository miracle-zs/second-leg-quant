#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一币安 USDⓈ-M 合约 HTTP 与签名客户端 (Unified Binance Futures Client)
==============================================================================
核心特性:
1. 复用 scripts/http_client.py 的线程安全 Keep-Alive 连接池与指数退避重试。
2. 线程安全 10 分钟时间偏移缓存 (server_time - local_time)，不持锁阻塞网络 IO；
   遇到 -1021 时间戳漂移错误时自动重校准并重试一次（不重置重试计数）。
3. 提供公共行情接口（批量价格、K 线、最新已收盘 15m K 线）与签名私有接口（账户、持仓、流水、成交），
   接口异常或缺失必填字段时显式抛出 BinanceAPIError，不做静默兜底。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import DEFAULT_ENV_PATH, clean_symbol, get_beijing_time_str, get_env_setting  # noqa: E402
import http_client  # noqa: E402
from http_client import DEFAULT_USER_AGENT, HttpError, RETRYABLE_STATUS_CODES, retry_delay  # noqa: E402

logger = logging.getLogger("binance_client")

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
POSITION_RISK_ENDPOINT = "/fapi/v2/positionRisk"
SERVER_TIME_ENDPOINT = "/fapi/v1/time"
TIME_SYNC_TTL_SEC = 600.0


class BinanceAPIError(RuntimeError):
    """币安接口请求、HTTP 状态码、鉴权或数据格式异常。"""

    def __init__(
        self,
        message: str,
        *,
        code: Any = None,
        endpoint: str = "",
        status_code: Optional[int] = None,
        binance_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.msg = message
        self.endpoint = endpoint
        self.status_code = status_code
        self.binance_code = binance_code


def _strict_int(val: Any) -> int:
    if isinstance(val, bool) or val is None:
        raise TypeError(f"Expected integer, got {val!r}")
    return int(val)


def _finite_float(val: Any) -> float:
    if isinstance(val, bool) or val is None:
        raise TypeError(f"Expected float, got {val!r}")
    num = float(val)
    if not math.isfinite(num):
        raise ValueError(f"Expected finite float, got {val!r}")
    return num


def _parse_binance_error_payload(body: str) -> tuple[Optional[int], str]:
    """Parse Binance JSON error payload `{"code": int, "msg": str}` when available."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None, ""
    if not isinstance(parsed, dict) or parsed.get("code") is None or isinstance(parsed.get("code"), bool):
        return None, ""
    try:
        return int(parsed["code"]), str(parsed.get("msg") or "")
    except (TypeError, ValueError):
        return None, ""


def _http_error_to_binance_error(err: HttpError, *, endpoint: str = "") -> BinanceAPIError:
    target = endpoint or err.url
    if err.status_code is not None:
        b_code, b_msg = _parse_binance_error_payload(err.body)
        if b_code is not None:
            message = f"HTTP {err.status_code} from {target} [{b_code}]: {b_msg}"
        else:
            message = f"HTTP {err.status_code} from {target}: {err.body}"
        return BinanceAPIError(
            message,
            code=err.status_code,
            endpoint=target,
            status_code=err.status_code,
            binance_code=b_code,
        )
    err_str = str(err)
    code = "INVALID_JSON" if err_str.startswith("Invalid JSON") else "NETWORK_ERROR"
    return BinanceAPIError(err_str, code=code, endpoint=target)


def fetch_json_with_retry(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 15,
    max_retries: int = 3,
    backoff: float = 0.8,
) -> Any:
    """带指数退避重试的通用 HTTP GET JSON 请求。"""
    try:
        return http_client.request_json(
            url,
            method="GET",
            headers=headers,
            timeout=float(timeout),
            max_retries=max(0, max_retries - 1),
            backoff_base=backoff,
            retry_statuses=RETRYABLE_STATUS_CODES,
        )
    except HttpError as e:
        raise _http_error_to_binance_error(e, endpoint=url) from e


def _parse_candle_row(symbol: str, interval: str, row: Sequence[Any]) -> dict[str, Any]:
    if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
        raise BinanceAPIError(
            f"Malformed kline row for {symbol} ({interval}): {row!r}",
            code="INVALID_RESPONSE",
            endpoint="/fapi/v1/klines",
        )
    try:
        open_ms = _strict_int(row[0])
        close_ms = _strict_int(row[6])
        open_p = _finite_float(row[1])
        high_p = _finite_float(row[2])
        low_p = _finite_float(row[3])
        close_p = _finite_float(row[4])
        vol = _finite_float(row[5])
    except (IndexError, TypeError, ValueError) as exc:
        raise BinanceAPIError(
            f"Malformed kline row for {symbol} ({interval}): {row!r}",
            code="INVALID_RESPONSE",
            endpoint="/fapi/v1/klines",
        ) from exc
    open_sec = open_ms // 1000
    close_sec = close_ms // 1000
    return {
        "symbol": symbol,
        "interval": interval,
        "open_time_ms": open_ms,
        "close_time_ms": close_ms,
        "open_time": open_sec,
        "close_time": close_sec,
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "close": close_p,
        "volume": vol,
        "candle_time": get_beijing_time_str(open_sec)[:16],
        "is_closed": True,
    }


class BinanceFuturesClient:
    """币安 USDⓈ-M 合约统一 API 客户端（含线程安全时间偏移缓存、签名鉴权与批量行情）。"""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = BINANCE_FUTURES_BASE_URL,
        timeout: int = 8,
        time_sync_ttl: float = TIME_SYNC_TTL_SEC,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.time_sync_ttl = float(time_sync_ttl)
        self._time_lock = threading.Lock()
        self._time_sync_lock = threading.Lock()
        self._time_offset_ms: Optional[int] = None
        self._synced_server_ms: Optional[int] = None
        self._time_synced_at: float = 0.0

    @classmethod
    def from_env(cls, env_path: Path = DEFAULT_ENV_PATH) -> "BinanceFuturesClient":
        """优先从环境变量，其次从 .env 文件初始化客户端。"""
        return cls(
            api_key=get_env_setting("BINANCE_API_KEY", "", env_path=env_path),
            api_secret=get_env_setting("BINANCE_API_SECRET", "", env_path=env_path),
        )

    def is_configured(self) -> bool:
        """检查 API 凭证是否已完整配置。"""
        return bool(self.api_key and self.api_secret)

    def _generate_signature(self, query_string: str) -> str:
        """根据 Query String 计算 HMAC-SHA256 签名。"""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _cached_server_time_locked(self, now_mono: float) -> Optional[int]:
        if (now_mono - self._time_synced_at) >= self.time_sync_ttl:
            return None
        if self._synced_server_ms is not None:
            return self._synced_server_ms + int((now_mono - self._time_synced_at) * 1000)
        if self._time_offset_ms is not None:
            return int(time.time() * 1000) + self._time_offset_ms
        return None

    def get_server_time(self, force_sync: bool = False) -> int:
        """
        获取币安服务器毫秒级时间戳（线程安全实例级单调时钟增量缓存，单飞防并发惊群）。
        拉取或解析失败时抛出 BinanceAPIError。
        """
        with self._time_lock:
            entry_synced_at = self._time_synced_at
            if not force_sync:
                cached = self._cached_server_time_locked(time.monotonic())
                if cached is not None:
                    return cached

        with self._time_sync_lock:
            with self._time_lock:
                now_mono = time.monotonic()
                if not force_sync:
                    cached = self._cached_server_time_locked(now_mono)
                    if cached is not None:
                        return cached
                elif self._time_synced_at > entry_synced_at:
                    cached = self._cached_server_time_locked(now_mono)
                    if cached is not None:
                        return cached

            url = f"{self.base_url}{SERVER_TIME_ENDPOINT}"
            m0 = time.monotonic()
            t0 = int(time.time() * 1000)
            data = fetch_json_with_retry(url, timeout=5, max_retries=2)
            m1 = time.monotonic()
            t1 = int(time.time() * 1000)
            if not isinstance(data, dict) or "serverTime" not in data:
                raise BinanceAPIError(
                    f"Missing serverTime in {SERVER_TIME_ENDPOINT} response: {data}",
                    code="INVALID_RESPONSE",
                    endpoint=SERVER_TIME_ENDPOINT,
                )
            try:
                server_ms = _strict_int(data["serverTime"])
                if server_ms <= 0:
                    raise ValueError(f"Non-positive serverTime: {server_ms}")
            except (TypeError, ValueError) as e:
                raise BinanceAPIError(
                    f"Invalid serverTime in {SERVER_TIME_ENDPOINT}: {data['serverTime']}",
                    code="INVALID_RESPONSE",
                    endpoint=SERVER_TIME_ENDPOINT,
                ) from e
            with self._time_lock:
                self._synced_server_ms = server_ms
                self._time_offset_ms = server_ms - (t0 + t1) // 2
                self._time_synced_at = (m0 + m1) / 2.0
            return server_ms

    def _send_signed_request(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        max_retries: int = 2,
        backoff: float = 0.8,
    ) -> object:
        """
        发送已签名的 GET 请求至币安合约 API。
        支持 429/5xx/网络异常指数退避重试，以及至多一次 -1021 时间偏移重校准。
        失败时显式抛出 BinanceAPIError。
        """
        if not self.is_configured():
            raise BinanceAPIError(
                f"{endpoint} failed [NOT_CONFIGURED]: API keys not configured",
                code="NOT_CONFIGURED",
                endpoint=endpoint,
            )

        time_resynced = False
        headers = {
            "X-MBX-APIKEY": self.api_key,
            "User-Agent": DEFAULT_USER_AGENT,
        }

        for attempt in range(max_retries + 1):
            server_ts = self.get_server_time()
            p: dict[str, Any] = dict(params or {})
            p["timestamp"] = server_ts
            p["recvWindow"] = 5000

            query_str = urllib.parse.urlencode(p)
            signature = self._generate_signature(query_str)
            url = f"{self.base_url}{endpoint}?{query_str}&signature={signature}"

            try:
                data = http_client.request_json(
                    url,
                    method="GET",
                    headers=headers,
                    timeout=float(self.timeout),
                    max_retries=0,
                )
            except HttpError as e:
                api_err = _http_error_to_binance_error(e, endpoint=endpoint)
                if api_err.binance_code == -1021 and not time_resynced and attempt < max_retries:
                    time_resynced = True
                    logger.info("检测到币安 -1021 时间偏移，重新同步服务器时间后重试 [%s]", endpoint)
                    self.get_server_time(force_sync=True)
                    continue
                retryable = e.status_code is None or e.status_code in RETRYABLE_STATUS_CODES
                if retryable and attempt < max_retries:
                    time.sleep(retry_delay(backoff, attempt, e.retry_after))
                    continue
                logger.warning("币安签名请求失败 [%s] (%s): %s", endpoint, api_err.code, api_err)
                raise api_err from e

            if isinstance(data, dict) and data.get("code") is not None:
                raw_code = data["code"]
                if isinstance(raw_code, bool):
                    raise BinanceAPIError(
                        f"{endpoint} returned malformed error code {raw_code!r}: {data.get('msg') or ''}",
                        code="INVALID_RESPONSE",
                        endpoint=endpoint,
                    )
                try:
                    b_code = int(raw_code)
                except (TypeError, ValueError) as exc:
                    raise BinanceAPIError(
                        f"{endpoint} returned malformed error code {raw_code!r}: {data.get('msg') or ''}",
                        code="INVALID_RESPONSE",
                        endpoint=endpoint,
                    ) from exc
                if b_code < 0:
                    if b_code == -1021 and not time_resynced and attempt < max_retries:
                        time_resynced = True
                        logger.info("检测到币安 -1021 时间偏移，重新同步服务器时间后重试 [%s]", endpoint)
                        self.get_server_time(force_sync=True)
                        continue
                    msg = str(data.get("msg") or "")
                    logger.error("币安接口返回错误 [%s]: [%s] %s", endpoint, b_code, msg)
                    raise BinanceAPIError(
                        f"{endpoint} failed [{b_code}]: {msg}",
                        code=b_code,
                        endpoint=endpoint,
                        binance_code=b_code,
                    )
            return data

        raise BinanceAPIError(
            f"Exhausted retries for {endpoint}",
            code="RETRY_EXHAUSTED",
            endpoint=endpoint,
        )

    def get_account_positions(self) -> List[dict]:
        """请求 /fapi/v2/positionRisk 获取账户所有合约头寸信息。"""
        data = self._send_signed_request(POSITION_RISK_ENDPOINT)
        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Expected list from {POSITION_RISK_ENDPOINT}, got {type(data).__name__}",
                code="INVALID_RESPONSE",
                endpoint=POSITION_RISK_ENDPOINT,
            )
        return data

    def get_account_info(self) -> dict:
        """请求 /fapi/v2/account 获取合约账户全量资产、钱包余额与保证金信息。"""
        data = self._send_signed_request("/fapi/v2/account")
        if not isinstance(data, dict):
            raise BinanceAPIError(
                f"Expected dict from /fapi/v2/account, got {type(data).__name__}",
                code="INVALID_RESPONSE",
                endpoint="/fapi/v2/account",
            )
        return data

    def get_income(
        self,
        income_type: Optional[str] = None,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> List[dict]:
        """请求 /fapi/v1/income 获取合约账户资金流水。"""
        params: Dict[str, Any] = {"limit": min(limit, 1000)}
        if income_type is not None:
            params["incomeType"] = income_type
        if symbol is not None:
            params["symbol"] = clean_symbol(symbol)
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        data = self._send_signed_request("/fapi/v1/income", params)
        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Expected list from /fapi/v1/income, got {type(data).__name__}",
                code="INVALID_RESPONSE",
                endpoint="/fapi/v1/income",
            )
        return data

    def get_user_trades(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        from_id: Optional[int] = None,
        limit: int = 1000,
    ) -> List[dict]:
        """请求 /fapi/v1/userTrades 获取指定交易对的历史成交记录。"""
        params: Dict[str, Any] = {
            "symbol": clean_symbol(symbol),
            "limit": min(limit, 1000),
        }
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        if from_id is not None:
            params["fromId"] = int(from_id)
        data = self._send_signed_request("/fapi/v1/userTrades", params)
        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Expected list from /fapi/v1/userTrades, got {type(data).__name__}",
                code="INVALID_RESPONSE",
                endpoint="/fapi/v1/userTrades",
            )
        return data

    def get_ticker_prices(self, symbols: Optional[Sequence[str]] = None) -> Dict[str, float]:
        """批量获取永续合约最新成交价（一次 HTTP 请求替代逐币轮询）。"""
        url = f"{self.base_url}/fapi/v1/ticker/price"
        data = fetch_json_with_retry(url, timeout=self.timeout, max_retries=2)
        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Expected list from /fapi/v1/ticker/price, got {type(data).__name__}",
                code="INVALID_RESPONSE",
                endpoint="/fapi/v1/ticker/price",
            )
        wanted = {clean_symbol(s) for s in symbols} if symbols is not None else None
        prices: Dict[str, float] = {}
        for item in data:
            if not isinstance(item, dict) or "symbol" not in item or "price" not in item:
                raise BinanceAPIError(
                    f"Malformed ticker item from /fapi/v1/ticker/price: {item}",
                    code="INVALID_RESPONSE",
                    endpoint="/fapi/v1/ticker/price",
                )
            sym = clean_symbol(item["symbol"])
            if not sym:
                raise BinanceAPIError(
                    f"Empty symbol in /fapi/v1/ticker/price: {item}",
                    code="INVALID_RESPONSE",
                    endpoint="/fapi/v1/ticker/price",
                )
            if wanted is None or sym in wanted:
                try:
                    prices[sym] = _finite_float(item["price"])
                except (TypeError, ValueError) as e:
                    raise BinanceAPIError(
                        f"Invalid ticker price for {sym}: {item['price']}",
                        code="INVALID_RESPONSE",
                        endpoint="/fapi/v1/ticker/price",
                    ) from e
        return prices

    def fetch_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 5,
        *,
        start_time_ms: Optional[int] = None,
        end_time_ms: Optional[int] = None,
        session: Optional[http_client.HttpClient] = None,
        max_retries: int = 3,
        backoff: float = 0.8,
    ) -> List[list]:
        """请求 /fapi/v1/klines 获取合约 K 线数组（空数组返回 []）。"""
        clean_sym = clean_symbol(symbol)
        query_params: Dict[str, Any] = {
            "symbol": clean_sym,
            "interval": interval,
            "limit": int(limit),
        }
        if start_time_ms is not None:
            query_params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            query_params["endTime"] = int(end_time_ms)
        url = f"{self.base_url}/fapi/v1/klines?{urllib.parse.urlencode(query_params)}"
        req_timeout = max(self.timeout, 10)
        if session is None:
            data = fetch_json_with_retry(url, timeout=req_timeout, max_retries=max_retries, backoff=backoff)
        else:
            try:
                data = session.request_json(
                    url,
                    method="GET",
                    timeout=float(req_timeout),
                    max_retries=max(0, max_retries - 1),
                    backoff_base=backoff,
                    retry_statuses=RETRYABLE_STATUS_CODES,
                )
            except HttpError as e:
                raise _http_error_to_binance_error(e, endpoint=url) from e
        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Expected list from /fapi/v1/klines for {clean_sym} ({interval}), got {type(data).__name__}",
                code="INVALID_RESPONSE",
                endpoint="/fapi/v1/klines",
            )
        return data

    def get_closed_candles(
        self,
        symbol: str,
        *,
        interval: str = "15m",
        limit: int = 5,
        allow_empty: bool = False,
        session: Optional[http_client.HttpClient] = None,
        max_retries: int = 3,
        backoff: float = 0.8,
    ) -> List[dict[str, Any]]:
        """拉取并严格过滤已收盘 K 线（close_time <= server_time_ms），返回标准化字典列表。"""
        clean_sym = clean_symbol(symbol)
        server_time_ms = self.get_server_time()
        klines_data = self.fetch_klines(
            clean_sym,
            interval=interval,
            limit=limit,
            session=session,
            max_retries=max_retries,
            backoff=backoff,
        )
        if not klines_data:
            if allow_empty:
                return []
            raise BinanceAPIError(
                f"No candlestick data returned for {clean_sym} ({interval})",
                code="EMPTY_KLINES",
                endpoint="/fapi/v1/klines",
            )

        try:
            closed_rows = [
                c
                for c in klines_data
                if not isinstance(c, (str, bytes))
                and isinstance(c, Sequence)
                and _strict_int(c[6]) <= server_time_ms
            ]
            if len(closed_rows) < len(klines_data):
                # Validate sequence structure on unclosed trailing rows too so malformed payloads fail fast.
                for c in klines_data:
                    if isinstance(c, (str, bytes)) or not isinstance(c, Sequence):
                        raise TypeError(f"Non-sequence kline row: {c!r}")
                    _strict_int(c[6])
        except (IndexError, TypeError, ValueError) as exc:
            raise BinanceAPIError(
                f"Malformed kline row for {clean_sym} ({interval}): {klines_data!r}",
                code="INVALID_RESPONSE",
                endpoint="/fapi/v1/klines",
            ) from exc
        if not closed_rows:
            if allow_empty:
                return []
            raise BinanceAPIError(
                f"No confirmed closed candle for {clean_sym} ({interval})",
                code="UNCLOSED_CANDLE",
                endpoint="/fapi/v1/klines",
            )
        return [_parse_candle_row(clean_sym, interval, row) for row in closed_rows]

    def get_closed_15m_candle(
        self,
        symbol: str,
        *,
        interval: str = "15m",
        limit: int = 5,
    ) -> dict[str, Any]:
        """
        拉取并严格确认最新已收盘 K 线（close_time <= server_time_ms），同时附带 prev_candle。
        """
        closed_candles = self.get_closed_candles(symbol, interval=interval, limit=limit, allow_empty=False)
        candle = closed_candles[-1]
        candle["prev_candle"] = closed_candles[-2] if len(closed_candles) >= 2 else None
        return candle

    def get_active_positions(self, include_short: bool = True) -> List[dict]:
        """
        筛选并标准化当前有效开仓的合约持仓。
        - include_short=True: 返回所有非零持仓（LONG 与 SHORT）
        - include_short=False: 仅返回多头持仓 (positionAmt > 0)
        """
        raw_list = self.get_account_positions()
        active: List[dict] = []
        for p in raw_list:
            try:
                sym = clean_symbol(p["symbol"])
                if not sym:
                    raise ValueError(f"Empty position symbol: {p!r}")
                amt = _finite_float(p["positionAmt"])
                if amt == 0.0:
                    continue
                if not include_short and amt < 0:
                    continue
                entry_p = _finite_float(p["entryPrice"])
                mark_p = _finite_float(p["markPrice"])
                pnl = _finite_float(p["unRealizedProfit"])
                leverage = _strict_int(p["leverage"])
                if "positionSide" in p:
                    raw_ps = p["positionSide"]
                    if not isinstance(raw_ps, str) or not raw_ps.strip():
                        raise ValueError(f"Invalid positionSide: {raw_ps!r}")
                    raw_side = raw_ps.strip().upper()
                    if raw_side not in ("BOTH", "LONG", "SHORT"):
                        raise ValueError(f"Unexpected positionSide: {raw_ps!r}")
                else:
                    raw_side = "BOTH"
                side = raw_side if raw_side in ("LONG", "SHORT") else ("LONG" if amt > 0 else "SHORT")
                if "updateTime" in p:
                    update_time: Optional[int] = _strict_int(p["updateTime"])
                    if update_time < 0:
                        raise ValueError(f"Negative updateTime: {update_time}")
                else:
                    update_time = None
                if "notional" in p:
                    notional = abs(_finite_float(p["notional"]))
                else:
                    notional = abs(amt * mark_p)
            except (KeyError, TypeError, ValueError) as e:
                raise BinanceAPIError(
                    f"Malformed position payload in {POSITION_RISK_ENDPOINT}: {p}",
                    code="INVALID_RESPONSE",
                    endpoint=POSITION_RISK_ENDPOINT,
                ) from e

            active.append({
                "symbol": sym,
                "side": side,
                "amount": abs(amt),
                "signed_amount": amt,
                "entry_price": entry_p,
                "mark_price": mark_p,
                "unrealized_pnl": pnl,
                "leverage": leverage,
                "position_side": raw_side,
                "update_time": update_time,
                "notional": notional,
            })
        return active


_PUBLIC_CLIENT_LOCK = threading.Lock()
_PUBLIC_CLIENT: Optional[BinanceFuturesClient] = None


def _get_public_client() -> BinanceFuturesClient:
    global _PUBLIC_CLIENT
    with _PUBLIC_CLIENT_LOCK:
        if _PUBLIC_CLIENT is None:
            _PUBLIC_CLIENT = BinanceFuturesClient()
        return _PUBLIC_CLIENT


def get_closed_candles(
    symbol: str,
    *,
    interval: str = "15m",
    limit: int = 5,
    allow_empty: bool = False,
    session: Optional[http_client.HttpClient] = None,
    max_retries: int = 3,
    backoff: float = 0.8,
) -> List[dict[str, Any]]:
    """共享公共已收盘 K 线列表拉取辅助函数。"""
    return _get_public_client().get_closed_candles(
        symbol,
        interval=interval,
        limit=limit,
        allow_empty=allow_empty,
        session=session,
        max_retries=max_retries,
        backoff=backoff,
    )


def get_closed_15m_candle(
    symbol: str,
    *,
    interval: str = "15m",
    limit: int = 5,
) -> dict[str, Any]:
    """共享公共最新已收盘 K 线拉取辅助函数。"""
    return _get_public_client().get_closed_15m_candle(symbol, interval=interval, limit=limit)


def fetch_ticker_prices(symbols: Optional[Sequence[str]] = None) -> Dict[str, float]:
    """共享公共批量最新成交价拉取辅助函数。"""
    return _get_public_client().get_ticker_prices(symbols=symbols)


def get_server_time_ms(force_sync: bool = False) -> int:
    """共享公共币安服务器毫秒级时间戳获取函数。"""
    return _get_public_client().get_server_time(force_sync=force_sync)
