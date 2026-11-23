#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一币安 USDⓈ-M 合约 HTTP 与签名客户端 (Unified Binance Futures Client)
==============================================================================
核心特性:
1. 10 分钟时间偏移缓存 (server_time - local_time)，避免每次请求重复拉取 /fapi/v1/time；
   遇到 -1021 时间戳漂移错误时自动重校准并重试一次。
2. 统一指数退避重试，覆盖临时网络抖动与 HTTP 429/5xx。
3. 提供公共行情接口（批量价格、单币价格、K 线）与签名私有接口（账户、持仓、流水、成交）。
4. 支持多空双向持仓查询，同时保留 get_active_long_positions() 向下兼容。
"""

from __future__ import annotations

import hashlib
import hmac
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
    from config import DEFAULT_ENV_PATH, clean_symbol, load_dotenv
except ImportError:
    from scripts.config import DEFAULT_ENV_PATH, clean_symbol, load_dotenv

logger = logging.getLogger("binance_client")

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
BINANCE_PUBLIC_BASE_URL = "https://www.binance.com"
POSITION_RISK_ENDPOINT = "/fapi/v2/positionRisk"
SERVER_TIME_ENDPOINT = "/fapi/v1/time"
TIME_SYNC_TTL_SEC = 600.0  # 10 分钟重校准一次时间偏移

# 进程级共享时间偏移缓存（公共请求与多客户端实例复用）
_TIME_LOCK = threading.Lock()
_SHARED_TIME_OFFSET_MS: Optional[int] = None
_SHARED_TIME_SYNCED_AT: float = 0.0


def fetch_json_with_retry(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 10,
    max_retries: int = 3,
    backoff: float = 0.8,
) -> Any:
    """带指数退避重试的通用 HTTP GET JSON 请求。"""
    req_headers = {"User-Agent": "TradeAlertDaemon/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            # 仅对 429 或 5xx 重试
            if e.code not in (429, 500, 502, 503, 504) or attempt == max_retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
        except Exception as e:
            last_err = e
            if attempt == max_retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise RuntimeError(f"Request failed: {url}") from last_err


def get_synced_binance_time_ms(
    base_url: str = BINANCE_FUTURES_BASE_URL,
    force_sync: bool = False,
) -> int:
    """
    获取校准后的币安服务器毫秒级时间戳。
    使用进程级偏移缓存（默认每 10 分钟同步一次），大幅降低 /fapi/v1/time 请求频次。
    """
    global _SHARED_TIME_OFFSET_MS, _SHARED_TIME_SYNCED_AT
    now_mono = time.monotonic()
    with _TIME_LOCK:
        if (
            not force_sync
            and _SHARED_TIME_OFFSET_MS is not None
            and (now_mono - _SHARED_TIME_SYNCED_AT) < TIME_SYNC_TTL_SEC
        ):
            return int(time.time() * 1000) + _SHARED_TIME_OFFSET_MS

    url = f"{base_url.rstrip('/')}{SERVER_TIME_ENDPOINT}"
    req = urllib.request.Request(url, headers={"User-Agent": "TradeAlertDaemon/1.0"})
    try:
        t0 = int(time.time() * 1000)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            server_ms = int(data.get("serverTime", t0))
        t1 = int(time.time() * 1000)
        local_mid = (t0 + t1) // 2
        offset = server_ms - local_mid
        with _TIME_LOCK:
            _SHARED_TIME_OFFSET_MS = offset
            _SHARED_TIME_SYNCED_AT = time.monotonic()
        return server_ms
    except Exception as e:
        logger.debug("拉取币安服务器时间异常，使用本地时间: %s", e)
        with _TIME_LOCK:
            if _SHARED_TIME_OFFSET_MS is not None:
                return int(time.time() * 1000) + _SHARED_TIME_OFFSET_MS
        return int(time.time() * 1000)


class BinanceFuturesClient:
    """币安 USDⓈ-M 合约统一 API 客户端（含时间偏移缓存、签名鉴权与批量行情）。"""

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
        获取币安服务器毫秒级时间戳（实例级 10 分钟偏移缓存）。
        遇到 -1021 时可传 force_sync=True 强制重校准。
        """
        now_mono = time.monotonic()
        if (
            not force_sync
            and self._time_offset_ms is not None
            and (now_mono - self._time_synced_at) < self.time_sync_ttl
        ):
            return int(time.time() * 1000) + self._time_offset_ms

        url = f"{self.base_url}{SERVER_TIME_ENDPOINT}"
        req = urllib.request.Request(url, headers={"User-Agent": "TradeAlertDaemon/1.0"})
        try:
            t0 = int(time.time() * 1000)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                server_ms = int(data.get("serverTime", t0))
            t1 = int(time.time() * 1000)
            self._time_offset_ms = server_ms - (t0 + t1) // 2
            self._time_synced_at = time.monotonic()
            return server_ms
        except Exception as e:
            logger.debug("拉取币安服务器时间异常，使用本地时间: %s", e)
            if self._time_offset_ms is not None:
                return int(time.time() * 1000) + self._time_offset_ms
            return int(time.time() * 1000)

    def _send_signed_request(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        _retry_time_sync: bool = True,
    ) -> Tuple[Optional[Any], Optional[dict]]:
        """
        发送已签名的 GET 请求至币安合约 API。
        返回 (data, error_info)。若收到 -1021 时间戳错误，自动重校准时间后重试一次。
        """
        if not self.is_configured():
            logger.warning("币安 API 凭据未配置，跳过网络请求。")
            return None, {"code": "NOT_CONFIGURED", "msg": "API keys not configured"}

        server_ts = self.get_server_time()
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
                "User-Agent": "TradeAlertDaemon/1.0",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("code") and int(data.get("code")) < 0:
                    code = int(data["code"])
                    if code == -1021 and _retry_time_sync:
                        logger.info("检测到币安 -1021 时间偏移，重新同步服务器时间后重试 [%s]", endpoint)
                        self.get_server_time(force_sync=True)
                        return self._send_signed_request(endpoint, params, _retry_time_sync=False)
                    logger.error("币安接口返回错误 [%s]: [%s] %s", endpoint, code, data.get("msg"))
                    return None, {"code": code, "msg": data.get("msg")}
                return data, None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8") if e.fp else ""
            if _retry_time_sync and '"code":-1021' in err_body.replace(" ", ""):
                logger.info("检测到币安 HTTP -1021 时间偏移，重新同步服务器时间后重试 [%s]", endpoint)
                self.get_server_time(force_sync=True)
                return self._send_signed_request(endpoint, params, _retry_time_sync=False)
            logger.warning("币安 HTTP 请求失败 [%s] (%s): %s", endpoint, e.code, err_body)
            return None, {"code": e.code, "msg": err_body}
        except Exception as e:
            logger.warning("请求币安接口网络异常 [%s]: %s", endpoint, e)
            return None, {"code": "NETWORK_ERROR", "msg": str(e)}

    def get_account_positions(self) -> Optional[List[dict]]:
        """请求 /fapi/v2/positionRisk 获取账户所有合约头寸信息。"""
        data, _ = self._send_signed_request(POSITION_RISK_ENDPOINT)
        return data if isinstance(data, list) else None

    def get_account_info(self) -> Optional[dict]:
        """请求 /fapi/v2/account 获取合约账户全量资产、钱包余额与保证金信息。"""
        data, _ = self._send_signed_request("/fapi/v2/account")
        return data if isinstance(data, dict) else None

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
        if income_type:
            params["incomeType"] = income_type
        if symbol:
            params["symbol"] = clean_symbol(symbol)
        if start_time:
            params["startTime"] = int(start_time)
        if end_time:
            params["endTime"] = int(end_time)
        data, _ = self._send_signed_request("/fapi/v1/income", params)
        return data if isinstance(data, list) else []

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
        if start_time:
            params["startTime"] = int(start_time)
        if end_time:
            params["endTime"] = int(end_time)
        if from_id:
            params["fromId"] = int(from_id)
        data, _ = self._send_signed_request("/fapi/v1/userTrades", params)
        return data if isinstance(data, list) else []

    def get_ticker_prices(self, symbols: Optional[Sequence[str]] = None) -> Dict[str, float]:
        """批量获取永续合约最新成交价（一次 HTTP 请求替代逐币轮询）。"""
        url = f"{self.base_url}/fapi/v1/ticker/price"
        try:
            data = fetch_json_with_retry(url, timeout=self.timeout, max_retries=2)
            if not isinstance(data, list):
                return {}
            wanted = {clean_symbol(s) for s in symbols} if symbols else None
            prices: Dict[str, float] = {}
            for item in data:
                sym = clean_symbol(item.get("symbol", ""))
                if wanted is None or sym in wanted:
                    try:
                        prices[sym] = float(item.get("price", 0.0))
                    except (TypeError, ValueError):
                        continue
            return prices
        except Exception as e:
            logger.warning("批量获取币安行情价格失败: %s", e)
            return {}

    def get_active_positions(self, include_short: bool = True) -> Optional[List[dict]]:
        """
        筛选并标准化当前有效开仓的合约持仓。
        - include_short=True: 返回所有非零持仓（LONG 与 SHORT）
        - include_short=False: 仅返回多头持仓 (positionAmt > 0)
        """
        raw_list = self.get_account_positions()
        if raw_list is None:
            return None

        active: List[dict] = []
        for p in raw_list:
            amt = float(p.get("positionAmt", 0.0))
            if amt == 0.0:
                continue
            if not include_short and amt < 0:
                continue
            sym = clean_symbol(p["symbol"])
            entry_p = float(p.get("entryPrice", 0.0))
            mark_p = float(p.get("markPrice", entry_p))
            pnl = float(p.get("unRealizedProfit", 0.0))
            leverage = int(p.get("leverage", 1))
            raw_side = str(p.get("positionSide", "BOTH")).upper()
            side = raw_side if raw_side in ("LONG", "SHORT") else ("LONG" if amt > 0 else "SHORT")
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
                "update_time": int(p.get("updateTime", 0)),
                "notional": abs(float(p.get("notional", amt * mark_p))),
            })
        return active

    def get_active_long_positions(self) -> Optional[List[dict]]:
        """筛选并标准化当前有效开仓的多头持仓 (positionAmt > 0)。"""
        return self.get_active_positions(include_short=False)
