#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安合约实时持仓同步客户端 (Binance USDⓈ-M Futures Position Client)
==============================================================================
功能:
1. 通过 HMAC-SHA256 签名鉴权请求币安永续合约持仓风险接口 (/fapi/v2/positionRisk)。
2. 过滤提取用户当前真实活跃多头持仓 (positionAmt > 0)。
3. 与本地风控持仓文件 (runs/trade_positions.json) 执行双向自动同步:
   - 币安出现新持仓 -> 自动录入实际开仓均价 entryPrice 与持仓数量 amount，
     并自动匹配 TradingView 专属布局 (ig0LuDL8) 中的下行趋势线。
   - 币安已平仓 (positionAmt == 0) -> 自动归档至 history 历史记录。
4. API Key 缺省时优雅降级为模拟/离线模式，保障系统高可用。
==============================================================================
"""

import os
import sys
import time
import json
import hmac
import hashlib
import logging
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# 路径基准
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 尝试导入 TV 画线引擎以自动关联趋势线
try:
    import tv_trendline_engine as tve
except ImportError:
    try:
        from scripts import tv_trendline_engine as tve
    except ImportError:
        tve = None

logger = logging.getLogger("binance_client")

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
POSITION_RISK_ENDPOINT = "/fapi/v2/positionRisk"
SERVER_TIME_ENDPOINT = "/fapi/v1/time"


def clean_symbol(raw: str) -> str:
    """标准化币种符号，去除 BINANCE: 前缀与 .P 后缀"""
    s = raw.strip().upper()
    if ":" in s:
        s = s.split(":")[-1]
    if s.endswith(".P"):
        s = s[:-2]
    return s


class BinanceFuturesClient:
    """币安 USDⓈ-M 合约 API 客户端"""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = BINANCE_FUTURES_BASE_URL,
        timeout: int = 8
    ):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls, env_path: Path = PROJECT_ROOT / ".env") -> "BinanceFuturesClient":
        """优先从项目根目录 .env 文件及环境变量中自动初始化客户端"""
        k = os.environ.get("BINANCE_API_KEY", "")
        s = os.environ.get("BINANCE_API_SECRET", "")
        if (not k or not s) and env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            pk, pv = line.split("=", 1)
                            pk = pk.strip()
                            pv = pv.strip().strip("\"'")
                            if pk == "BINANCE_API_KEY" and not k:
                                k = pv
                            elif pk == "BINANCE_API_SECRET" and not s:
                                s = pv
            except Exception:
                pass
        return cls(api_key=k, api_secret=s)

    def is_configured(self) -> bool:
        """检查 API 凭证是否已完整配置"""
        return bool(self.api_key and self.api_secret)

    def _generate_signature(self, query_string: str) -> str:
        """根据 Query String 计算 HMAC-SHA256 签名"""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def get_server_time(self) -> int:
        """获取币安服务器毫秒级绝对时间戳，避免本地时钟偏差触发 -1021 错误"""
        url = f"{self.base_url}{SERVER_TIME_ENDPOINT}"
        req = urllib.request.Request(url, headers={"User-Agent": "TradeAlertDaemon/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return int(data.get("serverTime", int(time.time() * 1000)))
        except Exception as e:
            logger.debug(f"拉取币安服务器时间异常，使用本地时间: {e}")
            return int(time.time() * 1000)

    def get_account_positions(self) -> List[dict]:
        """
        请求 /fapi/v2/positionRisk 获取账户所有合约头寸信息。
        需要有效的 API-Key 与 HMAC-SHA256 签名。
        """
        if not self.is_configured():
            logger.warning("币安 API 凭据未配置，无法拉取实盘持仓。")
            return []

        server_ts = self.get_server_time()
        params = {
            "timestamp": server_ts,
            "recvWindow": 5000,
        }
        query_str = urllib.parse.urlencode(params)
        signature = self._generate_signature(query_str)
        full_query = f"{query_str}&signature={signature}"
        url = f"{self.base_url}{POSITION_RISK_ENDPOINT}?{full_query}"

        req = urllib.request.Request(
            url,
            headers={
                "X-MBX-APIKEY": self.api_key,
                "User-Agent": "TradeAlertDaemon/1.0",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            method="GET"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict) and data.get("code"):
                    logger.error(f"币安持仓接口返回错误: [{data.get('code')}] {data.get('msg')}")
                    return []
                return []
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8") if e.fp else ""
            logger.error(f"币安 HTTP 请求失败 ({e.code}): {err_body}")
            return []
        except Exception as e:
            logger.error(f"请求币安持仓风险接口网络异常: {e}")
            return []

    def get_active_long_positions(self) -> List[dict]:
        """
        筛选并标准化当前有效开仓的多头持仓 (positionAmt > 0)。
        """
        raw_list = self.get_account_positions()
        active_longs = []

        for p in raw_list:
            amt = float(p.get("positionAmt", 0.0))
            if amt > 0:  # 仅追踪多头多单
                sym = clean_symbol(p.get("symbol", ""))
                entry_p = float(p.get("entryPrice", 0.0))
                mark_p = float(p.get("markPrice", entry_p))
                pnl = float(p.get("unRealizedProfit", 0.0))
                leverage = int(p.get("leverage", 1))
                active_longs.append({
                    "symbol": sym,
                    "amount": amt,
                    "entry_price": entry_p,
                    "mark_price": mark_p,
                    "unrealized_pnl": pnl,
                    "leverage": leverage,
                    "position_side": p.get("positionSide", "BOTH"),
                    "update_time": int(p.get("updateTime", 0)),
                    "notional": float(p.get("notional", amt * mark_p))
                })

        return active_longs


def sync_binance_positions(
    client: BinanceFuturesClient,
    pos_store: Any,
    layout_id: str = "ig0LuDL8"
) -> dict:
    """
    自动双向同步币安实盘持仓到本地 PositionStore:
    1. 发现新开多单 -> 自动录入实际开仓价/持仓量，并自动匹配 TradingView 对数趋势线。
    2. 发现持仓已平仓 (positionAmt == 0) -> 自动归档至 history 历史记录。
    3. 已存在的持仓 -> 自动更新持仓均价与持仓数量 (防加仓/减仓数据滞后)。
    """
    if not client.is_configured():
        logger.info("ℹ️ 币安 API 未配置，维持手动/本地持仓管理模式。")
        return {
            "synced": False,
            "reason": "API_NOT_CONFIGURED",
            "active_count": len(pos_store.get_open_positions()),
            "new_added": [],
            "auto_closed": [],
        }

    active_longs = client.get_active_long_positions()
    binance_symbols = {p["symbol"]: p for p in active_longs}

    # 获取当前 TV 云端画线用于自动挂载对数趋势线
    drawings_map = {}
    if tve and hasattr(tve, "get_layout_drawings"):
        try:
            drawings_map = tve.get_layout_drawings(layout_id)
        except Exception as e:
            logger.debug(f"同步拉取 TV 画线异常: {e}")

    new_added = []
    auto_closed = []

    # 1. 遍历币安当前持仓 -> 写入或刷新本地持仓
    current_open_positions = {p["symbol"]: p for p in pos_store.get_open_positions()}

    for sym, b_pos in binance_symbols.items():
        if sym not in current_open_positions:
            # 自动匹配该币种在 TradingView ig0LuDL8 中的趋势线
            matched_trendline = None
            for k, lines in drawings_map.items():
                if sym == clean_symbol(k):
                    matched_trendline = lines[0] if isinstance(lines, list) and lines else lines
                    break

            # 自动录入新持仓
            notes = f"Binance Real-Time Auto-Sync (Leverage: {b_pos['leverage']}x)"
            new_pos = pos_store.add_position(
                symbol=sym,
                entry_price=b_pos["entry_price"],
                amount=b_pos["amount"],
                trendline=matched_trendline,
                notes=notes
            )
            new_pos["source"] = "BINANCE_SYNC"
            new_pos["leverage"] = b_pos["leverage"]
            new_added.append(sym)
            logger.info(f"⚡ [币安实盘自动同步] 发现新开多仓: {sym} @ {b_pos['entry_price']} (数量: {b_pos['amount']})")
        else:
            # 更新已有仓位的数据 (如加仓拉低均价)
            local_p = current_open_positions[sym]
            if abs(local_p["entry_price"] - b_pos["entry_price"]) > 1e-6 or abs(local_p["amount"] - b_pos["amount"]) > 1e-6:
                logger.info(f"🔄 [币安实盘自动同步] 更新持仓均价/数量: {sym} {local_p['entry_price']} -> {b_pos['entry_price']}")
                local_p["entry_price"] = b_pos["entry_price"]
                local_p["amount"] = b_pos["amount"]
                local_p["source"] = "BINANCE_SYNC"
                pos_store.save()

    # 2. 检查本地中由币安同步创建但币安已平仓 (positionAmt == 0) 的仓位 -> 自动归档
    for p in pos_store.get_open_positions():
        sym = p["symbol"]
        # 仅针对由币安同步管理的仓位 (或者全部开仓)，若币安端已无此仓位则自动平仓
        if p.get("source") == "BINANCE_SYNC" and sym not in binance_symbols:
            closed_p = pos_store.close_position(sym, reason="BINANCE_POSITION_CLOSED")
            if closed_p:
                auto_closed.append(sym)
                logger.info(f"🏁 [币安实盘自动同步] 监测到币安已平仓，自动归档: {sym}")

    pos_store.save()

    return {
        "synced": True,
        "reason": "OK",
        "active_count": len(active_longs),
        "new_added": new_added,
        "auto_closed": auto_closed,
        "active_positions": active_longs
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_sec = os.environ.get("BINANCE_API_SECRET", "")
    client = BinanceFuturesClient(api_key, api_sec)
    print(f"Binance Futures Client: Configured={client.is_configured()}")
    if client.is_configured():
        server_ts = client.get_server_time()
        print(f"Server Time: {server_ts}")
        positions = client.get_active_long_positions()
        print(f"Active Long Positions ({len(positions)}):")
        for p in positions:
            print(f"  - {p['symbol']}: {p['amount']} @ {p['entry_price']} (mark: {p['mark_price']}, pnl: {p['unrealized_pnl']})")
    else:
        print("Tip: Set BINANCE_API_KEY and BINANCE_API_SECRET env variables to test live positions.")
