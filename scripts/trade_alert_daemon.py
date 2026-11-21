#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
“突破追多与移动保本系统”实盘风控与信号预警守护进程 (Trade Alert & Risk Management Daemon)
==============================================================================
核心风控四重闭环架构:
1. Feature 1 (新共振标的提醒): 
   监控 runs/second_leg_candidates_pool.json，比对 runs/resonance_state.json，
   当新币种进入共振区 (三重共振或双核黄金共振)，触发 Server酱 微信提醒，
   指引用户在 TradingView 专属布局 ig0LuDL8 上绘制 15m 下行趋势线。
2. Feature 2 (15m 突破开多提醒):
   调用 scripts/tv_trendline_engine.py 读取 TV 云端画线，
   对已有画线的共振标的，于每个 15m 根 K 线收盘后校验是否实体突破对数阻力线。
   突破即发 Server酱 开多买点②提醒。
3. Feature 3 (持仓破线无条件止损):
   针对 runs/trade_positions.json 中的开仓持仓，
   若 15m 收盘价跌破对应趋势线支撑，立即发出最高级别紧急平多止损通知。
4. Feature 4 (持仓动态移动止盈保护):
   针对开仓持仓，实时追踪币安最新成交价。
   当价格涨幅达到 +6% (>= entry_price * 1.06) 时，自动将止损线上移至 +1% (entry_price * 1.01)，
   锁定保本利润并推送通知；后续价格若回踩 <= 移动止损线，发出止盈离场平仓通知。
==============================================================================
"""

import os
import sys
import time
import json
import math
import signal
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import urllib.request
import urllib.error
import urllib.parse

# 路径基准
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"
CONFIG_DIR = PROJECT_ROOT / "config"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 尝试导入 TV 画线引擎模块
try:
    import tv_trendline_engine as tve
except ImportError:
    try:
        from scripts import tv_trendline_engine as tve
    except ImportError:
        tve = None

# 尝试导入币安实盘持仓客户端
try:
    import binance_position_client as bpc
except ImportError:
    try:
        from scripts import binance_position_client as bpc
    except ImportError:
        bpc = None

# 日志初始化
LOG_FILE = RUNS_DIR / "trade_alert_daemon.log"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    ]
)
logger = logging.getLogger("trade_alert_daemon")

# 默认配置路径
DEFAULT_CONFIG_PATH = CONFIG_DIR / "alerts_config.json"
DEFAULT_POSITIONS_PATH = RUNS_DIR / "trade_positions.json"
DEFAULT_RESONANCE_STATE_PATH = RUNS_DIR / "resonance_state.json"
DEFAULT_POOL_PATH = RUNS_DIR / "second_leg_candidates_pool.json"
DEFAULT_TV_LAYOUT_ID = "ig0LuDL8"


# ==============================================================================
# 0. 配置与安全存取实用工具
# ==============================================================================

def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """加载配置文件并结合环境变量"""
    cfg = {
        "serverchan_sendkey": "",
        "binance_api_key": "",
        "binance_api_secret": "",
        "tradingview_layout_id": DEFAULT_TV_LAYOUT_ID,
        "check_interval_seconds": 10,
        "resonance_pool_file": str(DEFAULT_POOL_PATH),
        "resonance_state_file": str(DEFAULT_RESONANCE_STATE_PATH),
        "positions_file": str(DEFAULT_POSITIONS_PATH),
        "trailing_activation_pct": 6.0,
        "trailing_lock_pct": 1.0,
    }
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
        except Exception as e:
            logger.warning(f"读取配置文件 {config_path} 异常，采用默认值: {e}")
    else:
        # 创建默认模板配置
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"写入初始配置文件异常: {e}")

    # 环境变量优先级最高
    env_sendkey = os.environ.get("SERVERCHAN_SENDKEY")
    if env_sendkey:
        cfg["serverchan_sendkey"] = env_sendkey
    env_layout = os.environ.get("TV_LAYOUT_ID")
    if env_layout:
        cfg["tradingview_layout_id"] = env_layout
    env_b_key = os.environ.get("BINANCE_API_KEY")
    if env_b_key:
        cfg["binance_api_key"] = env_b_key
    env_b_sec = os.environ.get("BINANCE_API_SECRET")
    if env_b_sec:
        cfg["binance_api_secret"] = env_b_sec

    return cfg


def atomic_write_json(filepath: Path, data: Any):
    """安全原子写入 JSON 文件，避免写入中断破坏文件"""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = filepath.with_suffix(f".tmp.{os.getpid()}.{int(time.time()*1000)}")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)


def get_beijing_time_str(ts_sec: Optional[float] = None) -> str:
    """格式化东八区北京时间"""
    tz_bj = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(ts_sec if ts_sec is not None else time.time(), tz=tz_bj)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ==============================================================================
# 1. Server酱 (ServerChan) 推送通知组件
# ==============================================================================

class ServerChanNotifier:
    """Server酱推送服务客户端，支持真实推送与优雅 Dry-Run / Log-Only 模式"""

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
                "User-Agent": "TradeAlertDaemon/1.0"
            },
            method="POST"
        )

        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    if resp_data.get("code") == 0 or resp_data.get("errno") == 0:
                        logger.info(f"    ✓ Server酱推送成功: {title}")
                        return True
                    else:
                        logger.error(f"    ✗ Server酱接口返回错误: {resp_data}")
                        return False
            except Exception as e:
                logger.warning(f"    ⚠️ Server酱网络请求异常 (尝试 {attempt+1}/2): {e}")
                time.sleep(1.0)
        return False


# ==============================================================================
# 2. 币安合约行情与 15m K线接口
# ==============================================================================

def get_binance_ticker_price(symbol: str) -> float:
    """获取币安永续合约当前价格"""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    url = f"https://www.binance.com/fapi/v1/ticker/price?symbol={sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return float(data.get("price", 0.0))
    except Exception as e:
        logger.warning(f"获取 {sym} 实时价格失败: {e}")
        return 0.0


def get_binance_closed_15m_candle(symbol: str) -> Optional[dict]:
    """
    拉取币安永续合约最新已收盘的 15m K 线。
    返回格式:
    {
        "symbol": symbol,
        "open_time": int (秒级),
        "close_time": int (秒级),
        "open": float,
        "high": float,
        "low": float,
        "close": float,
        "volume": float,
        "is_closed": bool
    }
    """
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    url = f"https://www.binance.com/fapi/v1/klines?symbol={sym}&interval=15m&limit=5"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            klines = json.loads(resp.read().decode("utf-8"))
            if not klines or len(klines) < 2:
                return None
            
            now_ms = int(time.time() * 1000)
            # 倒数第一根通常是正在进行的未闭合K线，倒数第二根为确定闭合的K线
            candle = klines[-2]
            if klines[-1][6] < now_ms:
                # 倒数第一根也已收盘
                candle = klines[-1]

            open_dt = datetime.fromtimestamp(candle[0] / 1000.0, tz=timezone(timedelta(hours=8)))
            close_dt = datetime.fromtimestamp(candle[6] / 1000.0, tz=timezone(timedelta(hours=8)))
            return {
                "symbol": sym,
                "open_time": int(candle[0] // 1000),
                "close_time": int(candle[6] // 1000),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
                "candle_time": open_dt.strftime("%Y-%m-%d %H:%M"),
                "candle_timestamp": int(candle[0] // 1000),
                "is_closed": True
            }
    except Exception as e:
        logger.warning(f"拉取 {sym} 15m K线失败: {e}")
        return None


# ==============================================================================
# 3. 对数坐标趋势线计算与画线引擎接入
# ==============================================================================

_DRAWINGS_CACHE = {"data": None, "ts": 0, "layout_id": ""}


def calculate_log_resistance(p1: float, t1: float, p2: float, t2: float, target_t: float) -> float:
    """
    计算 TradingView 对数坐标系下的趋势线阻力/支撑投影价:
    数学公式: P(t) = P1 * (P2 / P1) ** ((t - t1) / (t2 - t1))
    """
    if tve and hasattr(tve, "calculate_log_resistance"):
        try:
            return float(tve.calculate_log_resistance(p1, t1, p2, t2, target_t))
        except Exception as e:
            logger.debug(f"调用 tv_trendline_engine 计算异常，回退内置算法: {e}")

    if p1 <= 0 or p2 <= 0 or t2 == t1:
        return 0.0
    try:
        return float(p1 * ((p2 / p1) ** ((target_t - t1) / (t2 - t1))))
    except Exception:
        return 0.0


def fetch_layout_drawings(layout_id: str = DEFAULT_TV_LAYOUT_ID, ttl_sec: int = 60) -> Dict[str, Any]:
    """从 tv_trendline_engine 获取 TradingView 布局中的画线数据 (支持内存与本地缓存)"""
    global _DRAWINGS_CACHE
    now = time.time()
    if (
        _DRAWINGS_CACHE["data"] is not None
        and _DRAWINGS_CACHE["layout_id"] == layout_id
        and (now - _DRAWINGS_CACHE["ts"]) < ttl_sec
    ):
        return _DRAWINGS_CACHE["data"]

    # 优先检测本地近期生成的快照缓存 (有效窗口 180s)
    local_cache_file = RUNS_DIR / f"tradingview_drawings_{layout_id}.json"
    if local_cache_file.exists():
        try:
            mtime = local_cache_file.stat().st_mtime
            if now - mtime < 180:
                with open(local_cache_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    drawings = cdata.get("drawings_by_symbol", {})
                    if drawings:
                        _DRAWINGS_CACHE = {"data": drawings, "ts": now, "layout_id": layout_id}
                        return drawings
        except Exception:
            pass

    if tve and hasattr(tve, "get_layout_drawings"):
        try:
            drawings = tve.get_layout_drawings(layout_id)
            if isinstance(drawings, dict):
                _DRAWINGS_CACHE = {"data": drawings, "ts": now, "layout_id": layout_id}
                return drawings
        except Exception as e:
            logger.warning(f"获取 TV 布局画线异常: {e}")
    return {}


# ==============================================================================
# 4. 持仓状态与共振状态存储管理器
# ==============================================================================

class PositionStore:
    """管理 runs/trade_positions.json 持仓状态数据"""

    def __init__(self, filepath: Path = DEFAULT_POSITIONS_PATH):
        self.filepath = Path(filepath)
        self.data: Dict[str, Any] = {"version": 1, "positions": [], "history": []}
        self.load()

    def load(self):
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        self.data = content
                        if "positions" not in self.data:
                            self.data["positions"] = []
                        if "history" not in self.data:
                            self.data["history"] = []
            except Exception as e:
                logger.error(f"加载持仓文件失败: {e}")

    def save(self):
        self.data["updated_at"] = get_beijing_time_str()
        atomic_write_json(self.filepath, self.data)

    def get_open_positions(self) -> List[Dict[str, Any]]:
        return [p for p in self.data.get("positions", []) if p.get("status") == "OPEN"]

    def add_position(self, symbol: str, entry_price: float, amount: float = 0.0, trendline: Optional[dict] = None, notes: str = "") -> dict:
        sym = symbol.upper()
        if not sym.endswith("USDT"):
            sym = f"{sym}USDT"
        
        # 检查是否已存在同币种未平仓仓位
        for p in self.get_open_positions():
            if p["symbol"] == sym:
                logger.info(f"仓位 {sym} 已存在，正在更新开仓价 {entry_price}...")
                p["entry_price"] = float(entry_price)
                if amount > 0:
                    p["amount"] = float(amount)
                if trendline:
                    p["trendline"] = trendline
                if notes:
                    p["notes"] = notes
                p["updated_at"] = get_beijing_time_str()
                self.save()
                return p

        pos = {
            "symbol": sym,
            "entry_price": float(entry_price),
            "amount": float(amount),
            "entry_time": get_beijing_time_str(),
            "highest_price": float(entry_price),
            "current_price": float(entry_price),
            "trailing_stop_triggered": False,
            "trailing_stop_price": None,
            "stop_loss_notified": False,
            "profit_lock_notified": False,
            "trendline": trendline,
            "status": "OPEN",
            "notes": notes or "Manual / Signal Entry"
        }
        self.data["positions"].append(pos)
        self.save()
        logger.info(f"✅ 成功录入新持仓: {sym} @ {entry_price} (数量: {amount})")
        return pos

    def close_position(self, symbol: str, reason: str = "MANUAL_CLOSE", exit_price: Optional[float] = None) -> Optional[dict]:
        sym = symbol.upper()
        if not sym.endswith("USDT"):
            sym = f"{sym}USDT"

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
        if exit_price:
            target_pos["exit_price"] = float(exit_price)
            pnl_pct = (target_pos["exit_price"] - target_pos["entry_price"]) / target_pos["entry_price"] * 100.0
            target_pos["realized_pnl_pct"] = round(pnl_pct, 2)

        # 移动至历史
        self.data["positions"].remove(target_pos)
        self.data["history"].append(target_pos)
        self.save()
        logger.info(f"✅ 成功平仓/移除持仓: {sym} (原因: {reason})")
        return target_pos


class ResonanceStateStore:
    """管理 runs/resonance_state.json 共振状态记忆"""

    def __init__(self, filepath: Path = DEFAULT_RESONANCE_STATE_PATH):
        self.filepath = Path(filepath)
        self.data: Dict[str, Any] = {
            "version": 1,
            "last_updated": "",
            "active_resonance_symbols": [],
            "notified_breakout_keys": [],
            "history": {}
        }
        self.load()

    def load(self):
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        self.data = content
                        if "active_resonance_symbols" not in self.data:
                            self.data["active_resonance_symbols"] = []
                        if "notified_breakout_keys" not in self.data:
                            self.data["notified_breakout_keys"] = []
                        if "history" not in self.data:
                            self.data["history"] = {}
            except Exception as e:
                logger.error(f"加载共振状态文件失败: {e}")

    def save(self):
        self.data["last_updated"] = get_beijing_time_str()
        atomic_write_json(self.filepath, self.data)

    def get_active_symbols(self) -> set:
        return set(self.data.get("active_resonance_symbols", []))

    def has_breakout_notified(self, key: str) -> bool:
        return key in self.data.get("notified_breakout_keys", [])

    def mark_breakout_notified(self, key: str):
        keys = self.data.setdefault("notified_breakout_keys", [])
        if key not in keys:
            keys.append(key)
            # 保持最近 200 个防重记录
            if len(keys) > 200:
                self.data["notified_breakout_keys"] = keys[-200:]
            self.save()


# ==============================================================================
# 5. 四大核心业务功能实现 (Feature 1 ~ Feature 4)
# ==============================================================================

def check_feature_1_resonance(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    pool_file: Path = DEFAULT_POOL_PATH,
    layout_id: str = DEFAULT_TV_LAYOUT_ID
) -> List[dict]:
    """
    Feature 1 (新共振标的提醒):
    比对 runs/second_leg_candidates_pool.json 与 runs/resonance_state.json。
    新进入三重共振或双核黄金共振的标的，触发 Server酱 微信提醒，指引画线。
    """
    if not pool_file.exists():
        logger.warning(f"候选池文件不存在: {pool_file}")
        return []

    try:
        with open(pool_file, "r", encoding="utf-8") as f:
            candidates = json.load(f)
    except Exception as e:
        logger.error(f"读取候选池文件异常: {e}")
        return []

    # 提取当前处于共振状态的标的 (三重共振 或 双核黄金共振)
    current_resonance_coins = {}
    for c in candidates:
        is_res = (
            c.get("is_resonance")
            or c.get("resonance_score", 0) >= 2
            or (c.get("is_ema_bull") and c.get("turnover_ratio", 0) >= 1.5)
        )
        if is_res:
            current_resonance_coins[c["symbol"]] = c

    active_symbols = state_store.get_active_symbols()
    current_symbols = set(current_resonance_coins.keys())

    # 计算新进入共振的标的
    new_entered = current_symbols - active_symbols
    alerted_list = []

    if new_entered:
        logger.info(f"⚡ 检测到 {len(new_entered)} 个新进入共振区标的: {list(new_entered)}")

    for sym in sorted(new_entered):
        coin = current_resonance_coins[sym]
        res_score = coin.get("resonance_score", 2)
        is_neg_fr = coin.get("is_neg_funding") or coin.get("funding_rate_4h_pct", 0) < 0
        
        if res_score == 3 or is_neg_fr:
            res_tag = "🔥 三重共振 (双核共振 + 4h负费率逼空)"
            res_badge = "🔥 三重共振"
        else:
            res_tag = "👑 双核黄金共振 (1h顺势EMA + 高换手)"
            res_badge = "👑 双核共振"

        tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym}.P&interval=15"
        
        title = f"{res_badge} 提醒: {sym} 进入共振区，请画 15m 趋势线"
        desp = f"""### {res_tag} 选币确认！

- **标的代码**: `BINANCE:{sym}.P`
- **当前市价**: `${coin.get('price', 0):.4f}`
- **24h换手率**: `{coin.get('turnover_ratio', 0):.1f}x`
- **4h资金费率**: `{coin.get('funding_rate_4h_pct', 0):+.4f}%`
- **机会区标签**: {', '.join(coin.get('zones', [])) or '共振区'}
- **形态状态**: {coin.get('status', '多周期趋势共振')}
- **触发时间**: {get_beijing_time_str()}

---
> 🎯 **操作指引**:
> 该标的已进入第二段量化严选共振区！请立即打开下方 TradingView 专属布局，在 15m 图表连接近期下行高点绘制对数趋势线。
> 系统将持续监控该线，在 15m K 线收盘放量突破时发出买点②开多提醒。

👉 **[点击立即前往 TradingView 专属画线图表]({tv_link})**
"""
        sent_ok = notifier.send_alert(title, desp)
        if sent_ok:
            alerted_list.append(coin)
            # 记录历史
            state_store.data["history"][sym] = {
                "symbol": sym,
                "first_detected": get_beijing_time_str(),
                "res_score": res_score,
                "zones": coin.get("zones", []),
                "tv_link": tv_link
            }

    # 更新状态：当前共振标的存入，掉出共振的移除以便下次重新触发
    state_store.data["active_resonance_symbols"] = sorted(list(current_symbols))
    state_store.save()
    return alerted_list


def check_feature_2_breakout(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID
) -> List[dict]:
    """
    Feature 2 (15m 突破开多提醒):
    针对处于共振区的标的，若在 TradingView ig0LuDL8 中存在下行趋势线，
    校验最新 15m 已收盘 K 线是否实体突破该对数趋势线。突破即发送 Server酱 开多买点②提醒。
    """
    drawings_map = fetch_layout_drawings(layout_id)
    if not drawings_map:
        logger.debug(f"TV 布局 {layout_id} 暂无可用画线数据")
        return []

    active_symbols = state_store.get_active_symbols()
    breakout_alerts = []

    # 遍历当前共振币种
    for sym in active_symbols:
        # 支持多种 symbol 匹配格式: "YBUSDT", "BINANCE:YBUSDT.P", "BINANCE:YBUSDT"
        matched_lines = None
        for k, lines in drawings_map.items():
            k_clean = k.upper().replace(".P", "").replace("BINANCE:", "")
            if sym == k_clean or sym in k_clean:
                matched_lines = lines
                break

        if not matched_lines:
            continue

        # 单币种仅拉取一次最新已收盘 15m K线
        candle = get_binance_closed_15m_candle(sym)
        if not candle:
            continue

        c_ts = candle.get("candle_timestamp") or candle.get("close_time", 0)
        sym_candle_key = f"{sym}_{c_ts}"
        if state_store.has_breakout_notified(sym_candle_key):
            continue

        lines_to_check = matched_lines if isinstance(matched_lines, list) else [matched_lines]

        # 遍历该币种的所有趋势线
        for line in lines_to_check:
            # 兼容不同数据格式: points 或 p1/p2
            p1_price, p1_time, p2_price, p2_time = None, None, None, None
            if "p1" in line and "p2" in line and "t1" in line and "t2" in line:
                p1_time, p1_price = int(line["t1"]), float(line["p1"])
                p2_time, p2_price = int(line["t2"]), float(line["p2"])
            elif "points" in line and len(line["points"]) >= 2:
                pt1 = line["points"][0]
                pt2 = line["points"][1]
                p1_time = int(pt1.get("time_t") or pt1.get("time") or 0)
                p1_price = float(pt1.get("price") or 0)
                p2_time = int(pt2.get("time_t") or pt2.get("time") or 0)
                p2_price = float(pt2.get("price") or 0)

            if p1_price is None or p2_price is None or p1_time is None or p2_time is None:
                continue

            # 仅处理下行趋势线 (阻力线: p1_price >= p2_price)
            if p1_price < p2_price:
                continue

            # 优先调用 tve.check_symbol_breakout
            is_breakout = False
            close_price = 0.0
            res_price = 0.0
            pct = 0.0
            candle_info = {}

            if tve and hasattr(tve, "check_symbol_breakout"):
                try:
                    res = tve.check_symbol_breakout(sym, line, candle=candle)
                    if res:
                        is_breakout = bool(res.get("is_breakout"))
                        close_price = float(res.get("close_price", 0))
                        res_price = float(res.get("res_price", 0))
                        pct = float(res.get("pct", 0))
                        candle_info = res
                except Exception as e:
                    logger.debug(f"tve.check_symbol_breakout 运行异常，回退内置算法: {e}")

            if not is_breakout and not candle_info:
                close_time = candle["close_time"]
                close_price = candle["close"]
                res_price = calculate_log_resistance(p1_price, p1_time, p2_price, p2_time, close_time)
                if res_price > 0 and close_price > res_price:
                    is_breakout = True
                    pct = (close_price - res_price) / res_price * 100.0
                    candle_info = candle

            if is_breakout:
                state_store.mark_breakout_notified(sym_candle_key)
                info = {
                    "symbol": sym,
                    "close_price": close_price,
                    "res_price": res_price,
                    "pct": pct,
                    "candle_time": candle_info.get("candle_time") or candle_info.get("close_time"),
                    "trendline": line
                }
                breakout_alerts.append(info)
                _send_breakout_notification(notifier, sym, res_price, close_price, pct, candle_info, layout_id)
                break

    return breakout_alerts


def _send_breakout_notification(
    notifier: ServerChanNotifier,
    sym: str,
    res_price: float,
    close_price: float,
    pct: float,
    candle_or_res: dict,
    layout_id: str
):
    """发送突破买点②开多通知"""
    tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym}.P&interval=15"
    candle_time_str = candle_or_res.get("candle_time")
    if not candle_time_str and "close_time" in candle_or_res:
        candle_time_str = get_beijing_time_str(candle_or_res["close_time"])
    if not candle_time_str:
        candle_time_str = get_beijing_time_str()

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
> 3. **动态止盈保护**: 浮盈达到 +6% 自动上移止损至 +1% 保本锁利！

👉 **[查看 TradingView 15m 实战图表]({tv_link})**
"""
    notifier.send_alert(title, desp)


def check_feature_3_stop_loss(
    notifier: ServerChanNotifier,
    pos_store: PositionStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID
) -> List[dict]:
    """
    Feature 3 (持仓破线无条件止损):
    针对 runs/trade_positions.json 中的开仓持仓，
    在每个 15m K 线收盘后，校验收盘价是否跌破趋势线支撑位。
    若跌破，立即发出最高级别紧急平多止损通知。
    """
    open_positions = pos_store.get_open_positions()
    if not open_positions:
        return []

    drawings_map = fetch_layout_drawings(layout_id)
    stop_loss_alerts = []

    for pos in open_positions:
        sym = pos["symbol"]
        candle = get_binance_closed_15m_candle(sym)
        if not candle:
            continue

        close_time = candle["close_time"]
        close_price = candle["close"]
        entry_price = float(pos["entry_price"])

        # 寻找对应的趋势线：优先持仓中缓存的线，其次从 TV 画线匹配
        line = pos.get("trendline")
        if not line and drawings_map:
            for k, lines in drawings_map.items():
                k_clean = k.upper().replace(".P", "").replace("BINANCE:", "")
                if sym == k_clean or sym in k_clean:
                    line = lines[0] if isinstance(lines, list) and lines else lines
                    pos["trendline"] = line
                    break

        if not line:
            continue

        p1_price, p1_time, p2_price, p2_time = None, None, None, None
        if "p1" in line and "p2" in line and "t1" in line and "t2" in line:
            p1_time, p1_price = int(line["t1"]), float(line["p1"])
            p2_time, p2_price = int(line["t2"]), float(line["p2"])
        elif "points" in line and len(line["points"]) >= 2:
            pt1 = line["points"][0]
            pt2 = line["points"][1]
            p1_time = int(pt1.get("time_t") or pt1.get("time") or 0)
            p1_price = float(pt1.get("price") or 0)
            p2_time = int(pt2.get("time_t") or pt2.get("time") or 0)
            p2_price = float(pt2.get("price") or 0)

        if p1_price is None or p2_price is None or p1_time is None or p2_time is None:
            continue

        support_price = calculate_log_resistance(p1_price, p1_time, p2_price, p2_time, close_time)
        if support_price <= 0:
            continue

        # 检查是否跌破趋势线
        if close_price < support_price:
            # 破线止损
            if not pos.get("stop_loss_notified", False):
                drop_pct = (support_price - close_price) / support_price * 100.0
                pnl_pct = (close_price - entry_price) / entry_price * 100.0
                tv_link = f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym}.P&interval=15"

                title = f"🚨 【破线止损预警】{sym} 15m 跌破趋势线，请立即平多止损！"
                desp = f"""### 🚨 【破线无条件止损】纪律执行警报！

- **持仓标的**: `{sym}`
- **开仓价格**: `${entry_price:.4f}`
- **15m 收盘价**: `${close_price:.4f}`
- **趋势线基准**: `${support_price:.4f}`
- **跌破幅度**: `-{drop_pct:.2f}%`
- **持仓盈亏**: `{pnl_pct:+.2f}%`
- **持仓数量**: `{pos.get('amount', 0)}`
- **收盘时间**: {get_beijing_time_str(candle['close_time'])}

---
> ⚠️ **风控铁律提醒**:
> 15m K 线收盘价已有效跌破对数趋势线支撑，说明假突破或行情反转，买点②逻辑失效！
> 请立即执行市价平多止损，杜绝任何抗单侥幸心理！

👉 **[查看 TradingView 图表]({tv_link})**
"""
                notifier.send_alert(title, desp)
                pos["stop_loss_notified"] = True
                pos["status"] = "STOP_LOSS_TRIGGERED"
                pos["closed_at"] = get_beijing_time_str()
                pos["exit_price"] = close_price
                if pos in pos_store.data["positions"]:
                    pos_store.data["positions"].remove(pos)
                    pos_store.data["history"].append(pos)
                pos_store.save()
                stop_loss_alerts.append(pos)
                logger.warning(f"🚨 标的 {sym} 跌破趋势线，已发送止损警报！")

    return stop_loss_alerts


def check_feature_4_trailing_stops(
    notifier: ServerChanNotifier,
    pos_store: PositionStore,
    activation_pct: float = 6.0,
    lock_pct: float = 1.0
) -> List[dict]:
    """
    Feature 4 (持仓动态移动止盈保护):
    实时跟踪持仓市价。
    1. 当现价涨幅 >= entry_price * (1 + activation_pct/100) (默认 +6%) 时:
       自动将止损线上移至 entry_price * (1 + lock_pct/100) (默认 +1%)，锁定保本收益。
    2. 若后续价格回踩 <= 移动止损价:
       发出平仓锁利离场通知。
    """
    open_positions = pos_store.get_open_positions()
    if not open_positions:
        return []

    events = []

    for pos in open_positions:
        sym = pos["symbol"]
        curr_price = get_binance_ticker_price(sym)
        if curr_price <= 0:
            continue

        entry_price = float(pos["entry_price"])
        highest_price = max(pos.get("highest_price", entry_price), curr_price)
        pos["highest_price"] = highest_price
        pos["current_price"] = curr_price
        
        gain_pct = (curr_price - entry_price) / entry_price * 100.0
        pos["pnl_pct"] = round(gain_pct, 2)

        # 检查是否激活移动止盈 (涨幅 >= +6%)
        if not pos.get("trailing_stop_triggered", False):
            target_activation_price = entry_price * (1.0 + activation_pct / 100.0)
            if curr_price >= target_activation_price:
                # 触发移动止损线上移至 +1%
                lock_price = round(entry_price * (1.0 + lock_pct / 100.0), 8)
                pos["trailing_stop_triggered"] = True
                pos["trailing_stop_price"] = lock_price
                pos["trailing_stop_activated_at"] = get_beijing_time_str()

                title = f"🎯 【移动止盈锁定】{sym} 浮盈达 +{gain_pct:.1f}%，已启动 +{lock_pct:.0f}% 保本锁利！"
                desp = f"""### 🎯 【动态移动止盈启动】锁定确定性收益！

- **持仓标的**: `{sym}`
- **开仓价格**: `${entry_price:.4f}`
- **当前市价**: `${curr_price:.4f}` (最高: `${highest_price:.4f}`)
- **当前涨幅**: `+{gain_pct:.2f}%` (已突破 +{activation_pct:.0f}% 门槛)
- **移动止损新价**: `${lock_price:.4f}` (+{lock_pct:.1f}% 利润安全线)
- **触发时间**: {get_beijing_time_str()}

---
> 🛡️ **利润垫保护已锁死**:
> 恭喜！当前盈利已达成第一目标，止损位已自动提拉至开仓价上方 +{lock_pct:.1f}%。
> 后续即便行情深度回撤，亦能确保斩获稳健收益。若现价回跌触及 `${lock_price:.4f}`，系统将发出离场指引。
"""
                notifier.send_alert(title, desp)
                pos_store.save()
                events.append({"type": "TRAILING_ACTIVATED", "pos": pos})
                logger.info(f"🎯 {sym} 浮盈 +{gain_pct:.2f}% 激活移动止损，锁定保本价: {lock_price}")

        # 检查是否跌破移动止盈线 (已激活移动止盈的状态下)
        if pos.get("trailing_stop_triggered", False) and not pos.get("profit_lock_notified", False):
            lock_price = float(pos["trailing_stop_price"])
            if curr_price <= lock_price:
                pos["profit_lock_notified"] = True
                pos["status"] = "PROFIT_LOCKED"
                pos["closed_at"] = get_beijing_time_str()
                pos["exit_price"] = curr_price
                realized_pct = (curr_price - entry_price) / entry_price * 100.0

                title = f"🔔 【移动止盈离场】{sym} 触及移动止盈线，请平仓锁利离场！"
                desp = f"""### 🔔 【移动止盈触发】建议市价平多落袋为安！

- **持仓标的**: `{sym}`
- **开仓价格**: `${entry_price:.4f}`
- **触发行情价**: `${curr_price:.4f}`
- **锁利触发线**: `${lock_price:.4f}` (+{lock_pct:.1f}%)
- **锁定收益率**: `+{realized_pct:+.2f}%`
- **触发时间**: {get_beijing_time_str()}

---
> 💰 **操作指引**:
> 行情出现回踩并触及移动保本线，请严格执行锁利纪律，市价平多离场，确保利润安全入袋！
"""
                notifier.send_alert(title, desp)
                if pos in pos_store.data["positions"]:
                    pos_store.data["positions"].remove(pos)
                    pos_store.data["history"].append(pos)
                pos_store.save()
                events.append({"type": "PROFIT_LOCK_EXIT", "pos": pos})
                logger.info(f"🔔 {sym} 触及移动止盈线 {lock_price}，已通知平仓锁利离场！")

    return events


# ==============================================================================
# 6. 单次循环评测 (用于 CLI --check-once 及每小时 cron 集成)
# ==============================================================================

def run_evaluation_cycle(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    pos_store: PositionStore,
    cfg: dict
) -> dict:
    """运行全套风控四重闭环评测"""
    pool_file = Path(cfg.get("resonance_pool_file", DEFAULT_POOL_PATH))
    layout_id = cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
    act_pct = float(cfg.get("trailing_activation_pct", 6.0))
    lock_pct = float(cfg.get("trailing_lock_pct", 1.0))

    logger.info("--- 开始执行交易风控与预警循环评测 ---")
    
    # 0. 自动同步币安实盘活跃持仓 (若已配置凭据)
    binance_sync_info = None
    if bpc:
        b_client = bpc.BinanceFuturesClient(
            cfg.get("binance_api_key", ""),
            cfg.get("binance_api_secret", "")
        )
        if b_client.is_configured():
            binance_sync_info = bpc.sync_binance_positions(b_client, pos_store, layout_id)

    # 1. Feature 1: 新共振标的检测
    res_alerts = check_feature_1_resonance(notifier, state_store, pool_file, layout_id)
    
    # 2. Feature 2: 15m 趋势线突破开多检测
    bo_alerts = check_feature_2_breakout(notifier, state_store, layout_id)

    # 3. Feature 3: 持仓 15m 破线无条件止损检测
    sl_alerts = check_feature_3_stop_loss(notifier, pos_store, layout_id)

    # 4. Feature 4: 持仓动态移动止盈保护检测
    ts_events = check_feature_4_trailing_stops(notifier, pos_store, act_pct, lock_pct)

    result = {
        "timestamp": get_beijing_time_str(),
        "feature_1_resonance_new": len(res_alerts),
        "feature_2_breakout_signals": len(bo_alerts),
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
# 7. 持续守护进程模式 (--daemon)
# ==============================================================================

class TradeAlertDaemon:
    """持续后台守护进程"""

    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.notifier = ServerChanNotifier(cfg.get("serverchan_sendkey", ""), dry_run=dry_run)
        self.state_store = ResonanceStateStore(Path(cfg.get("resonance_state_file", DEFAULT_RESONANCE_STATE_PATH)))
        self.pos_store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
        self.running = True
        self.last_evaluated_15m_bucket = 0
        self.binance_client = None
        if bpc:
            self.binance_client = bpc.BinanceFuturesClient(
                cfg.get("binance_api_key", ""),
                cfg.get("binance_api_secret", "")
            )
            if self.binance_client.is_configured():
                logger.info("✅ 币安合约 API 凭证已就绪，实盘持仓自动同步已激活。")
            else:
                logger.info("ℹ️ 币安 API 凭据未配置，采用本地/手动持仓模式。")

    def handle_signal(self, signum, frame):
        logger.info(f"收到终止信号 ({signum})，正在优雅关闭守护进程...")
        self.running = False

    def start(self):
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        logger.info("=======================================================")
        logger.info("🛡️ “突破追多与移动保本系统”交易风控与信号预警守护进程 (Daemon) 启动")
        logger.info(f"   • TV 布局 ID: {self.cfg.get('tradingview_layout_id')}")
        logger.info(f"   • 移动止盈策略: +{self.cfg.get('trailing_activation_pct')}% 启动，锁定 +{self.cfg.get('trailing_lock_pct')}%")
        logger.info("   • 15m K线判定窗口: mm:00+10s, mm:15+10s, mm:30+10s, mm:45+10s")
        logger.info("   • 动态移动止盈刷新周期: 每 10 秒")
        if self.binance_client and self.binance_client.is_configured():
            logger.info("   • 币安实盘自动同步: 已开启 (自动感知新开仓与平仓)")
        else:
            logger.info("   • 币安实盘自动同步: 未配置 (手动持仓模式)")
        logger.info("=======================================================")

        # 启动时执行一次全量初检
        try:
            run_evaluation_cycle(self.notifier, self.state_store, self.pos_store, self.cfg)
        except Exception as e:
            logger.error(f"初始初检异常: {e}")

        while self.running:
            try:
                now = time.time()

                # 自动双向同步币安实盘活跃持仓
                if self.binance_client and self.binance_client.is_configured():
                    try:
                        bpc.sync_binance_positions(
                            self.binance_client,
                            self.pos_store,
                            self.cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
                        )
                    except Exception as e:
                        logger.debug(f"后台自动同步币安持仓异常: {e}")

                # 15m 周期判定逻辑 (900秒 = 15分钟)
                current_bucket = int(now // 900) * 900
                seconds_into_bucket = now - current_bucket

                # 在每个 15m 刚刚收盘后的 10s~30s 窗口执行 15m K线突破与破线止损判定
                if seconds_into_bucket >= 10 and self.last_evaluated_15m_bucket != current_bucket:
                    logger.info(f"⏰ 进入 15m K线收盘确认窗口 ({get_beijing_time_str()})，启动 Feature 1/2/3 联合校验...")
                    check_feature_1_resonance(
                        self.notifier,
                        self.state_store,
                        Path(self.cfg.get("resonance_pool_file", DEFAULT_POOL_PATH)),
                        self.cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
                    )
                    check_feature_2_breakout(
                        self.notifier,
                        self.state_store,
                        self.cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
                    )
                    check_feature_3_stop_loss(
                        self.notifier,
                        self.pos_store,
                        self.cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID)
                    )
                    self.last_evaluated_15m_bucket = current_bucket

                # 每 10 秒高频巡检 Feature 4: 持仓动态移动止盈
                check_feature_4_trailing_stops(
                    self.notifier,
                    self.pos_store,
                    float(self.cfg.get("trailing_activation_pct", 6.0)),
                    float(self.cfg.get("trailing_lock_pct", 1.0))
                )

                time.sleep(10)
            except Exception as e:
                logger.error(f"守护主循环异常: {e}", exc_info=True)
                time.sleep(5)

        logger.info("🛡️ 交易风控与预警守护进程已安全退出。")


# ==============================================================================
# 8. 命令行入口 (CLI Interface)
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


def main():
    parser = argparse.ArgumentParser(
        description="“突破追多与移动保本系统”四重闭环守护体系 (Trade Alert & Risk Management Daemon)"
    )
    parser.add_argument("--sync-binance", action="store_true", help="立即从币安合约拉取实盘活跃持仓并自动同步")
    parser.add_argument("--check-once", action="store_true", help="执行单次全量评测并输出汇总 (适用于 cron / hourly_sync)")
    parser.add_argument("--daemon", action="store_true", help="启动持续后台守护进程 (10s 移动止盈巡检 + 15m 整点收盘突破/止损)")
    parser.add_argument("--add-pos", nargs=2, metavar=("SYMBOL", "ENTRY_PRICE"), help="手动录入新持仓 (例如: --add-pos YBUSDT 0.54)")
    parser.add_argument("--amount", type=float, default=0.0, help="持仓数量 (与 --add-pos 配合使用)")
    parser.add_argument("--list-pos", action="store_true", help="打印当前未平仓持仓清单与动态止盈状态")
    parser.add_argument("--close-pos", metavar="SYMBOL", help="平仓并归档指定持仓 (例如: --close-pos YBUSDT)")
    parser.add_argument("--dry-run", action="store_true", help="强制以模拟/日志模式运行，不发送真实 Server酱 微信通知")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="指定配置文件路径")

    args = parser.parse_args()
    cfg = load_config(args.config)

    # 0. 币安实盘持仓同步指令: --sync-binance
    if args.sync_binance:
        if not bpc:
            logger.error("未找到 binance_position_client 模块。")
            return
        b_client = bpc.BinanceFuturesClient(
            cfg.get("binance_api_key", ""),
            cfg.get("binance_api_secret", "")
        )
        if not b_client.is_configured():
            print("\n⚠️ 币安 API Key / Secret 未配置，系统运行于手动/离线模式。")
            print("如需启用币安实盘自动同步，请在 config/alerts_config.json 或环境变量 (BINANCE_API_KEY, BINANCE_API_SECRET) 中配置凭据。\n")
            return

        store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
        res = bpc.sync_binance_positions(b_client, store, cfg.get("tradingview_layout_id", DEFAULT_TV_LAYOUT_ID))
        print("\n========================= 币安实盘持仓同步结果 =========================")
        print(f"• 同步状态: {'成功' if res['synced'] else '失败'} ({res['reason']})")
        print(f"• 币安多头持仓数: {res['active_count']}")
        print(f"• 新增同步标的: {', '.join(res['new_added']) if res['new_added'] else '无'}")
        print(f"• 自动归档平仓: {', '.join(res['auto_closed']) if res['auto_closed'] else '无'}")
        if res.get("active_positions"):
            print("-" * 72)
            print("币安实时活跃多头列表:")
            for p in res["active_positions"]:
                print(f"  - {p['symbol']}: 均价 {p['entry_price']} | 数量 {p['amount']} | 标记价 {p['mark_price']} | 未实现盈亏 {p['unrealized_pnl']} USDT ({p['leverage']}x)")
        print("========================================================================\n")
        return

    # 1. 持仓管理指令: --add-pos
    if args.add_pos:
        sym, price_str = args.add_pos
        try:
            entry_price = float(price_str)
        except ValueError:
            logger.error(f"无效的开仓价格: {price_str}")
            sys.exit(1)
        store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
        store.add_position(sym, entry_price, amount=args.amount)
        print(f"✅ 持仓 {sym.upper()} 录入成功！")
        return

    # 2. 持仓管理指令: --close-pos
    if args.close_pos:
        sym = args.close_pos
        store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
        res = store.close_position(sym, reason="MANUAL_CLOSE")
        if res:
            print(f"✅ 持仓 {sym.upper()} 已手动平仓并归档！")
        else:
            print(f"⚠️ 未找到 {sym.upper()} 的开仓记录。")
        return

    # 3. 持仓管理指令: --list-pos
    if args.list_pos:
        store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
        open_pos = store.get_open_positions()
        # 刷新实时市价以呈现最新盈亏
        for p in open_pos:
            curr_p = get_binance_ticker_price(p["symbol"])
            if curr_p > 0:
                p["current_price"] = curr_p
                p["pnl_pct"] = round((curr_p - p["entry_price"]) / p["entry_price"] * 100.0, 2)
        print("\n========================= 当前有效开仓清单 =========================")
        print(format_positions_table(open_pos))
        print("===================================================================\n")
        return

    # 4. 单次评测指令: --check-once
    if args.check_once:
        notifier = ServerChanNotifier(cfg.get("serverchan_sendkey", ""), dry_run=args.dry_run)
        state_store = ResonanceStateStore(Path(cfg.get("resonance_state_file", DEFAULT_RESONANCE_STATE_PATH)))
        pos_store = PositionStore(Path(cfg.get("positions_file", DEFAULT_POSITIONS_PATH)))
        res = run_evaluation_cycle(notifier, state_store, pos_store, cfg)
        print("\n================== 交易预警与风控单次评测报告 ==================")
        print(json.dumps(res, indent=2, ensure_ascii=False))
        print("================================================================\n")
        return

    # 5. 持续守护进程指令: --daemon
    if args.daemon:
        daemon = TradeAlertDaemon(cfg, dry_run=args.dry_run)
        daemon.start()
        return

    # 无参数时打印帮助信息
    parser.print_help()


if __name__ == "__main__":
    main()
