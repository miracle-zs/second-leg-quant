#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一币安 USDⓈ-M 合约 HTTP 与签名客户端 (Unified Binance Futures Client)
==============================================================================
核心特性:
1. 线程安全 10 分钟时间偏移缓存 (server_time - local_time)，避免每次请求重复拉取 /fapi/v1/time；
   遇到 -1021 时间戳漂移错误时自动重校准并重试一次。
2. 统一指数退避重试，仅对 429/5xx/URLError/TimeoutError 重试，400/401/403/404 快速失败。
3. 提供公共行情接口（批量价格、K 线、最新已收盘 15m K 线）与签名私有接口（账户、持仓、流水、成交），
   接口异常或缺失必填字段时显式抛出 BinanceAPIError，不做静默兜底。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from config import DEFAULT_ENV_PATH, clean_symbol, get_beijing_time_str, load_dotenv
except ImportError:
    from scripts.config import DEFAULT_ENV_PATH, clean_symbol, get_beijing_time_str, load_dotenv

logger = logging.getLogger("binance_client")

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
POSITION_RISK_ENDPOINT = "/fapi/v2/positionRisk"
SERVER_TIME_ENDPOINT = "/fapi/v1/time"
TIME_SYNC_TTL_SEC = 600.0
RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_USER_AGENT = "TradeAlertDaemon/1.0"


class BinanceAPIError(RuntimeError):
    """币安接口请求、HTTP 状态码、鉴权或数据格式异常。"""

    def __init__(self, message: str, *, code: Any = None, endpoint: str = ""):
        super().__init__(message)
        self.code = code
        self.msg = message
        self.endpoint = endpoint


def _retry_delay(attempt: int, backoff: float, err: Optional[urllib.error.HTTPError] = None) -> float:
    delay = backoff * (2 ** attempt)
    if err is not None and err.code == 429 and err.headers:
        retry_after = err.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = max(delay, min(float(retry_after), 10.0))
            except ValueError:
                pass
    return delay


def fetch_bytes_with_retry(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 15,
    max_retries: int = 3,
    backoff: float = 0.8,
) -> bytes:
    """带指数退避重试的通用 HTTP GET 请求（仅重试 429/5xx/网络超时，4xx 快速失败）。"""
    req_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    last_err: Optional[Exception] = None
    last_code: Any = None

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            last_err = e
            last_code = e.code
            if e.code not in RETRYABLE_HTTP_CODES:
                raise BinanceAPIError(
                    f"HTTP {e.code} from {url}: {err_body}",
                    code=e.code,
                    endpoint=url,
                ) from e
            if attempt == max_retries - 1:
                break
            time.sleep(_retry_delay(attempt, backoff, e))
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as e:
            last_err = e
            last_code = "NETWORK_ERROR"
            if attempt == max_retries - 1:
                break
            time.sleep(_retry_delay(attempt, backoff))

    raise BinanceAPIError(
        f"Request failed after {max_retries} attempts ({url}): {last_err}",
        code=last_code,
        endpoint=url,
    ) from last_err


def fetch_json_with_retry(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 15,
    max_retries: int = 3,
    backoff: float = 0.8,
) -> Any:
    """带指数退避重试的通用 HTTP GET JSON 请求。"""
    raw = fetch_bytes_with_retry(
        url,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        backoff=backoff,
    )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BinanceAPIError(f"Invalid JSON from {url}: {e}", code="INVALID_JSON", endpoint=url) from e


def _parse_candle_row(symbol: str, interval: str, row: Sequence[Any]) -> dict[str, Any]:
    open_ms = int(row[0])
    close_ms = int(row[6])
    open_sec = open_ms // 1000
    close_sec = close_ms // 1000
    return {
        "symbol": symbol,
        "interval": interval,
        "open_time_ms": open_ms,
        "close_time_ms": close_ms,
        "open_time": open_sec,
        "close_time": close_sec,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "candle_time": get_beijing_time_str(open_sec)[:16],
        "is_closed": True,
    }


def _extract_binance_error_code(err_body: str) -> Optional[int]:
    try:
        parsed = json.loads(err_body)
        if isinstance(parsed, dict) and parsed.get("code") is not None:
            return int(parsed["code"])
    except (ValueError, TypeError):
        pass
    if '"code":-1021' in err_body.replace(" ", ""):
        return -1021
    return None


class BinanceFuturesClient:
    """币安 USDⓈ-M 合约统一 API 客户端（含线程安全时间偏移缓存、签名鉴权与批量行情）。"""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = BINANCE_FUTURES_BASE_URL,
        timeout: int = 8,
        time_sync_ttl: float = TIME_SYNC_TTL_SEC,
    ):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.time_sync_ttl = float(time_sync_ttl)
        self._time_lock = threading.Lock()
        self._time_offset_ms: Optional[int] = None
        self._time_synced_at: float = 0.0

    @classmethod
    def from_env(cls, env_path: Path = DEFAULT_ENV_PATH) -> "BinanceFuturesClient":
        """优先从环境变量，其次从 .env 文件初始化客户端。"""
        k = os.environ.get("BINANCE_API_KEY", "")
        s = os.environ.get("BINANCE_API_SECRET", "")
        if not k or not s:
            dotenv = load_dotenv(env_path)
            if not k:
                k = dotenv.get("BINANCE_API_KEY", "")
            if not s:
                s = dotenv.get("BINANCE_API_SECRET", "")
        return cls(api_key=k, api_secret=s)

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

    def get_server_time(self, force_sync: bool = False) -> int:
        """
        获取币安服务器毫秒级时间戳（线程安全实例级 10 分钟偏移缓存）。
        拉取或解析失败时抛出 BinanceAPIError。
        """
        now_mono = time.monotonic()
        with self._time_lock:
            if (
                not force_sync
                and self._time_offset_ms is not None
                and (now_mono - self._time_synced_at) < self.time_sync_ttl
            ):
                return int(time.time() * 1000) + self._time_offset_ms

            url = f"{self.base_url}{SERVER_TIME_ENDPOINT}"
            t0 = int(time.time() * 1000)
            data = fetch_json_with_retry(url, timeout=5, max_retries=2)
            t1 = int(time.time() * 1000)
            if not isinstance(data, dict) or "serverTime" not in data:
                raise BinanceAPIError(
                    f"Missing serverTime in {SERVER_TIME_ENDPOINT} response: {data}",
                    code="INVALID_RESPONSE",
                    endpoint=SERVER_TIME_ENDPOINT,
                )
            try:
                server_ms = int(data["serverTime"])
            except (TypeError, ValueError) as e:
                raise BinanceAPIError(
                    f"Invalid serverTime in {SERVER_TIME_ENDPOINT}: {data['serverTime']}",
                    code="INVALID_RESPONSE",
                    endpoint=SERVER_TIME_ENDPOINT,
                ) from e
            self._time_offset_ms = server_ms - (t0 + t1) // 2
            self._time_synced_at = time.monotonic()
            return server_ms

    def _send_signed_request(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        _retry_time_sync: bool = True,
        max_retries: int = 3,
        backoff: float = 0.8,
    ) -> Tuple[Optional[Any], Optional[dict]]:
        """
        发送已签名的 GET 请求至币安合约 API。
        支持 429/5xx/URLError/TimeoutError 指数退避重试，以及 -1021 时间偏移重校准重试一次。
        """
        if not self.is_configured():
            logger.warning("币安 API 凭据未配置，跳过网络请求。")
            return None, {"code": "NOT_CONFIGURED", "msg": "API keys not configured"}

        for attempt in range(max_retries):
            try:
                server_ts = self.get_server_time()
            except BinanceAPIError as e:
                return None, {"code": e.code or "TIME_SYNC_ERROR", "msg": str(e)}

            p = dict(params or {})
            p["timestamp"] = server_ts
            p["recvWindow"] = 5000

            query_str = urllib.parse.urlencode(p)
            signature = self._generate_signature(query_str)
            url = f"{self.base_url}{endpoint}?{query_str}&signature={signature}"

            req = urllib.request.Request(
                url,
                headers={
                    "X-MBX-APIKEY": self.api_key,
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="GET",
            )

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("code") is not None and int(data["code"]) < 0:
                    code = int(data["code"])
                    if code == -1021 and _retry_time_sync:
                        logger.info("检测到币安 -1021 时间偏移，重新同步服务器时间后重试 [%s]", endpoint)
                        self.get_server_time(force_sync=True)
                        return self._send_signed_request(
                            endpoint,
                            params,
                            _retry_time_sync=False,
                            max_retries=max_retries,
                            backoff=backoff,
                        )
                    logger.error("币安接口返回错误 [%s]: [%s] %s", endpoint, code, data.get("msg"))
                    return None, {"code": code, "msg": data.get("msg", "")}
                return data, None
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
                if _retry_time_sync and _extract_binance_error_code(err_body) == -1021:
                    logger.info("检测到币安 HTTP -1021 时间偏移，重新同步服务器时间后重试 [%s]", endpoint)
                    try:
                        self.get_server_time(force_sync=True)
                    except BinanceAPIError as ts_err:
                        return None, {"code": ts_err.code or "TIME_SYNC_ERROR", "msg": str(ts_err)}
                    return self._send_signed_request(
                        endpoint,
                        params,
                        _retry_time_sync=False,
                        max_retries=max_retries,
                        backoff=backoff,
                    )
                if e.code in RETRYABLE_HTTP_CODES and attempt < max_retries - 1:
                    time.sleep(_retry_delay(attempt, backoff, e))
                    continue
                logger.warning("币安 HTTP 请求失败 [%s] (%s): %s", endpoint, e.code, err_body)
                return None, {"code": e.code, "msg": err_body}
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as e:
                if attempt < max_retries - 1:
                    time.sleep(_retry_delay(attempt, backoff))
                    continue
                logger.warning("请求币安接口网络异常 [%s]: %s", endpoint, e)
                return None, {"code": "NETWORK_ERROR", "msg": str(e)}
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                return None, {"code": "INVALID_JSON", "msg": str(e)}

        return None, {"code": "RETRY_EXHAUSTED", "msg": f"Exhausted retries for {endpoint}"}

    def _require_signed(self, endpoint: str, params: Optional[dict] = None) -> Any:
        data, err = self._send_signed_request(endpoint, params)
        if err is not None:
            raise BinanceAPIError(
                f"{endpoint} failed [{err.get('code')}]: {err.get('msg')}",
                code=err.get("code"),
                endpoint=endpoint,
            )
        return data

    def get_account_positions(self) -> List[dict]:
        """请求 /fapi/v2/positionRisk 获取账户所有合约头寸信息。"""
        data = self._require_signed(POSITION_RISK_ENDPOINT)
        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Expected list from {POSITION_RISK_ENDPOINT}, got {type(data).__name__}",
                code="INVALID_RESPONSE",
                endpoint=POSITION_RISK_ENDPOINT,
            )
        return data

    def get_account_info(self) -> dict:
        """请求 /fapi/v2/account 获取合约账户全量资产、钱包余额与保证金信息。"""
        data = self._require_signed("/fapi/v2/account")
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
        data = self._require_signed("/fapi/v1/income", params)
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
        data = self._require_signed("/fapi/v1/userTrades", params)
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
            if wanted is None or sym in wanted:
                try:
                    prices[sym] = float(item["price"])
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
    ) -> List[list]:
        """请求 /fapi/v1/klines 获取合约 K 线数组。"""
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
        data = fetch_json_with_retry(url, timeout=max(self.timeout, 10), max_retries=3)
        if not isinstance(data, list) or not data:
            raise BinanceAPIError(
                f"No candlestick data returned for {clean_sym} ({interval})",
                code="EMPTY_KLINES",
                endpoint="/fapi/v1/klines",
            )
        return data

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
        clean_sym = clean_symbol(symbol)
        server_time_ms = self.get_server_time()
        klines_data = self.fetch_klines(clean_sym, interval=interval, limit=limit)

        closed_candles = [c for c in klines_data if int(c[6]) <= server_time_ms]
        if closed_candles:
            target_candle = closed_candles[-1]
            prev_candle_raw = closed_candles[-2] if len(closed_candles) >= 2 else None
        elif len(klines_data) >= 2:
            target_candle = klines_data[-2]
            prev_candle_raw = klines_data[-3] if len(klines_data) >= 3 else None
        else:
            raise BinanceAPIError(
                f"No confirmed closed candle for {clean_sym} ({interval})",
                code="UNCLOSED_CANDLE",
                endpoint="/fapi/v1/klines",
            )

        candle = _parse_candle_row(clean_sym, interval, target_candle)
        candle["prev_candle"] = (
            _parse_candle_row(clean_sym, interval, prev_candle_raw)
            if prev_candle_raw is not None
            else None
        )
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
                amt = float(p["positionAmt"])
                if amt == 0.0:
                    continue
                if not include_short and amt < 0:
                    continue
                sym = clean_symbol(p["symbol"])
                entry_p = float(p["entryPrice"])
                mark_p = float(p["markPrice"])
                pnl = float(p["unRealizedProfit"])
                leverage = int(p["leverage"])
                raw_side = str(p.get("positionSide", "BOTH")).upper()
                side = raw_side if raw_side in ("LONG", "SHORT") else ("LONG" if amt > 0 else "SHORT")
                update_time = int(p["updateTime"]) if p.get("updateTime") is not None else 0
                notional = abs(float(p["notional"])) if p.get("notional") is not None else abs(amt * mark_p)
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


_PUBLIC_CLIENT = BinanceFuturesClient()


def fetch_klines(
    symbol: str,
    interval: str = "15m",
    limit: int = 5,
    *,
    start_time_ms: Optional[int] = None,
    end_time_ms: Optional[int] = None,
) -> List[list]:
    """共享公共行情 K 线拉取辅助函数。"""
    return _PUBLIC_CLIENT.fetch_klines(
        symbol,
        interval=interval,
        limit=limit,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
    )


def get_closed_15m_candle(
    symbol: str,
    *,
    interval: str = "15m",
    limit: int = 5,
) -> dict[str, Any]:
    """共享公共最新已收盘 K 线拉取辅助函数。"""
    return _PUBLIC_CLIENT.get_closed_15m_candle(symbol, interval=interval, limit=limit)


def fetch_ticker_prices(symbols: Optional[Sequence[str]] = None) -> Dict[str, float]:
    """共享公共批量最新成交价拉取辅助函数。"""
    return _PUBLIC_CLIENT.get_ticker_prices(symbols=symbols)
