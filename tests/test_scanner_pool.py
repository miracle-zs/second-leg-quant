#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for scripts/scan_second_leg_pool.py and scripts/sync_all.py."""

from __future__ import annotations

import datetime
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import binance_client as bc  # noqa: E402
from config import BEIJING_TZ  # noqa: E402
import db_store  # noqa: E402
import scan_second_leg_pool as ssp  # noqa: E402
import sync_all  # noqa: E402


def _make_klines(
    closes: list[float],
    close_times_ms: list[int],
) -> list[list[object]]:
    """Build minimal Binance futures kline rows matching [open_t, o, h, l, c, vol, close_t, ...]."""
    assert len(closes) == len(close_times_ms)
    rows: list[list[object]] = []
    for close_p, close_ms in zip(closes, close_times_ms):
        open_ms = close_ms - 3_599_999
        rows.append([
            open_ms,
            str(close_p),
            str(close_p),
            str(close_p),
            str(close_p),
            "1000.0",
            close_ms,
            "1000.0",
            10,
        ])
    return rows


def _make_fake_request_json(
    *,
    symbols: list[dict[str, object]],
    funding_intervals: dict[str, int] | None = None,
    klines_by_symbol: dict[str, list[list[object]]] | None = None,
    server_time_ms: int = 1_795_500_000_000,
):
    """Build a deterministic HttpClient.request_json stub covering Binance & CMC endpoints."""
    funding_map = funding_intervals or {}
    klines_map = klines_by_symbol or {}

    exchange_info = {
        "symbols": [
            {
                "symbol": item["symbol"],
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "underlyingType": "COIN",
                "underlyingSubType": ["Crypto"],
            }
            for item in symbols
        ]
    }
    tickers_24h = [
        {
            "symbol": item["symbol"],
            "quoteVolume": str(item["volume_usd"]),
            "priceChangePercent": str(item.get("price_chg", 5.0)),
            "lastPrice": str(item["price"]),
            "highPrice": str(item.get("high_price", float(item["price"]) * 1.05)),
            "lowPrice": str(item.get("low_price", item["price"])),
        }
        for item in symbols
    ]
    premiums = [
        {
            "symbol": item["symbol"],
            "lastFundingRate": str(item.get("funding_rate", 0.0001)),
        }
        for item in symbols
    ]
    funding_info = [
        {"symbol": sym, "fundingIntervalHours": interval}
        for sym, interval in funding_map.items()
    ]
    cmc_quotes = {
        "data": {
            str(item["base"]).upper(): {
                "circulating_supply": float(item["circulating_supply"]),
                "quote": {
                    "USD": {
                        "price": float(item["price"]),
                        "market_cap": float(item["market_cap"]),
                    }
                },
            }
            for item in symbols
        }
    }

    def _fake_request_json(url: str, **kwargs: object) -> object:
        if url.endswith("/fapi/v1/time"):
            return {"serverTime": server_time_ms}
        if url.endswith("/fapi/v1/exchangeInfo"):
            return exchange_info
        if url.endswith("/fapi/v1/ticker/24hr"):
            return tickers_24h
        if url.endswith("/fapi/v1/premiumIndex"):
            return premiums
        if url.endswith("/fapi/v1/fundingInfo"):
            return funding_info
        if url.startswith(ssp.CMC_QUOTES_URL):
            return cmc_quotes
        if "/fapi/v1/klines?" in url:
            parsed = urllib.parse.urlparse(url)
            sym = urllib.parse.parse_qs(parsed.query)["symbol"][0]
            return klines_map[sym]
        raise AssertionError(f"Unexpected URL requested in test: {url}")

    return _fake_request_json


class TestCalcEma(unittest.TestCase):
    """1. calc_ema edge cases and exact scalar recurrence."""

    def test_returns_none_when_fewer_bars_than_period(self) -> None:
        self.assertIsNone(ssp.calc_ema([], 5))
        self.assertIsNone(ssp.calc_ema([1.0, 2.0, 3.0, 4.0], 5))

    def test_invalid_non_positive_period_raises(self) -> None:
        with self.assertRaises(ValueError):
            ssp.calc_ema([1.0, 2.0], 0)
        with self.assertRaises(ValueError):
            ssp.calc_ema([1.0, 2.0], -3)

    def test_matches_exact_scalar_ema_on_known_sequences(self) -> None:
        # Exact length == period returns pure SMA seed
        self.assertAlmostEqual(ssp.calc_ema([2.0, 4.0, 6.0], 3), 4.0, places=12)

        # period=3 -> alpha = 0.5; seed=(10+12+14)/3=12 -> 14 -> 18
        self.assertAlmostEqual(
            ssp.calc_ema([10.0, 12.0, 14.0, 16.0, 22.0], 3),
            18.0,
            places=12,
        )

        # period=5 -> alpha = 2/6 = 1/3; seed=12 -> 13 -> 15
        self.assertAlmostEqual(
            ssp.calc_ema([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 19.0], 5),
            15.0,
            places=12,
        )


class TestScannerPool(unittest.TestCase):
    """2-5. Unclosed klines, fundingIntervalHours, sub30m_history, resonance_state & snapshot."""

    FIXED_NOW_TS = 1_795_500_000.0
    FIXED_NOW_MS = int(FIXED_NOW_TS * 1000)

    def _run_scan(
        self,
        db_path: Path,
        *,
        symbols: list[dict[str, object]],
        funding_intervals: dict[str, int] | None = None,
        klines_by_symbol: dict[str, list[list[object]]] | None = None,
        now_ts: float = FIXED_NOW_TS,
    ) -> tuple[list[dict[str, object]], str]:
        fake_req = _make_fake_request_json(
            symbols=symbols,
            funding_intervals=funding_intervals,
            klines_by_symbol=klines_by_symbol,
            server_time_ms=int(now_ts * 1000),
        )
        stdout_buf = io.StringIO()
        with patch.object(bc, "_PUBLIC_CLIENT", None), patch.object(
            ssp, "get_env_setting", return_value="test-cmc-key"
        ), patch.object(
            ssp.time, "time", return_value=now_ts
        ), patch.object(ssp.HttpClient, "request_json", side_effect=fake_req), redirect_stdout(
            stdout_buf
        ):
            candidates = ssp.scan_pool(database=db_path)
        return candidates, stdout_buf.getvalue()

    def test_unclosed_1h_candle_excluded_and_under_10_closed_bars_skipped(self) -> None:
        # ALPHAUSDT: 11 closed bars [2.0]*10 + [1.0], plus 1 trailing unclosed bar at 1000.0.
        # If the unclosed bar were NOT dropped, ema5 > ema10 would be True; dropping it keeps ema5 < ema10.
        alpha_closed_closes = [2.0] * 10 + [1.0]
        alpha_closes = alpha_closed_closes + [1000.0]
        alpha_close_times = [self.FIXED_NOW_MS - (11 - i) * 3_600_000 for i in range(11)] + [
            self.FIXED_NOW_MS + 60_000
        ]

        # BETAUSDT: 10 bars total where the last bar is unclosed -> only 9 closed bars (< 10) -> skipped.
        beta_closes = [5.0] * 10
        beta_close_times = [self.FIXED_NOW_MS - (9 - i) * 3_600_000 for i in range(9)] + [
            self.FIXED_NOW_MS + 120_000
        ]

        symbols = [
            {
                "symbol": "ALPHAUSDT",
                "base": "ALPHA",
                "price": 1.0,
                "circulating_supply": 10_000_000.0,
                "market_cap": 10_000_000.0,
                "volume_usd": 5_000_000.0,
            },
            {
                "symbol": "BETAUSDT",
                "base": "BETA",
                "price": 1.0,
                "circulating_supply": 12_000_000.0,
                "market_cap": 12_000_000.0,
                "volume_usd": 5_000_000.0,
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "scanner.sqlite3"
            candidates, _ = self._run_scan(
                db_path,
                symbols=symbols,
                klines_by_symbol={
                    "ALPHAUSDT": _make_klines(alpha_closes, alpha_close_times),
                    "BETAUSDT": _make_klines(beta_closes, beta_close_times),
                },
            )

        by_sym = {c["symbol"]: c for c in candidates}
        expected_ema5 = round(ssp.calc_ema(alpha_closed_closes, 5), 8)
        expected_ema10 = round(ssp.calc_ema(alpha_closed_closes, 10), 8)
        self.assertEqual(by_sym["ALPHAUSDT"]["ema5"], expected_ema5)
        self.assertEqual(by_sym["ALPHAUSDT"]["ema10"], expected_ema10)
        self.assertFalse(by_sym["ALPHAUSDT"]["is_ema_bull"])

        self.assertIsNone(by_sym["BETAUSDT"]["ema5"])
        self.assertIsNone(by_sym["BETAUSDT"]["ema10"])
        self.assertFalse(by_sym["BETAUSDT"]["is_ema_bull"])
        self.assertIsNone(by_sym["BETAUSDT"]["ema_spread_pct"])

    def test_invalid_funding_interval_hours_raises_value_error(self) -> None:
        symbols = [
            {
                "symbol": "ALPHAUSDT",
                "base": "ALPHA",
                "price": 1.0,
                "circulating_supply": 10_000_000.0,
                "market_cap": 10_000_000.0,
                "volume_usd": 5_000_000.0,
            }
        ]
        for bad_interval in (0, -4):
            with self.subTest(fundingIntervalHours=bad_interval):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = Path(tmpdir) / "scanner.sqlite3"
                    with self.assertRaisesRegex(ValueError, "Invalid fundingIntervalHours"):
                        self._run_scan(
                            db_path,
                            symbols=symbols,
                            funding_intervals={"ALPHAUSDT": bad_interval},
                        )

    def test_sub30m_history_7d_tracking_persisted_without_jump_back(self) -> None:
        # 12 closed bars so EMA evaluation succeeds cleanly
        closed_times = [self.FIXED_NOW_MS - (12 - i) * 3_600_000 for i in range(12)]
        klines = _make_klines([1.0] * 12, closed_times)

        symbols = [
            # mcap=45M (>30M, <=80M), but 24h low_price=0.5 -> low mcap=22.5M (<=30M)
            # and persisted gap is 2 days (>86400s): must refresh to exact now_ts (NOT now_ts - 3600).
            {
                "symbol": "ALPHAUSDT",
                "base": "ALPHA",
                "price": 1.0,
                "low_price": 0.5,
                "circulating_supply": 45_000_000.0,
                "market_cap": 45_000_000.0,
                "volume_usd": 10_000_000.0,
            },
            # mcap=50M, low_price=0.8 -> low mcap=40M (>30M), kept via 3-day-old history (4.0 days left).
            {
                "symbol": "BETAUSDT",
                "base": "BETA",
                "price": 1.0,
                "low_price": 0.8,
                "circulating_supply": 50_000_000.0,
                "market_cap": 50_000_000.0,
                "volume_usd": 10_000_000.0,
            },
        ]

        alpha_prev_ts = self.FIXED_NOW_TS - 2 * 86400.0
        beta_prev_ts = self.FIXED_NOW_TS - 3 * 86400.0
        expired_ts = self.FIXED_NOW_TS - 8 * 86400.0

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "scanner.sqlite3"
            db_store.save_sub30m_history(
                db_path,
                {
                    "ALPHAUSDT": {
                        "symbol": "ALPHAUSDT",
                        "base": "ALPHA",
                        "last_sub_30m_ts": alpha_prev_ts,
                        "last_sub_30m_iso": "2026-11-23T00:00:00Z",
                        "min_mcap_usd": 20_000_000.0,
                    },
                    "BETAUSDT": {
                        "symbol": "BETAUSDT",
                        "base": "BETA",
                        "last_sub_30m_ts": beta_prev_ts,
                        "last_sub_30m_iso": "2026-11-22T00:00:00Z",
                        "min_mcap_usd": 25_000_000.0,
                    },
                    "EXPIREDUSDT": {
                        "symbol": "EXPIREDUSDT",
                        "base": "EXPIRED",
                        "last_sub_30m_ts": expired_ts,
                        "last_sub_30m_iso": "2026-11-17T00:00:00Z",
                        "min_mcap_usd": 15_000_000.0,
                    },
                },
            )

            candidates, _ = self._run_scan(
                db_path,
                symbols=symbols,
                klines_by_symbol={"ALPHAUSDT": klines, "BETAUSDT": klines},
            )
            persisted = db_store.load_sub30m_history(db_path)

        by_sym = {c["symbol"]: c for c in candidates}
        self.assertTrue(by_sym["ALPHAUSDT"]["is_overflow_7d"])
        self.assertEqual(by_sym["ALPHAUSDT"]["overflow_days_left"], 7.0)
        self.assertEqual(persisted["ALPHAUSDT"]["last_sub_30m_ts"], self.FIXED_NOW_TS)
        self.assertEqual(persisted["ALPHAUSDT"]["min_mcap_usd"], 20_000_000.0)

        self.assertTrue(by_sym["BETAUSDT"]["is_overflow_7d"])
        self.assertEqual(by_sym["BETAUSDT"]["overflow_days_left"], 4.0)
        self.assertEqual(persisted["BETAUSDT"]["last_sub_30m_ts"], beta_prev_ts)

        self.assertNotIn("EXPIREDUSDT", persisted)

    def test_reads_resonance_state_and_publishes_candidate_snapshot(self) -> None:
        closed_times = [self.FIXED_NOW_MS - (12 - i) * 3_600_000 for i in range(12)]
        # Rising closes -> ema5 > ema10 -> is_ema_bull = True
        bull_klines = _make_klines([1.0 + 0.1 * i for i in range(12)], closed_times)

        symbols = [
            {
                "symbol": "ALPHAUSDT",
                "base": "ALPHA",
                "price": 2.1,
                "circulating_supply": 10_000_000.0,
                "market_cap": 20_000_000.0,
                # turnover_ratio = 40M / 20M = 2.0 >= 1.5 -> is_resonance = True
                "volume_usd": 40_000_000.0,
            }
        ]
        detected_dt = datetime.datetime.fromtimestamp(self.FIXED_NOW_TS - 3 * 3600.0, BEIJING_TZ)
        detected_str = detected_dt.strftime("%Y-%m-%d %H:%M:%S")
        expected_entry = detected_dt.strftime("%m-%d %H:%M")

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "scanner.sqlite3"
            db_store.save_resonance_state(
                db_path,
                {
                    "version": 1,
                    "initialized": True,
                    "active_resonance_symbols": ["ALPHAUSDT"],
                    "notified_breakout_keys": [],
                    "notified_breakout_times": {},
                    "history": {
                        "ALPHAUSDT": {"first_detected": detected_str},
                    },
                },
            )

            candidates, _ = self._run_scan(
                db_path,
                symbols=symbols,
                klines_by_symbol={"ALPHAUSDT": bull_klines},
            )
            snapshot = db_store.load_candidates(db_path)

        self.assertEqual(len(candidates), 1)
        alpha = candidates[0]
        self.assertTrue(alpha["is_resonance"])
        self.assertEqual(alpha["resonance_duration_hours"], 3)
        self.assertEqual(alpha["resonance_detected_at"], detected_str)
        self.assertEqual(alpha["resonance_entry_time"], expected_entry)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        updated_at, payload_json = snapshot
        self.assertTrue(updated_at.endswith("+08:00"))
        stored = json.loads(payload_json)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["symbol"], "ALPHAUSDT")
        self.assertEqual(stored[0]["resonance_duration_hours"], 3)

    def test_malformed_resonance_history_raises_value_error(self) -> None:
        closed_times = [self.FIXED_NOW_MS - (12 - i) * 3_600_000 for i in range(12)]
        bull_klines = _make_klines([1.0 + 0.1 * i for i in range(12)], closed_times)

        symbols = [
            {
                "symbol": "ALPHAUSDT",
                "base": "ALPHA",
                "price": 2.1,
                "circulating_supply": 10_000_000.0,
                "market_cap": 20_000_000.0,
                "volume_usd": 40_000_000.0,
            },
        ]

        # Case 1: malformed per-symbol first_detected or non-dict history entry raises ValueError
        for bad_history in (
            {"ALPHAUSDT": {"first_detected": "not-a-valid-datetime"}},
            {"ALPHAUSDT": "corrupted-entry"},
            ["not-a-dict"],
        ):
            with self.subTest(bad_history=bad_history):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = Path(tmpdir) / "scanner.sqlite3"
                    db_store.save_resonance_state(
                        db_path,
                        {
                            "version": 1,
                            "initialized": True,
                            "active_resonance_symbols": ["ALPHAUSDT"],
                            "history": bad_history,
                        },
                    )
                    with self.assertRaises(ValueError):
                        self._run_scan(
                            db_path,
                            symbols=symbols,
                            klines_by_symbol={"ALPHAUSDT": bull_klines},
                        )

        # Case 2: non-list active_resonance_symbols raises ValueError
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "scanner2.sqlite3"
            db_store.save_resonance_state(
                db_path,
                {
                    "version": 1,
                    "initialized": True,
                    "active_resonance_symbols": "ALPHAUSDT",
                    "history": {},
                },
            )
            with self.assertRaises(ValueError):
                self._run_scan(
                    db_path,
                    symbols=symbols,
                    klines_by_symbol={"ALPHAUSDT": bull_klines},
                )


class TestSyncAll(unittest.TestCase):
    """6. sync_all.py TimeoutExpired handling and continuing analytics on scanner failure."""

    def test_run_step_handles_timeout_expired_and_decodes_bytes(self) -> None:
        timeout_exc = subprocess.TimeoutExpired(
            cmd=["python3", "scripts/scan_second_leg_pool.py"],
            timeout=15,
            output=b"",
            stderr=b"first line\ntimed out detail\n",
        )
        stdout_buf = io.StringIO()
        with patch.object(sync_all.subprocess, "run", side_effect=timeout_exc), redirect_stdout(
            stdout_buf
        ):
            ok = sync_all.run_step("1/3 扫描", ["python3", "scripts/scan_second_leg_pool.py"], timeout=15)

        self.assertFalse(ok)
        output = stdout_buf.getvalue()
        self.assertIn("[✗ 超时]", output)
        self.assertIn("timeout=15s", output)
        self.assertIn("timed out detail", output)

    def test_main_continues_analytics_and_alerts_when_scanner_fails(self) -> None:
        recorded_cmds: list[list[str]] = []
        recorded_envs: list[dict[str, str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(cmd)
            env_arg = kwargs.get("env")
            assert isinstance(env_arg, dict)
            recorded_envs.append({str(k): str(v) for k, v in env_arg.items()})
            script_name = Path(cmd[1]).name
            if script_name == "scan_second_leg_pool.py":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=600, stderr="scanner timed out")
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="🎉 账户量化分析 完成\n",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sync_test.sqlite3"
            stdout_buf = io.StringIO()
            with patch.object(
                sys, "argv", ["sync_all.py", "--database", str(db_path)]
            ), patch.object(sync_all.subprocess, "run", side_effect=_fake_run), redirect_stdout(
                stdout_buf
            ):
                with self.assertRaises(SystemExit) as exit_ctx:
                    sync_all.main()

        self.assertEqual(exit_ctx.exception.code, 1)
        self.assertEqual(
            [Path(cmd[1]).name for cmd in recorded_cmds],
            [
                "scan_second_leg_pool.py",
                "account_analytics_engine.py",
                "trade_alert_daemon.py",
            ],
        )
        for env in recorded_envs:
            self.assertIsNotNone(env)
            assert env is not None
            self.assertEqual(env["COIN_DATABASE"], str(db_path.resolve()))
        output = stdout_buf.getvalue()
        self.assertIn("部分步骤存在警告或错误", output)
        self.assertNotIn("关键上游步骤失败", output)

    def test_main_refresh_only_skips_daemon_and_exits_zero_on_success(self) -> None:
        recorded_cmds: list[list[str]] = []

        def _fake_ok(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="✅ ok\n", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sync_ok.sqlite3"
            with patch.object(
                sys, "argv", ["sync_all.py", "--refresh-only", "--database", str(db_path)]
            ), patch.object(sync_all.subprocess, "run", side_effect=_fake_ok), redirect_stdout(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit) as exit_ctx:
                    sync_all.main()

        self.assertEqual(exit_ctx.exception.code, 0)
        self.assertEqual(
            [Path(cmd[1]).name for cmd in recorded_cmds],
            ["scan_second_leg_pool.py", "account_analytics_engine.py"],
        )

    def test_scan_pool_preserves_resonance_detected_at_from_previous_snapshot_and_iso(self) -> None:
        fixed_now_ts = 1_795_500_000.0
        fixed_now_ms = int(fixed_now_ts * 1000)
        closed_times = [fixed_now_ms - (12 - i) * 3_600_000 for i in range(12)]
        bull_klines = [
            [cm - 3_599_999, "1.0", "1.1", "0.9", str(1.0 + 0.1 * i), "100", cm, "100", 1]
            for i, cm in enumerate(closed_times)
        ]
        symbols = [
            {
                "symbol": "ALPHAUSDT",
                "base": "ALPHA",
                "price": 2.1,
                "circulating_supply": 10_000_000.0,
                "market_cap": 20_000_000.0,
                "volume_usd": 40_000_000.0,
            },
            {
                "symbol": "BETAUSDT",
                "base": "BETA",
                "price": 2.1,
                "circulating_supply": 10_000_000.0,
                "market_cap": 20_000_000.0,
                "volume_usd": 40_000_000.0,
            },
        ]
        prev_dt = datetime.datetime.fromtimestamp(fixed_now_ts - 2 * 3600.0, BEIJING_TZ)
        prev_str = prev_dt.strftime("%Y-%m-%d %H:%M:%S")
        iso_dt = datetime.datetime.fromtimestamp(fixed_now_ts - 4 * 3600.0, BEIJING_TZ)
        iso_str = iso_dt.isoformat()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "scanner_prev.sqlite3"
            # ALPHAUSDT not yet in active_resonance_symbols (e.g. daemon offline), but was resonant in previous snapshot
            db_store.publish_candidates(
                db_path,
                [{"symbol": "ALPHAUSDT", "is_resonance": True, "resonance_detected_at": prev_str}],
                updated_at=prev_str,
            )
            # BETAUSDT has ISO-8601 first_detected in resonance_state
            db_store.save_resonance_state(
                db_path,
                {
                    "version": 1,
                    "initialized": True,
                    "active_resonance_symbols": ["BETAUSDT"],
                    "notified_breakout_keys": [],
                    "notified_breakout_times": {},
                    "history": {"BETAUSDT": {"first_detected": iso_str}},
                },
            )

            def fake_request_json(url: str, **_kwargs: object) -> object:
                if url.endswith("/fapi/v1/time"):
                    return {"serverTime": fixed_now_ms}
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
                            "priceChangePercent": "5.0",
                            "lastPrice": str(s["price"]),
                            "highPrice": "2.2",
                            "lowPrice": "1.0",
                        }
                        for s in symbols
                    ]
                if url.endswith("/fapi/v1/premiumIndex"):
                    return [{"symbol": s["symbol"], "lastFundingRate": "0.0001"} for s in symbols]
                if url.endswith("/fapi/v1/fundingInfo"):
                    return [{"symbol": s["symbol"], "fundingIntervalHours": 4} for s in symbols]
                if "pro-api.coinmarketcap.com" in url:
                    return {
                        "data": {
                            s["base"]: {
                                "quote": {
                                    "USD": {
                                        "price": s["price"],
                                        "market_cap": s["market_cap"],
                                        "self_reported_market_cap": None,
                                    }
                                },
                                "circulating_supply": s["circulating_supply"],
                            }
                            for s in symbols
                        }
                    }
                if "/fapi/v1/klines?" in url:
                    return bull_klines
                raise AssertionError(f"Unexpected URL: {url}")

            with patch.dict("os.environ", {"CMC_API_KEY": "test-cmc"}), patch.object(
                ssp.HttpClient, "request_json", side_effect=fake_request_json
            ), patch("scan_second_leg_pool.time.time", return_value=fixed_now_ts), redirect_stdout(
                io.StringIO()
            ):
                candidates = ssp.scan_pool(database=db_path)

        by_sym = {c["symbol"]: c for c in candidates}
        self.assertEqual(by_sym["ALPHAUSDT"]["resonance_detected_at"], prev_str)
        self.assertEqual(by_sym["ALPHAUSDT"]["resonance_duration_hours"], 2)
        self.assertEqual(by_sym["BETAUSDT"]["resonance_detected_at"], iso_dt.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(by_sym["BETAUSDT"]["resonance_duration_hours"], 4)


if __name__ == "__main__":
    unittest.main()
