#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified SQLite Persistence Layer (WAL + BEGIN IMMEDIATE)
========================================================
Centralizes schema DDL and transactional read/write primitives for:
1. Account analytics snapshots, income ledger, trade fills, equity observations, and recording state.
2. Candidate pool snapshots and 7-day sub-30m symbol history.
3. Daemon open positions, closed position history, and resonance state.

All JSON payloads forbid NaN/Infinity (`allow_nan=False`), and all timestamps written by
this module use explicit ISO-8601 offsets (`+08:00` Beijing time or UTC `Z`).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config import BEIJING_TZ, clean_symbol, open_sqlite_connection  # noqa: E402

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS account_snapshot (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS income_ledger (
        income_type TEXT NOT NULL,
        transaction_id TEXT NOT NULL,
        timestamp INTEGER NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (income_type, transaction_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_income_ledger_ts
        ON income_ledger (timestamp)
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_fills (
        symbol TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        timestamp INTEGER NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (symbol, trade_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_fills_ts
        ON trade_fills (timestamp)
    """,
    """
    CREATE TABLE IF NOT EXISTS equity_observations (
        timestamp INTEGER PRIMARY KEY,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_recording (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_snapshot (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sub30m_history (
        symbol TEXT PRIMARY KEY,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS open_positions (
        symbol TEXT PRIMARY KEY,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        closed_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_position_history_symbol_closed
        ON position_history (symbol, closed_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS resonance_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        payload TEXT NOT NULL
    )
    """,
)


def now_beijing_iso(ts_sec: Optional[float] = None) -> str:
    """Format a timestamp (or current time) as explicit ISO-8601 Beijing time (+08:00)."""
    dt = (
        datetime.fromtimestamp(ts_sec, tz=BEIJING_TZ)
        if ts_sec is not None
        else datetime.now(BEIJING_TZ)
    )
    return dt.isoformat(timespec="seconds")


def normalize_beijing_iso(raw: Optional[str] = None) -> str:
    """
    Normalize a timestamp string to explicit ISO-8601 (+08:00 or UTC Z).
    Naive ISO/datetime strings are interpreted as Beijing time (+08:00).
    Raises ValueError on empty or malformed strings.
    """
    if raw is None:
        return now_beijing_iso()
    text = raw.strip()
    if not text:
        raise ValueError("Timestamp string cannot be empty")
    if text.endswith("Z"):
        datetime.fromisoformat(text[:-1] + "+00:00")
        return text
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING_TZ).isoformat(timespec="seconds")
    return dt.isoformat(timespec="seconds")


def _dumps_json(data: object) -> str:
    """Serialize JSON strictly with UTF-8 preservation and allow_nan=False."""
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


def _loads_dict(raw_json: str, *, context: str) -> dict[str, object]:
    """Parse a JSON object string, raising ValueError if the decoded value is not a dict."""
    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object in {context}, got {type(parsed).__name__}")
    return parsed


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all managed SQLite tables and indexes if they do not exist."""
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)


@contextmanager
def transaction(database: Path, *, ensure: bool = True) -> Iterator[sqlite3.Connection]:
    """
    Open a WAL SQLite connection and execute an atomic BEGIN IMMEDIATE write transaction.
    Guarantees multi-process concurrency safety without read-to-write lock upgrade deadlocks.
    """
    with closing(open_sqlite_connection(database, readonly=False)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if ensure:
                ensure_schema(conn)
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")


@contextmanager
def _read_conn(
    database: Path,
    conn: Optional[sqlite3.Connection],
) -> Iterator[Optional[sqlite3.Connection]]:
    """Yield caller connection or open a temporary readonly connection if database exists."""
    if conn is not None:
        yield conn
        return
    target = Path(database)
    if not target.exists():
        yield None
        return
    with closing(open_sqlite_connection(target, readonly=True)) as ro_conn:
        yield ro_conn


@contextmanager
def _write_conn(
    database: Path,
    conn: Optional[sqlite3.Connection],
) -> Iterator[sqlite3.Connection]:
    """Yield caller connection or open a BEGIN IMMEDIATE transaction."""
    if conn is not None:
        ensure_schema(conn)
        yield conn
        return
    with transaction(database) as tx_conn:
        yield tx_conn


# ------------------------------------------------------------------------------
# a) Account analytics persistence
# ------------------------------------------------------------------------------


def publish_report(
    database: Path,
    report: Mapping[str, object],
    income: Sequence[Mapping[str, object]] = (),
    fills: Sequence[Mapping[str, object]] = (),
) -> None:
    """Persist the latest complete account report, ledger events, fills, and equity observations."""
    payload = _dumps_json(report)
    with transaction(database) as conn:
        recording = report.get("recording")
        if recording is not None:
            if not isinstance(recording, Mapping):
                raise ValueError(f"report['recording'] must be a mapping, got {type(recording).__name__}")
            conn.execute(
                """
                INSERT INTO account_recording (id, payload) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
                """,
                (_dumps_json(recording),),
            )
            equity_curve = report["equity_curve"]
            if not isinstance(equity_curve, Sequence) or isinstance(equity_curve, (str, bytes)):
                raise ValueError(f"report['equity_curve'] must be a sequence, got {type(equity_curve).__name__}")
            desired_points: dict[int, str] = {}
            for point in equity_curve:
                if not isinstance(point, Mapping):
                    raise ValueError(f"equity_curve entry must be a mapping, got {type(point).__name__}")
                desired_points[int(point["timestamp"])] = _dumps_json(point)

            existing_points: dict[int, str] = dict(
                conn.execute("SELECT timestamp, payload FROM equity_observations").fetchall()
            )
            stale_timestamps = [(ts,) for ts in existing_points if ts not in desired_points]
            if stale_timestamps:
                conn.executemany(
                    "DELETE FROM equity_observations WHERE timestamp = ?",
                    stale_timestamps,
                )
            changed_points = [
                (ts, pt_json)
                for ts, pt_json in desired_points.items()
                if existing_points.get(ts) != pt_json
            ]
            if changed_points:
                conn.executemany(
                    "INSERT INTO equity_observations (timestamp, payload) VALUES (?, ?) "
                    "ON CONFLICT(timestamp) DO UPDATE SET payload = excluded.payload",
                    changed_points,
                )

        if income:
            conn.executemany(
                "INSERT OR IGNORE INTO income_ledger (income_type, transaction_id, timestamp, payload) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        str(item["incomeType"]),
                        str(item["tranId"]),
                        int(item["time"]),
                        _dumps_json(item),
                    )
                    for item in income
                ],
            )

        if fills:
            conn.executemany(
                "INSERT OR IGNORE INTO trade_fills (symbol, trade_id, timestamp, payload) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        str(item["symbol"]),
                        str(item["id"]),
                        int(item["time"]),
                        _dumps_json(item),
                    )
                    for item in fills
                ],
            )

        conn.execute(
            """
            INSERT INTO account_snapshot (id, payload) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """,
            (payload,),
        )


def load_raw_report(database: Path) -> Optional[str]:
    """Load the raw JSON payload string for the latest persisted account report if available."""
    with _read_conn(database, None) as conn:
        if conn is None or not _table_exists(conn, "account_snapshot"):
            return None
        row = conn.execute("SELECT payload FROM account_snapshot WHERE id = 1").fetchone()
    if row is None:
        return None
    return str(row[0])


def load_report(database: Path) -> Optional[dict[str, object]]:
    """Load the latest persisted account report from SQLite if available."""
    raw = load_raw_report(database)
    if raw is None:
        return None
    return _loads_dict(raw, context="account_snapshot")


def load_recording(database: Path) -> Optional[dict[str, object]]:
    """Load persisted account recording metadata from `account_recording` if available."""
    with _read_conn(database, None) as conn:
        if conn is None or not _table_exists(conn, "account_recording"):
            return None
        row = conn.execute("SELECT payload FROM account_recording WHERE id = 1").fetchone()
    if row is None:
        return None
    return _loads_dict(row[0], context="account_recording")


def load_ledger_bundle(
    database: Path,
    *,
    start_ms: Optional[int] = None,
) -> dict[str, object]:
    """
    Load persisted account recording metadata, income ledger, trade fills, and equity observations.
    When `start_ms` is provided, filters ledger, fill, and observation rows by `timestamp >= start_ms`.
    """
    bundle: dict[str, object] = {
        "recording": None,
        "income": [],
        "fills": [],
        "observations": [],
    }
    with _read_conn(database, None) as conn:
        if conn is None:
            return bundle
        if _table_exists(conn, "account_recording"):
            rec_row = conn.execute("SELECT payload FROM account_recording WHERE id = 1").fetchone()
            if rec_row is not None:
                bundle["recording"] = _loads_dict(rec_row[0], context="account_recording")

        if _table_exists(conn, "income_ledger"):
            if start_ms is None:
                rows = conn.execute("SELECT payload FROM income_ledger ORDER BY timestamp").fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM income_ledger WHERE timestamp >= ? ORDER BY timestamp",
                    (int(start_ms),),
                ).fetchall()
            bundle["income"] = [_loads_dict(r[0], context="income_ledger") for r in rows]

        if _table_exists(conn, "trade_fills"):
            if start_ms is None:
                rows = conn.execute("SELECT payload FROM trade_fills ORDER BY timestamp").fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM trade_fills WHERE timestamp >= ? ORDER BY timestamp",
                    (int(start_ms),),
                ).fetchall()
            bundle["fills"] = [_loads_dict(r[0], context="trade_fills") for r in rows]

        if _table_exists(conn, "equity_observations"):
            if start_ms is None:
                rows = conn.execute("SELECT payload FROM equity_observations ORDER BY timestamp").fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM equity_observations WHERE timestamp >= ? ORDER BY timestamp",
                    (int(start_ms),),
                ).fetchall()
            bundle["observations"] = [_loads_dict(r[0], context="equity_observations") for r in rows]

    return bundle


# ------------------------------------------------------------------------------
# b) Candidate pool & sub-30m history persistence
# ------------------------------------------------------------------------------


def publish_candidates(
    database: Path,
    candidates: Sequence[Mapping[str, object]],
    updated_at: Optional[str] = None,
) -> None:
    """Persist the candidate pool snapshot with an explicit ISO-8601 timestamp."""
    normalized_updated_at = normalize_beijing_iso(updated_at)
    payload = _dumps_json(list(candidates))
    with transaction(database) as conn:
        conn.execute(
            """
            INSERT INTO candidate_snapshot (id, updated_at, payload)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                payload = excluded.payload
            """,
            (normalized_updated_at, payload),
        )


def load_candidates(database: Path) -> Optional[tuple[str, str]]:
    """Return `(updated_at, payload_json)` for the latest candidate snapshot if available."""
    with _read_conn(database, None) as conn:
        if conn is None or not _table_exists(conn, "candidate_snapshot"):
            return None
        row = conn.execute("SELECT updated_at, payload FROM candidate_snapshot WHERE id = 1").fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def load_candidate_version(database: Path) -> Optional[str]:
    """Return `updated_at` version marker for the latest candidate snapshot if available."""
    with _read_conn(database, None) as conn:
        if conn is None or not _table_exists(conn, "candidate_snapshot"):
            return None
        row = conn.execute("SELECT updated_at FROM candidate_snapshot WHERE id = 1").fetchone()
    return str(row[0]) if row is not None else None


def load_sub30m_history(
    database: Path,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, dict[str, object]]:
    """Load persisted 7-day sub-$30M market cap tracking history keyed by symbol."""
    with _read_conn(database, conn) as r_conn:
        if r_conn is None or not _table_exists(r_conn, "sub30m_history"):
            return {}
        rows = r_conn.execute("SELECT symbol, payload FROM sub30m_history").fetchall()
    return {
        str(sym): _loads_dict(payload_json, context=f"sub30m_history[{sym}]")
        for sym, payload_json in rows
    }


def save_sub30m_history(
    database: Path,
    history: Mapping[str, Mapping[str, object]],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Replace persisted sub-$30M history rows atomically inside a BEGIN IMMEDIATE transaction."""
    desired: dict[str, str] = {}
    for raw_sym, entry in history.items():
        sym = clean_symbol(str(raw_sym))
        if not sym:
            raise ValueError(f"Empty symbol in sub30m_history: {raw_sym!r}")
        if sym in desired:
            raise ValueError(f"Duplicate normalized symbol in sub30m_history: {sym}")
        desired[sym] = _dumps_json(dict(entry))

    with _write_conn(database, conn) as w_conn:
        existing = dict(w_conn.execute("SELECT symbol, payload FROM sub30m_history").fetchall())
        stale = [(sym,) for sym in existing if sym not in desired]
        if stale:
            w_conn.executemany("DELETE FROM sub30m_history WHERE symbol = ?", stale)
        changed = [
            (sym, payload_json)
            for sym, payload_json in desired.items()
            if existing.get(sym) != payload_json
        ]
        if changed:
            w_conn.executemany(
                "INSERT INTO sub30m_history (symbol, payload) VALUES (?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET payload = excluded.payload",
                changed,
            )


# ------------------------------------------------------------------------------
# c) Daemon open positions, position history, and resonance state
# ------------------------------------------------------------------------------


def load_open_positions(
    database: Path,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, object]]:
    """Load currently open daemon positions without touching closed history."""
    with _read_conn(database, conn) as r_conn:
        if r_conn is None or not _table_exists(r_conn, "open_positions"):
            return []
        rows = r_conn.execute("SELECT symbol, payload FROM open_positions ORDER BY rowid").fetchall()
    return [
        _loads_dict(payload_json, context=f"open_positions[{sym}]")
        for sym, payload_json in rows
    ]


def save_open_positions(
    database: Path,
    positions: Sequence[Mapping[str, object]],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """
    Persist the full set of currently open positions cleanly (diffing against `open_positions`),
    never rewriting `position_history`.
    """
    desired: dict[str, str] = {}
    for pos in positions:
        sym = clean_symbol(str(pos["symbol"]))
        if not sym:
            raise ValueError(f"Position missing valid symbol: {pos!r}")
        if sym in desired:
            raise ValueError(f"Duplicate open position symbol: {sym}")
        record = dict(pos)
        record["symbol"] = sym
        desired[sym] = _dumps_json(record)

    with _write_conn(database, conn) as w_conn:
        existing = dict(w_conn.execute("SELECT symbol, payload FROM open_positions").fetchall())
        stale = [(sym,) for sym in existing if sym not in desired]
        if stale:
            w_conn.executemany("DELETE FROM open_positions WHERE symbol = ?", stale)
        changed = [
            (sym, payload_json)
            for sym, payload_json in desired.items()
            if existing.get(sym) != payload_json
        ]
        if changed:
            w_conn.executemany(
                "INSERT INTO open_positions (symbol, payload) VALUES (?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET payload = excluded.payload",
                changed,
            )


def upsert_open_position(
    database: Path,
    position: Mapping[str, object],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Insert or update a single open position by normalized symbol."""
    sym = clean_symbol(str(position["symbol"]))
    if not sym:
        raise ValueError(f"Position missing valid symbol: {position!r}")
    record = dict(position)
    record["symbol"] = sym
    payload = _dumps_json(record)
    with _write_conn(database, conn) as w_conn:
        w_conn.execute(
            """
            INSERT INTO open_positions (symbol, payload) VALUES (?, ?)
            ON CONFLICT(symbol) DO UPDATE SET payload = excluded.payload
            """,
            (sym, payload),
        )


def load_position_history(
    database: Path,
    *,
    symbol: Optional[str] = None,
    limit: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, object]]:
    """Load closed position history ordered chronologically (oldest first; when limited, latest N)."""
    if limit is not None and int(limit) <= 0:
        return []
    with _read_conn(database, conn) as r_conn:
        if r_conn is None or not _table_exists(r_conn, "position_history"):
            return []
        where_clause = ""
        params: list[object] = []
        if symbol is not None:
            sym = clean_symbol(symbol)
            where_clause = " WHERE symbol = ?"
            params.append(sym)
        if limit is not None:
            query = (
                "SELECT payload FROM ("
                f"SELECT id, payload FROM position_history{where_clause} ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC"
            )
            params.append(int(limit))
        else:
            query = f"SELECT payload FROM position_history{where_clause} ORDER BY id ASC"
        rows = r_conn.execute(query, params).fetchall()
    return [_loads_dict(row[0], context="position_history") for row in rows]


def append_position_history(
    database: Path,
    position: Mapping[str, object],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Append a closed position record to `position_history` with explicit ISO-8601 `closed_at`."""
    sym = clean_symbol(str(position["symbol"]))
    if not sym:
        raise ValueError(f"Closed position missing valid symbol: {position!r}")
    raw_closed_at = position.get("closed_at")
    closed_at = normalize_beijing_iso(str(raw_closed_at) if raw_closed_at is not None else None)
    record = dict(position)
    record["symbol"] = sym
    record["closed_at"] = closed_at
    payload = _dumps_json(record)
    with _write_conn(database, conn) as w_conn:
        w_conn.execute(
            "INSERT INTO position_history (symbol, closed_at, payload) VALUES (?, ?, ?)",
            (sym, closed_at, payload),
        )


def default_resonance_state() -> dict[str, object]:
    """Return a fresh default resonance state structure."""
    return {
        "version": 1,
        "initialized": False,
        "last_updated": "",
        "active_resonance_symbols": [],
        "notified_breakout_keys": [],
        "notified_breakout_times": {},
        "history": {},
    }


def load_resonance_state(
    database: Path,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, object]:
    """Load persisted resonance state from SQLite or return defaults if not initialized."""
    with _read_conn(database, conn) as r_conn:
        if r_conn is None or not _table_exists(r_conn, "resonance_state"):
            return default_resonance_state()
        row = r_conn.execute("SELECT payload FROM resonance_state WHERE id = 1").fetchone()
    if row is None:
        return default_resonance_state()

    loaded = _loads_dict(row[0], context="resonance_state")
    defaults = default_resonance_state()
    for key, default_val in defaults.items():
        if key != "initialized":
            loaded.setdefault(key, default_val)
    if "initialized" not in loaded:
        loaded["initialized"] = bool(
            loaded.get("last_updated")
            or loaded.get("active_resonance_symbols")
            or loaded.get("history")
        )
    return loaded


def save_resonance_state(
    database: Path,
    state: Mapping[str, object],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, object]:
    """Persist resonance state atomically with an explicit ISO-8601 `last_updated` timestamp."""
    record = dict(state)
    record["last_updated"] = now_beijing_iso()
    payload = _dumps_json(record)
    with _write_conn(database, conn) as w_conn:
        w_conn.execute(
            """
            INSERT INTO resonance_state (id, payload) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """,
            (payload,),
        )
    return record
