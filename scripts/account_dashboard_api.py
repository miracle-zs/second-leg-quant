"""Store/serve static HUD shells and JSON APIs backed by SQLite (WAL) via db_store."""
import argparse
from collections import OrderedDict
import hashlib
import http.client
import json
import mimetypes
import sqlite3
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
WEB_DIR = PROJECT_ROOT / "web"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db_store  # noqa: E402
import http_client  # noqa: E402

_TEXT_CONTENT_TYPES = {"application/javascript", "application/json"}
_BINANCE_FAPI_BASE = "https://fapi.binance.com"
_BINANCE_ALLOWED_INTERVALS = frozenset({"15m", "1h", "4h", "1d"})
_BINANCE_PROXY_TTL_SEC = 15.0
_BINANCE_KLINES_TTL_SEC = 12.0
_BINANCE_PROXY_CACHE_MAX_ENTRIES = 128
_BINANCE_PROXY_TIMEOUT_SEC = 10.0


def _is_valid_symbol(symbol: str) -> bool:
    if not symbol or len(symbol) > 32:
        return False
    return not any(ch.isspace() or ord(ch) < 32 or ch in "/?&#%\\" for ch in symbol)


def _content_type_for(asset: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(asset))
    ctype = ctype or "application/octet-stream"
    if ctype.startswith("text/") or ctype in _TEXT_CONTENT_TYPES:
        return f"{ctype}; charset=utf-8"
    return ctype


def _etag_for(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:16]}"'


def _matches_etag(if_none_match: Optional[str], etag: str) -> bool:
    if not if_none_match:
        return False
    for raw_tag in if_none_match.split(","):
        tag = raw_tag.strip()
        if tag == "*" or tag == etag or (tag.startswith("W/") and tag[2:] == etag):
            return True
    return False


def create_handler(database: Path):
    """Build an HTTP request handler serving static HUD shells, /static/* assets, and JSON APIs."""
    web_root = WEB_DIR.resolve()
    proxy_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
    proxy_lock = threading.Lock()
    static_cache: dict[Path, tuple[bytes, str, str, int]] = {}
    static_lock = threading.Lock()
    candidates_cache: Optional[tuple[str, str, bytes, str]] = None
    candidates_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            cache_control: str = "no-store",
            extra_headers: Optional[dict[str, str]] = None,
            send_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _send_not_modified(self, etag: str) -> None:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def _send_json_with_etag(self, body: bytes, *, send_body: bool = True) -> None:
            etag = _etag_for(body)
            if _matches_etag(self.headers.get("If-None-Match"), etag):
                self._send_not_modified(etag)
                return
            self._send_bytes(
                200,
                body,
                "application/json; charset=utf-8",
                cache_control="no-cache",
                extra_headers={"ETag": etag},
                send_body=send_body,
            )

        def _send_static_file(self, asset: Path, *, send_body: bool = True) -> None:
            try:
                st = asset.stat()
            except FileNotFoundError:
                self.send_error(404)
                return
            if not stat.S_ISREG(st.st_mode):
                self.send_error(404)
                return
            mtime_ns = st.st_mtime_ns

            body: Optional[bytes] = None
            content_type = ""
            etag = ""
            with static_lock:
                cached = static_cache.get(asset)
                if cached is not None and cached[3] == mtime_ns:
                    body, content_type, etag, _ = cached

            if body is None:
                try:
                    body = asset.read_bytes()
                    fresh_mtime_ns = asset.stat().st_mtime_ns
                except FileNotFoundError:
                    self.send_error(404)
                    return
                content_type = _content_type_for(asset)
                etag = _etag_for(body)
                with static_lock:
                    static_cache[asset] = (body, content_type, etag, fresh_mtime_ns)

            if _matches_etag(self.headers.get("If-None-Match"), etag):
                self._send_not_modified(etag)
                return
            self._send_bytes(
                200,
                body,
                content_type,
                cache_control="no-cache",
                extra_headers={"ETag": etag},
                send_body=send_body,
            )

        def _send_binance_proxy(
            self,
            cache_key: Optional[str],
            url: str,
            *,
            ttl_sec: float = _BINANCE_PROXY_TTL_SEC,
            send_body: bool = True,
        ) -> None:
            now = time.monotonic()
            if cache_key is not None:
                with proxy_lock:
                    cached = proxy_cache.get(cache_key)
                    if cached is not None:
                        expires_at, cached_body = cached
                        if now < expires_at:
                            proxy_cache.move_to_end(cache_key)
                            self._send_bytes(
                                200,
                                cached_body,
                                "application/json; charset=utf-8",
                                cache_control="no-store",
                                send_body=send_body,
                            )
                            return
                        del proxy_cache[cache_key]
            try:
                body = http_client.request_bytes(
                    url,
                    timeout=_BINANCE_PROXY_TIMEOUT_SEC,
                    max_retries=1,
                )
            except http_client.HttpError as exc:
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    err_body = (
                        exc.body.encode("utf-8")
                        if exc.body
                        else json.dumps({"error": str(exc)}).encode("utf-8")
                    )
                    self._send_bytes(
                        exc.status_code,
                        err_body,
                        "application/json; charset=utf-8",
                        cache_control="no-store",
                        send_body=send_body,
                    )
                    return
                self.log_error("Binance proxy error for %s: %s", url, exc)
                err_body = json.dumps({"error": f"Binance upstream error: {exc}"}).encode("utf-8")
                self._send_bytes(
                    502,
                    err_body,
                    "application/json; charset=utf-8",
                    cache_control="no-store",
                    send_body=send_body,
                )
                return
            except (http.client.HTTPException, TimeoutError, OSError) as exc:
                self.log_error("Binance proxy error for %s: %s", url, exc)
                err_body = json.dumps({"error": f"Binance upstream error: {exc}"}).encode("utf-8")
                self._send_bytes(
                    502,
                    err_body,
                    "application/json; charset=utf-8",
                    cache_control="no-store",
                    send_body=send_body,
                )
                return
            if cache_key is not None:
                store_now = time.monotonic()
                with proxy_lock:
                    expired_keys = [k for k, (exp, _) in proxy_cache.items() if store_now >= exp]
                    for k in expired_keys:
                        del proxy_cache[k]
                    proxy_cache[cache_key] = (store_now + ttl_sec, body)
                    proxy_cache.move_to_end(cache_key)
                    while len(proxy_cache) > _BINANCE_PROXY_CACHE_MAX_ENTRIES:
                        proxy_cache.popitem(last=False)
            self._send_bytes(
                200,
                body,
                "application/json; charset=utf-8",
                cache_control="no-store",
                send_body=send_body,
            )

        def _handle_get(self, *, send_body: bool) -> None:
            nonlocal candidates_cache
            parsed = urlsplit(self.path)
            path = parsed.path

            if path.startswith("/static/"):
                rel_name = path[len("/static/"):]
                static_file = (web_root / rel_name).resolve()
                if (
                    not rel_name
                    or rel_name.startswith("/")
                    or not static_file.is_relative_to(web_root)
                ):
                    self.send_error(404)
                    return
                self._send_static_file(static_file, send_body=send_body)
                return

            if path in ("/", "/index.html"):
                self._send_static_file(web_root / "index.html", send_body=send_body)
                return

            if path == "/account.html":
                self._send_static_file(web_root / "account.html", send_body=send_body)
                return

            if path == "/playbook.html":
                self._send_static_file(web_root / "playbook.html", send_body=send_body)
                return

            if path == "/api/account":
                try:
                    report = db_store.load_report(database)
                except sqlite3.Error as exc:
                    self.log_error("SQLite error reading account_snapshot: %s", exc)
                    self.send_error(500, "Database error")
                    return
                if report is None:
                    self.send_error(503, "Snapshot unavailable")
                    return
                body = json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self._send_json_with_etag(body, send_body=send_body)
                return

            if path == "/api/candidate-version":
                try:
                    updated_at = db_store.load_candidate_version(database)
                except sqlite3.Error as exc:
                    self.log_error("SQLite error reading candidate_snapshot version: %s", exc)
                    self.send_error(500, "Database error")
                    return
                if updated_at is None:
                    self.send_error(503, "Snapshot unavailable")
                    return
                body = json.dumps({"updated_at": updated_at}, ensure_ascii=False).encode("utf-8")
                self._send_bytes(
                    200,
                    body,
                    "application/json; charset=utf-8",
                    cache_control="no-store",
                    send_body=send_body,
                )
                return

            if path == "/api/candidates":
                try:
                    row = db_store.load_candidates(database)
                except sqlite3.Error as exc:
                    self.log_error("SQLite error reading candidate_snapshot: %s", exc)
                    self.send_error(500, "Database error")
                    return
                if row is None:
                    self.send_error(503, "Snapshot unavailable")
                    return
                updated_at, payload_json = row
                with candidates_lock:
                    if (
                        candidates_cache is not None
                        and candidates_cache[0] == updated_at
                        and candidates_cache[1] == payload_json
                    ):
                        body = candidates_cache[2]
                        etag = candidates_cache[3]
                    else:
                        updated_json = json.dumps(updated_at, ensure_ascii=False).encode("utf-8")
                        body = (
                            b'{"updated_at":'
                            + updated_json
                            + b',"candidates":'
                            + payload_json.encode("utf-8")
                            + b"}"
                        )
                        etag = _etag_for(body)
                        candidates_cache = (updated_at, payload_json, body, etag)

                if _matches_etag(self.headers.get("If-None-Match"), etag):
                    self._send_not_modified(etag)
                    return
                self._send_bytes(
                    200,
                    body,
                    "application/json; charset=utf-8",
                    cache_control="no-cache",
                    extra_headers={"ETag": etag},
                    send_body=send_body,
                )
                return

            if path == "/api/binance/ticker24hr":
                self._send_binance_proxy(
                    "ticker24hr",
                    f"{_BINANCE_FAPI_BASE}/fapi/v1/ticker/24hr",
                    ttl_sec=_BINANCE_PROXY_TTL_SEC,
                    send_body=send_body,
                )
                return

            if path == "/api/binance/premiumIndex":
                self._send_binance_proxy(
                    "premiumIndex",
                    f"{_BINANCE_FAPI_BASE}/fapi/v1/premiumIndex",
                    ttl_sec=_BINANCE_PROXY_TTL_SEC,
                    send_body=send_body,
                )
                return

            if path == "/api/binance/klines":
                qs = parse_qs(parsed.query)
                symbol = (qs.get("symbol") or [""])[0].strip()
                interval = (qs.get("interval") or [""])[0].strip()
                raw_limit = (qs.get("limit") or ["168"])[0].strip()
                try:
                    limit = int(raw_limit)
                except ValueError:
                    limit = -1
                if (
                    not _is_valid_symbol(symbol)
                    or interval not in _BINANCE_ALLOWED_INTERVALS
                    or not (1 <= limit <= 500)
                ):
                    err_body = json.dumps({"error": "Invalid klines parameters"}).encode("utf-8")
                    self._send_bytes(
                        400,
                        err_body,
                        "application/json; charset=utf-8",
                        cache_control="no-store",
                        send_body=send_body,
                    )
                    return
                upstream = (
                    f"{_BINANCE_FAPI_BASE}/fapi/v1/klines"
                    f"?symbol={quote(symbol, safe='')}&interval={interval}&limit={limit}"
                )
                self._send_binance_proxy(
                    f"klines:{symbol}:{interval}:{limit}",
                    upstream,
                    ttl_sec=_BINANCE_KLINES_TTL_SEC,
                    send_body=send_body,
                )
                return

            self.send_error(404)

        def do_GET(self) -> None:
            self._handle_get(send_body=True)

        def do_HEAD(self) -> None:
            self._handle_get(send_body=False)

    return Handler


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18083)
    args = parser.parse_args()
    handler_cls = create_handler(args.database)
    with DashboardHTTPServer(("127.0.0.1", args.port), handler_cls) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
