"""Store/serve static HUD shells and JSON APIs backed by SQLite (WAL) via db_store."""
import argparse
from collections import OrderedDict
import gzip
import hashlib
import http.client
import json
import mimetypes
import os
import sqlite3
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import parse_qs, quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
WEB_DIR = PROJECT_ROOT / "web"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_client import BINANCE_FUTURES_BASE_URL  # noqa: E402
from config import DEFAULT_TV_LAYOUT_ID, get_env_setting  # noqa: E402
import db_store  # noqa: E402
import http_client  # noqa: E402

_TEXT_CONTENT_TYPES = {"application/javascript", "application/json"}
_BINANCE_FAPI_BASE = BINANCE_FUTURES_BASE_URL
_BINANCE_ALLOWED_INTERVALS = frozenset({"15m", "1h", "4h", "1d"})
_BINANCE_PROXY_TTL_SEC = 15.0
_BINANCE_KLINES_TTL_SEC = 12.0
_BINANCE_PROXY_CACHE_MAX_ENTRIES = 128
_STATIC_CACHE_MAX_ENTRIES = 64
_BINANCE_PROXY_TIMEOUT_SEC = 10.0
_GZIP_MIN_BYTES = 1024
_GZIP_COMPRESS_LEVEL = 6

_T = TypeVar("_T")


class _ProxyFlight:
    """Singleflight coordination slot for concurrent cache-miss proxy requests."""

    __slots__ = ("event", "status", "body", "gz_body", "err_exc")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.status: int = 0
        self.body: bytes = b""
        self.gz_body: Optional[bytes] = None
        self.err_exc: Optional[BaseException] = None


def _maybe_gzip(body: bytes) -> Optional[bytes]:
    if len(body) >= _GZIP_MIN_BYTES:
        return gzip.compress(body, compresslevel=_GZIP_COMPRESS_LEVEL)
    return None


def _etag_pair(body: bytes, gz_body: Optional[bytes]) -> tuple[str, Optional[str]]:
    digest = hashlib.sha256(body).hexdigest()[:16]
    etag = f'"{digest}"'
    gz_etag = f'"{digest}-gzip"' if gz_body is not None else None
    return etag, gz_etag


def _accepts_gzip(headers: Any) -> bool:
    if not headers:
        return False
    raw = headers.get("Accept-Encoding")
    if raw is None:
        for key, val in headers.items():
            if key.lower() == "accept-encoding":
                raw = val
                break
    if not raw:
        return False
    for part in str(raw).split(","):
        tokens = [tok.strip().lower() for tok in part.split(";")]
        if tokens[0] != "gzip":
            continue
        q_val = 1.0
        for param in tokens[1:]:
            if param.startswith("q="):
                try:
                    q_val = float(param[2:])
                except ValueError:
                    q_val = 0.0
                break
        if q_val > 0.0:
            return True
    return False


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
    proxy_cache: OrderedDict[str, tuple[float, bytes, Optional[bytes]]] = OrderedDict()
    proxy_inflight: dict[str, _ProxyFlight] = {}
    proxy_lock = threading.Lock()
    static_cache: OrderedDict[Path, tuple[bytes, Optional[bytes], str, str, Optional[str], int]] = OrderedDict()
    static_lock = threading.Lock()
    snapshot_cache: dict[str, tuple[Any, bytes, Optional[bytes], str, Optional[str]]] = {}
    snapshot_lock = threading.Lock()

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

        def _send_json_error(self, status: int, message: str, *, send_body: bool = True) -> None:
            err_body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
            self._send_bytes(
                status,
                err_body,
                "application/json; charset=utf-8",
                cache_control="no-store",
                send_body=send_body,
            )

        def _send_cached_response(
            self,
            body: bytes,
            gz_body: Optional[bytes],
            content_type: str,
            etag: str,
            gz_etag: Optional[str],
            *,
            send_body: bool = True,
        ) -> None:
            use_gzip = gz_body is not None and _accepts_gzip(self.headers)
            active_etag = gz_etag if (use_gzip and gz_etag is not None) else etag
            active_body = gz_body if use_gzip else body
            if _matches_etag(self.headers.get("If-None-Match"), active_etag):
                self.send_response(304)
                self.send_header("ETag", active_etag)
                self.send_header("Cache-Control", "no-cache")
                if gz_body is not None:
                    self.send_header("Vary", "Accept-Encoding")
                self.end_headers()
                return
            extra_headers = {"ETag": active_etag}
            if gz_body is not None:
                extra_headers["Vary"] = "Accept-Encoding"
            if use_gzip:
                extra_headers["Content-Encoding"] = "gzip"
            self._send_bytes(
                200,
                active_body,
                content_type,
                cache_control="no-cache",
                extra_headers=extra_headers,
                send_body=send_body,
            )

        def _serve_snapshot_json(
            self,
            cache_key: str,
            loader: Callable[[], Optional[_T]],
            build_body: Callable[[_T], bytes],
            err_context: str,
            *,
            send_body: bool = True,
        ) -> None:
            try:
                raw = loader()
            except sqlite3.Error as exc:
                self.log_error("SQLite error reading %s: %s", err_context, exc)
                self._send_json_error(500, "Database error", send_body=send_body)
                return
            if raw is None:
                self._send_json_error(503, "Snapshot unavailable", send_body=send_body)
                return
            with snapshot_lock:
                cached = snapshot_cache.get(cache_key)
                if cached is not None and cached[0] == raw:
                    _, body, gz_body, etag, gz_etag = cached
                else:
                    body = build_body(raw)
                    gz_body = _maybe_gzip(body)
                    etag, gz_etag = _etag_pair(body, gz_body)
                    snapshot_cache[cache_key] = (raw, body, gz_body, etag, gz_etag)
            self._send_cached_response(
                body, gz_body, "application/json; charset=utf-8", etag, gz_etag, send_body=send_body
            )

        def _send_static_file(self, asset: Path, *, send_body: bool = True) -> None:
            body: Optional[bytes] = None
            gz_body: Optional[bytes] = None
            content_type = ""
            etag = ""
            gz_etag: Optional[str] = None

            with static_lock:
                cached = static_cache.get(asset)

            if cached is not None:
                try:
                    st = asset.stat()
                except FileNotFoundError:
                    self.send_error(404)
                    return
                if not stat.S_ISREG(st.st_mode):
                    self.send_error(404)
                    return
                if cached[5] == st.st_mtime_ns:
                    with static_lock:
                        if asset in static_cache:
                            static_cache.move_to_end(asset)
                    body, gz_body, content_type, etag, gz_etag, _ = cached

            if body is None:
                try:
                    with open(asset, "rb") as f:
                        st = os.fstat(f.fileno())
                        if not stat.S_ISREG(st.st_mode):
                            self.send_error(404)
                            return
                        body = f.read()
                except (FileNotFoundError, IsADirectoryError):
                    self.send_error(404)
                    return
                fresh_mtime_ns = st.st_mtime_ns
                content_type = _content_type_for(asset)
                gz_body = _maybe_gzip(body)
                etag, gz_etag = _etag_pair(body, gz_body)
                with static_lock:
                    static_cache[asset] = (body, gz_body, content_type, etag, gz_etag, fresh_mtime_ns)
                    static_cache.move_to_end(asset)
                    while len(static_cache) > _STATIC_CACHE_MAX_ENTRIES:
                        static_cache.popitem(last=False)

            self._send_cached_response(
                body, gz_body, content_type, etag, gz_etag, send_body=send_body
            )

        def _send_proxy_payload(
            self,
            status: int,
            body: bytes,
            gz_body: Optional[bytes],
            *,
            send_body: bool,
        ) -> None:
            if status == 200 and gz_body is not None:
                if _accepts_gzip(self.headers):
                    self._send_bytes(
                        200,
                        gz_body,
                        "application/json; charset=utf-8",
                        cache_control="no-store",
                        extra_headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
                        send_body=send_body,
                    )
                    return
                self._send_bytes(
                    200,
                    body,
                    "application/json; charset=utf-8",
                    cache_control="no-store",
                    extra_headers={"Vary": "Accept-Encoding"},
                    send_body=send_body,
                )
                return
            self._send_bytes(
                status,
                body,
                "application/json; charset=utf-8",
                cache_control="no-store",
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
            cached_hit: Optional[tuple[bytes, Optional[bytes]]] = None
            flight: Optional[_ProxyFlight] = None
            is_leader = True
            if cache_key is not None:
                now = time.monotonic()
                with proxy_lock:
                    cached = proxy_cache.get(cache_key)
                    if cached is not None:
                        expires_at, cached_body, cached_gz = cached
                        if now < expires_at:
                            proxy_cache.move_to_end(cache_key)
                            cached_hit = (cached_body, cached_gz)
                        else:
                            del proxy_cache[cache_key]
                    if cached_hit is None:
                        existing = proxy_inflight.get(cache_key)
                        if existing is None:
                            flight = _ProxyFlight()
                            proxy_inflight[cache_key] = flight
                            is_leader = True
                        else:
                            flight = existing
                            is_leader = False

            if cached_hit is not None:
                self._send_proxy_payload(200, cached_hit[0], cached_hit[1], send_body=send_body)
                return

            if not is_leader:
                assert flight is not None
                flight.event.wait()
                if flight.err_exc is not None:
                    raise flight.err_exc
                self._send_proxy_payload(flight.status, flight.body, flight.gz_body, send_body=send_body)
                return

            status = 200
            body = b""
            gz_body: Optional[bytes] = None
            try:
                try:
                    body = http_client.request_bytes(
                        url,
                        timeout=_BINANCE_PROXY_TIMEOUT_SEC,
                        max_retries=1,
                    )
                except http_client.HttpError as exc:
                    if exc.status_code is not None and 400 <= exc.status_code < 500:
                        err_body = b""
                        if exc.body:
                            try:
                                json.loads(exc.body)
                                err_body = exc.body.encode("utf-8")
                            except ValueError:
                                err_body = b""
                        if not err_body:
                            err_body = json.dumps(
                                {"error": exc.body or str(exc)}, ensure_ascii=False
                            ).encode("utf-8")
                        status = exc.status_code
                        body = err_body
                    else:
                        self.log_error("Binance proxy error for %s: %s", url, exc)
                        status = 502
                        body = json.dumps(
                            {"error": f"Binance upstream error: {exc}"}, ensure_ascii=False
                        ).encode("utf-8")
                except (http.client.HTTPException, TimeoutError, OSError) as exc:
                    self.log_error("Binance proxy error for %s: %s", url, exc)
                    status = 502
                    body = json.dumps(
                        {"error": f"Binance upstream error: {exc}"}, ensure_ascii=False
                    ).encode("utf-8")
                else:
                    gz_body = _maybe_gzip(body)
                    if cache_key is not None:
                        store_now = time.monotonic()
                        with proxy_lock:
                            expired_keys = [k for k, (exp, _, _) in proxy_cache.items() if store_now >= exp]
                            for k in expired_keys:
                                del proxy_cache[k]
                            proxy_cache[cache_key] = (store_now + ttl_sec, body, gz_body)
                            proxy_cache.move_to_end(cache_key)
                            while len(proxy_cache) > _BINANCE_PROXY_CACHE_MAX_ENTRIES:
                                proxy_cache.popitem(last=False)
                if flight is not None:
                    flight.status = status
                    flight.body = body
                    flight.gz_body = gz_body
            except BaseException as exc:
                if flight is not None:
                    flight.err_exc = exc
                raise
            finally:
                if flight is not None and cache_key is not None:
                    with proxy_lock:
                        if proxy_inflight.get(cache_key) is flight:
                            del proxy_inflight[cache_key]
                    flight.event.set()

            self._send_proxy_payload(status, body, gz_body, send_body=send_body)

        def _handle_get(self, *, send_body: bool) -> None:
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

            if path in ("/account.html", "/playbook.html"):
                self._send_static_file(web_root / path.lstrip("/"), send_body=send_body)
                return

            if path == "/api/config":
                layout_id = get_env_setting("TRADINGVIEW_LAYOUT_ID", DEFAULT_TV_LAYOUT_ID)
                body = json.dumps({"tradingview_layout_id": layout_id}, ensure_ascii=False).encode("utf-8")
                self._send_bytes(
                    200,
                    body,
                    "application/json; charset=utf-8",
                    cache_control="no-store",
                    send_body=send_body,
                )
                return

            if path == "/api/account":
                self._serve_snapshot_json(
                    "account",
                    lambda: db_store.load_raw_report(database),
                    lambda payload_json: payload_json.encode("utf-8"),
                    "account_snapshot",
                    send_body=send_body,
                )
                return

            if path == "/api/candidate-version":
                try:
                    updated_at = db_store.load_candidate_version(database)
                except sqlite3.Error as exc:
                    self.log_error("SQLite error reading candidate_snapshot version: %s", exc)
                    self._send_json_error(500, "Database error", send_body=send_body)
                    return
                if updated_at is None:
                    self._send_json_error(503, "Snapshot unavailable", send_body=send_body)
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
                self._serve_snapshot_json(
                    "candidates",
                    lambda: db_store.load_candidates(database),
                    lambda row: (
                        b'{"updated_at":'
                        + json.dumps(row[0], ensure_ascii=False).encode("utf-8")
                        + b',"candidates":'
                        + row[1].encode("utf-8")
                        + b"}"
                    ),
                    "candidate_snapshot",
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
                symbol = (qs.get("symbol") or [""])[0].strip().upper()
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
                    self._send_json_error(400, "Invalid klines parameters", send_body=send_body)
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

            if path.startswith("/api/"):
                self._send_json_error(404, "Not found", send_body=send_body)
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
