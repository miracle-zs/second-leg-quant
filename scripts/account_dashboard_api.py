"""Store the last complete account report and serve it over a loopback HTTP endpoint."""
import argparse
import json
import mimetypes
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
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


def load_report(database: Path) -> Optional[dict]:
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


def render_candidates_page(
    candidates: list,
    updated_at: str,
    base_prefix: str = "",
) -> bytes:
    """渲染候选池看板 HTML，自动兼容 Nginx 反代前缀与本地直连模式。"""
    from build_index import HEAD_HTML
    from hud_common import compute_pool_stats, render_index_body

    prefix = base_prefix.rstrip("/")
    version_json = json.dumps(updated_at)
    version_url = f"{prefix}/api/candidate-version" if prefix else "api/candidate-version"

    refresh_script = f"""
<script>
  const candidateVersion = {version_json};
  const versionEndpoint = window.location.pathname.startsWith('/ai-coin-selector')
    ? '/ai-coin-selector/api/candidate-version'
    : {json.dumps(version_url)};
  setInterval(async () => {{
    if (document.hidden) return;
    try {{
      const response = await fetch(versionEndpoint, {{cache: 'no-store'}});
      if (response.ok && (await response.json()).updated_at !== candidateVersion) location.reload();
    }} catch (error) {{
      console.warn('候选池更新检查失败', error);
    }}
  }}, 30000);
</script>
"""
    page = HEAD_HTML + "\n" + render_index_body(candidates, compute_pool_stats(candidates))
    updated = datetime.fromisoformat(updated_at).replace(tzinfo=timezone.utc)
    local_time = updated.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    page = page.replace(
        "</nav>",
        f'</nav><span class="text-xs text-slate-400">候选池更新：{local_time}（北京时间）</span>',
        1,
    )
    if prefix:
        page = page.replace('href="index.html"', f'href="{prefix}/"')
        page = page.replace('href="account.html"', f'href="{prefix}/account/"')
        page = page.replace('href="reports/strategy_second_leg_playbook.html"', f'href="{prefix}/strategy/"')
    else:
        page = page.replace('href="index.html"', 'href="./"')
        page = page.replace('href="account.html"', 'href="./account/"')
        page = page.replace('href="reports/strategy_second_leg_playbook.html"', 'href="./strategy/"')
    return page.replace("</body>", refresh_script + "</body>").encode("utf-8")


def create_handler(database: Path, base_prefix: str = ""):
    database_uri = database.resolve().as_uri() + "?mode=ro"

    class Handler(BaseHTTPRequestHandler):
        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            # 忽略 query string，避免 /candidates?t=123 或 /account?v=1 返回 404
            path = urlsplit(self.path).path

            # 1. 独立静态资源服务 (/static/* -> web/*)
            if path.startswith("/static/"):
                rel_name = path[len("/static/"):]
                static_file = (WEB_DIR / rel_name).resolve()
                if not str(static_file).startswith(str(WEB_DIR.resolve())) or not static_file.is_file():
                    self.send_error(404)
                    return
                ctype, _ = mimetypes.guess_type(str(static_file))
                self._send_bytes(200, static_file.read_bytes(), f"{ctype or 'application/octet-stream'}; charset=utf-8")
                return

            # 2. 本地直连账户页与战法页支持
            if path in ("/account/", "/account.html"):
                from build_account_analytics import render_account_html
                html_bytes = render_account_html().encode("utf-8")
                self._send_bytes(200, html_bytes, "text/html; charset=utf-8")
                return

            if path in ("/strategy/", "/reports/strategy_second_leg_playbook.html"):
                playbook = PROJECT_ROOT / "reports" / "strategy_second_leg_playbook.html"
                if playbook.is_file():
                    self._send_bytes(200, playbook.read_bytes(), "text/html; charset=utf-8")
                    return
                self.send_error(404)
                return

            # 3. JSON API 与候选池页面路由
            if path not in (
                "/",
                "/account",
                "/api/account",
                "/candidates",
                "/api/candidates",
                "/candidate-version",
                "/api/candidate-version",
            ):
                self.send_error(404)
                return

            is_account_req = path in ("/account", "/api/account")
            try:
                with closing(sqlite3.connect(database_uri, uri=True)) as connection:
                    if is_account_req:
                        snapshot = connection.execute(
                            "SELECT payload FROM account_snapshot WHERE id = 1"
                        ).fetchone()
                    else:
                        snapshot = connection.execute(
                            "SELECT payload, updated_at FROM candidate_snapshot WHERE id = 1"
                        ).fetchone()
            except sqlite3.OperationalError:
                self.log_error("Could not read snapshot from SQLite")
                snapshot = None

            if snapshot is None:
                self.send_error(503, "Snapshot unavailable")
                return

            if path in ("/candidate-version", "/api/candidate-version"):
                body = json.dumps({"updated_at": snapshot[1]}).encode("utf-8")
                self._send_bytes(200, body, "application/json; charset=utf-8")
                return

            if path == "/api/candidates":
                self._send_bytes(200, snapshot[0].encode("utf-8"), "application/json; charset=utf-8")
                return

            if path in ("/", "/candidates"):
                req_prefix = self.headers.get("X-Forwarded-Prefix", "").strip() or base_prefix
                candidates = json.loads(snapshot[0])
                body = render_candidates_page(candidates, snapshot[1], base_prefix=req_prefix)
                self._send_bytes(200, body, "text/html; charset=utf-8")
                return

            # /account 或 /api/account 返回账户 JSON
            self._send_bytes(200, snapshot[0].encode("utf-8"), "application/json; charset=utf-8")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument(
        "--base-prefix",
        type=str,
        default="",
        help="可选反向代理前缀（留空时自动使用相对路径，同时兼容本地直连与 Nginx 反代）",
    )
    args = parser.parse_args()
    handler_cls = create_handler(args.database, base_prefix=args.base_prefix)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_cls) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
