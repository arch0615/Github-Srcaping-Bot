#!/usr/bin/env python3
"""Outreach engine: send a weekly plan's messages to collected developers.

Selects developers matching a plan's continent/country filter that have **not**
been successfully emailed before, personalises the template, sends over SMTP
(your own domain), throttles between sends, and records every attempt in
`outreach_messages`.

Runnable as a script (the web UI launches it this way):

    PLAN_ID=3 python outreach.py

Config (read from .env, same as the scraper):

    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS
    FROM_EMAIL (default SMTP_USER), FROM_NAME
    SEND_DELAY   seconds to wait between messages (default 8)
    DRY_RUN      "1" to simulate without actually sending (auto-on if SMTP unset)

Safety: if SMTP is not fully configured, the run falls back to DRY_RUN — nothing
leaves your machine, attempts are recorded with status 'skipped'.
"""

from __future__ import annotations

import os
import sys
import ssl
import time
import signal
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
_STOP = False


def _handle_sigint(signum, frame):
    global _STOP
    _STOP = True
    print("\n>> Stop requested — finishing current message, then exiting.", flush=True)


signal.signal(signal.SIGINT, _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigint)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def smtp_config() -> dict:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
    from_email = os.environ.get("FROM_EMAIL", "").strip() or user
    from_name = os.environ.get("FROM_NAME", "").strip()
    configured = bool(host and user and pw and from_email)
    dry = os.environ.get("DRY_RUN", "").strip() == "1" or not configured
    return {"host": host, "port": port, "user": user, "pass": pw,
            "from_email": from_email, "from_name": from_name,
            "configured": configured, "dry_run": dry}


def send_delay() -> float:
    try:
        return max(0.0, float(os.environ.get("SEND_DELAY", "8").strip() or "8"))
    except ValueError:
        return 8.0


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def db():
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        sys.exit("ERROR: DATABASE_URL not set. Launch via serve.py or set it in .env.")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with open(os.path.join(HERE, "schema.sql")) as f, conn.cursor() as cur:
        cur.execute(f.read())
    return conn


def load_plan(cur, plan_id: int) -> dict | None:
    cur.execute("SELECT * FROM outreach_plans WHERE id = %s", (plan_id,))
    return cur.fetchone()


def select_recipients(cur, plan: dict) -> list[dict]:
    """Developers matching the plan's filters that were never successfully
    emailed, newest accounts first, capped at the plan's per-day cap.
    A blank/zero per_day means no cap — every eligible recipient in the filter."""
    where = ["d.email IS NOT NULL", "d.email <> ''"]
    params: list = []
    if plan.get("continent"):
        where.append("d.continent = %s")
        params.append(plan["continent"])
    if plan.get("country"):
        where.append("d.country = %s")
        params.append(plan["country"])
    # exclude anyone already emailed successfully (any plan)
    where.append(
        "NOT EXISTS (SELECT 1 FROM outreach_messages m "
        "WHERE lower(m.email) = lower(d.email) AND m.status = 'sent')"
    )
    clause = " AND ".join(where)
    limit = max(0, int(plan.get("per_day") or 0))
    limit_sql = "LIMIT %s" if limit else ""
    cur.execute(
        f"""SELECT d.login, d.name, d.email, d.country, d.location
            FROM developers d
            WHERE {clause}
            ORDER BY d.github_created DESC
            {limit_sql}""",
        params + ([limit] if limit else []),
    )
    return cur.fetchall()


def record(cur, plan_id: int, dev: dict, subject: str, body: str,
           status: str, error: str | None):
    cur.execute(
        """INSERT INTO outreach_messages
               (plan_id, login, email, name, country, subject, body, status, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (plan_id, dev["login"], dev["email"], dev.get("name"),
         dev.get("country"), subject, body, status, error),
    )


def set_plan_status(cur, plan_id: int, status: str):
    cur.execute("UPDATE outreach_plans SET status = %s WHERE id = %s",
                (status, plan_id))


def bump_sent(cur, plan_id: int):
    cur.execute("UPDATE outreach_plans SET sent = sent + 1 WHERE id = %s", (plan_id,))


# --------------------------------------------------------------------------- #
# Templating + sending
# --------------------------------------------------------------------------- #
def personalise(text: str, dev: dict) -> str:
    """Replace the known {tokens}; leaves any other braces untouched."""
    name = (dev.get("name") or "").strip()
    first = name.split()[0] if name else (dev.get("login") or "there")
    repl = {
        "{name}": name or dev.get("login") or "there",
        "{first_name}": first,
        "{login}": dev.get("login") or "",
        "{country}": dev.get("country") or "",
        "{location}": dev.get("location") or "",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def build_message(cfg: dict, dev: dict, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"] or None, cfg["from_email"]))
    msg["To"] = formataddr((dev.get("name") or None, dev["email"]))
    msg["Reply-To"] = cfg["from_email"]
    msg.set_content(body)
    return msg


def open_smtp(cfg: dict):
    if cfg["port"] == 465:
        s = smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                             context=ssl.create_default_context(), timeout=30)
    else:
        s = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        s.ehlo()
        s.starttls(context=ssl.create_default_context())
        s.ehlo()
    s.login(cfg["user"], cfg["pass"])
    return s


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run_plan(plan_id: int):
    cfg = smtp_config()
    delay = send_delay()
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    plan = load_plan(cur, plan_id)
    if not plan:
        sys.exit(f"ERROR: plan {plan_id} not found.")

    print(f">> Plan #{plan_id} — week of {plan['week_start']} | "
          f"continent={plan['continent'] or 'any'} country={plan['country'] or 'any'} "
          f"per_day={plan['per_day'] or 'all eligible'}", flush=True)
    if cfg["dry_run"]:
        why = "SMTP not configured" if not cfg["configured"] else "DRY_RUN=1"
        print(f">> DRY RUN ({why}) — no email will actually be sent.", flush=True)
    else:
        print(f">> Sending as {formataddr((cfg['from_name'] or None, cfg['from_email']))} "
              f"via {cfg['host']}:{cfg['port']} | {delay:g}s between messages", flush=True)

    recipients = select_recipients(cur, plan)
    print(f">> {len(recipients)} eligible recipient(s) selected.\n", flush=True)
    if not recipients:
        set_plan_status(cur, plan_id, "done")
        print(">> Nothing to send. Done.", flush=True)
        return

    set_plan_status(cur, plan_id, "sending")
    smtp = None
    sent = failed = skipped = 0
    try:
        for i, dev in enumerate(recipients, 1):
            if _STOP:
                print(">> Stopped before sending remaining messages.", flush=True)
                break
            subject = personalise(plan["subject"], dev)
            body = personalise(plan["body"], dev)
            label = f"[{i}/{len(recipients)}] {dev['login']} <{dev['email']}>"

            if cfg["dry_run"]:
                record(cur, plan_id, dev, subject, body, "skipped", "dry-run")
                skipped += 1
                print(f"  ~ {label}  (dry-run, recorded as skipped)", flush=True)
                continue

            try:
                if smtp is None:
                    smtp = open_smtp(cfg)
                smtp.send_message(build_message(cfg, dev, subject, body))
                record(cur, plan_id, dev, subject, body, "sent", None)
                bump_sent(cur, plan_id)
                sent += 1
                print(f"  ✓ {label}", flush=True)
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as e:
                # connection dropped — try once to reopen on the next iteration
                smtp = None
                record(cur, plan_id, dev, subject, body, "failed", str(e))
                failed += 1
                print(f"  ✗ {label}  ({e})", flush=True)
            except Exception as e:  # noqa: BLE001 — record and keep going
                record(cur, plan_id, dev, subject, body, "failed", str(e))
                failed += 1
                print(f"  ✗ {label}  ({e})", flush=True)

            if i < len(recipients) and not _STOP and not cfg["dry_run"]:
                time.sleep(delay)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass
        set_plan_status(cur, plan_id, "stopped" if _STOP else "done")

    print(f"\n>> Finished. sent={sent} failed={failed} skipped={skipped}", flush=True)


def main():
    raw = os.environ.get("PLAN_ID", "").strip() or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not raw:
        sys.exit("ERROR: set PLAN_ID env var (or pass a plan id argument).")
    run_plan(int(raw))


if __name__ == "__main__":
    main()
