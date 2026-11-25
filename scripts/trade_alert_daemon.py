#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
“突破追多与移动保本系统”实盘风控与信号预警守护进程 (Trade Alert & Risk Management Daemon)
==============================================================================
核心风控四重闭环架构:
1. Feature 1 (新共振标的提醒):
   监控 SQLite 候选池快照 (`candidate_snapshot`)，比对 SQLite 共振状态 (`resonance_state`)，
   当新币种进入共振区，触发 Server酱 微信提醒，指引在 TradingView 绘制 15m 下行趋势线。
2. Feature 2 (15m 突破开多提醒):
   调用 tv_trendline_engine 读取 TV 云端画线，
   对已有画线的共振标的，于每个 15m K 线收盘后校验是否实体突破对数阻力线。
3. Feature 3 (持仓破线无条件止损):
   针对 SQLite `open_positions` 中的开仓持仓，
   若 15m 收盘价跌破对应趋势线支撑，立即发出最高级别紧急平多止损通知。
4. Feature 4 (持仓动态移动止盈保护):
   针对开仓持仓，批量实时追踪币安最新成交价。
   当价格涨幅达到 +6% 时自动上移止损至 +1%，回踩触线发出止盈离场通知。
==============================================================================
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_client import (  # noqa: E402
    BinanceAPIError,
    BinanceFuturesClient,
    fetch_ticker_prices,
    get_closed_15m_candle,
)
import binance_position_client as bpc  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_ENV_PATH,
    DEFAULT_TV_LAYOUT_ID,
    RUNS_DIR,
    clean_symbol,
    get_beijing_time_str,
    get_env_setting,
    resolve_database_path,
)
import db_store  # noqa: E402
from http_client import HttpError  # noqa: E402
import tv_trendline_engine as tve  # noqa: E402
from tv_trendline_engine import (  # noqa: E402
    calculate_log_resistance,
    is_valid_15m_trendline,
)

LOG_FILE = RUNS_DIR / "trade_alert_daemon.log"
logger = logging.getLogger("trade_alert_daemon")
_NOTIFIED_KEY_MAX_AGE_SEC = 30 * 86400


def configure_logging(log_file: Optional[Path] = LOG_FILE) -> list[logging.Handler]:
    """配置标准输出与文件日志处理器（显式返回文件处理器便于退出时关闭）。"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    managed_handlers: list[logging.Handler] = []
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
        managed_handlers.append(sh)

    if log_file is not None:
        target = Path(log_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved = str(target.resolve())
        already = any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == resolved
            for h in root.handlers
        )
        if not already:
            fh = RotatingFileHandler(
                str(target),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
            managed_handlers.append(fh)

    return managed_handlers


def close_managed_handlers(handlers: list[logging.Handler]) -> None:
    """刷新并关闭日志处理器，避免文件描述符泄漏。"""
    root = logging.getLogger()
    for h in handlers:
        try:
            h.flush()
            h.close()
        finally:
            if h in root.handlers:
                root.removeHandler(h)


def format_alert_price(val: float) -> str:
    """Format prices dynamically (2-8 decimals) so sub-$0.001 micro-caps never truncate to $0.0000."""
    num = float(val)
    abs_v = abs(num)
    if abs_v >= 100:
        dec = 2
    elif abs_v >= 0.01:
        dec = 4
    elif abs_v >= 0.0001:
        dec = 6
    else:
        dec = 8
    return f"{num:.{dec}f}"


def _normalize_futures_symbol(symbol: str) -> str:
    sym = clean_symbol(symbol)
    if sym and not sym.endswith(("USDT", "USDC", "BUSD")):
        sym = f"{sym}USDT"
    return sym


# ==============================================================================
# 0. 配置加载
# ==============================================================================

def load_config(env_path: Optional[Path] = None) -> dict[str, Any]:
    """
    统一从环境变量与 .env 文件加载守护进程配置与密钥。
    优先级：OS 环境变量 > .env 文件 > 内置默认值。
    """
    target_env = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    return {
        "serverchan_sendkey": get_env_setting("SERVERCHAN_SENDKEY", "", env_path=target_env),
        "binance_api_key": get_env_setting("BINANCE_API_KEY", "", env_path=target_env),
        "binance_api_secret": get_env_setting("BINANCE_API_SECRET", "", env_path=target_env),
        "tradingview_layout_id": get_env_setting("TRADINGVIEW_LAYOUT_ID", DEFAULT_TV_LAYOUT_ID, env_path=target_env),
        "trailing_activation_pct": float(get_env_setting("TRAILING_ACTIVATION_PCT", "6.0", env_path=target_env)),
        "trailing_lock_pct": float(get_env_setting("TRAILING_LOCK_PCT", "1.0", env_path=target_env)),
        "database_path": resolve_database_path(env_path=target_env) or DEFAULT_DB_PATH,
    }


# ==============================================================================
# 1. Server酱 (ServerChan) 推送通知组件
# ==============================================================================

class ServerChanNotifier:
    """Server酱推送服务客户端，支持真实推送与 Dry-Run / 日志模式"""

    def __init__(self, sendkey: str = "", dry_run: bool = False):
        self.sendkey = (sendkey or "").strip()
        self.dry_run = dry_run or not bool(self.sendkey)
        if self.dry_run:
            if not self.sendkey:
                logger.info("ℹ️ 未配置 SERVERCHAN_SENDKEY，预警守护进程将运行在 [Dry-Run / 日志模式]。")
            else:
                logger.info("ℹ️ 已显式指定 --dry-run，预警守护进程将运行在 [Dry-Run / 日志模式]。")
        else:
            masked = self.sendkey[:4] + "***" + self.sendkey[-3:] if len(self.sendkey) > 7 else "***"
            logger.info(f"✅ Server酱通知通道已就绪 (SendKey: {masked})")

    def send_alert(self, title: str, desp: str) -> bool:
        """发送通知消息到微信 Server酱"""
        prefix = "[DRY-RUN]" if self.dry_run else "[LIVE]"
        logger.info(f"🔔 {prefix} 发送通知: {title}")

        if self.dry_run:
            print("\n" + "=" * 60)
            print(f"📣 [Server酱模拟推送] {title}")
            print("-" * 60)
            print(desp.strip())
            print("=" * 60 + "\n")
            return True

        url = f"https://sctapi.ftqq.com/{self.sendkey}.send"
        payload = json.dumps({"title": title, "desp": desp}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "TradeAlertDaemon/1.0",
            },
            method="POST",
        )

        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    if resp_data.get("code") == 0:
                        logger.info(f"    ✓ Server酱推送成功: {title}")
                        return True
                    logger.error(f"    ✗ Server酱接口返回错误: {resp_data}")
                    return False
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                logger.warning(f"    ⚠️ Server酱网络请求异常 (尝试 {attempt + 1}/2): {e}")
                if attempt == 0:
                    time.sleep(1.0)
        return False


# ==============================================================================
# 2. 画线缓存接入
# ==============================================================================

_DRAWINGS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0, "layout_id": ""}


def fetch_layout_drawings(
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    ttl_sec: int = 60,
) -> Dict[str, Any]:
    """
    从 tv_trendline_engine 获取 TradingView 布局中的画线数据 (支持内存与近期快照缓存)。
    在线拉取失败时显式抛出异常，避免覆盖有效状态或误判 15m 窗口完成。
    """
    global _DRAWINGS_CACHE
    now = time.time()
    raw_drawings: Optional[dict[str, Any]] = None

    if (
        _DRAWINGS_CACHE["data"] is not None
        and _DRAWINGS_CACHE["layout_id"] == layout_id
        and (now - float(_DRAWINGS_CACHE["ts"])) < ttl_sec
    ):
        raw_drawings = _DRAWINGS_CACHE["data"]
    else:
        local_cache_file = RUNS_DIR / f"tradingview_drawings_{layout_id}.json"
        if local_cache_file.exists():
            mtime = local_cache_file.stat().st_mtime
            if (now - mtime) < ttl_sec:
                with open(local_cache_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                drawings = cdata.get("drawings_by_symbol") if isinstance(cdata, dict) else None
                if isinstance(drawings, dict):
                    _DRAWINGS_CACHE = {"data": drawings, "ts": mtime, "layout_id": layout_id}
                    raw_drawings = drawings

        if raw_drawings is None:
            drawings = tve.get_layout_drawings(layout_id)
            if not isinstance(drawings, dict):
                raise RuntimeError(f"Unexpected drawings payload for {layout_id}: {type(drawings).__name__}")
            _DRAWINGS_CACHE = {"data": drawings, "ts": now, "layout_id": layout_id}
            raw_drawings = drawings

    return raw_drawings


# ==============================================================================
# 3. 持仓状态与共振状态存储管理器 (SQLite WAL + BEGIN IMMEDIATE)
# ==============================================================================

class PositionStore:
    """基于 db_store.py 管理 open_positions 与只追加 position_history。"""

    def __init__(self, database: Optional[Path] = None):
        self.database = Path(database) if database is not None else (resolve_database_path() or DEFAULT_DB_PATH)
        self._tx_conn: Optional[sqlite3.Connection] = None
        self._open_positions: list[dict[str, Any]] = []
        self.load()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """跨进程原子 BEGIN IMMEDIATE 读写事务，支持同实例嵌套重入。"""
        if self._tx_conn is not None:
            yield self._tx_conn
            return
        with db_store.transaction(self.database) as conn:
            self._tx_conn = conn
            try:
                self.load()
                yield conn
            finally:
                self._tx_conn = None

    def load(self) -> None:
        self._open_positions = db_store.load_open_positions(self.database, conn=self._tx_conn)

    def save(self) -> None:
        db_store.save_open_positions(self.database, self._open_positions, conn=self._tx_conn)

    def get_open_positions(self) -> List[Dict[str, Any]]:
        if self._tx_conn is None:
            self.load()
        return [p for p in self._open_positions if p.get("status", "OPEN") == "OPEN"]

    def get_history(
        self,
        *,
        symbol: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return db_store.load_position_history(
            self.database,
            symbol=symbol,
            limit=limit,
            conn=self._tx_conn,
        )

    def add_position(
        self,
        symbol: str,
        entry_price: float,
        amount: float = 0.0,
        trendline: Optional[dict] = None,
        notes: str = "",
        **kwargs: Any,
    ) -> dict:
        entry_val = float(entry_price)
        if entry_val <= 0:
            raise ValueError(f"entry_price must be positive for {symbol}: {entry_price}")

        with self.transaction():
            sym = _normalize_futures_symbol(symbol)
            for p in self.get_open_positions():
                if p["symbol"] == sym:
                    logger.info(f"仓位 {sym} 已存在，正在更新开仓价 {entry_val}...")
                    p["entry_price"] = entry_val
                    if amount > 0:
                        p["amount"] = float(amount)
                    if trendline:
                        p["trendline"] = trendline
                    if notes:
                        p["notes"] = notes
                    p.update(kwargs)
                    p["updated_at"] = get_beijing_time_str()
                    self.save()
                    return p

            pos = {
                "symbol": sym,
                "entry_price": entry_val,
                "amount": float(amount),
                "entry_time": get_beijing_time_str(),
                "highest_price": entry_val,
                "current_price": entry_val,
                "trailing_stop_triggered": False,
                "trailing_stop_price": None,
                "stop_loss_notified": False,
                "profit_lock_notified": False,
                "trendline": trendline,
                "status": "OPEN",
                "notes": notes or "Manual / Signal Entry",
            }
            pos.update(kwargs)
            self._open_positions.append(pos)
            self.save()
            logger.info(f"✅ 成功录入新持仓: {sym} @ {entry_val} (数量: {amount})")
            return pos

    def close_position(
        self,
        symbol: str,
        reason: str = "MANUAL_CLOSE",
        exit_price: Optional[float] = None,
    ) -> Optional[dict]:
        with self.transaction():
            sym = _normalize_futures_symbol(symbol)
            target_pos = None
            for p in self._open_positions:
                if p["symbol"] == sym and p.get("status", "OPEN") == "OPEN":
                    target_pos = p
                    break

            if not target_pos:
                logger.warning(f"未找到 {sym} 的开仓记录")
                return None

            target_pos["status"] = reason
            target_pos["closed_at"] = db_store.now_beijing_iso()
            if exit_price is not None and float(exit_price) > 0:
                target_pos["exit_price"] = float(exit_price)
                entry_val = float(target_pos["entry_price"])
                if entry_val > 0:
                    pnl_pct = (target_pos["exit_price"] - entry_val) / entry_val * 100.0
                    target_pos["realized_pnl_pct"] = round(pnl_pct, 2)

            self._open_positions.remove(target_pos)
            self.save()
            db_store.append_position_history(self.database, target_pos, conn=self._tx_conn)
            logger.info(f"✅ 成功平仓/移除持仓: {sym} (原因: {reason})")
            return target_pos


class ResonanceStateStore:
    """基于 db_store.py 管理 SQLite resonance_state 共振状态记忆。"""

    def __init__(self, database: Optional[Path] = None):
        self.database = Path(database) if database is not None else (resolve_database_path() or DEFAULT_DB_PATH)
        self._tx_conn: Optional[sqlite3.Connection] = None
        self.data: Dict[str, Any] = db_store.default_resonance_state()
        self.load()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """跨进程原子 BEGIN IMMEDIATE 读写事务，支持同实例嵌套重入。"""
        if self._tx_conn is not None:
            yield self._tx_conn
            return
        with db_store.transaction(self.database) as conn:
            self._tx_conn = conn
            try:
                self.load()
                yield conn
            finally:
                self._tx_conn = None

    def load(self) -> None:
        self.data = db_store.load_resonance_state(self.database, conn=self._tx_conn)

    def save(self) -> None:
        self.data = db_store.save_resonance_state(self.database, self.data, conn=self._tx_conn)

    def get_active_symbols(self) -> set[str]:
        if self._tx_conn is None:
            self.load()
        return set(self.data.get("active_resonance_symbols", []))

    def has_breakout_notified(self, key: str) -> bool:
        if self._tx_conn is None:
            self.load()
        return key in self.data.get("notified_breakout_keys", [])

    def mark_breakout_notified(
        self,
        key: str,
        *,
        timestamp_sec: Optional[int] = None,
        active_line_keys: Optional[set[str]] = None,
        save: bool = True,
    ) -> None:
        with self.transaction():
            now_sec = int(timestamp_sec if timestamp_sec is not None else time.time())
            keys: list[str] = self.data.setdefault("notified_breakout_keys", [])
            times: dict[str, int] = self.data.setdefault("notified_breakout_times", {})
            if key not in keys:
                keys.append(key)
            times[key] = now_sec
            self._prune_expired_breakout_keys(now_sec=now_sec, active_line_keys=active_line_keys)
            if save:
                self.save()

    def _prune_expired_breakout_keys(
        self,
        *,
        now_sec: Optional[int] = None,
        active_line_keys: Optional[set[str]] = None,
    ) -> None:
        cutoff = int(now_sec if now_sec is not None else time.time()) - _NOTIFIED_KEY_MAX_AGE_SEC
        keys: list[str] = self.data.get("notified_breakout_keys", [])
        times: dict[str, int] = self.data.get("notified_breakout_times", {})
        protected = active_line_keys or set()
        kept: list[str] = []
        for k in keys:
            recorded_ts = times.get(k)
            if k in protected or (recorded_ts is not None and recorded_ts >= cutoff):
                kept.append(k)
            else:
                times.pop(k, None)
        self.data["notified_breakout_keys"] = kept


# ==============================================================================
# 4. 四大核心业务功能实现 (Feature 1 ~ Feature 4)
# ==============================================================================

def _load_candidate_pool(database: Path) -> Optional[list[dict[str, Any]]]:
    snapshot = db_store.load_candidates(database)
    if snapshot is None:
        logger.warning(f"候选池尚未生成: {database}")
        return None
    _, payload_json = snapshot
    payload = json.loads(payload_json)
    if not isinstance(payload, list):
        raise ValueError(f"Invalid candidate_snapshot payload type: {type(payload).__name__}")
    return payload


def _parse_optional_finite_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, bool):
        raise ValueError(f"Unexpected boolean numeric value: {val!r}")
    num = float(val)
    if not math.isfinite(num):
        raise ValueError(f"Non-finite numeric value: {val!r}")
    return num


def check_feature_1_resonance(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    cold_start: bool = True,
    debounce_hours: float = 24.0,
    database_path: Optional[Path] = None,
) -> List[dict]:
    """
    Feature 1 (新共振标的提醒):
    1. 冷启动基准建立: 仅当 state_store 从未初始化过时记录初始基准，市场共振归零后再出现新标的不会重新触发冷启动抑制。
    2. 失败不吞掉: 仅当 Server酱 发送成功或处于 24h 防抖期内才将标的计入 active_resonance_symbols，发信失败时下轮自动重试。
    3. 24h 防抖: 标的掉出后重新进入时重置 first_detected，但 24 小时内不重复打扰。
    """
    db_target = Path(database_path) if database_path is not None else state_store.database
    candidates = _load_candidate_pool(db_target)
    if candidates is None:
        return []

    current_resonance_coins: dict[str, dict[str, Any]] = {}
    for idx, c in enumerate(candidates):
        if not isinstance(c, dict):
            logger.warning(f"跳过第 {idx} 条非字典候选记录: {type(c).__name__}")
            continue
        raw_sym = c.get("symbol")
        sym = _normalize_futures_symbol(raw_sym) if isinstance(raw_sym, str) else ""
        if not sym:
            logger.warning(f"跳过缺失有效 symbol 的候选记录: {c!r}")
            continue
        if not c.get("is_resonance"):
            continue
        try:
            price_val = _parse_optional_finite_float(c.get("price"))
            if price_val is not None and price_val <= 0:
                raise ValueError(f"non-positive price={price_val}")
            _parse_optional_finite_float(c.get("turnover_ratio"))
            _parse_optional_finite_float(c.get("funding_rate_4h_pct"))
            raw_score = c.get("resonance_score")
            if raw_score is not None:
                if isinstance(raw_score, bool):
                    raise ValueError(f"invalid resonance_score={raw_score!r}")
                int(raw_score)
        except (TypeError, ValueError) as e:
            logger.warning(f"跳过 {sym} 畸形共振候选记录: {e}")
            continue
        current_resonance_coins[sym] = c

    now_ts = time.time()
    now_str = get_beijing_time_str(now_ts)
    current_symbols = set(current_resonance_coins.keys())
    pending_notifications: list[tuple[str, dict[str, Any], str, str, str, Optional[int], str]] = []

    with state_store.transaction():
        active_symbols = state_store.get_active_symbols()
        is_initialized = bool(state_store.data.get("initialized"))
        if cold_start and not is_initialized:
            logger.info(
                f"冷启动基准建立: 记录当前 {len(current_symbols)} 个共振标的为初始基准，不发送微信通知。"
            )
            state_store.data["initialized"] = True
            state_store.data["active_resonance_symbols"] = sorted(current_symbols)
            history = state_store.data.setdefault("history", {})
            for sym in current_symbols:
                coin = current_resonance_coins[sym]
                detected_at = coin.get("resonance_detected_at") or now_str
                tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym}.P&interval=15"
                if sym not in history:
                    raw_score = coin.get("resonance_score")
                    history[sym] = {
                        "symbol": sym,
                        "first_detected": detected_at,
                        "last_notified_at": now_str,
                        "last_notified_time": int(now_ts),
                        "res_score": int(raw_score) if raw_score is not None else None,
                        "zones": coin.get("zones", []),
                        "tv_link": tv_link,
                    }
            state_store.save()
            return []

        new_entered = current_symbols - active_symbols
        next_active = set(active_symbols & current_symbols)

        if new_entered:
            logger.info(f"⚡ 检测到 {len(new_entered)} 个新进入共振区标的: {sorted(new_entered)}")

        history = state_store.data.setdefault("history", {})
        history_updated = False
        for sym in sorted(new_entered):
            coin = current_resonance_coins[sym]
            detected_at = coin.get("resonance_detected_at") or now_str
            raw_score = coin.get("resonance_score")
            res_score = int(raw_score) if raw_score is not None else None
            fr_pct = _parse_optional_finite_float(coin.get("funding_rate_4h_pct"))
            is_neg_fr = bool(coin.get("is_neg_funding")) or (fr_pct is not None and fr_pct < 0)

            hist = history.get(sym, {})
            last_notified_ts = hist.get("last_notified_time")
            if debounce_hours > 0 and last_notified_ts is not None:
                age_sec = now_ts - float(last_notified_ts)
                if age_sec < (debounce_hours * 3600.0):
                    logger.info(
                        f"24h防抖拦截: {sym} 在前 24 小时内 ({age_sec / 3600:.1f}h 前) 已通知过，跳过重复打扰。"
                    )
                    history[sym] = {
                        **hist,
                        "symbol": sym,
                        "first_detected": detected_at,
                        "last_notified_at": hist.get("last_notified_at") or hist.get("first_detected") or now_str,
                        "last_notified_time": int(last_notified_ts),
                    }
                    history_updated = True
                    next_active.add(sym)
                    continue

            fr_str = f"{fr_pct:+.4f}%" if fr_pct is not None else "--"
            if is_neg_fr:
                res_tag = "⚠️ 贴水双核 (1h顺势+高换手，但合约深度贴水/空头拥挤)"
                res_badge = "⚠️ 贴水双核"
                guidance_block = f"""> ⚠️ **风控严正警示 (合约深度折价/空头拥挤)**:
> 该标的 4h 资金费率为 `{fr_str}` (合约价格贴水于现货)。
> 负费率反映衍生品市场做空情绪过度堆积，往往顺应趋势继续阴跌，极少为真逼空！
> 若绘制 15m 趋势线，**必须极度警惕假突破与诱多**，坚决以 15m 阳线实体收盘突破为准，开仓必须以趋势线严设止损！"""
            else:
                res_tag = "👑 双核黄金共振 (1h顺势EMA + 高换手，资金面健康)"
                res_badge = "👑 黄金双核"
                guidance_block = """> 🎯 **操作指引**:
> 该标的资金面健康，已进入第二段量化严选共振区！请立即打开下方 TradingView 专属布局，在 15m 图表连接近期下行高点绘制对数趋势线。
> 系统将持续监控该线，在 15m K 线收盘放量突破时发出买点②开多提醒。"""

            tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym}.P&interval=15"
            price_val = _parse_optional_finite_float(coin.get("price"))
            turnover_val = _parse_optional_finite_float(coin.get("turnover_ratio"))
            price_str = f"${format_alert_price(price_val)}" if price_val is not None else "--"
            turnover_str = f"{turnover_val:.1f}x" if turnover_val is not None else "--"
            detected_line = f"\n- **入圈时间**: {detected_at}" if detected_at[:16] != now_str[:16] else ""

            title = f"{res_badge} 提醒: {sym} 进入共振区，请画 15m 趋势线"
            desp = f"""### {res_tag} 选币确认！

- **标的代码**: `BINANCE:{sym}.P`
- **当前市价**: `{price_str}`
- **24h换手率**: `{turnover_str}`
- **4h资金费率**: `{fr_str}`
- **机会区标签**: {', '.join(coin.get('zones', [])) or '共振区'}
- **形态状态**: {coin.get('status', '多周期趋势共振')}{detected_line}
- **触发时间**: {now_str}

---
{guidance_block}

👉 **[点击立即前往 TradingView 专属画线图表]({tv_link})**
"""
            pending_notifications.append((sym, coin, title, desp, detected_at, res_score, tv_link))

        new_active_sorted = sorted(next_active)
        if (
            new_active_sorted != sorted(active_symbols)
            or history_updated
            or not is_initialized
        ):
            state_store.data["initialized"] = True
            state_store.data["active_resonance_symbols"] = new_active_sorted
            state_store.save()

    sent_items: list[tuple[str, dict[str, Any], str, Optional[int], str]] = []
    alerted_list: list[dict[str, Any]] = []
    for sym, coin, title, desp, detected_at, res_score, tv_link in pending_notifications:
        if notifier.send_alert(title, desp):
            sent_items.append((sym, coin, detected_at, res_score, tv_link))
            alerted_list.append(coin)
        else:
            logger.warning(f"Server酱发送失败，保留 {sym} 待下轮重试。")

    if sent_items:
        with state_store.transaction():
            active_now = set(state_store.get_active_symbols())
            history_now = state_store.data.setdefault("history", {})
            for sym, coin, detected_at, res_score, tv_link in sent_items:
                active_now.add(sym)
                history_now[sym] = {
                    "symbol": sym,
                    "first_detected": detected_at,
                    "last_notified_at": now_str,
                    "last_notified_time": int(now_ts),
                    "res_score": res_score,
                    "zones": coin.get("zones", []),
                    "tv_link": tv_link,
                }
            state_store.data["initialized"] = True
            state_store.data["active_resonance_symbols"] = sorted(active_now)
            state_store.save()

    return alerted_list


def _send_breakout_notification(
    notifier: ServerChanNotifier,
    sym: str,
    res_price: float,
    close_price: float,
    pct: float,
    candle_or_res: dict,
    layout_id: str,
) -> bool:
    """发送突破买点②开多通知"""
    tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym}.P&interval=15"
    candle_time_str = candle_or_res.get("candle_time")
    if not candle_time_str:
        ts_val = candle_or_res.get("open_time") or candle_or_res.get("close_time")
        candle_time_str = get_beijing_time_str(float(ts_val)) if ts_val else "--"

    title = f"🚀 突破开多信号: {sym} 突破 15m 对数趋势线 (+{pct:.2f}%)"
    desp = f"""### 🚀 【买点②确认】15m 阳线实体收盘放量突破！

- **交易标的**: `BINANCE:{sym}.P`
- **K线周期**: 15m (已确定收盘)
- **收盘时间**: {candle_time_str}
- **最新收盘价**: `${format_alert_price(close_price)}`
- **对数阻力线**: `${format_alert_price(res_price)}`
- **突破幅度**: `+{pct:.2f}%`

---
> 🎯 **实战交易指令**:
> 1. **开仓买入**: 建议按纪律执行突破追多开仓。
> 2. **无条件止损**: 以突破的下行趋势线作为铁律生命线，若 15m 收盘价跌回线下方，立即坚决止损！
> 3. **动态止盈保护**: 浮盈达到 +6% 后设立 +1% 锁利提醒线，触及时发送通知。

👉 **[查看 TradingView 15m 实战图表]({tv_link})**
"""
    return notifier.send_alert(title, desp)


def _is_candle_for_closed_bucket(candle: dict[str, Any], current_bucket: int) -> bool:
    expected_open = current_bucket - 900
    open_t = int(candle["open_time"])
    close_t = int(candle["close_time"])
    return open_t == expected_open and expected_open <= close_t < current_bucket


def _fetch_15m_candles_batch(
    symbols: Sequence[str],
    *,
    candle_cache: Optional[Dict[str, dict]] = None,
    status: Optional[Dict[str, Any]] = None,
    current_bucket: Optional[int] = None,
    error_label: str = "15m K线",
) -> dict[str, dict[str, Any]]:
    """
    批量并发拉取多个币种的最新已收盘 15m K线，并在主线程安全校验窗口对齐与写入 candle_cache。
    """
    resolved: dict[str, dict[str, Any]] = {}
    to_fetch: list[str] = []
    seen: set[str] = set()

    for sym_clean in symbols:
        if not sym_clean or sym_clean in seen:
            continue
        seen.add(sym_clean)
        cached = candle_cache.get(sym_clean) if candle_cache is not None else None
        if cached is not None:
            if current_bucket is not None and not _is_candle_for_closed_bucket(cached, current_bucket):
                logger.warning(
                    f"{sym_clean} {error_label}尚未对齐当前已收盘窗口 "
                    f"(open={cached.get('open_time')}, close={cached.get('close_time')}, expected_open={current_bucket - 900})"
                )
                if status is not None:
                    status["had_error"] = True
                continue
            resolved[sym_clean] = cached
        else:
            to_fetch.append(sym_clean)

    if not to_fetch:
        return resolved

    def _fetch_one(sym_clean: str) -> tuple[str, Optional[dict[str, Any]], Optional[BinanceAPIError]]:
        try:
            return sym_clean, get_closed_15m_candle(sym_clean), None
        except BinanceAPIError as exc:
            return sym_clean, None, exc

    if len(to_fetch) == 1:
        results = [_fetch_one(to_fetch[0])]
    else:
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(_fetch_one, to_fetch))

    for sym_clean, candle, err in results:
        if err is not None:
            logger.warning(f"拉取 {sym_clean} {error_label}失败: {err}")
            if status is not None:
                status["had_error"] = True
            continue
        assert candle is not None
        if current_bucket is not None and not _is_candle_for_closed_bucket(candle, current_bucket):
            logger.warning(
                f"{sym_clean} {error_label}尚未对齐当前已收盘窗口 "
                f"(open={candle.get('open_time')}, close={candle.get('close_time')}, expected_open={current_bucket - 900})"
            )
            if status is not None:
                status["had_error"] = True
            continue
        resolved[sym_clean] = candle
        if candle_cache is not None:
            candle_cache[sym_clean] = candle

    return resolved


def _flush_retried_breakouts(
    state_store: ResonanceStateStore,
    retried_succeeded: Sequence[tuple[str, list[str]]],
    *,
    active_line_keys: Optional[set[str]] = None,
) -> None:
    if not retried_succeeded:
        return
    with state_store.transaction():
        pending = state_store.data.setdefault("pending_breakouts", {})
        for key, keys_to_mark in retried_succeeded:
            for k in keys_to_mark:
                state_store.mark_breakout_notified(
                    k,
                    active_line_keys=active_line_keys,
                    save=False,
                )
            pending.pop(key, None)
        state_store.save()


def check_feature_2_breakout(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    *,
    retry_only: bool = False,
    candle_cache: Optional[Dict[str, dict]] = None,
    status: Optional[Dict[str, Any]] = None,
    current_bucket: Optional[int] = None,
) -> List[dict]:
    """
    Feature 2 (15m 突破开多提醒):
    直接复用 tv_trendline_engine 校验严格 Crossover、15m 周期与 72h 时效。
    所有网络 I/O（TV 画线、15m K线、Server酱 通知）均在 SQLite 写事务外执行。
    """
    with state_store.transaction():
        pending_snapshot = [
            (key, dict(event))
            for key, event in state_store.data.get("pending_breakouts", {}).items()
        ]
        active_symbols = sorted(state_store.get_active_symbols())
        notified_keys = set(state_store.data.get("notified_breakout_keys", []))

    retried_succeeded: list[tuple[str, list[str]]] = []
    for key, event in pending_snapshot:
        if _send_breakout_notification(
            notifier,
            event["symbol"],
            event["res_price"],
            event["close_price"],
            event["pct"],
            event["candle"],
            event["layout_id"],
        ):
            retried_succeeded.append((key, list(event["keys"])))

    if retry_only:
        _flush_retried_breakouts(state_store, retried_succeeded)
        return []

    try:
        drawings_map = fetch_layout_drawings(layout_id)
    except (BinanceAPIError, RuntimeError, TimeoutError, urllib.error.URLError) as e:
        logger.warning(f"获取 TV 布局画线异常，跳过本轮突破扫描: {e}")
        _flush_retried_breakouts(state_store, retried_succeeded)
        if status is not None:
            status["had_error"] = True
        return []

    if not drawings_map:
        _flush_retried_breakouts(state_store, retried_succeeded)
        logger.debug(f"TV 布局 {layout_id} 暂无可用画线数据")
        return []

    active_line_keys: set[str] = set()
    for sym_k, lines in drawings_map.items():
        for line in lines:
            if isinstance(line, dict) and line.get("id"):
                active_line_keys.add(f"{sym_k}_{line['id']}")

    _flush_retried_breakouts(state_store, retried_succeeded, active_line_keys=active_line_keys)
    pending_keys_set = {key for key, _ in pending_snapshot} - {key for key, _ in retried_succeeded}

    target_symbols = [
        clean_symbol(sym)
        for sym in active_symbols
        if drawings_map.get(clean_symbol(sym))
    ]
    candles_by_symbol = _fetch_15m_candles_batch(
        target_symbols,
        candle_cache=candle_cache,
        status=status,
        current_bucket=current_bucket,
        error_label="15m K线",
    )

    candidates_by_sym: list[list[dict[str, Any]]] = []
    for sym in active_symbols:
        sym_clean = clean_symbol(sym)
        matched_lines = drawings_map.get(sym_clean)
        candle = candles_by_symbol.get(sym_clean)
        if not matched_lines or candle is None:
            continue

        c_ts = int(candle["open_time"])
        sym_candidates: list[dict[str, Any]] = []
        for line in matched_lines:
            if not isinstance(line, dict):
                logger.warning(f"跳过 {sym} 非字典画线数据: {type(line).__name__}")
                continue
            line_id = line.get("id")
            if not line_id:
                continue
            trendline_key = f"{sym}_{line_id}"

            if trendline_key in pending_keys_set or trendline_key in notified_keys:
                continue
            if not is_valid_15m_trendline(line, current_time=c_ts):
                continue
            try:
                p1_val = float(line["p1"])
                p2_val = float(line["p2"])
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"跳过 {sym} 畸形趋势线 ({line_id}): {e}")
                continue
            if p1_val < p2_val:
                continue

            try:
                res = tve.check_symbol_breakout(sym_clean, line, candle=candle)
            except ValueError as e:
                logger.warning(f"跳过 {sym} 无效几何趋势线 ({line_id}): {e}")
                continue
            if not res.get("is_breakout"):
                continue

            res_price = float(res["res_price"])
            close_price = float(res["close_price"])
            pct = float(res["pct"])
            if res_price <= 0 or close_price > res_price * 1.25:
                continue

            info = {
                "symbol": sym,
                "close_price": close_price,
                "res_price": res_price,
                "pct": pct,
                "candle_time": res["candle_time"],
                "trendline": line,
                "trendline_id": line_id,
            }
            keys = [trendline_key]
            pending_record = {
                "symbol": sym,
                "res_price": res_price,
                "close_price": close_price,
                "pct": pct,
                "candle": res,
                "layout_id": layout_id,
                "keys": keys,
            }
            sym_candidates.append({
                "trendline_key": trendline_key,
                "keys": keys,
                "info": info,
                "pending_record": pending_record,
                "res_price": res_price,
                "close_price": close_price,
                "pct": pct,
                "res": res,
            })

        if sym_candidates:
            candidates_by_sym.append(sym_candidates)

    breakout_alerts: list[dict[str, Any]] = []
    for sym_candidates in candidates_by_sym:
        for cand in sym_candidates:
            trendline_key = cand["trendline_key"]
            sym = cand["info"]["symbol"]
            with state_store.transaction():
                pending = state_store.data.setdefault("pending_breakouts", {})
                if trendline_key in pending or state_store.has_breakout_notified(trendline_key):
                    continue
                pending[trendline_key] = cand["pending_record"]
                state_store.save()

            if _send_breakout_notification(
                notifier,
                sym,
                cand["res_price"],
                cand["close_price"],
                cand["pct"],
                cand["res"],
                layout_id,
            ):
                with state_store.transaction():
                    for k in cand["keys"]:
                        state_store.mark_breakout_notified(
                            k,
                            active_line_keys=active_line_keys,
                            save=False,
                        )
                    state_store.data.setdefault("pending_breakouts", {}).pop(trendline_key, None)
                    state_store.save()
                breakout_alerts.append(cand["info"])
                break

    return breakout_alerts


def check_feature_3_stop_loss(
    notifier: ServerChanNotifier,
    pos_store: PositionStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    *,
    retry_only: bool = False,
    candle_cache: Optional[Dict[str, dict]] = None,
    status: Optional[Dict[str, Any]] = None,
    current_bucket: Optional[int] = None,
) -> List[dict]:
    """
    Feature 3 (持仓破线无条件止损):
    在每个 15m K 线收盘后校验收盘价是否跌破趋势线支撑位；新匹配到的趋势线自动持久化。
    所有网络 I/O（TV 画线、15m K线、Server酱 通知）均在 SQLite 写事务外执行。
    """
    open_positions = [dict(p) for p in pos_store.get_open_positions()]
    if not open_positions:
        return []

    eligible = [p for p in open_positions if not p.get("stop_loss_notified", False)]
    if retry_only:
        eligible = [p for p in eligible if p.get("pending_stop_loss_candle") is not None]
    if not eligible:
        return []

    drawings_map: dict[str, Any] = {}
    candles_by_symbol: dict[str, dict[str, Any]] = {}
    if not retry_only:
        if any(p.get("pending_stop_loss_candle") is None and not p.get("trendline") for p in eligible):
            try:
                drawings_map = fetch_layout_drawings(layout_id)
            except (BinanceAPIError, RuntimeError, TimeoutError, urllib.error.URLError) as e:
                logger.warning(f"获取 TV 画线失败，仅校验已缓存趋势线的持仓: {e}")
                if status is not None:
                    status["had_error"] = True

        symbols_to_fetch = [
            clean_symbol(pos["symbol"])
            for pos in eligible
            if pos.get("pending_stop_loss_candle") is None
        ]
        if symbols_to_fetch:
            candles_by_symbol = _fetch_15m_candles_batch(
                symbols_to_fetch,
                candle_cache=candle_cache,
                status=status,
                current_bucket=current_bucket,
                error_label="止损校验 15m K线",
            )

    matched_trendlines: dict[str, dict[str, Any]] = {}
    triggered_alerts: list[tuple[str, dict[str, Any], float, str, str]] = []

    for pos in eligible:
        sym = pos["symbol"]
        sym_clean = clean_symbol(sym)
        candle = pos.get("pending_stop_loss_candle") or candles_by_symbol.get(sym_clean)
        if candle is None:
            continue

        try:
            close_time = int(candle["close_time"])
            candle_open_time = int(candle["open_time"])
            close_price = float(candle["close"])
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"跳过 {sym} 缺失关键时间/收盘价字段的 15m K线: {e}")
            if status is not None:
                status["had_error"] = True
            continue

        entry_price = float(pos["entry_price"])
        line = pos.get("trendline")
        if not line and drawings_map:
            matched = drawings_map.get(sym_clean)
            if matched:
                line = bpc.select_best_position_trendline(matched, current_time=candle_open_time)
                if line:
                    matched_trendlines[sym] = line

        if not line:
            continue

        try:
            p1_price = float(line["p1"])
            p1_time = int(line["t1"])
            p2_price = float(line["p2"])
            p2_time = int(line["t2"])
            support_price = calculate_log_resistance(p1_price, p1_time, p2_price, p2_time, candle_open_time)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"跳过 {sym} 无效止损趋势线: {e}")
            continue

        if support_price <= 0 or close_price >= support_price:
            continue

        if entry_price <= 0:
            logger.error(f"持仓 {sym} entry_price={entry_price} 非法，跳过破线止损计算")
            continue

        drop_pct = (support_price - close_price) / support_price * 100.0
        pnl_pct = (close_price - entry_price) / entry_price * 100.0
        tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym_clean}.P&interval=15"

        title = f"🚨 【破线止损预警】{sym} 15m 跌破趋势线，请立即平多止损！"
        desp = f"""### 🚨 【破线无条件止损】纪律执行警报！

- **持仓标的**: `{sym}`
- **开仓价格**: `${format_alert_price(entry_price)}`
- **15m 收盘价**: `${format_alert_price(close_price)}`
- **趋势线基准**: `${format_alert_price(support_price)}`
- **跌破幅度**: `-{drop_pct:.2f}%`
- **持仓盈亏**: `{pnl_pct:+.2f}%`
- **持仓数量**: `{pos.get('amount', 0)}`
- **收盘时间**: {get_beijing_time_str(close_time)}

---
> ⚠️ **风控铁律提醒**:
> 15m K 线收盘价已有效跌破对数趋势线支撑，说明假突破或行情反转，买点②逻辑失效！
> 请立即执行市价平多止损，杜绝任何抗单侥幸心理！

👉 **[查看 TradingView 图表]({tv_link})**
"""
        triggered_alerts.append((sym, candle, close_price, title, desp))

    if matched_trendlines or triggered_alerts:
        triggered_candles = {sym: candle for sym, candle, _, _, _ in triggered_alerts}
        with pos_store.transaction():
            dirty = False
            for pos in pos_store.get_open_positions():
                sym = pos["symbol"]
                if sym in matched_trendlines and not pos.get("trendline"):
                    pos["trendline"] = matched_trendlines[sym]
                    dirty = True
                if sym in triggered_candles and not pos.get("stop_loss_notified", False):
                    if "pending_stop_loss_candle" not in pos:
                        pos["pending_stop_loss_candle"] = triggered_candles[sym]
                        dirty = True
            if dirty:
                pos_store.save()

    succeeded: list[tuple[str, float]] = []
    for sym, _candle, close_price, title, desp in triggered_alerts:
        if notifier.send_alert(title, desp):
            succeeded.append((sym, close_price))
        else:
            if status is not None:
                status["had_error"] = True

    if not succeeded:
        return []

    stop_loss_alerts: list[dict[str, Any]] = []
    with pos_store.transaction():
        dirty = False
        for sym, close_price in succeeded:
            pos = next(
                (p for p in pos_store.get_open_positions() if p["symbol"] == sym),
                None,
            )
            if pos is None:
                continue
            pos.pop("pending_stop_loss_candle", None)
            pos["stop_loss_notified"] = True
            if pos.get("source") != "BINANCE_SYNC":
                closed_pos = pos_store.close_position(
                    sym,
                    reason="STOP_LOSS_TRIGGERED",
                    exit_price=close_price,
                )
                stop_loss_alerts.append(closed_pos or pos)
            else:
                dirty = True
                stop_loss_alerts.append(pos)
            logger.warning(f"🚨 标的 {sym} 跌破趋势线，已发送止损警报！")
        if dirty:
            pos_store.save()

    return stop_loss_alerts


def check_feature_4_trailing_stops(
    notifier: ServerChanNotifier,
    pos_store: PositionStore,
    activation_pct: float = 6.0,
    lock_pct: float = 1.0,
    price_cache: Optional[Dict[str, float]] = None,
) -> List[dict]:
    """
    Feature 4 (持仓动态移动止盈保护):
    使用批量 ticker 接口获取缺失现价，持久化 highest_price/current_price/pnl_pct 状态变更。
    所有网络 I/O（批量行情拉取、Server酱 通知）均在 SQLite 写事务外执行。
    """
    initial_open = pos_store.get_open_positions()
    if not initial_open:
        return []

    resolved_prices: dict[str, float] = dict(price_cache) if price_cache else {}
    missing_syms = [
        clean_symbol(p["symbol"])
        for p in initial_open
        if not resolved_prices.get(p["symbol"]) and not resolved_prices.get(clean_symbol(p["symbol"]))
    ]
    if missing_syms:
        try:
            fetched = fetch_ticker_prices(missing_syms)
            resolved_prices.update(fetched)
        except BinanceAPIError as e:
            logger.warning(f"批量获取移动止盈实时价格失败: {e}")

    activation_alerts: list[tuple[str, float, str, str]] = []
    profit_lock_alerts: list[tuple[str, float, float, str, str]] = []

    with pos_store.transaction():
        open_positions = pos_store.get_open_positions()
        if not open_positions:
            return []

        dirty = False
        for pos in open_positions:
            sym = pos["symbol"]
            sym_clean = clean_symbol(sym)
            curr_price = float(resolved_prices.get(sym) or resolved_prices.get(sym_clean) or 0.0)
            if curr_price <= 0 and not (
                "pending_trailing_activation_price" in pos or "pending_profit_lock_price" in pos
            ):
                continue

            entry_price = float(pos["entry_price"])
            if entry_price <= 0:
                logger.error(f"持仓 {sym} entry_price={entry_price} 非法，跳过移动止盈计算")
                continue

            raw_highest = pos.get("highest_price")
            prev_highest = float(raw_highest) if raw_highest is not None else entry_price
            if curr_price > 0:
                highest_price = max(prev_highest, curr_price)
                new_pnl_pct = round((curr_price - entry_price) / entry_price * 100.0, 2)
                if (
                    highest_price != pos.get("highest_price")
                    or curr_price != pos.get("current_price")
                    or new_pnl_pct != pos.get("pnl_pct")
                ):
                    pos["highest_price"] = highest_price
                    pos["current_price"] = curr_price
                    pos["pnl_pct"] = new_pnl_pct
                    dirty = True
            else:
                highest_price = prev_highest

            target_activation_price = entry_price * (1.0 + activation_pct / 100.0)
            if not pos.get("trailing_stop_triggered", False) and curr_price >= target_activation_price:
                pos["trailing_stop_triggered"] = True
                pos["trailing_stop_price"] = round(entry_price * (1.0 + lock_pct / 100.0), 8)
                pos["trailing_stop_activated_at"] = get_beijing_time_str()
                pos["pending_trailing_activation_price"] = curr_price
                dirty = True

            if "pending_trailing_activation_price" in pos:
                raw_lock = pos.get("trailing_stop_price")
                if raw_lock is None:
                    logger.error(f"持仓 {sym} 已触发移动止盈但缺失 trailing_stop_price，跳过通知")
                    continue
                activation_price = float(pos["pending_trailing_activation_price"])
                activation_gain = (activation_price - entry_price) / entry_price * 100.0
                lock_price = float(raw_lock)
                title = f"🎯 【移动止盈锁定】{sym} 浮盈达 +{activation_gain:.1f}%，已启动 +{lock_pct:.0f}% 保本锁利！"
                desp = f"""### 🎯 【动态移动止盈启动】移动锁利提醒

- **持仓标的**: `{sym}`
- **开仓价格**: `${format_alert_price(entry_price)}`
- **激活时行情价**: `${format_alert_price(activation_price)}` (最高: `${format_alert_price(highest_price)}`)
- **激活时涨幅**: `+{activation_gain:.2f}%` (已突破 +{activation_pct:.0f}% 门槛)
- **移动止损新价**: `${format_alert_price(lock_price)}` (+{lock_pct:.1f}% 利润安全线)
- **触发时间**: {get_beijing_time_str()}

---
> 系统已记录锁利预警线 `${format_alert_price(lock_price)}`。实时巡检发现价格触线时会发出离场通知。
"""
                activation_alerts.append((sym, lock_price, title, desp))

            if pos.get("trailing_stop_triggered", False) and not pos.get("profit_lock_notified", False):
                raw_lock = pos.get("trailing_stop_price")
                if raw_lock is None:
                    logger.error(f"持仓 {sym} 已触发移动止盈但缺失 trailing_stop_price，跳过锁利离场检查")
                    continue
                lock_price = float(raw_lock)
                if "pending_profit_lock_price" in pos or 0 < curr_price <= lock_price:
                    if "pending_profit_lock_price" not in pos:
                        pos["pending_profit_lock_price"] = curr_price
                        dirty = True
                    trigger_price = float(pos["pending_profit_lock_price"])
                    trigger_pct = (trigger_price - entry_price) / entry_price * 100.0

                    title = f"🔔 【移动止盈离场】{sym} 触及移动止盈线，请平仓锁利离场！"
                    desp = f"""### 🔔 【移动止盈触发】建议市价平多落袋为安！

- **持仓标的**: `{sym}`
- **开仓价格**: `${format_alert_price(entry_price)}`
- **触发行情价**: `${format_alert_price(trigger_price)}`
- **锁利触发线**: `${format_alert_price(lock_price)}` (+{lock_pct:.1f}%)
- **触发时浮动收益率**: `{trigger_pct:+.2f}%`
- **触发时间**: {get_beijing_time_str()}

---
> 💰 **操作指引**:
> 行情出现回踩并触及移动保本线，请在币安确认当前仓位与可执行价格。
"""
                    profit_lock_alerts.append((sym, trigger_price, lock_price, title, desp))

        if dirty:
            pos_store.save()

    activated_syms: list[tuple[str, float]] = []
    for sym, lock_price, title, desp in activation_alerts:
        if notifier.send_alert(title, desp):
            activated_syms.append((sym, lock_price))

    exited_items: list[tuple[str, float, float]] = []
    for sym, trigger_price, lock_price, title, desp in profit_lock_alerts:
        if notifier.send_alert(title, desp):
            exited_items.append((sym, trigger_price, lock_price))

    if not activated_syms and not exited_items:
        return []

    events: list[dict[str, Any]] = []
    with pos_store.transaction():
        dirty = False
        for sym, lock_price in activated_syms:
            pos = next((p for p in pos_store.get_open_positions() if p["symbol"] == sym), None)
            if pos is None:
                continue
            if "pending_trailing_activation_price" in pos:
                del pos["pending_trailing_activation_price"]
                dirty = True
            events.append({"type": "TRAILING_ACTIVATED", "pos": dict(pos)})
            logger.info(f"🎯 {sym} 已发送移动止损激活通知: {lock_price}")

        for sym, trigger_price, lock_price in exited_items:
            pos = next((p for p in pos_store.get_open_positions() if p["symbol"] == sym), None)
            if pos is None:
                continue
            pos.pop("pending_profit_lock_price", None)
            pos["profit_lock_notified"] = True
            if pos.get("source") != "BINANCE_SYNC":
                closed_pos = pos_store.close_position(
                    sym,
                    reason="PROFIT_LOCKED",
                    exit_price=trigger_price,
                )
                events.append({"type": "PROFIT_LOCK_EXIT", "pos": closed_pos or pos})
            else:
                dirty = True
                events.append({"type": "PROFIT_LOCK_EXIT", "pos": pos})
            logger.info(f"🔔 {sym} 触及移动止盈线 {lock_price}，已通知平仓锁利离场！")

        if dirty:
            pos_store.save()

    return events


# ==============================================================================
# 5. 单次循环评测 (用于 CLI --check-once 及每小时 cron 集成)
# ==============================================================================

def run_evaluation_cycle(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    pos_store: PositionStore,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """运行全套风控四重闭环评测"""
    layout_id = cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
    act_pct = float(cfg.get("trailing_activation_pct", 6.0))
    lock_pct = float(cfg.get("trailing_lock_pct", 1.0))
    db_path = Path(cfg["database_path"]) if cfg.get("database_path") else state_store.database

    logger.info("--- 开始执行交易风控与预警循环评测 ---")

    binance_sync_info = None
    b_client = BinanceFuturesClient(
        cfg.get("binance_api_key", ""),
        cfg.get("binance_api_secret", ""),
    )
    if b_client.is_configured():
        binance_sync_info = bpc.sync_binance_positions(
            b_client, pos_store, layout_id, load_drawings=fetch_layout_drawings
        )

    current_bucket = int(time.time() // 900) * 900
    candle_cache: dict[str, dict] = {}
    f2_status: dict[str, Any] = {}
    f3_status: dict[str, Any] = {}
    res_alerts = check_feature_1_resonance(
        notifier, state_store, layout_id=layout_id, database_path=db_path
    )
    bo_alerts = check_feature_2_breakout(
        notifier,
        state_store,
        layout_id,
        candle_cache=candle_cache,
        status=f2_status,
        current_bucket=current_bucket,
    )
    sl_alerts = check_feature_3_stop_loss(
        notifier,
        pos_store,
        layout_id,
        candle_cache=candle_cache,
        status=f3_status,
        current_bucket=current_bucket,
    )
    ts_events = check_feature_4_trailing_stops(notifier, pos_store, act_pct, lock_pct)

    result = {
        "timestamp": get_beijing_time_str(),
        "feature_1_resonance_new": len(res_alerts),
        "feature_2_breakout_signals": len(bo_alerts),
        "feature_2_had_error": bool(f2_status.get("had_error")),
        "feature_3_stop_loss_signals": len(sl_alerts),
        "feature_3_had_error": bool(f3_status.get("had_error")),
        "feature_4_trailing_stop_events": len(ts_events),
        "active_resonance_count": len(state_store.get_active_symbols()),
        "open_positions_count": len(pos_store.get_open_positions()),
        "binance_synced": binance_sync_info.get("synced") if binance_sync_info else False,
        "binance_active_positions": binance_sync_info.get("active_count", 0) if binance_sync_info else 0,
    }
    logger.info(
        f"✓ 评测完成: 新共振={len(res_alerts)}, 突破开多={len(bo_alerts)}, "
        f"破线止损={len(sl_alerts)}, 移动止盈事件={len(ts_events)}, 币安同步={result['binance_synced']}"
    )
    return result


# ==============================================================================
# 6. 持续守护进程模式 (--daemon)
# ==============================================================================

class TradeAlertDaemon:
    """持续后台守护进程"""

    def __init__(self, cfg: dict[str, Any], dry_run: bool = False):
        self.cfg = cfg
        self.database = (
            Path(cfg["database_path"])
            if cfg.get("database_path")
            else (resolve_database_path() or DEFAULT_DB_PATH)
        )
        self.notifier = ServerChanNotifier(cfg.get("serverchan_sendkey", ""), dry_run=dry_run)
        self.state_store = ResonanceStateStore(self.database)
        self.pos_store = PositionStore(self.database)
        self.running = True
        self.last_evaluated_15m_bucket = 0
        self._eval_bucket = 0
        self._feature_retry_state: dict[str, dict[str, Any]] = {
            "f2": {"done": False, "count": 0, "last_ts": 0.0},
            "f3": {"done": False, "count": 0, "last_ts": 0.0},
        }
        self._last_pool_stamp: Optional[str] = None
        self.binance_client = BinanceFuturesClient(
            cfg.get("binance_api_key", ""),
            cfg.get("binance_api_secret", ""),
        )
        if self.binance_client.is_configured():
            logger.info("✅ 币安合约 API 凭证已就绪，实盘持仓自动同步已激活。")
        else:
            logger.info("ℹ️ 币安 API 凭据未配置，采用本地/手动持仓模式。")

    def handle_signal(self, signum, frame):
        logger.info(f"收到终止信号 ({signum})，正在优雅关闭守护进程...")
        self.running = False

    def _run_bucket_feature(
        self,
        key: str,
        label: str,
        now_ts: float,
        fn: Callable[[dict[str, Any]], Any],
    ) -> bool:
        state = self._feature_retry_state[key]
        if state["done"]:
            return False
        if state["count"] > 0 and (now_ts - state["last_ts"]) < 60.0:
            return False
        if state["count"] > 0:
            logger.info(f"🔁 重试 15m {label} (第 {state['count'] + 1} 次)...")
        status: dict[str, Any] = {}
        fn(status)
        if not status.get("had_error"):
            state["done"] = True
            state["count"] = 0
        else:
            state["count"] += 1
            state["last_ts"] = now_ts
            if state["count"] >= 3:
                logger.warning(f"{label} 本 15m 窗口重试已达上限 (3 次)，等待下个 15m 周期")
                state["done"] = True
        return True

    def run_iteration(self, now: Optional[float] = None) -> None:
        """执行单次守护主循环节拍。"""
        now_ts = now if now is not None else time.time()
        layout_id = self.cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)

        price_cache: Dict[str, float] = {}
        positions_synced = False
        if self.binance_client.is_configured():
            try:
                sync_res = bpc.sync_binance_positions(
                    self.binance_client,
                    self.pos_store,
                    layout_id,
                    load_drawings=fetch_layout_drawings,
                )
                if sync_res.get("synced"):
                    positions_synced = True
                    for p in sync_res.get("active_positions", []):
                        if p.get("mark_price"):
                            price_cache[p["symbol"]] = float(p["mark_price"])
            except (BinanceAPIError, HttpError, urllib.error.URLError, TimeoutError, OSError) as e:
                logger.warning(f"后台自动同步币安持仓异常: {e}")

        if not positions_synced:
            self.pos_store.load()
        has_pending_stop_loss = any(
            p.get("status", "OPEN") == "OPEN" and "pending_stop_loss_candle" in p
            for p in self.pos_store._open_positions
        )

        current_bucket = int(now_ts // 900) * 900
        seconds_into_bucket = now_ts - current_bucket
        candle_cache: dict[str, dict] = {}
        pool_stamp = db_store.load_candidate_version(self.database)
        pool_updated = pool_stamp is not None and pool_stamp != self._last_pool_stamp
        f1_checked_in_iteration = False

        if pool_updated:
            check_feature_1_resonance(
                self.notifier,
                self.state_store,
                layout_id=layout_id,
                database_path=self.database,
            )
            self._last_pool_stamp = pool_stamp
            f1_checked_in_iteration = True

        if seconds_into_bucket >= 10 and self.last_evaluated_15m_bucket != current_bucket:
            if self._eval_bucket != current_bucket:
                logger.info(f"⏰ 进入 15m K线收盘确认窗口 ({get_beijing_time_str(now_ts)})，启动 Feature 1/2/3 联合校验...")
                if not f1_checked_in_iteration:
                    check_feature_1_resonance(
                        self.notifier,
                        self.state_store,
                        layout_id=layout_id,
                        database_path=self.database,
                    )
                    if pool_stamp is not None:
                        self._last_pool_stamp = pool_stamp
                self._eval_bucket = current_bucket
                for key in ("f2", "f3"):
                    self._feature_retry_state[key] = {"done": False, "count": 0, "last_ts": 0.0}

            self._run_bucket_feature(
                "f2",
                "Feature 2 突破扫描",
                now_ts,
                lambda st: check_feature_2_breakout(
                    self.notifier,
                    self.state_store,
                    layout_id,
                    candle_cache=candle_cache,
                    status=st,
                    current_bucket=current_bucket,
                ),
            )
            f3_ran = self._run_bucket_feature(
                "f3",
                "Feature 3 止损校验",
                now_ts,
                lambda st: check_feature_3_stop_loss(
                    self.notifier,
                    self.pos_store,
                    layout_id,
                    candle_cache=candle_cache,
                    status=st,
                    current_bucket=current_bucket,
                ),
            )
            if not f3_ran and has_pending_stop_loss:
                check_feature_3_stop_loss(
                    self.notifier,
                    self.pos_store,
                    layout_id,
                    retry_only=True,
                    candle_cache=candle_cache,
                    current_bucket=current_bucket,
                )

            if self._feature_retry_state["f2"]["done"] and self._feature_retry_state["f3"]["done"]:
                self.last_evaluated_15m_bucket = current_bucket
        else:
            self.state_store.load()
            if self.state_store.data.get("pending_breakouts"):
                check_feature_2_breakout(
                    self.notifier,
                    self.state_store,
                    layout_id,
                    retry_only=True,
                )
            if has_pending_stop_loss:
                check_feature_3_stop_loss(
                    self.notifier,
                    self.pos_store,
                    layout_id,
                    retry_only=True,
                    candle_cache=candle_cache,
                    current_bucket=current_bucket,
                )

        check_feature_4_trailing_stops(
            self.notifier,
            self.pos_store,
            float(self.cfg.get("trailing_activation_pct", 6.0)),
            float(self.cfg.get("trailing_lock_pct", 1.0)),
            price_cache=price_cache or None,
        )

    def start(self) -> None:
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        logger.info("=======================================================")
        logger.info("🛡️ “突破追多与移动保本系统”交易风控与信号预警守护进程 (Daemon) 启动")
        logger.info(f"   • TV 布局 ID: {self.cfg.get('tradingview_layout_id')}")
        logger.info(f"   • 移动止盈策略: +{self.cfg.get('trailing_activation_pct')}% 启动，锁定 +{self.cfg.get('trailing_lock_pct')}%")
        logger.info("   • 15m K线判定窗口: mm:00+10s, mm:15+10s, mm:30+10s, mm:45+10s")
        logger.info("   • 动态移动止盈刷新周期: 每 10 秒")
        if self.binance_client.is_configured():
            logger.info("   • 币安实盘自动同步: 已开启 (自动感知新开仓与平仓)")
        else:
            logger.info("   • 币安实盘自动同步: 未配置 (手动持仓模式)")
        logger.info("=======================================================")

        startup_ts = time.time()
        try:
            cycle_res = run_evaluation_cycle(self.notifier, self.state_store, self.pos_store, self.cfg)
            self._last_pool_stamp = db_store.load_candidate_version(self.database)
            startup_bucket = int(startup_ts // 900) * 900
            if (
                (startup_ts - startup_bucket) >= 10
                and not cycle_res.get("feature_2_had_error")
                and not cycle_res.get("feature_3_had_error")
            ):
                self.last_evaluated_15m_bucket = startup_bucket
        except (BinanceAPIError, HttpError, urllib.error.URLError, TimeoutError, OSError) as e:
            logger.error(f"初始初检网络/接口异常: {e}")

        while self.running:
            try:
                self.run_iteration()
                time.sleep(10)
            except (BinanceAPIError, HttpError, urllib.error.URLError, TimeoutError, OSError) as e:
                logger.error(f"守护主循环网络/接口异常: {e}", exc_info=True)
                time.sleep(5)

        logger.info("🛡️ 交易风控与预警守护进程已安全退出。")


# ==============================================================================
# 7. 命令行入口 (CLI Interface)
# ==============================================================================

def format_positions_table(positions: List[dict]) -> str:
    """生成格式化持仓控制台输出表格"""
    if not positions:
        return "  (当前无未平仓持仓记录)"

    header = f"{'Symbol':<12} | {'开仓价':<10} | {'当前价/最高价':<18} | {'盈亏%':<8} | {'移动止损价':<12} | {'状态':<14} | {'开仓时间'}"
    divider = "-" * 95
    lines = [header, divider]
    for p in positions:
        sym = p.get("symbol", "")
        entry_val = float(p.get("entry_price") or 0.0)
        curr_raw = p.get("current_price")
        curr_val = float(curr_raw) if curr_raw is not None else entry_val
        high_raw = p.get("highest_price")
        high_val = float(high_raw) if high_raw is not None else entry_val
        entry = format_alert_price(entry_val)
        curr = format_alert_price(curr_val)
        highest = format_alert_price(high_val)
        curr_high = f"{curr} / {highest}"
        pnl = f"{float(p.get('pnl_pct') or 0.0):+.2f}%"
        ts_raw = p.get("trailing_stop_price")
        ts_price = format_alert_price(float(ts_raw)) if ts_raw else "--"
        status = p.get("status", "OPEN")
        entry_t = p.get("entry_time", "")
        lines.append(f"{sym:<12} | {entry:<10} | {curr_high:<18} | {pnl:<8} | {ts_price:<12} | {status:<14} | {entry_t}")
    return "\n".join(lines)


def main() -> None:
    managed_handlers = configure_logging()
    try:
        parser = argparse.ArgumentParser(
            description="“突破追多与移动保本系统”四重闭环守护体系 (Trade Alert & Risk Management Daemon)"
        )
        parser.add_argument("--sync-binance", action="store_true", help="立即从币安合约拉取实盘活跃持仓并自动同步")
        parser.add_argument("--check-once", action="store_true", help="执行单次全量评测并输出汇总")
        parser.add_argument("--daemon", action="store_true", help="启动持续后台守护进程 (10s 移动止盈巡检 + 15m 整点收盘突破/止损)")
        parser.add_argument("--add-pos", nargs=2, metavar=("SYMBOL", "ENTRY_PRICE"), help="手动录入新持仓 (例如: --add-pos YBUSDT 0.54)")
        parser.add_argument("--amount", type=float, default=0.0, help="持仓数量 (与 --add-pos 配合使用)")
        parser.add_argument("--list-pos", action="store_true", help="打印当前未平仓持仓清单与动态止盈状态")
        parser.add_argument("--close-pos", metavar="SYMBOL", help="平仓并归档指定持仓 (例如: --close-pos YBUSDT)")
        parser.add_argument("--dry-run", action="store_true", help="强制以模拟/日志模式运行，不发送真实 Server酱 微信通知")

        args = parser.parse_args()
        cfg = load_config()
        db_path = Path(cfg["database_path"])

        if args.sync_binance:
            b_client = BinanceFuturesClient(
                cfg.get("binance_api_key", ""),
                cfg.get("binance_api_secret", ""),
            )
            if not b_client.is_configured():
                print("\n⚠️ 币安 API Key / Secret 未配置，系统运行于手动/离线模式。")
                print("如需启用币安实盘自动同步，请在项目根目录 .env 或环境变量 (BINANCE_API_KEY, BINANCE_API_SECRET) 中配置凭据。\n")
                return

            store = PositionStore(db_path)
            res = bpc.sync_binance_positions(
                b_client,
                store,
                cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID),
                load_drawings=fetch_layout_drawings,
            )
            print("\n========================= 币安实盘持仓同步结果 =========================")
            print(f"• 同步状态: {'成功' if res['synced'] else '失败'} ({res['reason']})")
            count_label = "币安多头持仓数" if res["synced"] else "保留的本地持仓数"
            print(f"• {count_label}: {res['active_count']}")
            print(f"• 新增同步标的: {', '.join(res['new_added']) if res['new_added'] else '无'}")
            print(f"• 自动归档平仓: {', '.join(res['auto_closed']) if res['auto_closed'] else '无'}")
            if res.get("active_positions"):
                print("-" * 72)
                print("币安实时活跃多头列表:")
                for p in res["active_positions"]:
                    print(
                        f"  - {p['symbol']}: 均价 {p['entry_price']} | 数量 {p['amount']} | "
                        f"标记价 {p['mark_price']} | 未实现盈亏 {p['unrealized_pnl']} USDT ({p['leverage']}x)"
                    )
            print("========================================================================\n")
            return

        if args.add_pos:
            sym, price_str = args.add_pos
            try:
                entry_price = float(price_str)
            except ValueError:
                logger.error(f"无效的开仓价格: {price_str}")
                sys.exit(1)
            store = PositionStore(db_path)
            store.add_position(sym, entry_price, amount=args.amount)
            print(f"✅ 持仓 {clean_symbol(sym)} 录入成功！")
            return

        if args.close_pos:
            sym = args.close_pos
            store = PositionStore(db_path)
            res = store.close_position(sym, reason="MANUAL_CLOSE")
            if res:
                print(f"✅ 持仓 {clean_symbol(sym)} 已手动平仓并归档！")
            else:
                print(f"⚠️ 未找到 {clean_symbol(sym)} 的开仓记录。")
            return

        if args.list_pos:
            store = PositionStore(db_path)
            open_pos = store.get_open_positions()
            price_fetch_warning = ""
            if open_pos:
                try:
                    prices = fetch_ticker_prices([clean_symbol(p["symbol"]) for p in open_pos])
                except BinanceAPIError as e:
                    logger.warning(f"批量获取行情失败: {e}")
                    price_fetch_warning = f"⚠️ 实时行情拉取失败 ({e})，以下价格与盈亏为本地缓存快照，仅供参考。"
                    prices = {}
                for p in open_pos:
                    curr_p = prices.get(clean_symbol(p["symbol"]), 0.0)
                    entry_val = float(p.get("entry_price", 0.0))
                    if curr_p > 0 and entry_val > 0:
                        p["current_price"] = curr_p
                        p["pnl_pct"] = round((curr_p - entry_val) / entry_val * 100.0, 2)
            print("\n========================= 当前有效开仓清单 =========================")
            if price_fetch_warning:
                print(price_fetch_warning)
            print(format_positions_table(open_pos))
            print("===================================================================\n")
            return

        if args.check_once:
            notifier = ServerChanNotifier(cfg.get("serverchan_sendkey", ""), dry_run=args.dry_run)
            state_store = ResonanceStateStore(db_path)
            pos_store = PositionStore(db_path)
            res = run_evaluation_cycle(notifier, state_store, pos_store, cfg)
            print("\n================== 交易预警与风控单次评测报告 ==================")
            print(json.dumps(res, indent=2, ensure_ascii=False))
            print("================================================================\n")
            return

        if args.daemon:
            daemon = TradeAlertDaemon(cfg, dry_run=args.dry_run)
            daemon.start()
            return

        parser.print_help()
    finally:
        close_managed_handlers(managed_handlers)


if __name__ == "__main__":
    main()
