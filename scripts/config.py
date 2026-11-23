#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目统一配置、路径、原子存储与跨进程文件锁中心 (Unified Config & Storage Helpers)
==============================================================================
收口所有脚本的根目录、.env 解析、SQLite 路径解析、币种名称规范化、北京时间格式化、
原子 JSON 写入以及跨进程建议锁 (fcntl.flock)，消除各脚本重复实现与配置漂移。
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"
CONFIG_DIR = PROJECT_ROOT / "config"
WEB_DIR = PROJECT_ROOT / "web"

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DB_PATH = RUNS_DIR / "account.sqlite3"
DEFAULT_POOL_PATH = RUNS_DIR / "second_leg_candidates_pool.json"
DEFAULT_SUB30M_HISTORY_PATH = RUNS_DIR / "sub_30m_history.json"
DEFAULT_POSITIONS_PATH = RUNS_DIR / "trade_positions.json"
DEFAULT_RESONANCE_STATE_PATH = RUNS_DIR / "resonance_state.json"
DEFAULT_ANALYTICS_PATH = RUNS_DIR / "account_analytics.json"
DEFAULT_ALERTS_CONFIG_PATH = CONFIG_DIR / "alerts_config.json"
DEFAULT_TV_LAYOUT_ID = "ig0LuDL8"

BEIJING_TZ = timezone(timedelta(hours=8))


def load_dotenv(env_path: Optional[Path] = None) -> dict[str, str]:
    """解析 .env 文件为字典（跳过空行与注释，剥离首尾引号）。"""
    target = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    result: dict[str, str] = {}
    if not target.exists():
        return result
    try:
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, v = stripped.split("=", 1)
                    result[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return result


def get_env_setting(key: str, default: str = "", env_path: Optional[Path] = None) -> str:
    """按 OS 环境变量 > .env 文件 > 默认值 的优先级读取配置项。"""
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


def atomic_write_json(filepath: Path, data: Any) -> None:
    """原子化写入 JSON 文件（写临时文件 + fsync + os.replace），防止中断损坏或半写读。"""
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
    """
    基于 fcntl.flock 的跨进程独占建议锁，保护 JSON 状态文件的 read-modify-write 事务，
    防止 --daemon 与 --check-once 并发写入造成状态丢失。
    """
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
