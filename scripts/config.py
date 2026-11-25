#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目统一配置、路径、原子存储、SQLite 连接与跨进程文件锁中心
==============================================================================
收口所有脚本的根目录、.env 解析、SQLite 路径与连接策略 (WAL + busy_timeout)、
币种名称规范化、北京时间格式化、原子 JSON 写入以及跨进程建议锁 (fcntl.flock)。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"
WEB_DIR = PROJECT_ROOT / "web"

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DB_PATH = RUNS_DIR / "account.sqlite3"
DEFAULT_TV_LAYOUT_ID = "ig0LuDL8"

BEIJING_TZ = timezone(timedelta(hours=8))


def _parse_dotenv_value(raw_val: str) -> str:
    """解析单行 .env 值：引号内保留 #，无引号时剥离行内 # 注释；未闭合引号直接抛 ValueError。"""
    val = raw_val.strip()
    if not val:
        return ""
    if val[0] in ("'", '"'):
        quote = val[0]
        chars: list[str] = []
        escaped = False
        for ch in val[1:]:
            if escaped:
                chars.append(ch)
                escaped = False
                continue
            if ch == "\\" and quote == '"':
                escaped = True
                continue
            if ch == quote:
                return "".join(chars)
            chars.append(ch)
        raise ValueError(f"Unclosed {quote} quote in .env value: {raw_val!r}")
    if val.startswith("#"):
        return ""
    return re.split(r"\s+#", val, maxsplit=1)[0].strip()


_DOTENV_CACHE: dict[Path, tuple[int, int, dict[str, str]]] = {}
_DOTENV_LOCK = threading.Lock()


def load_dotenv(env_path: Optional[Path] = None) -> dict[str, str]:
    """线程安全地解析 .env 文件为字典（跳过空行与注释，剥离引号与无引号行内注释；按 mtime/size 缓存）。"""
    target = Path(env_path).resolve() if env_path is not None else DEFAULT_ENV_PATH.resolve()
    with _DOTENV_LOCK:
        if not target.exists():
            _DOTENV_CACHE.pop(target, None)
            return {}
        st = target.stat()
        cached = _DOTENV_CACHE.get(target)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return dict(cached[2])

        result: dict[str, str] = {}
        with open(target, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                if stripped.startswith("export "):
                    stripped = stripped[7:].strip()
                    if "=" not in stripped:
                        continue
                k, v = stripped.split("=", 1)
                key = k.strip()
                if key:
                    try:
                        result[key] = _parse_dotenv_value(v)
                    except ValueError as exc:
                        raise ValueError(f"Invalid .env line {lineno} in {target} ({key}): {exc}") from exc
        _DOTENV_CACHE[target] = (st.st_mtime_ns, st.st_size, result)
        return dict(result)


def get_env_setting(key: str, default: str = "", env_path: Optional[Path] = None) -> str:
    """
    按 非空 OS 环境变量 > 非空 .env 值 > 默认值 的优先级读取配置项。
    空白值 (\"\") 视为未配置，避免 .env 占位空行覆盖数字/默认配置或阻塞可选通道。
    """
    val = os.environ.get(key)
    if val is not None and val != "":
        return val
    dotenv = load_dotenv(env_path)
    if key in dotenv and dotenv[key] != "":
        return dotenv[key]
    return default


def resolve_database_path(
    explicit: Optional[Path] = None,
    env_path: Optional[Path] = None,
) -> Optional[Path]:
    """
    统一解析 SQLite 数据库路径。
    优先级：显式 CLI 参数 > COIN_DATABASE 环境变量 > .env 中的 COIN_DATABASE。
    """
    if explicit is not None:
        return Path(explicit)
    raw = get_env_setting("COIN_DATABASE", "", env_path=env_path).strip()
    return Path(raw) if raw else None


def open_sqlite_connection(
    db_path: Path,
    *,
    readonly: bool = False,
    timeout: float = 15.0,
) -> sqlite3.Connection:
    """统一打开 SQLite 连接（只读模式使用 mode=ro + busy_timeout；读写模式开启 WAL）。"""
    target = Path(db_path)
    if readonly:
        conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, timeout=timeout)
    try:
        busy_ms = max(0, int(timeout * 1000))
        # Set busy_timeout before PRAGMA journal_mode=WAL so initial WAL switch also honors timeout.
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        if not readonly:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            current_mode = str(row[0]).lower() if row and row[0] is not None else ""
            if current_mode != "wal":
                deadline = time.monotonic() + max(timeout, 0.0)
                delay = 0.01
                while True:
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        err_msg = str(exc).lower()
                        if ("locked" not in err_msg and "busy" not in err_msg) or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                        delay = min(delay * 1.5, 0.1)
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    except Exception:
        conn.close()
        raise


def clean_symbol(raw: str) -> str:
    """标准化币种符号，去除交易所前缀 (如 BINANCE:) 与永续后缀 (.P)。"""
    if not raw:
        return ""
    sym = str(raw).strip().upper()
    if ":" in sym:
        sym = sym.split(":")[-1]
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym


def get_beijing_time_str(ts_sec: Optional[float] = None) -> str:
    """格式化东八区北京时间 YYYY-MM-DD HH:MM:SS。"""
    dt = datetime.fromtimestamp(ts_sec if ts_sec is not None else time.time(), tz=BEIJING_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# Retained for scripts/tv_trendline_engine.py drawings JSON cache (`runs/tradingview_drawings_<layout>.json`).
def atomic_write_json(filepath: Path, data: object, *, allow_nan: bool = False) -> None:
    """原子化写入 JSON 文件（写临时文件 + fsync + os.replace），默认禁止 NaN/Infinity。"""
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=allow_nan)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


@contextmanager
def file_lock(filepath: Path) -> Iterator[None]:
    """基于 fcntl.flock 的跨进程独占建议锁（供 tv_trendline_engine.py 缓存写入使用）。"""
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    lock_fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()
