#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared stdlib-only HTTP/1.1 keep-alive connection pool and retry client.
"""

from __future__ import annotations

import atexit
from collections import deque
import http.client
import json
import math
import random
import socket
import threading
import time
import urllib.parse
from typing import Mapping, Optional, Sequence

DEFAULT_USER_AGENT = "second-leg-quant/3.5"
RETRYABLE_STATUS_CODES: tuple[int, ...] = (429, 500, 502, 503, 504)
_MAX_RETRY_AFTER_SEC = 10.0
_RECONNECT_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.IncompleteRead,
    http.client.CannotSendRequest,
    http.client.ResponseNotReady,
    BrokenPipeError,
    ConnectionResetError,
)


class HttpError(RuntimeError):
    """HTTP status, network transport, or JSON decode error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: str = "",
        url: str = "",
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.url = url
        self.retry_after = retry_after


def _parse_retry_after(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds <= 0.0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SEC)


def retry_delay(
    backoff_base: float,
    attempt: int,
    retry_after: Optional[float] = None,
) -> float:
    """Compute exponential backoff delay with Retry-After floor and bounded jitter."""
    delay = backoff_base * (2 ** attempt)
    if retry_after is not None:
        delay = max(delay, retry_after)
    if delay > 0.0:
        delay += random.uniform(0.0, min(0.5, delay * 0.25))
    return delay


def _close_quietly(conn: http.client.HTTPConnection) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _build_request_headers(headers: Optional[Mapping[str, str]], default_ua: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    has_ua = False
    has_conn = False
    if headers:
        for k, v in headers.items():
            merged[k] = v
            low = k.lower()
            if low == "user-agent":
                has_ua = True
            elif low == "connection":
                has_conn = True
    if not has_ua:
        merged["User-Agent"] = default_ua
    if not has_conn:
        merged["Connection"] = "keep-alive"
    return merged


class HttpClient:
    """Thread-safe bounded HTTP/1.1 keep-alive connection pool using stdlib http.client."""

    def __init__(
        self,
        *,
        max_per_host: int = 8,
        default_user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if max_per_host <= 0:
            raise ValueError(f"max_per_host must be positive, got {max_per_host}")
        self.max_per_host = max_per_host
        self.default_user_agent = default_user_agent
        self._lock = threading.Lock()
        self._pools: dict[tuple[str, str, int], deque[http.client.HTTPConnection]] = {}
        self._closed = False

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Close all idle pooled connections and reject future checkouts."""
        with self._lock:
            self._closed = True
            pools = list(self._pools.values())
            self._pools.clear()
        for q in pools:
            while q:
                _close_quietly(q.popleft())

    @staticmethod
    def _parse_url(url: str) -> tuple[tuple[str, str, int], str]:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if scheme not in ("http", "https") or not host:
            raise ValueError(f"Unsupported URL for HttpClient: {url!r}")
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return (scheme, host, port), path

    @staticmethod
    def _new_conn(scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(host, port=port, timeout=timeout)
        return http.client.HTTPConnection(host, port=port, timeout=timeout)

    def _acquire_conn(
        self,
        key: tuple[str, str, int],
        timeout: float,
    ) -> tuple[http.client.HTTPConnection, bool]:
        with self._lock:
            if self._closed:
                raise RuntimeError("HttpClient is closed")
            q = self._pools.get(key)
            if q:
                conn = q.pop()
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                return conn, True
        scheme, host, port = key
        return self._new_conn(scheme, host, port, timeout), False

    def _release_conn(self, key: tuple[str, str, int], conn: http.client.HTTPConnection) -> None:
        with self._lock:
            if self._closed:
                _close_quietly(conn)
                return
            q = self._pools.setdefault(key, deque())
            if len(q) >= self.max_per_host:
                _close_quietly(conn)
                return
            q.append(conn)

    def _send_once(
        self,
        key: tuple[str, str, int],
        path: str,
        method: str,
        req_headers: dict[str, str],
        req_body: Optional[bytes],
        timeout: float,
    ) -> tuple[int, Optional[float], bytes]:
        conn, from_pool = self._acquire_conn(key, timeout)
        reconnected = False
        while True:
            try:
                conn.request(method, path, body=req_body, headers=req_headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn_header = (resp.getheader("Connection") or "").lower()
                keep_alive = resp.version == 11 and conn_header != "close"
                if keep_alive:
                    self._release_conn(key, conn)
                else:
                    _close_quietly(conn)
                retry_after = _parse_retry_after(resp.getheader("Retry-After")) if resp.status == 429 else None
                return resp.status, retry_after, raw
            except _RECONNECT_ERRORS:
                _close_quietly(conn)
                if from_pool and not reconnected and method in ("GET", "HEAD", "OPTIONS"):
                    reconnected = True
                    from_pool = False
                    scheme, host, port = key
                    conn = self._new_conn(scheme, host, port, timeout)
                    continue
                raise
            except (http.client.HTTPException, OSError, TimeoutError, socket.timeout):
                _close_quietly(conn)
                raise

    def request_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes | str] = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        retry_statuses: Sequence[int] = RETRYABLE_STATUS_CODES,
    ) -> bytes:
        """
        Execute an HTTP(S) request over the keep-alive pool with exponential backoff retries.
        `max_retries` is the number of retries after the initial attempt (total attempts = max_retries + 1).
        """
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        key, path = self._parse_url(url)
        req_headers = _build_request_headers(headers, self.default_user_agent)
        req_body = body.encode("utf-8") if isinstance(body, str) else body
        retry_set = frozenset(retry_statuses)

        for attempt in range(max_retries + 1):
            retry_after: Optional[float] = None
            try:
                status, retry_after, raw = self._send_once(
                    key, path, method.upper(), req_headers, req_body, float(timeout)
                )
            except (http.client.HTTPException, OSError, TimeoutError, socket.timeout) as exc:
                if attempt == max_retries:
                    raise HttpError(
                        f"Request failed after {max_retries + 1} attempts ({url}): {exc}",
                        status_code=None,
                        body=str(exc),
                        url=url,
                    ) from exc
            else:
                if 200 <= status < 300:
                    return raw

                err_body = raw.decode("utf-8", errors="replace")
                if status not in retry_set or attempt == max_retries:
                    raise HttpError(
                        f"HTTP {status} from {url}: {err_body}",
                        status_code=status,
                        body=err_body,
                        url=url,
                        retry_after=retry_after,
                    )

            time.sleep(retry_delay(backoff_base, attempt, retry_after))

        raise AssertionError("unreachable")

    def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes | str] = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        retry_statuses: Sequence[int] = RETRYABLE_STATUS_CODES,
    ) -> object:
        """Execute an HTTP(S) request and decode the response body as JSON."""
        raw = self.request_bytes(
            url,
            method=method,
            headers=headers,
            body=body,
            timeout=timeout,
            max_retries=max_retries,
            backoff_base=backoff_base,
            retry_statuses=retry_statuses,
        )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(
                f"Invalid JSON from {url}: {exc}",
                status_code=None,
                body=raw[:500].decode("utf-8", errors="replace"),
                url=url,
            ) from exc


_DEFAULT_CLIENT_LOCK = threading.Lock()
_DEFAULT_CLIENT: Optional[HttpClient] = None


def _get_default_client() -> HttpClient:
    global _DEFAULT_CLIENT
    with _DEFAULT_CLIENT_LOCK:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = HttpClient()
        return _DEFAULT_CLIENT


def close_default_client() -> None:
    """Close the module-level shared HTTP connection pool."""
    global _DEFAULT_CLIENT
    with _DEFAULT_CLIENT_LOCK:
        client = _DEFAULT_CLIENT
        _DEFAULT_CLIENT = None
    if client is not None:
        client.close()


atexit.register(close_default_client)


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes | str] = None,
    timeout: float = 15.0,
    max_retries: int = 2,
    backoff_base: float = 0.5,
    retry_statuses: Sequence[int] = RETRYABLE_STATUS_CODES,
) -> bytes:
    """Send an HTTP(S) request through the shared keep-alive pool and return raw bytes."""
    return _get_default_client().request_bytes(
        url,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        max_retries=max_retries,
        backoff_base=backoff_base,
        retry_statuses=retry_statuses,
    )


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes | str] = None,
    timeout: float = 15.0,
    max_retries: int = 2,
    backoff_base: float = 0.5,
    retry_statuses: Sequence[int] = RETRYABLE_STATUS_CODES,
) -> object:
    """Send an HTTP(S) request through the shared keep-alive pool and parse JSON."""
    return _get_default_client().request_json(
        url,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        max_retries=max_retries,
        backoff_base=backoff_base,
        retry_statuses=retry_statuses,
    )
