#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
“突破追多与移动保本系统”实盘风控与信号预警守护进程 (Trade Alert & Risk Management Daemon)
==============================================================================
核心风控四重闭环架构:
1. Feature 1 (新共振标的提醒):
   监控候选池 (SQLite candidate_snapshot 或 JSON)，比对 runs/resonance_state.json，
   当新币种进入共振区，触发 Server酱 微信提醒，指引在 TradingView 绘制 15m 下行趋势线。
2. Feature 2 (15m 突破开多提醒):
   调用 tv_trendline_engine 读取 TV 云端画线，
   对已有画线的共振标的，于每个 15m K 线收盘后校验是否实体突破对数阻力线。
3. Feature 3 (持仓破线无条件止损):
   针对 runs/trade_positions.json 中的开仓持仓，
   若 15m 收盘价跌破对应趋势线支撑，立即发出最高级别紧急平多止损通知。
4. Feature 4 (持仓动态移动止盈保护):
   针对开仓持仓，批量实时追踪币安最新成交价。
   当价格涨幅达到 +6% 时自动上移止损至 +1%，回踩触线发出止盈离场通知。
==============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
    BEIJING_TZ,
    DEFAULT_ENV_PATH,
    DEFAULT_POOL_PATH,
    DEFAULT_POSITIONS_PATH,
    DEFAULT_RESONANCE_STATE_PATH,
    DEFAULT_TV_LAYOUT_ID,
    RUNS_DIR,
    atomic_write_json,
    clean_symbol,
    file_lock,
    get_beijing_time_str,
    load_dotenv,
    open_sqlite_connection,
    resolve_database_path,
)
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
            fh = logging.FileHandler(str(target), encoding="utf-8")
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
    dotenv_map = load_dotenv(target_env) if target_env else {}

    def _setting(key: str, default: str = "") -> str:
        val = os.environ.get(key)
        if val is not None and val != "":
            return val
        if key in dotenv_map and dotenv_map[key] != "":
            return dotenv_map[key]
        return default

    return {
        "serverchan_sendkey": _setting("SERVERCHAN_SENDKEY", ""),
        "binance_api_key": _setting("BINANCE_API_KEY", ""),
        "binance_api_secret": _setting("BINANCE_API_SECRET", ""),
        "tradingview_layout_id": _setting("TRADINGVIEW_LAYOUT_ID", DEFAULT_TV_LAYOUT_ID),
        "resonance_pool_file": str(DEFAULT_POOL_PATH),
        "resonance_state_file": str(DEFAULT_RESONANCE_STATE_PATH),
        "positions_file": str(DEFAULT_POSITIONS_PATH),
        "trailing_activation_pct": float(_setting("TRAILING_ACTIVATION_PCT", "6.0")),
        "trailing_lock_pct": float(_setting("TRAILING_LOCK_PCT", "1.0")),
        "database_path": resolve_database_path(env_path=target_env),
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
                    if resp_data.get("code") == 0 or resp_data.get("errno") == 0:
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
    filter_15m: bool = False,
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
            if now - mtime < 180:
                with open(local_cache_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                drawings = cdata.get("drawings_by_symbol") if isinstance(cdata, dict) else None
                if isinstance(drawings, dict) and drawings:
                    _DRAWINGS_CACHE = {"data": drawings, "ts": now, "layout_id": layout_id}
                    raw_drawings = drawings

        if raw_drawings is None:
            drawings = tve.get_layout_drawings(layout_id)
            if not isinstance(drawings, dict):
                raise RuntimeError(f"Unexpected drawings payload for {layout_id}: {type(drawings).__name__}")
            _DRAWINGS_CACHE = {"data": drawings, "ts": now, "layout_id": layout_id}
            raw_drawings = drawings

    if not raw_drawings:
        return {}

    if not filter_15m:
        return raw_drawings

    filtered_map: dict[str, Any] = {}
    for sym, lines in raw_drawings.items():
        line_list = lines if isinstance(lines, list) else [lines]
        valid_lines = [l for l in line_list if is_valid_15m_trendline(l, current_time=now)]
        if valid_lines:
            filtered_map[sym] = valid_lines
    return filtered_map


# ==============================================================================
# 3. 持仓状态与共振状态存储管理器 (共享事务基类)
# ==============================================================================

class JsonStateStore:
    """支持跨进程 fcntl 独占锁与可重入事务的 JSON 状态存储基类（损坏 JSON 显式失败，绝不静默清空）。"""

    def __init__(
        self,
        filepath: Path,
        *,
        timestamp_key: str,
        default_factory: Callable[[], Dict[str, Any]],
    ):
        self.filepath = Path(filepath)
        self._timestamp_key = timestamp_key
        self._default_factory = default_factory
        self.data: Dict[str, Any] = default_factory()
        self._last_mtime_ns: int = 0
        self._lock_depth: int = 0
        self.load(force=True)

    @contextmanager
    def transaction(self):
        """跨进程独占读-改-写事务上下文：加锁后强制从磁盘重载最新状态，支持同实例嵌套重入。"""
        if self._lock_depth > 0:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        with file_lock(self.filepath):
            self._lock_depth += 1
            try:
                self.load(force=True)
                yield
            finally:
                self._lock_depth -= 1

    def _normalize_loaded(self, content: Dict[str, Any]) -> Dict[str, Any]:
        return content

    def load(self, force: bool = False) -> None:
        if not self.filepath.exists():
            if self._last_mtime_ns != 0:
                self.data = self._default_factory()
                self._last_mtime_ns = 0
            return

        mtime_ns = self.filepath.stat().st_mtime_ns
        if force or mtime_ns != self._last_mtime_ns:
            with open(self.filepath, "r", encoding="utf-8") as f:
                content = json.load(f)
            if not isinstance(content, dict):
                raise ValueError(f"State file {self.filepath} must contain a JSON object")
            self.data = self._normalize_loaded(content)
            self._last_mtime_ns = mtime_ns

    def save(self) -> None:
        self.data[self._timestamp_key] = get_beijing_time_str()
        if self._lock_depth > 0:
            atomic_write_json(self.filepath, self.data)
            self._last_mtime_ns = self.filepath.stat().st_mtime_ns
        else:
            with file_lock(self.filepath):
                atomic_write_json(self.filepath, self.data)
                self._last_mtime_ns = self.filepath.stat().st_mtime_ns


class PositionStore(JsonStateStore):
    """管理 runs/trade_positions.json 持仓状态数据"""

    def __init__(self, filepath: Path = DEFAULT_POSITIONS_PATH):
        super().__init__(
            filepath,
            timestamp_key="updated_at",
            default_factory=lambda: {"version": 1, "positions": [], "history": []},
        )

    def _normalize_loaded(self, content: Dict[str, Any]) -> Dict[str, Any]:
        content.setdefault("positions", [])
        content.setdefault("history", [])
        return content

    def get_open_positions(self) -> List[Dict[str, Any]]:
        if self._lock_depth == 0:
            self.load()
        return [p for p in self.data.get("positions", []) if p.get("status") == "OPEN"]

    def add_position(
        self,
        symbol: str,
        entry_price: float,
        amount: float = 0.0,
        trendline: Optional[dict] = None,
        notes: str = "",
        *,
        auto_save: bool = True,
        **kwargs: Any,
    ) -> dict:
        entry_val = float(entry_price)
        if entry_val <= 0:
            raise ValueError(f"entry_price must be positive for {symbol}: {entry_price}")
        if not auto_save and self._lock_depth == 0:
            raise RuntimeError("auto_save=False requires an active transaction()")

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
                    if auto_save:
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
            self.data["positions"].append(pos)
            if auto_save:
                self.save()
            logger.info(f"✅ 成功录入新持仓: {sym} @ {entry_val} (数量: {amount})")
            return pos

    def close_position(
        self,
        symbol: str,
        reason: str = "MANUAL_CLOSE",
        exit_price: Optional[float] = None,
        *,
        auto_save: bool = True,
    ) -> Optional[dict]:
        if not auto_save and self._lock_depth == 0:
            raise RuntimeError("auto_save=False requires an active transaction()")
        with self.transaction():
            sym = _normalize_futures_symbol(symbol)
            target_pos = None
            for p in self.data.get("positions", []):
                if p["symbol"] == sym and p.get("status") == "OPEN":
                    target_pos = p
                    break

            if not target_pos:
                logger.warning(f"未找到 {sym} 的开仓记录")
                return None

            target_pos["status"] = reason
            target_pos["closed_at"] = get_beijing_time_str()
            if exit_price is not None and float(exit_price) > 0:
                target_pos["exit_price"] = float(exit_price)
                entry_val = float(target_pos["entry_price"])
                if entry_val > 0:
                    pnl_pct = (target_pos["exit_price"] - entry_val) / entry_val * 100.0
                    target_pos["realized_pnl_pct"] = round(pnl_pct, 2)

            self.data["positions"].remove(target_pos)
            self.data["history"].append(target_pos)
            if auto_save:
                self.save()
            logger.info(f"✅ 成功平仓/移除持仓: {sym} (原因: {reason})")
            return target_pos


class ResonanceStateStore(JsonStateStore):
    """管理 runs/resonance_state.json 共振状态记忆"""

    def __init__(self, filepath: Path = DEFAULT_RESONANCE_STATE_PATH):
        super().__init__(
            filepath,
            timestamp_key="last_updated",
            default_factory=lambda: {
                "version": 1,
                "initialized": False,
                "last_updated": "",
                "active_resonance_symbols": [],
                "notified_breakout_keys": [],
                "notified_breakout_times": {},
                "history": {},
            },
        )

    def _normalize_loaded(self, content: Dict[str, Any]) -> Dict[str, Any]:
        content.setdefault("active_resonance_symbols", [])
        content.setdefault("notified_breakout_keys", [])
        content.setdefault("notified_breakout_times", {})
        content.setdefault("history", {})
        if "initialized" not in content:
            content["initialized"] = bool(
                content.get("last_updated")
                or content["active_resonance_symbols"]
                or content["history"]
            )
        return content

    def get_active_symbols(self) -> set[str]:
        if self._lock_depth == 0:
            self.load()
        return set(self.data.get("active_resonance_symbols", []))

    def has_breakout_notified(self, key: str) -> bool:
        if self._lock_depth == 0:
            self.load()
        return key in self.data.get("notified_breakout_keys", [])

    def mark_breakout_notified(
        self,
        key: str,
        *,
        timestamp_sec: Optional[int] = None,
        active_line_keys: Optional[set[str]] = None,
        auto_save: bool = True,
    ) -> None:
        if not auto_save and self._lock_depth == 0:
            raise RuntimeError("auto_save=False requires an active transaction()")
        with self.transaction():
            now_sec = int(timestamp_sec if timestamp_sec is not None else time.time())
            keys: list[str] = self.data.setdefault("notified_breakout_keys", [])
            times: dict[str, int] = self.data.setdefault("notified_breakout_times", {})
            if key not in keys:
                keys.append(key)
            times[key] = now_sec
            self._prune_expired_breakout_keys(now_sec=now_sec, active_line_keys=active_line_keys)
            if auto_save:
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
            if k in protected or recorded_ts is None or recorded_ts >= cutoff:
                kept.append(k)
            else:
                times.pop(k, None)
        self.data["notified_breakout_keys"] = kept


# ==============================================================================
# 4. 四大核心业务功能实现 (Feature 1 ~ Feature 4)
# ==============================================================================

def _with_store_tx(store_kwarg: str):
    """确保风控评测函数的完整读-改-写周期在跨进程独占事务锁内执行。"""
    from contextlib import nullcontext

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            store = kwargs.get(store_kwarg) if store_kwarg in kwargs else (args[1] if len(args) > 1 else None)
            ctx = store.transaction() if (store is not None and hasattr(store, "transaction")) else nullcontext()
            with ctx:
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def _get_candidate_pool_stamp(
    pool_file: Path,
    database_path: Optional[Path] = None,
) -> Optional[str]:
    """返回候选池快照的轻量版本标记（SQLite updated_at 或文件 mtime_ns）。"""
    db_path = database_path if database_path is not None else resolve_database_path()
    if db_path is not None:
        db_target = Path(db_path)
        if not db_target.exists():
            return None
        try:
            with closing(open_sqlite_connection(db_target, readonly=True)) as connection:
                row = connection.execute(
                    "SELECT updated_at FROM candidate_snapshot WHERE id = 1"
                ).fetchone()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                return None
            raise
        return f"db:{row[0]}" if row is not None else None

    if not pool_file.exists():
        return None
    return f"file:{pool_file.stat().st_mtime_ns}"


def _load_candidate_pool(
    pool_file: Path,
    database_path: Optional[Path] = None,
) -> Optional[list[dict[str, Any]]]:
    db_path = database_path if database_path is not None else resolve_database_path()
    if db_path is not None:
        db_target = Path(db_path)
        if not db_target.exists():
            logger.warning(f"候选池数据库尚未生成: {db_target}")
            return None
        try:
            with closing(open_sqlite_connection(db_target, readonly=True)) as connection:
                snapshot = connection.execute(
                    "SELECT payload FROM candidate_snapshot WHERE id = 1"
                ).fetchone()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                logger.warning("候选池数据表 candidate_snapshot 尚未初始化")
                return None
            raise
        if snapshot is None:
            logger.warning("候选池尚未生成")
            return None
        payload = json.loads(snapshot[0])
        if not isinstance(payload, list):
            raise ValueError(f"Invalid candidate_snapshot payload type: {type(payload).__name__}")
        return payload

    if not pool_file.exists():
        logger.warning(f"候选池文件不存在: {pool_file}")
        return None
    with open(pool_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, list):
        raise ValueError(f"Invalid candidate pool JSON in {pool_file}: {type(payload).__name__}")
    return payload


@_with_store_tx("state_store")
def check_feature_1_resonance(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    pool_file: Path = DEFAULT_POOL_PATH,
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
    candidates = _load_candidate_pool(pool_file, database_path=database_path)
    if candidates is None:
        return []

    current_resonance_coins: dict[str, dict[str, Any]] = {}
    for c in candidates:
        sym = _normalize_futures_symbol(c["symbol"])
        is_res = bool(
            c.get("is_resonance")
            or c.get("resonance_score", 0) >= 2
            or (c.get("is_ema_bull") and c.get("turnover_ratio", 0) >= 1.5)
        )
        if is_res and sym:
            current_resonance_coins[sym] = c

    active_symbols = state_store.get_active_symbols()
    current_symbols = set(current_resonance_coins.keys())
    now_ts = time.time()
    now_str = get_beijing_time_str(now_ts)

    is_initialized = bool(
        state_store.data.get("initialized")
        or state_store.data.get("last_updated")
        or state_store.data.get("history")
        or active_symbols
    )
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
                history[sym] = {
                    "symbol": sym,
                    "first_detected": detected_at,
                    "last_notified_at": now_str,
                    "last_notified_time": int(now_ts),
                    "res_score": coin.get("resonance_score", 2),
                    "zones": coin.get("zones", []),
                    "tv_link": tv_link,
                }
        state_store.save()
        return []

    new_entered = current_symbols - active_symbols
    next_active = set(active_symbols & current_symbols)
    alerted_list: list[dict[str, Any]] = []

    if new_entered:
        logger.info(f"⚡ 检测到 {len(new_entered)} 个新进入共振区标的: {sorted(new_entered)}")

    history = state_store.data.setdefault("history", {})
    history_updated = False
    for sym in sorted(new_entered):
        coin = current_resonance_coins[sym]
        detected_at = coin.get("resonance_detected_at") or now_str
        res_score = coin.get("resonance_score", 2)
        fr_pct = float(coin["funding_rate_4h_pct"]) if coin.get("funding_rate_4h_pct") is not None else 0.0
        is_neg_fr = bool(coin.get("is_neg_funding") or fr_pct < 0)

        hist = history.get(sym, {})
        last_notified_ts = hist.get("last_notified_time")
        if last_notified_ts is None and "first_detected" in hist:
            dt = datetime.strptime(hist["first_detected"], "%Y-%m-%d %H:%M:%S")
            last_notified_ts = dt.replace(tzinfo=BEIJING_TZ).timestamp()

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

        if is_neg_fr:
            res_tag = "⚠️ 贴水双核 (1h顺势+高换手，但合约深度贴水/空头拥挤)"
            res_badge = "⚠️ 贴水双核"
            guidance_block = f"""> ⚠️ **风控严正警示 (合约深度折价/空头拥挤)**:
> 该标的 4h 资金费率为 `{fr_pct:+.4f}%` (合约价格贴水于现货)。
> 负费率反映衍生品市场做空情绪过度堆积，往往顺应趋势继续阴跌，极少为真逼空！
> 若绘制 15m 趋势线，**必须极度警惕假突破与诱多**，坚决以 15m 阳线实体收盘突破为准，开仓必须以趋势线严设止损！"""
        else:
            res_tag = "👑 双核黄金共振 (1h顺势EMA + 高换手，资金面健康)"
            res_badge = "👑 黄金双核"
            guidance_block = """> 🎯 **操作指引**:
> 该标的资金面健康，已进入第二段量化严选共振区！请立即打开下方 TradingView 专属布局，在 15m 图表连接近期下行高点绘制对数趋势线。
> 系统将持续监控该线，在 15m K 线收盘放量突破时发出买点②开多提醒。"""

        tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym}.P&interval=15"
        price_val = float(coin.get("price") or 0.0)
        turnover_val = float(coin.get("turnover_ratio") or 0.0)
        detected_line = f"\n- **入圈时间**: {detected_at}" if detected_at[:16] != now_str[:16] else ""

        title = f"{res_badge} 提醒: {sym} 进入共振区，请画 15m 趋势线"
        desp = f"""### {res_tag} 选币确认！

- **标的代码**: `BINANCE:{sym}.P`
- **当前市价**: `${price_val:.4f}`
- **24h换手率**: `{turnover_val:.1f}x`
- **4h资金费率**: `{fr_pct:+.4f}%`
- **机会区标签**: {', '.join(coin.get('zones', [])) or '共振区'}
- **形态状态**: {coin.get('status', '多周期趋势共振')}{detected_line}
- **触发时间**: {now_str}

---
{guidance_block}

👉 **[点击立即前往 TradingView 专属画线图表]({tv_link})**
"""
        if notifier.send_alert(title, desp):
            next_active.add(sym)
            alerted_list.append(coin)
            history[sym] = {
                "symbol": sym,
                "first_detected": detected_at,
                "last_notified_at": now_str,
                "last_notified_time": int(now_ts),
                "res_score": res_score,
                "zones": coin.get("zones", []),
                "tv_link": tv_link,
            }
        else:
            logger.warning(f"Server酱发送失败，保留 {sym} 待下轮重试。")

    new_active_sorted = sorted(next_active)
    if (
        new_active_sorted != sorted(active_symbols)
        or alerted_list
        or history_updated
        or not state_store.data.get("initialized")
    ):
        state_store.data["initialized"] = True
        state_store.data["active_resonance_symbols"] = new_active_sorted
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
- **最新收盘价**: `${close_price:.4f}`
- **对数阻力线**: `${res_price:.4f}`
- **突破幅度**: `+{pct:.2f}%`

---
> 🎯 **实战交易指令**:
> 1. **开仓买入**: 建议按纪律执行突破追多开仓。
> 2. **无条件止损**: 以突破的下行趋势线作为铁律生命线，若 15m 收盘价跌回线下方，立即坚决止损！
> 3. **动态止盈保护**: 浮盈达到 +6% 后设立 +1% 锁利提醒线，触及时发送通知。

👉 **[查看 TradingView 15m 实战图表]({tv_link})**
"""
    return notifier.send_alert(title, desp)


@_with_store_tx("state_store")
def check_feature_2_breakout(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    *,
    retry_only: bool = False,
    candle_cache: Optional[Dict[str, dict]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> List[dict]:
    """
    Feature 2 (15m 突破开多提醒):
    直接复用 tv_trendline_engine 校验严格 Crossover、15m 周期与 72h 时效。
    """
    pending = state_store.data.setdefault("pending_breakouts", {})
    pending_dirty = False
    for key, event in list(pending.items()):
        if _send_breakout_notification(
            notifier,
            event["symbol"],
            event["res_price"],
            event["close_price"],
            event["pct"],
            event["candle"],
            event["layout_id"],
        ):
            for k in event["keys"]:
                state_store.mark_breakout_notified(k, auto_save=False)
            del pending[key]
            pending_dirty = True
    if pending_dirty:
        state_store.save()

    if retry_only:
        return []

    try:
        drawings_map = fetch_layout_drawings(layout_id)
    except (BinanceAPIError, RuntimeError, TimeoutError, urllib.error.URLError) as e:
        logger.warning(f"获取 TV 布局画线异常，跳过本轮突破扫描: {e}")
        if status is not None:
            status["had_error"] = True
        return []

    if not drawings_map:
        logger.debug(f"TV 布局 {layout_id} 暂无可用画线数据")
        return []

    norm_drawings: dict[str, list[dict[str, Any]]] = {}
    active_line_keys: set[str] = set()
    for k, lines in drawings_map.items():
        norm_k = clean_symbol(k)
        if not norm_k:
            continue
        line_list = lines if isinstance(lines, list) else [lines]
        norm_drawings.setdefault(norm_k, []).extend(line_list)
        for line in line_list:
            if isinstance(line, dict) and line.get("id"):
                active_line_keys.add(f"{norm_k}_{line['id']}")

    active_symbols = state_store.get_active_symbols()
    breakout_alerts: list[dict[str, Any]] = []

    for sym in sorted(active_symbols):
        sym_clean = clean_symbol(sym)
        matched_lines = norm_drawings.get(sym_clean)
        if not matched_lines:
            continue

        candle = candle_cache.get(sym_clean) if candle_cache is not None else None
        if candle is None:
            try:
                candle = get_closed_15m_candle(sym_clean)
            except BinanceAPIError as e:
                logger.warning(f"拉取 {sym_clean} 15m K线失败: {e}")
                if status is not None:
                    status["had_error"] = True
                continue
            if candle_cache is not None:
                candle_cache[sym_clean] = candle

        c_ts = int(candle["open_time"])
        for line in matched_lines:
            line_id = line.get("id")
            if not line_id:
                continue
            trendline_key = f"{sym}_{line_id}"

            if trendline_key in pending or state_store.has_breakout_notified(trendline_key):
                continue
            if not is_valid_15m_trendline(line, current_time=c_ts):
                continue
            if float(line["p1"]) < float(line["p2"]):
                continue

            res = tve.check_symbol_breakout(sym_clean, line, candle=candle)
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
            keys = [trendline_key, f"{sym}_{c_ts}"]
            pending[trendline_key] = {
                "symbol": sym,
                "res_price": res_price,
                "close_price": close_price,
                "pct": pct,
                "candle": res,
                "layout_id": layout_id,
                "keys": keys,
            }
            state_store.save()

            if _send_breakout_notification(notifier, sym, res_price, close_price, pct, res, layout_id):
                for k in keys:
                    state_store.mark_breakout_notified(
                        k,
                        active_line_keys=active_line_keys,
                        auto_save=False,
                    )
                del pending[trendline_key]
                state_store.save()
                breakout_alerts.append(info)
            break

    return breakout_alerts


@_with_store_tx("pos_store")
def check_feature_3_stop_loss(
    notifier: ServerChanNotifier,
    pos_store: PositionStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    *,
    candle_cache: Optional[Dict[str, dict]] = None,
) -> List[dict]:
    """
    Feature 3 (持仓破线无条件止损):
    在每个 15m K 线收盘后校验收盘价是否跌破趋势线支撑位；新匹配到的趋势线自动持久化。
    """
    open_positions = pos_store.get_open_positions()
    if not open_positions:
        return []

    norm_drawings: dict[str, list[dict[str, Any]]] = {}
    if any(not p.get("trendline") for p in open_positions):
        try:
            drawings_map = fetch_layout_drawings(layout_id)
            for k, lines in drawings_map.items():
                norm_k = clean_symbol(k)
                if norm_k:
                    norm_drawings.setdefault(norm_k, []).extend(lines if isinstance(lines, list) else [lines])
        except (BinanceAPIError, RuntimeError, TimeoutError, urllib.error.URLError) as e:
            logger.warning(f"获取 TV 画线失败，仅校验已缓存趋势线的持仓: {e}")

    stop_loss_alerts: list[dict[str, Any]] = []
    dirty = False

    for pos in open_positions:
        if pos.get("stop_loss_notified", False):
            continue

        sym = pos["symbol"]
        sym_clean = clean_symbol(sym)
        candle = pos.get("pending_stop_loss_candle")
        if candle is None and candle_cache is not None:
            candle = candle_cache.get(sym_clean)
        if candle is None:
            try:
                candle = get_closed_15m_candle(sym_clean)
            except BinanceAPIError as e:
                logger.warning(f"拉取 {sym_clean} 止损校验 15m K线失败: {e}")
                continue
            if candle_cache is not None:
                candle_cache[sym_clean] = candle

        close_time = int(candle["close_time"])
        candle_open_time = int(candle.get("open_time", close_time))
        close_price = float(candle["close"])
        entry_price = float(pos["entry_price"])

        line = pos.get("trendline")
        if not line and norm_drawings:
            matched = norm_drawings.get(sym_clean)
            if matched:
                line = bpc.select_best_position_trendline(matched, current_time=candle_open_time)
                if line:
                    pos["trendline"] = line
                    dirty = True

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
- **开仓价格**: `${entry_price:.4f}`
- **15m 收盘价**: `${close_price:.4f}`
- **趋势线基准**: `${support_price:.4f}`
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
        if not notifier.send_alert(title, desp):
            if "pending_stop_loss_candle" not in pos:
                pos["pending_stop_loss_candle"] = candle
                dirty = True
            continue

        pos.pop("pending_stop_loss_candle", None)
        pos["stop_loss_notified"] = True
        if pos.get("source") != "BINANCE_SYNC":
            pos["status"] = "STOP_LOSS_TRIGGERED"
            pos["closed_at"] = get_beijing_time_str()
            pos["exit_price"] = close_price
            pos_store.data["positions"].remove(pos)
            pos_store.data["history"].append(pos)
        pos_store.save()
        dirty = False
        stop_loss_alerts.append(pos)
        logger.warning(f"🚨 标的 {sym} 跌破趋势线，已发送止损警报！")

    if dirty:
        pos_store.save()

    return stop_loss_alerts


@_with_store_tx("pos_store")
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
    """
    open_positions = pos_store.get_open_positions()
    if not open_positions:
        return []

    resolved_prices: dict[str, float] = dict(price_cache) if price_cache else {}
    missing_syms = [
        clean_symbol(p["symbol"])
        for p in open_positions
        if not resolved_prices.get(p["symbol"]) and not resolved_prices.get(clean_symbol(p["symbol"]))
    ]
    if missing_syms:
        try:
            fetched = fetch_ticker_prices(missing_syms)
            resolved_prices.update(fetched)
        except BinanceAPIError as e:
            logger.warning(f"批量获取移动止盈实时价格失败: {e}")

    events: list[dict[str, Any]] = []
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

        if curr_price > 0:
            prev_highest = float(pos.get("highest_price", entry_price))
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
            highest_price = float(pos.get("highest_price", entry_price))

        target_activation_price = entry_price * (1.0 + activation_pct / 100.0)
        if not pos.get("trailing_stop_triggered", False) and curr_price >= target_activation_price:
            pos["trailing_stop_triggered"] = True
            pos["trailing_stop_price"] = round(entry_price * (1.0 + lock_pct / 100.0), 8)
            pos["trailing_stop_activated_at"] = get_beijing_time_str()
            pos["pending_trailing_activation_price"] = curr_price
            pos_store.save()
            dirty = False

        if "pending_trailing_activation_price" in pos:
            activation_price = float(pos["pending_trailing_activation_price"])
            activation_gain = (activation_price - entry_price) / entry_price * 100.0
            lock_price = float(pos["trailing_stop_price"])
            title = f"🎯 【移动止盈锁定】{sym} 浮盈达 +{activation_gain:.1f}%，已启动 +{lock_pct:.0f}% 保本锁利！"
            desp = f"""### 🎯 【动态移动止盈启动】移动锁利提醒

- **持仓标的**: `{sym}`
- **开仓价格**: `${entry_price:.4f}`
- **激活时行情价**: `${activation_price:.4f}` (最高: `${highest_price:.4f}`)
- **激活时涨幅**: `+{activation_gain:.2f}%` (已突破 +{activation_pct:.0f}% 门槛)
- **移动止损新价**: `${lock_price:.4f}` (+{lock_pct:.1f}% 利润安全线)
- **触发时间**: {get_beijing_time_str()}

---
> 系统已记录锁利预警线 `${lock_price:.4f}`。实时巡检发现价格触线时会发出离场通知。
"""
            if notifier.send_alert(title, desp):
                del pos["pending_trailing_activation_price"]
                pos_store.save()
                dirty = False
                events.append({"type": "TRAILING_ACTIVATED", "pos": pos})
                logger.info(f"🎯 {sym} 已发送移动止损激活通知: {lock_price}")

        if pos.get("trailing_stop_triggered", False) and not pos.get("profit_lock_notified", False):
            lock_price = float(pos["trailing_stop_price"])
            if "pending_profit_lock_price" in pos or 0 < curr_price <= lock_price:
                trigger_price = float(pos.get("pending_profit_lock_price", curr_price))
                trigger_pct = (trigger_price - entry_price) / entry_price * 100.0

                title = f"🔔 【移动止盈离场】{sym} 触及移动止盈线，请平仓锁利离场！"
                desp = f"""### 🔔 【移动止盈触发】建议市价平多落袋为安！

- **持仓标的**: `{sym}`
- **开仓价格**: `${entry_price:.4f}`
- **触发行情价**: `${trigger_price:.4f}`
- **锁利触发线**: `${lock_price:.4f}` (+{lock_pct:.1f}%)
- **触发时浮动收益率**: `{trigger_pct:+.2f}%`
- **触发时间**: {get_beijing_time_str()}

---
> 💰 **操作指引**:
> 行情出现回踩并触及移动保本线，请在币安确认当前仓位与可执行价格。
"""
                if not notifier.send_alert(title, desp):
                    if "pending_profit_lock_price" not in pos:
                        pos["pending_profit_lock_price"] = trigger_price
                        pos_store.save()
                        dirty = False
                    continue

                pos.pop("pending_profit_lock_price", None)
                pos["profit_lock_notified"] = True
                if pos.get("source") != "BINANCE_SYNC":
                    pos["status"] = "PROFIT_LOCKED"
                    pos["closed_at"] = get_beijing_time_str()
                    pos["exit_price"] = trigger_price
                    pos_store.data["positions"].remove(pos)
                    pos_store.data["history"].append(pos)
                pos_store.save()
                dirty = False
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
    pool_file = Path(cfg.get("resonance_pool_file", DEFAULT_POOL_PATH))
    layout_id = cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
    act_pct = float(cfg.get("trailing_activation_pct", 6.0))
    lock_pct = float(cfg.get("trailing_lock_pct", 1.0))
    db_path = cfg.get("database_path")

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

    candle_cache: dict[str, dict] = {}
    f2_status: dict[str, Any] = {}
    res_alerts = check_feature_1_resonance(
        notifier, state_store, pool_file, layout_id, database_path=db_path
    )
    bo_alerts = check_feature_2_breakout(
        notifier, state_store, layout_id, candle_cache=candle_cache, status=f2_status
    )
    sl_alerts = check_feature_3_stop_loss(
        notifier, pos_store, layout_id, candle_cache=candle_cache
    )
    ts_events = check_feature_4_trailing_stops(notifier, pos_store, act_pct, lock_pct)

    result = {
        "timestamp": get_beijing_time_str(),
        "feature_1_resonance_new": len(res_alerts),
        "feature_2_breakout_signals": len(bo_alerts),
        "feature_2_had_error": bool(f2_status.get("had_error")),
        "feature_3_stop_loss_signals": len(sl_alerts),
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
        self.notifier = ServerChanNotifier(cfg.get("serverchan_sendkey", ""), dry_run=dry_run)
        self.state_store = ResonanceStateStore(Path(cfg.get("resonance_state_file", DEFAULT_RESONANCE_STATE_PATH)))
        self.pos_store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
        self.running = True
        self.last_evaluated_15m_bucket = 0
        self._f2_retry_bucket = 0
        self._f2_retry_count = 0
        self._f2_last_retry_ts = 0.0
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

    def run_iteration(self, now: Optional[float] = None) -> None:
        """执行单次守护主循环节拍。"""
        now_ts = now if now is not None else time.time()
        layout_id = self.cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
        pool_file = Path(self.cfg.get("resonance_pool_file", DEFAULT_POOL_PATH))
        db_path = self.cfg.get("database_path")

        price_cache: Dict[str, float] = {}
        if self.binance_client.is_configured():
            try:
                sync_res = bpc.sync_binance_positions(
                    self.binance_client,
                    self.pos_store,
                    layout_id,
                    load_drawings=fetch_layout_drawings,
                )
                if sync_res.get("synced"):
                    for p in sync_res.get("active_positions", []):
                        if p.get("mark_price"):
                            price_cache[p["symbol"]] = float(p["mark_price"])
            except Exception as e:
                logger.warning(f"后台自动同步币安持仓异常: {e}")

        current_bucket = int(now_ts // 900) * 900
        seconds_into_bucket = now_ts - current_bucket
        candle_cache: dict[str, dict] = {}
        pool_stamp = _get_candidate_pool_stamp(pool_file, database_path=db_path)
        pool_updated = pool_stamp is not None and pool_stamp != self._last_pool_stamp
        f1_checked_in_iteration = False

        if pool_updated:
            check_feature_1_resonance(
                self.notifier,
                self.state_store,
                pool_file,
                layout_id,
                database_path=db_path,
            )
            self._last_pool_stamp = pool_stamp
            f1_checked_in_iteration = True

        if seconds_into_bucket >= 10 and self.last_evaluated_15m_bucket != current_bucket:
            is_retry = self._f2_retry_bucket == current_bucket and self._f2_retry_count > 0
            if is_retry and (now_ts - self._f2_last_retry_ts) < 60.0:
                evaluate_stop_loss = any(
                    "pending_stop_loss_candle" in pos for pos in self.pos_store.get_open_positions()
                )
            else:
                if not is_retry:
                    logger.info(f"⏰ 进入 15m K线收盘确认窗口 ({get_beijing_time_str(now_ts)})，启动 Feature 1/2/3 联合校验...")
                    if not f1_checked_in_iteration:
                        check_feature_1_resonance(
                            self.notifier,
                            self.state_store,
                            pool_file,
                            layout_id,
                            database_path=db_path,
                        )
                        if pool_stamp is not None:
                            self._last_pool_stamp = pool_stamp
                    self._f2_retry_bucket = current_bucket
                    self._f2_retry_count = 0
                else:
                    logger.info(f"🔁 重试 15m 突破扫描 (第 {self._f2_retry_count + 1} 次)...")

                f2_status: dict[str, Any] = {}
                check_feature_2_breakout(
                    self.notifier,
                    self.state_store,
                    layout_id,
                    candle_cache=candle_cache,
                    status=f2_status,
                )
                if not f2_status.get("had_error"):
                    self.last_evaluated_15m_bucket = current_bucket
                    self._f2_retry_count = 0
                else:
                    self._f2_retry_count += 1
                    self._f2_last_retry_ts = now_ts
                    if self._f2_retry_count >= 3:
                        logger.warning("Feature 2 本 15m 窗口重试已达上限 (3 次)，等待下个 15m 周期")
                        self.last_evaluated_15m_bucket = current_bucket
                evaluate_stop_loss = not is_retry
        else:
            self.state_store.load()
            if self.state_store.data.get("pending_breakouts"):
                check_feature_2_breakout(
                    self.notifier,
                    self.state_store,
                    layout_id,
                    retry_only=True,
                )
            evaluate_stop_loss = any(
                "pending_stop_loss_candle" in pos for pos in self.pos_store.get_open_positions()
            )

        if evaluate_stop_loss:
            check_feature_3_stop_loss(
                self.notifier,
                self.pos_store,
                layout_id,
                candle_cache=candle_cache,
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
            self._last_pool_stamp = _get_candidate_pool_stamp(
                Path(self.cfg.get("resonance_pool_file", DEFAULT_POOL_PATH)),
                database_path=self.cfg.get("database_path"),
            )
            startup_bucket = int(startup_ts // 900) * 900
            if (startup_ts - startup_bucket) >= 10 and not cycle_res.get("feature_2_had_error"):
                self.last_evaluated_15m_bucket = startup_bucket
        except Exception as e:
            logger.error(f"初始初检异常: {e}")

        while self.running:
            try:
                self.run_iteration()
                time.sleep(10)
            except Exception as e:
                logger.error(f"守护主循环异常: {e}", exc_info=True)
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
        entry = f"{p.get('entry_price', 0):.4f}"
        curr = f"{p.get('current_price', p.get('entry_price', 0)):.4f}"
        highest = f"{p.get('highest_price', p.get('entry_price', 0)):.4f}"
        curr_high = f"{curr} / {highest}"
        pnl = f"{p.get('pnl_pct', 0):+.2f}%"
        ts_price = f"{p.get('trailing_stop_price', 0):.4f}" if p.get("trailing_stop_price") else "--"
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
        parser.add_argument("--check-once", "--evaluate-once", action="store_true", dest="check_once", help="执行单次全量评测并输出汇总 (适用于 cron / hourly_sync)")
        parser.add_argument("--daemon", action="store_true", help="启动持续后台守护进程 (10s 移动止盈巡检 + 15m 整点收盘突破/止损)")
        parser.add_argument("--add-pos", nargs=2, metavar=("SYMBOL", "ENTRY_PRICE"), help="手动录入新持仓 (例如: --add-pos YBUSDT 0.54)")
        parser.add_argument("--amount", type=float, default=0.0, help="持仓数量 (与 --add-pos 配合使用)")
        parser.add_argument("--list-pos", action="store_true", help="打印当前未平仓持仓清单与动态止盈状态")
        parser.add_argument("--close-pos", metavar="SYMBOL", help="平仓并归档指定持仓 (例如: --close-pos YBUSDT)")
        parser.add_argument("--dry-run", action="store_true", help="强制以模拟/日志模式运行，不发送真实 Server酱 微信通知")

        args = parser.parse_args()
        cfg = load_config()

        if args.sync_binance:
            b_client = BinanceFuturesClient(
                cfg.get("binance_api_key", ""),
                cfg.get("binance_api_secret", ""),
            )
            if not b_client.is_configured():
                print("\n⚠️ 币安 API Key / Secret 未配置，系统运行于手动/离线模式。")
                print("如需启用币安实盘自动同步，请在项目根目录 .env 或环境变量 (BINANCE_API_KEY, BINANCE_API_SECRET) 中配置凭据。\n")
                return

            store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
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
            store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
            store.add_position(sym, entry_price, amount=args.amount)
            print(f"✅ 持仓 {clean_symbol(sym)} 录入成功！")
            return

        if args.close_pos:
            sym = args.close_pos
            store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
            res = store.close_position(sym, reason="MANUAL_CLOSE")
            if res:
                print(f"✅ 持仓 {clean_symbol(sym)} 已手动平仓并归档！")
            else:
                print(f"⚠️ 未找到 {clean_symbol(sym)} 的开仓记录。")
            return

        if args.list_pos:
            store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
            open_pos = store.get_open_positions()
            if open_pos:
                try:
                    prices = fetch_ticker_prices([clean_symbol(p["symbol"]) for p in open_pos])
                except BinanceAPIError as e:
                    logger.warning(f"批量获取行情失败: {e}")
                    prices = {}
                for p in open_pos:
                    curr_p = prices.get(clean_symbol(p["symbol"]), 0.0)
                    entry_val = float(p.get("entry_price", 0.0))
                    if curr_p > 0 and entry_val > 0:
                        p["current_price"] = curr_p
                        p["pnl_pct"] = round((curr_p - entry_val) / entry_val * 100.0, 2)
            print("\n========================= 当前有效开仓清单 =========================")
            print(format_positions_table(open_pos))
            print("===================================================================\n")
            return

        if args.check_once:
            notifier = ServerChanNotifier(cfg.get("serverchan_sendkey", ""), dry_run=args.dry_run)
            state_store = ResonanceStateStore(Path(cfg.get("resonance_state_file", DEFAULT_RESONANCE_STATE_PATH)))
            pos_store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
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
