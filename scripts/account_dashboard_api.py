"""Store account/candidate snapshots in SQLite (WAL) and serve pure static HUD shells + JSON APIs."""
import argparse
import hashlib
import json
import mimetypes
import sqlite3
import sys
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
WEB_DIR = PROJECT_ROOT / "web"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import open_sqlite_connection  # noqa: E402

_TEXT_CONTENT_TYPES = {"application/javascript", "application/json"}


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


def publish_report(database: Path, report: dict, income: Sequence[dict] = (), fills: Sequence[dict] = ()) -> None:
    """Persist the latest complete account report, ledger events, fills, and equity observations."""
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
    with closing(open_sqlite_connection(database, readonly=False)) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS account_snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS income_ledger (
                income_type TEXT NOT NULL,
                transaction_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (income_type, transaction_id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS trade_fills (
                symbol TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (symbol, trade_id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS equity_observations (
                timestamp INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS account_recording (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL
            )
        """)
        if report.get("recording") is not None:
            connection.execute("""
                INSERT INTO account_recording VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """, (json.dumps(report["recording"], ensure_ascii=False, allow_nan=False),))
            desired_points = {
                int(point["timestamp"]): json.dumps(point, ensure_ascii=False, allow_nan=False)
                for point in report["equity_curve"]
            }
            existing_points = dict(
                connection.execute("SELECT timestamp, payload FROM equity_observations").fetchall()
            )
            stale_timestamps = [(ts,) for ts in existing_points if ts not in desired_points]
            if stale_timestamps:
                connection.executemany(
                    "DELETE FROM equity_observations WHERE timestamp = ?",
                    stale_timestamps,
                )
            changed_points = [
                (ts, pt_json)
                for ts, pt_json in desired_points.items()
                if existing_points.get(ts) != pt_json
            ]
            if changed_points:
                connection.executemany(
                    "INSERT INTO equity_observations VALUES (?, ?) "
                    "ON CONFLICT(timestamp) DO UPDATE SET payload = excluded.payload",
                    changed_points,
                )
        if income:
            connection.executemany(
                "INSERT OR IGNORE INTO income_ledger VALUES (?, ?, ?, ?)",
                [(item["incomeType"], str(item["tranId"]), item["time"],
                  json.dumps(item, ensure_ascii=False, allow_nan=False)) for item in income],
            )
        if fills:
            connection.executemany(
                "INSERT OR IGNORE INTO trade_fills VALUES (?, ?, ?, ?)",
                [(item["symbol"], str(item["id"]), item["time"],
                  json.dumps(item, ensure_ascii=False, allow_nan=False)) for item in fills],
            )
        connection.execute("""
            INSERT INTO account_snapshot (id, payload) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
        """, (payload,))


def load_report(database: Path) -> Optional[dict]:
    """Load the latest persisted account report from SQLite if available."""
    if not database.exists():
        return None
    with closing(open_sqlite_connection(database, readonly=True)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_snapshot'"
        ).fetchone()
        if table is None:
            return None
        snapshot = connection.execute("SELECT payload FROM account_snapshot WHERE id = 1").fetchone()
    return json.loads(snapshot[0]) if snapshot is not None else None


def create_handler(database: Path):
    """Build an HTTP request handler serving static HUD shells, /static/* assets, and JSON APIs."""
    web_root = WEB_DIR.resolve()

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
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_not_modified(self, etag: str) -> None:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def _send_json_with_etag(self, body: bytes) -> None:
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
            )

        def _read_snapshot(self, table: str, columns: str) -> tuple[Optional[tuple], Optional[int]]:
            if not database.exists():
                return None, 503
            try:
                with closing(open_sqlite_connection(database, readonly=True)) as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                    if exists is None:
                        return None, 503
                    row = connection.execute(
                        f"SELECT {columns} FROM {table} WHERE id = 1"
                    ).fetchone()
                    if row is None:
                        return None, 503
                    return row, None
            except sqlite3.Error as exc:
                self.log_error("SQLite error reading %s: %s", table, exc)
                return None, 500

        def do_GET(self) -> None:
            path = urlsplit(self.path).path

            if path.startswith("/static/"):
                rel_name = path[len("/static/"):]
                static_file = (web_root / rel_name).resolve()
                if (
                    not rel_name
                    or rel_name.startswith("/")
                    or not static_file.is_relative_to(web_root)
                    or not static_file.is_file()
                ):
                    self.send_error(404)
                    return
                self._send_bytes(
                    200,
                    static_file.read_bytes(),
                    _content_type_for(static_file),
                    cache_control="no-cache",
                )
                return

            if path == "/account/":
                self._send_redirect("../account.html")
                return

            if path == "/strategy/":
                self._send_redirect("../reports/strategy_second_leg_playbook.html")
                return

            if path in ("/", "/candidates", "/index.html"):
                from hud_common import render_index_shell_html

                self._send_bytes(
                    200,
                    render_index_shell_html().encode("utf-8"),
                    "text/html; charset=utf-8",
                    cache_control="no-cache",
                )
                return

            if path == "/account.html":
                from build_account_analytics import render_account_html

                self._send_bytes(
                    200,
                    render_account_html().encode("utf-8"),
                    "text/html; charset=utf-8",
                    cache_control="no-cache",
                )
                return

            if path == "/reports/strategy_second_leg_playbook.html":
                from hud_common import render_playbook_html

                self._send_bytes(
                    200,
                    render_playbook_html().encode("utf-8"),
                    "text/html; charset=utf-8",
                    cache_control="no-cache",
                )
                return

            if path in ("/account", "/api/account"):
                row, err_status = self._read_snapshot("account_snapshot", "payload")
                if err_status is not None:
                    self.send_error(
                        err_status,
                        "Snapshot unavailable" if err_status == 503 else "Database error",
                    )
                    return
                self._send_json_with_etag(row[0].encode("utf-8"))
                return

            if path in ("/candidate-version", "/api/candidate-version"):
                row, err_status = self._read_snapshot("candidate_snapshot", "updated_at")
                if err_status is not None:
                    self.send_error(
                        err_status,
                        "Snapshot unavailable" if err_status == 503 else "Database error",
                    )
                    return
                body = json.dumps({"updated_at": row[0]}, ensure_ascii=False).encode("utf-8")
                self._send_bytes(200, body, "application/json; charset=utf-8", cache_control="no-store")
                return

            if path == "/api/candidates":
                row, err_status = self._read_snapshot("candidate_snapshot", "payload, updated_at")
                if err_status is not None:
                    self.send_error(
                        err_status,
                        "Snapshot unavailable" if err_status == 503 else "Database error",
                    )
                    return
                body = json.dumps(
                    {"updated_at": row[1], "candidates": json.loads(row[0])},
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                self._send_json_with_etag(body)
                return

            self.send_error(404)

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
