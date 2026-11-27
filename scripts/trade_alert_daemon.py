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
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import signal
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alert_notifier import ServerChanNotifier, format_alert_price  # noqa: E402
from alert_risk_checks import (  # noqa: E402
    check_feature_1_resonance,
    check_feature_2_breakout,
    check_feature_3_stop_loss,
    check_feature_4_trailing_stops,
)
from binance_client import (  # noqa: E402
    BinanceAPIError,
    BinanceFuturesClient,
    fetch_ticker_prices,
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
    normalize_futures_symbol,
    open_sqlite_connection,
    resolve_database_path,
)
import db_store  # noqa: E402
from http_client import HttpError  # noqa: E402

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


class _SqliteStore:
    """Shared SQLite WAL BEGIN IMMEDIATE transaction manager for daemon stores."""

    def __init__(self, database: Optional[Path] = None):
        self.database = Path(database) if database is not None else (resolve_database_path() or DEFAULT_DB_PATH)
        self._tx_conn: Optional[sqlite3.Connection] = None

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
            except BaseException:
                self._on_tx_rollback()
                raise
            finally:
                self._tx_conn = None

    def _on_tx_rollback(self) -> None:
        pass

    def load(self) -> None:
        raise NotImplementedError

    def save(self) -> None:
        raise NotImplementedError


class PositionStore(_SqliteStore):
    """基于 db_store.py 管理 open_positions 与只追加 position_history。"""

    def __init__(self, database: Optional[Path] = None):
        super().__init__(database)
        self._open_positions: list[dict[str, Any]] = []
        self._loaded: bool = False
        self.load()

    def _on_tx_rollback(self) -> None:
        self._loaded = False

    def load(self, *, conn: Optional[sqlite3.Connection] = None) -> None:
        read_conn = self._tx_conn if self._tx_conn is not None else conn
        self._open_positions = db_store.load_open_positions(self.database, conn=read_conn)
        self._loaded = True

    def save(self) -> None:
        db_store.save_open_positions(self.database, self._open_positions, conn=self._tx_conn)
        self._loaded = True

    def cached_open_positions(self) -> List[Dict[str, Any]]:
        """Return currently cached open positions without issuing a DB read."""
        return [p for p in self._open_positions if p["status"] == "OPEN"]

    def get_open_positions(self, *, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        if self._tx_conn is None:
            self.load(conn=conn)
        return self.cached_open_positions()

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
        *,
        save: bool = True,
        **kwargs: Any,
    ) -> dict:
        entry_val = float(entry_price)
        if not math.isfinite(entry_val) or entry_val <= 0:
            raise ValueError(f"entry_price must be positive for {symbol}: {entry_price}")

        with self.transaction():
            sym = normalize_futures_symbol(symbol)
            for p in self.get_open_positions():
                if p["symbol"] == sym:
                    old_entry = float(p["entry_price"])
                    entry_changed = not math.isclose(old_entry, entry_val, rel_tol=1e-9, abs_tol=1e-12)
                    logger.info(f"仓位 {sym} 已存在，正在更新开仓价 {entry_val}...")
                    p["entry_price"] = entry_val
                    if entry_changed:
                        p["highest_price"] = entry_val
                        p["trailing_stop_triggered"] = False
                        p["trailing_stop_price"] = None
                        p["profit_lock_notified"] = False
                        p["stop_loss_notified"] = False
                        for key in [k for k in p if k.startswith("pending_")] + ["trailing_stop_activated_at"]:
                            p.pop(key, None)
                    if amount > 0:
                        p["amount"] = float(amount)
                    if trendline:
                        p["trendline"] = trendline
                    if notes:
                        p["notes"] = notes
                    p.update(kwargs)
                    p["updated_at"] = get_beijing_time_str()
                    if save:
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
            if save:
                self.save()
            logger.info(f"✅ 成功录入新持仓: {sym} @ {entry_val} (数量: {amount})")
            return pos

    def close_position(
        self,
        symbol: str,
        reason: str = "MANUAL_CLOSE",
        exit_price: Optional[float] = None,
        *,
        save: bool = True,
    ) -> Optional[dict]:
        with self.transaction():
            sym = normalize_futures_symbol(symbol)
            target_pos = None
            for p in self._open_positions:
                if p["symbol"] == sym and p["status"] == "OPEN":
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
            if save:
                self.save()
            db_store.append_position_history(self.database, target_pos, conn=self._tx_conn)
            logger.info(f"✅ 成功平仓/移除持仓: {sym} (原因: {reason})")
            return target_pos


class ResonanceStateStore(_SqliteStore):
    """基于 db_store.py 管理 SQLite resonance_state 共振状态记忆。"""

    def __init__(self, database: Optional[Path] = None):
        super().__init__(database)
        self.data: Dict[str, Any] = db_store.default_resonance_state()
        self._state_loaded: bool = False
        self._last_raw_payload: Optional[str] = None
        self.load()

    def _on_tx_rollback(self) -> None:
        self._state_loaded = False
        self._last_raw_payload = None

    @staticmethod
    def _read_raw_payload(conn: sqlite3.Connection) -> Optional[str]:
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'resonance_state'"
        ).fetchone()
        if table_row is None:
            return None
        row = conn.execute("SELECT payload FROM resonance_state WHERE id = 1").fetchone()
        return str(row[0]) if row is not None else None

    def _apply_raw_payload(self, raw_payload: Optional[str]) -> None:
        if raw_payload is None:
            self.data = db_store.default_resonance_state()
            self._last_raw_payload = None
            self._state_loaded = True
            return
        self.data = db_store.parse_resonance_state(raw_payload)
        self._last_raw_payload = raw_payload
        self._state_loaded = True

    def load(
        self,
        *,
        only_if_changed: bool = False,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        read_conn = self._tx_conn if self._tx_conn is not None else conn
        if read_conn is not None:
            raw_payload = self._read_raw_payload(read_conn)
            if only_if_changed and self._state_loaded and raw_payload == self._last_raw_payload:
                return
            self._apply_raw_payload(raw_payload)
            return
        if not self.database.exists():
            if only_if_changed and self._state_loaded and self._last_raw_payload is None:
                return
            self._apply_raw_payload(None)
            return
        with closing(open_sqlite_connection(self.database, readonly=True)) as ro_conn:
            raw_payload = self._read_raw_payload(ro_conn)
        if only_if_changed and self._state_loaded and raw_payload == self._last_raw_payload:
            return
        self._apply_raw_payload(raw_payload)

    def save(self) -> None:
        self.data = db_store.save_resonance_state(self.database, self.data, conn=self._tx_conn)
        self._last_raw_payload = json.dumps(self.data, ensure_ascii=False, allow_nan=False)
        self._state_loaded = True

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


def run_evaluation_cycle(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    pos_store: PositionStore,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """运行全套风控四重闭环评测。"""
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
        binance_sync_info = bpc.sync_binance_positions(b_client, pos_store, layout_id)

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


class TradeAlertDaemon:
    """持续后台守护进程。"""

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
        self._ro_loop_active: bool = False
        self._shared_ro_conn: Optional[sqlite3.Connection] = None
        self._ro_inode: Optional[tuple[int, int]] = None
        self._ro_data_version: Optional[int] = None
        self.binance_client = BinanceFuturesClient(
            cfg.get("binance_api_key", ""),
            cfg.get("binance_api_secret", ""),
        )
        if self.binance_client.is_configured():
            logger.info("✅ 币安合约 API 凭证已就绪，实盘持仓自动同步已激活。")
        else:
            logger.info("ℹ️ 币安 API 凭据未配置，采用本地/手动持仓模式。")

    def _close_shared_ro_conn(self) -> None:
        if self._shared_ro_conn is not None:
            try:
                self._shared_ro_conn.close()
            finally:
                self._shared_ro_conn = None
                self._ro_inode = None
                self._ro_data_version = None

    def _ensure_shared_ro_conn(self) -> tuple[Optional[sqlite3.Connection], bool]:
        """Return `(ro_conn, db_unchanged)` for long-lived daemon polling using `PRAGMA data_version`."""
        if not self.database.exists():
            self._close_shared_ro_conn()
            return None, False
        st = self.database.stat()
        cur_inode = (st.st_dev, st.st_ino)
        if self._shared_ro_conn is not None and self._ro_inode != cur_inode:
            self._close_shared_ro_conn()
        if self._shared_ro_conn is None:
            conn = open_sqlite_connection(self.database, readonly=True)
            conn.isolation_level = None
            self._shared_ro_conn = conn
            self._ro_inode = cur_inode
            self._ro_data_version = None
            return conn, False
        row = self._shared_ro_conn.execute("PRAGMA data_version").fetchone()
        cur_ver = int(row[0]) if row is not None else None
        unchanged = (
            cur_ver is not None
            and cur_ver == self._ro_data_version
            and self.pos_store._loaded
            and self.state_store._state_loaded
        )
        return self._shared_ro_conn, unchanged

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

        if self.binance_client.is_configured():
            try:
                bpc.sync_binance_positions(self.binance_client, self.pos_store, layout_id)
            except (BinanceAPIError, HttpError, TimeoutError, OSError) as e:
                logger.warning(f"后台自动同步币安持仓异常: {e}")

        db_unchanged = False
        ephemeral_ro_conn: Optional[sqlite3.Connection] = None
        if self._ro_loop_active:
            ro_conn, db_unchanged = self._ensure_shared_ro_conn()
        elif self.database.exists():
            ephemeral_ro_conn = open_sqlite_connection(self.database, readonly=True)
            ro_conn = ephemeral_ro_conn
        else:
            ro_conn = None

        try:
            open_positions = (
                self.pos_store.cached_open_positions()
                if db_unchanged
                else self.pos_store.get_open_positions(conn=ro_conn)
            )
            has_pending_stop_loss = any(
                "pending_stop_loss_candle" in p
                for p in open_positions
            )

            current_bucket = int(now_ts // 900) * 900
            seconds_into_bucket = now_ts - current_bucket
            candle_cache: dict[str, dict] = {}
            pool_stamp = (
                self._last_pool_stamp
                if db_unchanged
                else db_store.load_candidate_version(self.database, conn=ro_conn)
            )
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

            f3_mutated = False
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
                f3_mutated = f3_ran
                if not f3_ran and has_pending_stop_loss:
                    check_feature_3_stop_loss(
                        self.notifier,
                        self.pos_store,
                        layout_id,
                        retry_only=True,
                        candle_cache=candle_cache,
                        current_bucket=current_bucket,
                    )
                    f3_mutated = True

                if self._feature_retry_state["f2"]["done"] and self._feature_retry_state["f3"]["done"]:
                    self.last_evaluated_15m_bucket = current_bucket
            else:
                if not db_unchanged:
                    self.state_store.load(only_if_changed=True, conn=ro_conn)
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
                    f3_mutated = True

            if ro_conn is self._shared_ro_conn and ro_conn is not None and not db_unchanged:
                ver_row = ro_conn.execute("PRAGMA data_version").fetchone()
                self._ro_data_version = int(ver_row[0]) if ver_row is not None else None

            has_open_positions = (
                bool(self.pos_store.get_open_positions()) if f3_mutated else bool(open_positions)
            )
            if has_open_positions:
                check_feature_4_trailing_stops(
                    self.notifier,
                    self.pos_store,
                    float(self.cfg.get("trailing_activation_pct", 6.0)),
                    float(self.cfg.get("trailing_lock_pct", 1.0)),
                )
        finally:
            if ephemeral_ro_conn is not None:
                ephemeral_ro_conn.close()

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
        except (BinanceAPIError, HttpError, TimeoutError, OSError) as e:
            logger.error(f"初始初检网络/接口异常: {e}")

        self._ro_loop_active = True
        try:
            while self.running:
                try:
                    self.run_iteration()
                    time.sleep(10)
                except (BinanceAPIError, HttpError, TimeoutError, OSError) as e:
                    logger.error(f"守护主循环网络/接口异常: {e}", exc_info=True)
                    time.sleep(5)
        finally:
            self._ro_loop_active = False
            self._close_shared_ro_conn()

        logger.info("🛡️ 交易风控与预警守护进程已安全退出。")


def format_positions_table(positions: List[dict]) -> str:
    """生成格式化持仓控制台输出表格。"""
    if not positions:
        return "  (当前无未平仓持仓记录)"

    header = f"{'Symbol':<12} | {'开仓价':<10} | {'当前价/最高价':<18} | {'盈亏%':<8} | {'移动止损价':<12} | {'状态':<14} | {'开仓时间'}"
    divider = "-" * 95
    lines = [header, divider]
    for p in positions:
        sym = p["symbol"]
        entry_val = float(p["entry_price"])
        curr_raw = p.get("current_price")
        curr_val = float(curr_raw) if curr_raw is not None else entry_val
        high_raw = p.get("highest_price")
        high_val = float(high_raw) if high_raw is not None else entry_val
        entry = format_alert_price(entry_val)
        curr = format_alert_price(curr_val)
        highest = format_alert_price(high_val)
        curr_high = f"{curr} / {highest}"
        raw_pnl = p.get("pnl_pct")
        pnl = f"{float(raw_pnl):+.2f}%" if raw_pnl is not None else "--"
        ts_raw = p.get("trailing_stop_price")
        ts_price = format_alert_price(float(ts_raw)) if ts_raw is not None else "--"
        status = p["status"]
        entry_t = p.get("entry_time") or "--"
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
                    curr_p = prices.get(clean_symbol(p["symbol"]))
                    entry_val = float(p["entry_price"])
                    if curr_p is not None and curr_p > 0 and entry_val > 0:
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
