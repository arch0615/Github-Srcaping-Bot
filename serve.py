#!/usr/bin/env python3
"""Zero-setup launcher: boots an embedded PostgreSQL (no root / Docker needed)
then serves the web UI. Use this when you don't have a system Postgres.

    python serve.py        ->  http://localhost:8000

The database lives in ./pgdata and persists between runs.
"""
import os

import pgserver

HERE = os.path.dirname(os.path.abspath(__file__))
# PostgreSQL's unix socket dir cannot contain spaces, and this project's path
# does ("github scrapping"), so keep the data dir at a space-free location.
PGDATA = os.environ.get("PGDATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".ghscraper", "pgdata")

print(f">> Starting embedded PostgreSQL (data dir: {PGDATA})...")
os.makedirs(PGDATA, exist_ok=True)
server = pgserver.get_server(PGDATA)
uri = server.get_uri()
print(f">> PostgreSQL ready: {uri}")

# Make the connection string available to the web app *and* to the scrape
# subprocesses it launches (they inherit this process's environment).
os.environ["DATABASE_URL"] = uri

# Import after DATABASE_URL is set so the app picks it up.
import webapp  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # Bind to all interfaces by default so the dashboard is reachable from
    # other machines (e.g. http://<server-ip>:8000). Set HOST=127.0.0.1 to
    # restrict it to this machine only.
    host = os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0"
    shown = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f">> Web UI:  http://{shown}:{port}  (binding {host})")
    if not (os.environ.get("DASH_USER") and os.environ.get("DASH_PASS")):
        print(">> WARNING: no DASH_USER/DASH_PASS set — the dashboard is "
              "UNAUTHENTICATED. Anyone who can reach this port can read the "
              "collected personal emails and trigger sends. Set them in .env.")
    try:
        webapp.app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        print(">> Shutting down PostgreSQL...")
        server.cleanup()
