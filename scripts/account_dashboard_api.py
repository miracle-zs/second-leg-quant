"""Store the last complete account report and serve it over a loopback HTTP endpoint."""
import argparse
import json
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def publish_report(database: Path, report: dict) -> None:
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS account_snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL
            )
        """)
        connection.execute("""
            INSERT INTO account_snapshot (id, payload) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
        """, (payload,))


def load_report(database: Path) -> dict | None:
    if not database.exists():
        return None
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        snapshot = connection.execute("SELECT payload FROM account_snapshot WHERE id = 1").fetchone()
    return json.loads(snapshot[0]) if snapshot is not None else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18083)
    args = parser.parse_args()
    database_uri = args.database.resolve().as_uri() + "?mode=ro"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/account":
                self.send_error(404)
                return
            try:
                with closing(sqlite3.connect(database_uri, uri=True)) as connection:
                    snapshot = connection.execute(
                        "SELECT payload FROM account_snapshot WHERE id = 1"
                    ).fetchone()
            except sqlite3.OperationalError:
                self.log_error("Could not read account snapshot")
                snapshot = None

            if snapshot is None:
                self.send_error(503, "Account snapshot unavailable")
                return

            body = snapshot[0].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
