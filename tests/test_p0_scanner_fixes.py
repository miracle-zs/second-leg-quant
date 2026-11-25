#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for P0/P1 fixes in config, binance_client, quant_metrics, tv_trendline_engine, and scan_second_leg_pool."""

from __future__ import annotations

import datetime
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import binance_client as bc  # noqa: E402
import config as cfg  # noqa: E402
import db_store  # noqa: E402
import http_client  # noqa: E402
import quant_metrics as qm  # noqa: E402
import scan_second_leg_pool as ssp  # noqa: E402
import tv_trendline_engine as tve  # noqa: E402


class TestConfigSQLiteConnection(unittest.TestCase):
    def test_busy_timeout_honors_caller_timeout_and_retries_wal_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "wal_retry.sqlite3"
            real_connect = sqlite3.connect
            wal_attempts = {"count": 0}

            class _WrappedConn:
                def __init__(self, inner: sqlite3.Connection) -> None:
                    self._inner = inner

                def execute(self, sql: str, *args: object, **kwargs: object) -> object:
                    if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                        wal_attempts["count"] += 1
                        if wal_attempts["count"] < 3:
                            raise sqlite3.OperationalError("database is locked")
                    return self._inner.execute(sql, *args, **kwargs)

                def __getattr__(self, name: str) -> object:
                    return getattr(self._inner, name)

            with patch.object(cfg.sqlite3, "connect", side_effect=lambda *a, **kw: _WrappedConn(real_connect(*a, **kw))), patch.object(
                cfg.time, "sleep"
            ):
                conn = cfg.open_sqlite_connection(db_path, timeout=2.5)
                try:
                    busy_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                    mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                finally:
                    conn.close()

            self.assertEqual(wal_attempts["count"], 3)
            self.assertEqual(busy_ms, 2500)
            self.assertEqual(mode, "wal")


class TestQuantMetricsFixes(unittest.TestCase):
    def test_db_equity_curve_excludes_transfers_at_or_before_start_and_preserves_bucket_extrema(self) -> None:
        start_ms = 1_800_000_000_000  # divisible by 300_000
        recording = {
            "start_ms": start_ms,
            "started_at": cfg.get_beijing_time_str(start_ms / 1000.0),
            "initial_equity": 1000.0,
        }
        transfers = [(start_ms - 1000, 500.0), (start_ms, 200.0), (start_ms + 1000, 100.0)]

        # 1) Peak inside 5m bucket (1000 -> 1300) followed by drop in same bucket (1100)
        eq1, flow1 = qm.build_db_equity_curve(
            recording,
            start_ms,
            None,
            {"totalMarginBalance": "1300", "totalWalletBalance": "1300"},
            transfers,
            now_ms=start_ms + 20_000,
        )
        self.assertEqual(flow1, 100.0)
        self.assertEqual(eq1[0]["cumulative_pnl"], 0.0)
        self.assertEqual(eq1[-1]["cumulative_pnl"], 200.0)

        prev_report = {"recording": recording, "equity_curve": eq1}
        eq2, _ = qm.build_db_equity_curve(
            recording,
            None,
            prev_report,
            {"totalMarginBalance": "1100", "totalWalletBalance": "1100"},
            transfers,
            now_ms=start_ms + 40_000,
        )
        self.assertEqual(len(eq2), 2)
        self.assertEqual(eq2[-1]["drawdown_usd"], 200.0)

        # 2) Recovery within the same bucket to 1400 must preserve bucket max drawdown (200)
        eq3, _ = qm.build_db_equity_curve(
            recording,
            None,
            {"recording": recording, "equity_curve": eq2},
            {"totalMarginBalance": "1400", "totalWalletBalance": "1400"},
            transfers,
            now_ms=start_ms + 60_000,
        )
        self.assertEqual(len(eq3), 2)
        self.assertEqual(eq3[-1]["drawdown_usd"], 0.0)
        summary, _, _ = qm.calculate_quant_risk_metrics([], [], None, equity_curve_override=eq3)
        self.assertEqual(summary["max_drawdown_usd"], 200.0)

    def test_duration_seconds_none_with_entry_time_and_invalid_closed_at(self) -> None:
        trades = [
            {
                "symbol": "BTCUSDT",
                "realized_pnl": 10.0,
                "fee": 0.0,
                "net_pnl": 10.0,
                "pnl_pct": 1.0,
                "entry_time": "2026-11-20 10:00:00",
                "closed_at": "2026-11-20 11:00:00",
                "closed_ts": 1_795_172_400_000,
                "duration_seconds": None,
            }
        ]
        summary, _, _ = qm.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["avg_hold_duration_seconds"], 0)
        self.assertEqual(summary["avg_hold_duration_str"], "--")

        with self.assertRaises(ValueError):
            qm._resolve_trade_beijing_date({"closed_at": "2026-99-99 invalid"})

    def test_build_pnl_distribution_symmetric_breakeven_bounds(self) -> None:
        trades = [
            {"trade_id": "1", "symbol": "A", "pnl_pct": -0.0001, "roe_pct": -0.0001, "net_pnl": 0.0, "duration_seconds": 60, "closed_at": "2026-11-20 10:00:00", "outcome": "BREAKEVEN"},
            {"trade_id": "2", "symbol": "B", "pnl_pct": 0.0001, "roe_pct": 0.0001, "net_pnl": 0.0, "duration_seconds": 60, "closed_at": "2026-11-20 10:01:00", "outcome": "BREAKEVEN"},
            {"trade_id": "3", "symbol": "C", "pnl_pct": 0.0002, "roe_pct": 0.0002, "net_pnl": 0.1, "duration_seconds": 60, "closed_at": "2026-11-20 10:02:00", "outcome": "WIN"},
        ]
        dist = qm.build_pnl_distribution(trades)
        by_label = {b["label"]: b for b in dist["bins"]}
        self.assertEqual(by_label["保本 (0%)"]["trades"], ["A", "B"])
        self.assertEqual(by_label["0% ~ +2%"]["trades"], ["C"])


class TestBinanceClientFixes(unittest.TestCase):
    def test_server_time_monotonic_and_single_flight(self) -> None:
        client = bc.BinanceFuturesClient("k", "s", time_sync_ttl=600.0)
        calls = {"count": 0}
        barrier = threading.Barrier(4)

        def fake_fetch(url: str, **kwargs: object) -> dict[str, int]:
            calls["count"] += 1
            time.sleep(0.02)
            return {"serverTime": 1_800_000_000_000}

        with patch.object(bc, "fetch_json_with_retry", side_effect=fake_fetch):
            results: list[int] = []

            def worker() -> None:
                barrier.wait()
                results.append(client.get_server_time())

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(calls["count"], 1)
            self.assertEqual(len(results), 4)

            # Wall-clock jump backward must not shift cached server time
            with patch.object(bc.time, "time", return_value=100.0):
                cached_ms = client.get_server_time()
            self.assertGreaterEqual(cached_ms, 1_800_000_000_000)
            self.assertEqual(calls["count"], 1)

    def test_malformed_code_and_malformed_kline_raise_binance_api_error(self) -> None:
        client = bc.BinanceFuturesClient("k", "s")
        with patch.object(client, "get_server_time", return_value=1_800_000_000_000):
            for bad_code in ("BAD_ERR", True):
                with patch.object(bc.http_client, "request_json", return_value={"code": bad_code, "msg": "oops"}):
                    with self.assertRaises(bc.BinanceAPIError):
                        client._send_signed_request("/fapi/v2/account", max_retries=0)

            with patch.object(client, "fetch_klines", return_value=[[123, "bad"]]):
                with self.assertRaises(bc.BinanceAPIError):
                    client.get_closed_15m_candle("BTCUSDT")


class TestTVTrendlineEngineFixes(unittest.TestCase):
    def test_breakout_overflow_direction_and_target_time_alignment(self) -> None:
        # Upward exponential overflow (p2 >> p1): resistance astronomically above price -> is_runaway must be False
        up_line = {"id": "UP", "p1": 1.0, "t1": 1000, "p2": 100.0, "t2": 1010}
        candle = {
            "open": 10.0,
            "close": 11.0,
            "open_time": 100000,
            "close_time": 100899,
            "prev_candle": {"close": 9.0, "open_time": 99100, "close_time": 99999},
        }
        res_up = tve.check_symbol_breakout("YBUSDT", up_line, candle=candle)
        self.assertFalse(res_up["is_runaway"])
        self.assertFalse(res_up["is_breakout"])

        # Downward collapse overflow (p2 << p1): resistance collapsed to 0 -> is_runaway True
        down_line = {"id": "DOWN", "p1": 100.0, "t1": 1000, "p2": 1.0, "t2": 1010}
        res_down = tve.check_symbol_breakout("YBUSDT", down_line, candle=candle)
        self.assertTrue(res_down["is_runaway"])
        self.assertFalse(res_down["is_breakout"])

        # target_time shifts prev_t by the candle bar step
        lin_line = {"id": "STEP", "p1": 100.0, "t1": 0, "p2": 50.0, "t2": 900}
        c_step = {
            "open": 12.0,
            "close": 13.0,
            "open_time": 900,
            "close_time": 1799,
            "prev_candle": {"close": 24.0, "open_time": 0, "close_time": 899},
        }
        res_shifted = tve.check_symbol_breakout("YBUSDT", lin_line, candle=c_step, target_time=1800)
        self.assertAlmostEqual(res_shifted["prev_res"], 50.0, places=5)
        self.assertAlmostEqual(res_shifted["res_price"], 25.0, places=5)

    def test_is_valid_15m_trendline_enforces_anchor_recency_even_with_server_update_time(self) -> None:
        now_ts = 1_800_000_000.0
        ancient_anchor_recent_edit = {
            "id": "OLD",
            "interval": "15",
            "t1": now_ts - 90 * 3600,
            "t2": now_ts - 80 * 3600,
            "server_update_time": now_ts - 60,
        }
        self.assertFalse(tve.is_valid_15m_trendline(ancient_anchor_recent_edit, current_time=now_ts))

    def test_get_chart_token_single_flight_and_drawings_cache_and_schema_guard(self) -> None:
        calls = {"tok": 0}
        barrier = threading.Barrier(4)

        def fake_tok_req(url: str, **kwargs: object) -> dict[str, str]:
            calls["tok"] += 1
            time.sleep(0.02)
            return {"token": "tok-123"}

        with patch.dict(tve._TOKEN_CACHE, {}, clear=True), patch.dict(
            tve._TOKEN_FETCH_LOCKS, {}, clear=True
        ), patch.object(tve, "request_json", side_effect=fake_tok_req):
            out: list[str] = []

            def worker() -> None:
                barrier.wait()
                out.append(tve.get_chart_token("layout-single-flight"))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(calls["tok"], 1)
            self.assertEqual(out, ["tok-123"] * 4)

        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            cache_file = runs_dir / "tradingview_drawings_L1.json"
            cached_payload = {"drawings_by_symbol": {"YBUSDT": [{"id": "CACHED"}]}}
            cache_file.write_text(json.dumps(cached_payload), encoding="utf-8")

            with patch.object(tve, "RUNS_DIR", runs_dir), patch.object(
                tve, "get_chart_token", return_value="tok"
            ) as mock_tok, patch.object(tve, "get_layout_chart_ids", return_value=["_shared"]), patch.object(
                tve,
                "request_json",
                return_value={
                    "payload": {
                        "sources": {
                            "bad_lw": {
                                "symbol": "BINANCE:YBUSDT.P",
                                "state": {
                                    "type": "LineToolTrendLine",
                                    "points": [{"time_t": 100, "price": 1.0}, {"time_t": 200, "price": 0.9}],
                                    "state": {"interval": "15", "linewidth": "2px"},
                                },
                            },
                            "good": {
                                "symbol": "BINANCE:YBUSDT.P",
                                "state": {
                                    "type": "LineToolTrendLine",
                                    "points": [{"time_t": 100, "price": 1.0}, {"time_t": 200, "price": 0.9}],
                                    "state": {"interval": "15", "linewidth": 2},
                                },
                            },
                        }
                    }
                },
            ):
                # cache_ttl_sec + force_refresh=False returns disk cache
                res_cached = tve.get_layout_drawings("L1", force_refresh=False, cache_ttl_sec=60.0)
                self.assertEqual(res_cached["YBUSDT"][0]["id"], "CACHED")
                mock_tok.assert_not_called()

                # force_refresh=True bypasses disk cache and refreshes token
                res_live = tve.get_layout_drawings("L1", force_refresh=True, save_cache=False, cache_ttl_sec=60.0)
                mock_tok.assert_called_once_with(layout_id="L1", force_refresh=True)
                self.assertEqual([d["id"] for d in res_live["YBUSDT"]], ["good"])

            # All trendlines malformed -> RuntimeError and existing cache preserved
            cache_file.write_text(json.dumps(cached_payload), encoding="utf-8")
            with patch.object(tve, "RUNS_DIR", runs_dir), patch.object(
                tve, "get_chart_token", return_value="tok"
            ), patch.object(tve, "get_layout_chart_ids", return_value=["_shared"]), patch.object(
                tve,
                "request_json",
                return_value={
                    "payload": {
                        "sources": {
                            "broken": {
                                "symbol": "BINANCE:YBUSDT.P",
                                "state": {"type": "LineToolTrendLine", "points": []},
                            }
                        }
                    }
                },
            ):
                with self.assertRaises(RuntimeError):
                    tve.get_layout_drawings("L1", force_refresh=True, save_cache=True)
                self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8")), cached_payload)


class TestScannerPoolFixes(unittest.TestCase):
    def test_cmc_400_non_symbol_error_fails_loudly_without_bisection(self) -> None:
        client = http_client.HttpClient(max_per_host=2)
        calls = {"count": 0}
        try:
            def fake_req(url: str, **kwargs: object) -> object:
                calls["count"] += 1
                raise http_client.HttpError(
                    "HTTP 400",
                    status_code=400,
                    body='{"status":{"error_code":1001,"error_message":"This API Key is invalid."}}',
                    url=url,
                )

            with patch.object(client, "request_json", side_effect=fake_req):
                with self.assertRaises(http_client.HttpError):
                    ssp._fetch_cmc_batch(["BTC", "ETH", "SOL", "ADA"], "bad-key", client)
            self.assertEqual(calls["count"], 1)
        finally:
            client.close()

    def test_server_time_min_mcap_update_and_single_symbol_kline_skip(self) -> None:
        fixed_now = 1_795_500_000.0
        server_ms = int(fixed_now * 1000)
        # Make local wall clock intentionally +2h ahead; server time must govern kline closure
        skewed_local = fixed_now + 7200.0
        closed_times = [server_ms - (12 - i) * 3_600_000 for i in range(12)]
        kline_rows = [
            [cm - 3_599_999, "1.0", "1.0", "1.0", "1.0", "100", cm, "100", 1]
            for cm in closed_times
        ] + [[server_ms - 60_000, "1.0", "1.0", "1.0", "999.0", "100", server_ms + 300_000, "100", 1]]

        symbols = [
            {
                "symbol": "ALPHAUSDT",
                "base": "ALPHA",
                "price": 1.0,
                "low_price": 0.4,
                "circulating_supply": 45_000_000.0,
                "market_cap": 45_000_000.0,
                "volume_usd": 10_000_000.0,
            },
            {
                "symbol": "BADKLINEUSDT",
                "base": "BADKLINE",
                "price": 1.0,
                "low_price": 1.0,
                "circulating_supply": 10_000_000.0,
                "market_cap": 10_000_000.0,
                "volume_usd": 5_000_000.0,
            },
        ]

        def fake_req(url: str, **kwargs: object) -> object:
            if url.endswith("/fapi/v1/time"):
                return {"serverTime": server_ms}
            if url.endswith("/fapi/v1/exchangeInfo"):
                return {
                    "symbols": [
                        {
                            "symbol": s["symbol"],
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                            "underlyingType": "COIN",
                            "underlyingSubType": ["Crypto"],
                        }
                        for s in symbols
                    ]
                }
            if url.endswith("/fapi/v1/ticker/24hr"):
                return [
                    {
                        "symbol": s["symbol"],
                        "quoteVolume": str(s["volume_usd"]),
                        "priceChangePercent": "1.0",
                        "lastPrice": str(s["price"]),
                        "highPrice": "1.1",
                        "lowPrice": str(s["low_price"]),
                    }
                    for s in symbols
                ]
            if url.endswith("/fapi/v1/premiumIndex"):
                return [{"symbol": s["symbol"], "lastFundingRate": "0.0001"} for s in symbols]
            if url.endswith("/fapi/v1/fundingInfo"):
                return []
            if url.startswith(ssp.CMC_QUOTES_URL):
                return {
                    "data": {
                        s["base"]: {
                            "circulating_supply": s["circulating_supply"],
                            "quote": {"USD": {"price": s["price"], "market_cap": s["market_cap"]}},
                        }
                        for s in symbols
                    }
                }
            if "/fapi/v1/klines?" in url:
                sym = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["symbol"][0]
                if sym == "BADKLINEUSDT":
                    raise http_client.HttpError("HTTP 400", status_code=400, url=url)
                return kline_rows
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scanner.sqlite3"
            # Existing history 12h old (<= 86400s) with higher min_mcap_usd (25M); new 24h low is 0.4 * 45M = 18M
            prev_ts = skewed_local - 12 * 3600.0
            db_store.save_sub30m_history(
                db_path,
                {
                    "ALPHAUSDT": {
                        "symbol": "ALPHAUSDT",
                        "base": "ALPHA",
                        "last_sub_30m_ts": prev_ts,
                        "last_sub_30m_iso": "2026-11-25T00:00:00Z",
                        "min_mcap_usd": 25_000_000.0,
                    }
                },
            )
            out = io.StringIO()
            with patch.object(bc, "_PUBLIC_CLIENT", None), patch.object(
                ssp, "get_env_setting", return_value="test-cmc"
            ), patch.object(ssp.time, "time", return_value=skewed_local), patch.object(
                ssp.HttpClient, "request_json", side_effect=fake_req
            ), redirect_stdout(out):
                candidates = ssp.scan_pool(database=db_path)

            persisted = db_store.load_sub30m_history(db_path)

        self.assertEqual([c["symbol"] for c in candidates], ["ALPHAUSDT"])
        # Unclosed bar (999.0) must still be excluded by server_ms even with +2h local clock skew
        self.assertAlmostEqual(candidates[0]["ema5"], 1.0, places=6)
        self.assertAlmostEqual(persisted["ALPHAUSDT"]["min_mcap_usd"], 18_000_000.0, places=2)
        self.assertEqual(persisted["ALPHAUSDT"]["last_sub_30m_ts"], prev_ts)
        self.assertIn("跳过 1h K线拉取失败标的", out.getvalue())


if __name__ == "__main__":
    unittest.main()
