"""Store the last complete account report and serve it over a loopback HTTP endpoint."""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence


def publish_report(database: Path, report: dict, income: Sequence[dict] = (), fills: Sequence[dict] = ()) -> None:
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
            connection.executemany(
                "INSERT INTO equity_observations VALUES (?, ?) "
                "ON CONFLICT(timestamp) DO UPDATE SET payload = excluded.payload",
                [(point["timestamp"], json.dumps(point, ensure_ascii=False, allow_nan=False))
                 for point in report["equity_curve"]],
            )
        connection.executemany(
            "INSERT OR IGNORE INTO income_ledger VALUES (?, ?, ?, ?)",
            [(item["incomeType"], str(item["tranId"]), item["time"],
              json.dumps(item, ensure_ascii=False, allow_nan=False)) for item in income],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO trade_fills VALUES (?, ?, ?, ?)",
            [(item["symbol"], str(item["id"]), item["time"],
              json.dumps(item, ensure_ascii=False, allow_nan=False)) for item in fills],
        )
        connection.execute("""
            INSERT INTO account_snapshot (id, payload) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
        """, (payload,))


def load_report(database: Path) -> dict | None:
    if not database.exists():
        return None
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_snapshot'"
        ).fetchone()
        if table is None:
            return None
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
            if self.path not in ("/account", "/candidates", "/candidate-version"):
                self.send_error(404)
                return
            try:
                with closing(sqlite3.connect(database_uri, uri=True)) as connection:
                    if self.path == "/account":
                        snapshot = connection.execute(
                            "SELECT payload FROM account_snapshot WHERE id = 1"
                        ).fetchone()
                    else:
                        snapshot = connection.execute(
                            "SELECT payload, updated_at FROM candidate_snapshot WHERE id = 1"
                        ).fetchone()
            except sqlite3.OperationalError:
                self.log_error("Could not read account snapshot")
                snapshot = None

            if snapshot is None:
                self.send_error(503, "Account snapshot unavailable")
                return

            if self.path == "/candidate-version":
                body = json.dumps({"updated_at": snapshot[1]}).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path == "/candidates":
                from build_index import HEAD_HTML
                from hud_common import compute_pool_stats, render_index_body

                candidates = json.loads(snapshot[0])
                version = json.dumps(snapshot[1])
                refresh_script = """
<script>
  const candidateVersion = VERSION;
  setInterval(async () => {
    if (document.hidden) return;
    try {
      const response = await fetch('/ai-coin-selector/api/candidate-version', {cache: 'no-store'});
      if (response.ok && (await response.json()).updated_at !== candidateVersion) location.reload();
    } catch (error) {
      console.warn('候选池更新检查失败', error);
    }
  }, 30000);
</script>
""".replace("VERSION", version)
                page = HEAD_HTML + "\n" + render_index_body(
                    candidates, compute_pool_stats(candidates)
                )
                updated = datetime.fromisoformat(snapshot[1]).replace(tzinfo=timezone.utc)
                local_time = updated.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                page = page.replace(
                    "</nav>",
                    f'</nav><span class="text-xs text-slate-400">候选池更新：{local_time}（北京时间）</span>',
                    1,
                )
                page = page.replace('href="index.html"', 'href="/ai-coin-selector/"')
                page = page.replace('href="account.html"', 'href="/ai-coin-selector/account/"')
                page = page.replace(
                    'href="reports/strategy_second_leg_playbook.html"',
                    'href="/ai-coin-selector/strategy/"',
                )
                body = page.replace("</body>", refresh_script + "</body>").encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                body = snapshot[0].encode("utf-8")
                content_type = "application/json; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
