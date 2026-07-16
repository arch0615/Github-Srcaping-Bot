#!/usr/bin/env python3
"""Web UI for the GitHub personal-email collector.

  * Browse / search / filter collected developers (server-side paginated).
  * Live stats (totals, top email providers, top locations).
  * Launch and stop scrape runs from the browser, with the same filters as
    the CLI, streaming the run log.

Run:  python webapp.py     ->  http://localhost:8000
Reads DATABASE_URL / GITHUB_TOKEN from .env, same as the scraper.
"""

from __future__ import annotations

import os
import sys
import signal
import subprocess
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for

from geo import CONTINENTS, COUNTRIES

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "run.log")
SEND_LOG_PATH = os.path.join(HERE, "send.log")
PAGE_SIZE = 50

# Default outreach template — used to pre-fill the plan form the first time.
DEFAULT_SUBJECT = "Collaborating on a project"
DEFAULT_BODY = (
    "Hi {first_name},\n\n"
    "I came across your GitHub profile and was impressed by your work. "
    "I'm looking for a developer to collaborate with me on a project and "
    "thought you might be a great fit.\n\n"
    "Would you be open to a short chat to see if it's interesting to you?\n\n"
    "Best,\n"
)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # pick up template edits without restart


# --- Optional HTTP Basic Auth ------------------------------------------------
# Active only when both DASH_USER and DASH_PASS are set. Protects the dashboard
# when it's exposed beyond localhost (it holds personal data + can send email).
import hmac
from functools import wraps
from flask import Response

DASH_USER = os.environ.get("DASH_USER", "")
DASH_PASS = os.environ.get("DASH_PASS", "")


@app.before_request
def _require_auth():
    if not (DASH_USER and DASH_PASS):
        return None  # auth disabled
    a = request.authorization
    ok = a and hmac.compare_digest(a.username or "", DASH_USER) \
           and hmac.compare_digest(a.password or "", DASH_PASS)
    if not ok:
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="GitHub Dev Collector"'})
    return None

# Tracks the single active scrape subprocess (None when idle).
_run: dict = {"proc": None, "started": None, "params": None}
# Tracks the single active send subprocess (None when idle).
_send: dict = {"proc": None, "started": None, "plan_id": None}


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
_schema_ready = False


def db():
    global _schema_ready
    dsn = os.environ.get("DATABASE_URL", "").strip() or DATABASE_URL
    if not dsn:
        sys.exit("ERROR: DATABASE_URL not set. See .env.example.")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    if not _schema_ready:
        # Idempotent — makes sure the outreach tables exist even if the scraper
        # has never run yet.
        try:
            with open(os.path.join(HERE, "schema.sql")) as f, conn.cursor() as cur:
                cur.execute(f.read())
            _schema_ready = True
        except Exception:
            pass
    return conn


def _scalar(cur):
    """First value of the current row, whether the cursor yields tuples or dicts."""
    row = cur.fetchone()
    if row is None:
        return None
    return row[0] if isinstance(row, (tuple, list)) else next(iter(row.values()))


def table_exists(cur) -> bool:
    cur.execute("SELECT to_regclass('public.developers') IS NOT NULL")
    return bool(_scalar(cur))


def fetch_stats(cur) -> dict:
    if not table_exists(cur):
        return {"total": 0, "providers": [], "countries": [], "continents": [],
                "skills": [], "yearly": [], "latest": None, "ready": False}
    cur.execute("SELECT count(*) FROM developers")
    total = _scalar(cur)
    cur.execute("""SELECT EXTRACT(YEAR FROM github_created)::int AS yr, count(*) c
                   FROM developers GROUP BY 1 ORDER BY 1""")
    yearly = cur.fetchall()
    cur.execute("""SELECT email_domain, count(*) c FROM developers
                   GROUP BY 1 ORDER BY c DESC LIMIT 8""")
    providers = cur.fetchall()
    cur.execute("""SELECT country, count(*) c FROM developers
                   WHERE country IS NOT NULL AND country <> 'Unknown'
                   GROUP BY 1 ORDER BY c DESC LIMIT 10""")
    countries = cur.fetchall()
    cur.execute("""SELECT continent, count(*) c FROM developers
                   WHERE continent IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 8""")
    continents = cur.fetchall()
    cur.execute("""SELECT skill, count(*) c FROM developers, unnest(tech_skills) skill
                   GROUP BY 1 ORDER BY c DESC, skill ASC LIMIT 8""")
    skills = cur.fetchall()
    cur.execute("SELECT max(collected_at) FROM developers")
    latest = _scalar(cur)
    return {"total": total, "providers": providers, "countries": countries,
            "continents": continents, "skills": skills, "yearly": yearly,
            "latest": latest, "ready": True}


def fetch_skill_options(cur):
    """Distinct tech skills present in the data, most common first (for the
    multi-select dropdown)."""
    if not table_exists(cur):
        return []
    cur.execute("""SELECT skill, count(*) c
                   FROM developers, unnest(tech_skills) AS skill
                   GROUP BY skill ORDER BY c DESC, skill ASC LIMIT 60""")
    return [r["skill"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]


def fetch_country_options(cur):
    """Distinct countries present in the data, most common first (for the filter)."""
    if not table_exists(cur):
        return []
    cur.execute("""SELECT country FROM developers
                   WHERE country IS NOT NULL AND country <> 'Unknown'
                   GROUP BY country ORDER BY count(*) DESC, country ASC""")
    return [r["country"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]


def fetch_developers(cur, q, domain, continent, country, skills, created_after, page):
    if not table_exists(cur):
        return [], 0
    where, params = [], []
    if q:
        where.append("(login ILIKE %s OR name ILIKE %s OR email ILIKE %s OR location ILIKE %s)")
        params += [f"%{q}%"] * 4
    if domain:
        where.append("email_domain = %s")
        params.append(domain)
    if continent:
        where.append("continent = %s")
        params.append(continent)
    if country:
        where.append("country = %s")
        params.append(country)
    if skills:
        # overlap: keep devs who have ANY of the selected skills
        where.append("tech_skills && %s")
        params.append(list(skills))
    if created_after:
        where.append("github_created >= %s")
        params.append(created_after)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    cur.execute(f"SELECT count(*) FROM developers {clause}", params)
    total = _scalar(cur)

    cur.execute(
        f"""SELECT login, name, email, email_domain, location, country, continent,
                   tech_skills, company, followers, public_repos, github_created, html_url
            FROM developers {clause}
            ORDER BY github_created DESC
            LIMIT %s OFFSET %s""",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    return cur.fetchall(), total


def page_window(page, pages, span=2):
    """Compact list of page numbers around the current page, with '...' gaps:
    e.g. [1, '...', 4, 5, 6, 7, 8, '...', 20]."""
    lo, hi = max(1, page - span), min(pages, page + span)
    out = []
    if lo > 1:
        out.append(1)
        if lo > 2:
            out.append("...")
    out.extend(range(lo, hi + 1))
    if hi < pages:
        if hi < pages - 1:
            out.append("...")
        out.append(pages)
    return out


# --------------------------------------------------------------------------- #
# Outreach helpers
# --------------------------------------------------------------------------- #
def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def eligible_count(cur, continent, country) -> int:
    """How many collected devs match the filter and were never emailed yet."""
    if not table_exists(cur):
        return 0
    where = ["email IS NOT NULL", "email <> ''"]
    params = []
    if continent:
        where.append("continent = %s")
        params.append(continent)
    if country:
        where.append("country = %s")
        params.append(country)
    where.append(
        "NOT EXISTS (SELECT 1 FROM outreach_messages m "
        "WHERE lower(m.email) = lower(developers.email) AND m.status = 'sent')"
    )
    cur.execute(f"SELECT count(*) FROM developers WHERE {' AND '.join(where)}", params)
    return _scalar(cur) or 0


def fetch_plans(cur, limit=20):
    cur.execute(
        """SELECT p.*,
                  (SELECT count(*) FROM outreach_messages m
                   WHERE m.plan_id = p.id AND m.status = 'sent')   AS sent_n,
                  (SELECT count(*) FROM outreach_messages m
                   WHERE m.plan_id = p.id AND m.status = 'failed')  AS failed_n,
                  (SELECT count(*) FROM outreach_messages m
                   WHERE m.plan_id = p.id AND m.status = 'skipped') AS skipped_n
           FROM outreach_plans p ORDER BY p.created_at DESC LIMIT %s""",
        (limit,),
    )
    return cur.fetchall()


def fetch_plan(cur, plan_id):
    cur.execute("SELECT * FROM outreach_plans WHERE id = %s", (plan_id,))
    return cur.fetchone()


def fetch_history(cur, status, page):
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(f"SELECT count(*) FROM outreach_messages {clause}", params)
    total = _scalar(cur)
    cur.execute(
        f"""SELECT id, plan_id, login, email, name, country, subject,
                   status, error, sent_at
            FROM outreach_messages {clause}
            ORDER BY sent_at DESC LIMIT %s OFFSET %s""",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    return cur.fetchall(), total


def history_stats(cur):
    cur.execute("""SELECT status, count(*) c FROM outreach_messages
                   GROUP BY status""")
    counts = {r["status"] if isinstance(r, dict) else r[0]:
              (r["c"] if isinstance(r, dict) else r[1]) for r in cur.fetchall()}
    return {"sent": counts.get("sent", 0), "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "total": sum(counts.values())}


def is_sending() -> bool:
    p = _send["proc"]
    return p is not None and p.poll() is None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def dashboard():
    conn = db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        stats = fetch_stats(cur)
    conn.close()
    return render_template("dashboard.html", active="dashboard",
                           stats=stats, running=is_running(), run=_run)


@app.route("/scrape")
def scrape_page():
    return render_template("scrape.html", active="scrape",
                           running=is_running(), run=_run,
                           today=date.today().isoformat(),
                           years=list(range(2018, max(2026, date.today().year) + 1)),
                           continents=[c for c in CONTINENTS if c != "Unknown"],
                           countries=COUNTRIES,
                           default_start=os.environ.get("START_DATE", "2021-01-01"))


@app.route("/users")
def users_page():
    q = request.args.get("q", "").strip()
    domain = request.args.get("domain", "").strip()
    continent = request.args.get("continent", "").strip()
    country = request.args.get("country", "").strip()
    skills = [s for s in request.args.getlist("skills") if s.strip()]
    created_after = request.args.get("created_after", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    conn = db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        skill_options = fetch_skill_options(cur)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        country_options = fetch_country_options(cur)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        rows, total = fetch_developers(cur, q, domain, continent, country, skills, created_after, page)
    conn.close()

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render_template(
        "users.html", active="users",
        page_window=page_window(page, pages),
        page_size=PAGE_SIZE,
        rows=rows, total=total, page=page, pages=pages,
        q=q, domain=domain, continent=continent, country=country, skills=skills,
        created_after=created_after,
        continents=[c for c in CONTINENTS if c != "Unknown"],
        country_options=country_options,
        skill_options=skill_options,
        running=is_running(), run=_run,
    )


@app.route("/run", methods=["POST"])
def run():
    if is_running():
        return redirect(url_for("scrape_page"))

    env = os.environ.copy()
    # Selected years (multi-checkbox) take precedence over the date range.
    years = sorted({y for y in request.form.getlist("years") if y.strip().isdigit()},
                   key=int)
    env["YEARS"] = ",".join(years)
    env["FILTER_CONTINENTS"] = ",".join(c for c in request.form.getlist("continents") if c.strip())
    env["FILTER_COUNTRIES"] = ",".join(c for c in request.form.getlist("countries") if c.strip())
    env["START_DATE"] = request.form.get("start_date", "2021-01-01").strip() or "2021-01-01"
    env["END_DATE"] = request.form.get("end_date", "").strip()
    env["EXTRA_QUALIFIERS"] = request.form.get("qualifiers", "").strip()
    env["MAX_USERS"] = request.form.get("max_users", "0").strip() or "0"

    logf = open(LOG_PATH, "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(HERE, "github_scraper.py")],
        cwd=HERE, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    _run.update({"proc": proc, "started": datetime.now(timezone.utc),
                 "params": {k: env[k] for k in ("YEARS", "FILTER_CONTINENTS",
                                                 "FILTER_COUNTRIES", "START_DATE",
                                                 "END_DATE", "EXTRA_QUALIFIERS",
                                                 "MAX_USERS")}})
    return redirect(url_for("scrape_page"))


@app.route("/stop", methods=["POST"])
def stop():
    if is_running():
        _run["proc"].send_signal(signal.SIGINT)  # graceful: finishes current item
    return redirect(url_for("scrape_page"))


@app.route("/status")
def status():
    """Polled by the page: run state + tail of the log."""
    tail = ""
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, errors="replace") as f:
            tail = "".join(f.readlines()[-200:])
    rc = None if (_run["proc"] is None) else _run["proc"].poll()
    return jsonify({
        "running": is_running(),
        "returncode": rc,
        "started": _run["started"].isoformat() if _run["started"] else None,
        "params": _run["params"],
        "log": tail,
    })


def is_running() -> bool:
    p = _run["proc"]
    return p is not None and p.poll() is None


# --------------------------------------------------------------------------- #
# Outreach routes
# --------------------------------------------------------------------------- #
def smtp_ready() -> bool:
    return all(os.environ.get(k, "").strip()
               for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")) and \
        bool(os.environ.get("FROM_EMAIL", "").strip()
             or os.environ.get("SMTP_USER", "").strip())


@app.route("/outreach")
def outreach_page():
    conn = db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        country_options = fetch_country_options(cur)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        plans = fetch_plans(cur)
    conn.close()
    sending_plan = _send["plan_id"] if is_sending() else None
    return render_template(
        "outreach.html", active="outreach",
        continents=[c for c in CONTINENTS if c != "Unknown"],
        country_options=country_options,
        plans=plans,
        default_subject=DEFAULT_SUBJECT, default_body=DEFAULT_BODY,
        smtp_ready=smtp_ready(),
        from_email=os.environ.get("FROM_EMAIL", "").strip()
                   or os.environ.get("SMTP_USER", "").strip(),
        sending=is_sending(), sending_plan=sending_plan,
        running=is_running(), run=_run,
    )


@app.route("/outreach/preview")
def outreach_preview():
    """AJAX: how many devs match the chosen continent/country and are unseen."""
    continent = request.args.get("continent", "").strip()
    country = request.args.get("country", "").strip()
    conn = db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        n = eligible_count(cur, continent, country)
    conn.close()
    return jsonify({"eligible": n})


@app.route("/outreach/plan", methods=["POST"])
def outreach_create_plan():
    continent = request.form.get("continent", "").strip() or None
    country = request.form.get("country", "").strip() or None
    subject = request.form.get("subject", "").strip() or DEFAULT_SUBJECT
    body = request.form.get("body", "").strip() or DEFAULT_BODY
    try:
        count = max(0, int(request.form.get("send_count", "0").strip() or "0"))
    except ValueError:
        count = 0
    week_start = monday_of(date.today())

    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO outreach_plans
                   (week_start, continent, country, send_count, subject, body)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (week_start, continent, country, count, subject, body),
        )
    conn.close()
    return redirect(url_for("outreach_page"))


@app.route("/outreach/send/<int:plan_id>", methods=["POST"])
def outreach_send(plan_id):
    if is_sending():
        return redirect(url_for("outreach_page"))
    conn = db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        plan = fetch_plan(cur, plan_id)
    conn.close()
    if not plan:
        return redirect(url_for("outreach_page"))

    env = os.environ.copy()
    env["PLAN_ID"] = str(plan_id)
    logf = open(SEND_LOG_PATH, "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(HERE, "outreach.py")],
        cwd=HERE, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    _send.update({"proc": proc, "started": datetime.now(timezone.utc),
                  "plan_id": plan_id})
    return redirect(url_for("outreach_page"))


@app.route("/outreach/stop", methods=["POST"])
def outreach_stop():
    if is_sending():
        _send["proc"].send_signal(signal.SIGINT)  # graceful: finishes current msg
    return redirect(url_for("outreach_page"))


@app.route("/outreach/status")
def outreach_status():
    tail = ""
    if os.path.exists(SEND_LOG_PATH):
        with open(SEND_LOG_PATH, errors="replace") as f:
            tail = "".join(f.readlines()[-200:])
    rc = None if (_send["proc"] is None) else _send["proc"].poll()
    return jsonify({
        "sending": is_sending(),
        "returncode": rc,
        "plan_id": _send["plan_id"],
        "started": _send["started"].isoformat() if _send["started"] else None,
        "log": tail,
    })


@app.route("/history")
def history_page():
    status_filter = request.args.get("status", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    conn = db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        rows, total = fetch_history(cur, status_filter, page)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        stats = history_stats(cur)
    conn.close()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render_template(
        "history.html", active="history",
        rows=rows, total=total, page=page, pages=pages,
        page_size=PAGE_SIZE, page_window=page_window(page, pages),
        status=status_filter, stats=stats,
        running=is_running(), run=_run,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="127.0.0.1", port=port, debug=False)
