#!/usr/bin/env python3
"""Find GitHub developers who created their account on/after a start date
(default 2021-01-01) AND expose a public *personal* email, then store them
in PostgreSQL.

Pipeline:
  1. Search users with `created:<window> type:user [extra qualifiers]`,
     slicing the date range so each window stays under GitHub's 1000-result
     search cap (recursive bisection when a window is too dense).
  2. For every login found, fetch the full profile to read its email.
  3. Keep only profiles whose email domain is a known consumer provider
     (see personal_domains.py); skip orgs, empty emails and business domains.
  4. Upsert into PostgreSQL.  Already-seen logins are skipped on re-runs.

Run:  python github_scraper.py
Config comes from environment / .env (see .env.example).
"""

from __future__ import annotations

import os
import re
import sys
import time
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime, timedelta, timezone

import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from personal_domains import is_personal_email, domain_of
from geo import continent_of, country_of, is_india_or_pakistan

load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


def parse_tokens(raw: str) -> list[str]:
    """Accept one or many tokens separated by commas / whitespace / newlines."""
    import re
    toks = [t.strip() for t in re.split(r"[\s,]+", raw) if t.strip()]
    seen, out = set(), []
    for t in toks:               # de-dupe, preserve order
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Comma-separated years to scrape (e.g. "2021,2023"). Takes precedence over the
# START_DATE/END_DATE range when set.
YEARS = os.environ.get("YEARS", "").strip()
START_DATE = os.environ.get("START_DATE", "2021-01-01").strip()
END_DATE = os.environ.get("END_DATE", "").strip()
EXTRA_QUALIFIERS = os.environ.get("EXTRA_QUALIFIERS", "").strip()
MAX_USERS = int(os.environ.get("MAX_USERS", "0") or "0")
# Optional geo allow-lists (comma-separated). When either is set, only keep
# developers whose continent OR country is among the selections — this also
# overrides the default "Asia except India/Pakistan" rule.
FILTER_CONTINENTS = {c.strip() for c in os.environ.get("FILTER_CONTINENTS", "").split(",") if c.strip()}
FILTER_COUNTRIES = {c.strip() for c in os.environ.get("FILTER_COUNTRIES", "").split(",") if c.strip()}
# Concurrent profile/repo fetches. 0 => auto (a few per token, capped).
WORKERS = int(os.environ.get("WORKERS", "0") or "0")

API = "https://api.github.com"
SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_CAP = 1000  # GitHub hard cap per search query

_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    print("\n[!] Stop requested — finishing current item, then exiting...", flush=True)


signal.signal(signal.SIGINT, _handle_sigint)


# --------------------------------------------------------------------------- #
# GitHub HTTP client with rate-limit handling
# --------------------------------------------------------------------------- #
class _Token:
    """One token's HTTP session plus the epoch until which it's rate-limited."""
    def __init__(self, token: str):
        self.token = token
        self.blocked_until = 0.0          # epoch; 0 = available now
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personal-email-collector",
        })


class GitHub:
    """Round-robins requests across N tokens, multiplying the rate-limit budget.

    A token that hits a primary or secondary limit is parked until its reset;
    requests rotate to the next available token. Only when *every* token is
    parked do we sleep — until the soonest reset."""

    def __init__(self, tokens: list[str]):
        if not tokens:
            sys.exit("ERROR: GITHUB_TOKEN is not set. See .env.example.")
        self.pool = [_Token(t) for t in tokens]
        self.rr = 0
        self._lock = threading.Lock()   # guards rr + token parking (multi-threaded)
        print(f"Using {len(self.pool)} GitHub token(s).")

    def _acquire(self):
        """Thread-safe: return (token, delay). token=None means every token is
        parked — the caller should sleep `delay` seconds and retry. Never sleeps
        while holding the lock, so many threads can pull tokens concurrently."""
        with self._lock:
            n = len(self.pool)
            now = time.time()
            for i in range(n):
                tok = self.pool[(self.rr + i) % n]
                if tok.blocked_until <= now:
                    self.rr = (self.rr + i + 1) % n
                    return tok, 0
            delay = max(min(t.blocked_until for t in self.pool) - now, 1)
            return None, min(delay, 30)

    def _park(self, tok: _Token, until: float):
        with self._lock:
            tok.blocked_until = max(tok.blocked_until, until)

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET that rotates tokens on rate limits instead of blocking. Safe to
        call from many threads. Transient network errors are retried with
        backoff so a blip never ends the run — only _stop / Ctrl-C breaks out."""
        net_fails = 0
        while True:
            if _stop:
                raise KeyboardInterrupt
            tok, delay = self._acquire()
            if tok is None:
                time.sleep(delay)
                continue
            try:
                r = tok.s.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                net_fails += 1
                back = min(2 ** min(net_fails, 6), 60)  # 2,4,8,…,60s
                print(f"    [network error: {type(e).__name__}] retry in {back}s", flush=True)
                time.sleep(back)
                continue
            net_fails = 0

            if r.status_code == 403 and "secondary rate limit" in r.text.lower():
                self._park(tok, time.time() + int(r.headers.get("Retry-After", "60")))
                continue  # rotate to another token

            if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
                self._park(tok, max(int(r.headers.get("X-RateLimit-Reset", "0")), time.time() + 2))
                continue  # rotate to another token

            # Proactively park a token that just used its last unit.
            if r.headers.get("X-RateLimit-Remaining") == "0":
                self._park(tok, max(int(r.headers.get("X-RateLimit-Reset", "0")), time.time() + 1))
            return r

    def throttle_search(self, resp: requests.Response):
        """Search API allows 30 req/min *per token*. With rotation the budget
        is shared, so only a light pause is needed to avoid abuse detection."""
        remaining = resp.headers.get("X-RateLimit-Remaining")
        # Per-request pacing scales down with more tokens (min 0.3s).
        time.sleep(max(2.1 / len(self.pool), 0.3))


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class DB:
    def __init__(self, dsn: str):
        if not dsn:
            sys.exit("ERROR: DATABASE_URL is not set. See .env.example.")
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
            with self.conn.cursor() as cur:
                cur.execute(f.read())

    def already_seen(self, login: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM seen_logins WHERE login = %s", (login,))
            return cur.fetchone() is not None

    def mark_seen(self, login: str, status: str):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO seen_logins (login, status) VALUES (%s, %s) "
                "ON CONFLICT (login) DO UPDATE SET status = EXCLUDED.status, examined_at = now()",
                (login, status),
            )

    def upsert_developer(self, u: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO developers (
                    id, login, name, email, email_domain, company, location, country,
                    continent, tech_skills, bio, blog, twitter, public_repos,
                    public_gists, followers, following, hireable, github_created, html_url
                ) VALUES (
                    %(id)s, %(login)s, %(name)s, %(email)s, %(email_domain)s, %(company)s,
                    %(location)s, %(country)s, %(continent)s, %(tech_skills)s, %(bio)s,
                    %(blog)s, %(twitter)s, %(public_repos)s, %(public_gists)s, %(followers)s,
                    %(following)s, %(hireable)s, %(github_created)s, %(html_url)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    email = EXCLUDED.email,
                    email_domain = EXCLUDED.email_domain,
                    name = EXCLUDED.name,
                    location = EXCLUDED.location,
                    country = EXCLUDED.country,
                    continent = EXCLUDED.continent,
                    tech_skills = EXCLUDED.tech_skills,
                    followers = EXCLUDED.followers,
                    public_repos = EXCLUDED.public_repos,
                    collected_at = now()
                """,
                u,
            )

    def count_developers(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM developers")
            return cur.fetchone()[0]


# --------------------------------------------------------------------------- #
# Search with recursive date-window slicing
# --------------------------------------------------------------------------- #
def search_user_logins(gh: GitHub, start: date, end: date, qualifiers: str):
    """Yield logins for `created:start..end type:user <qualifiers>`,
    bisecting the window whenever total_count exceeds the 1000 cap."""
    q = f"created:{start.isoformat()}..{end.isoformat()} type:user"
    if qualifiers:
        q += f" {qualifiers}"

    resp = gh.get(f"{API}/search/users", params={"q": q, "per_page": 1})
    gh.throttle_search(resp)
    if resp.status_code != 200:
        print(f"    [search error {resp.status_code}] {resp.text[:160]}", flush=True)
        return
    try:
        total = resp.json().get("total_count", 0)
    except ValueError:
        print("    [search: bad JSON, skipping window]", flush=True)
        return

    if total == 0:
        return

    # Too dense: split the window. If it's a single day we can't split further,
    # so we accept the 1000-result truncation for that day.
    if total > SEARCH_RESULT_CAP and start < end:
        mid = start + (end - start) // 2
        yield from search_user_logins(gh, start, mid, qualifiers)
        yield from search_user_logins(gh, mid + timedelta(days=1), end, qualifiers)
        return

    if total > SEARCH_RESULT_CAP:
        print(f"    [warn] {start} has {total} users; only first {SEARCH_RESULT_CAP} reachable", flush=True)

    pages = min((total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE,
                SEARCH_RESULT_CAP // SEARCH_PAGE_SIZE)
    label = start.isoformat() if start == end else f"{start}..{end}"
    print(f"  window {label}: {total} users ({pages} pages)", flush=True)

    for page in range(1, pages + 1):
        resp = gh.get(f"{API}/search/users",
                      params={"q": q, "per_page": SEARCH_PAGE_SIZE, "page": page})
        gh.throttle_search(resp)
        if resp.status_code != 200:
            print(f"    [page {page} error {resp.status_code}]", flush=True)
            break
        try:
            items = resp.json().get("items", [])
        except ValueError:
            print(f"    [page {page}: bad JSON, skipping]", flush=True)
            break
        for item in items:
            yield item["login"]


# --------------------------------------------------------------------------- #
# Profile enrichment
# --------------------------------------------------------------------------- #
def fetch_tech_skills(gh: GitHub, login: str, top: int = 15) -> list[str]:
    """Languages used across the user's public repos, most-used first.
    One API call (first 100 repos by recent activity)."""
    resp = gh.get(f"{API}/users/{login}/repos",
                  params={"per_page": 100, "type": "owner", "sort": "pushed"})
    if resp.status_code != 200:
        return []
    try:
        repos = resp.json()
    except ValueError:
        return []
    counts: dict[str, int] = {}
    for repo in repos:
        lang = repo.get("language")
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return [lang for lang, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:top]


def fetch_profile(gh: GitHub, login: str) -> dict | None:
    resp = gh.get(f"{API}/users/{login}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        print(f"    [profile {login} error {resp.status_code}]", flush=True)
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def normalize(p: dict) -> dict:
    created = p["created_at"].replace("Z", "+00:00")
    return {
        "id": p["id"],
        "login": p["login"],
        "name": p.get("name"),
        "email": p["email"],
        "email_domain": domain_of(p["email"]),
        "company": p.get("company"),
        "location": p.get("location"),
        "country": country_of(p.get("location")),
        "continent": continent_of(p.get("location")),
        "tech_skills": [],   # filled in by main() via fetch_tech_skills()
        "bio": p.get("bio"),
        "blog": (p.get("blog") or None),
        "twitter": p.get("twitter_username"),
        "public_repos": p.get("public_repos"),
        "public_gists": p.get("public_gists"),
        "followers": p.get("followers"),
        "following": p.get("following"),
        "hireable": p.get("hireable"),
        "github_created": datetime.fromisoformat(created),
        "html_url": p.get("html_url"),
    }


def process_login(gh: GitHub, login: str):
    """Worker (runs in a thread): all network I/O for one candidate, no DB.
    Returns (login, status, record) where status is one of
    no_email / business_email / unknown_continent / kept. `record` is set only
    when kept. Never raises — errors come back as ('...', 'error', None)."""
    try:
        profile = fetch_profile(gh, login)
        if not profile:
            return (login, "no_email", None)
        email = profile.get("email")
        if not email:
            return (login, "no_email", None)
        if not is_personal_email(email):
            return (login, "business_email", None)
        location = profile.get("location")
        cont = continent_of(location)
        if cont == "Unknown":
            return (login, "unknown_continent", None)
        if FILTER_CONTINENTS or FILTER_COUNTRIES:
            # Explicit geo selection: keep only matching continents/countries.
            if cont not in FILTER_CONTINENTS and country_of(location) not in FILTER_COUNTRIES:
                return (login, "excluded_region", None)
        elif cont == "Asia" and not is_india_or_pakistan(location):
            # Default rule when nothing is selected: Asia only India & Pakistan.
            return (login, "excluded_region", None)
        record = normalize(profile)
        record["tech_skills"] = fetch_tech_skills(gh, login)
        return (login, "kept", record)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        return (login, "error", ("%s: %s" % (type(e).__name__, e)))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_windows():
    """The (start, end) date windows to search. When YEARS is set, one window
    per selected year (Jan 1 .. Dec 31, capped at today); otherwise the single
    START_DATE .. END_DATE range."""
    today = datetime.now(timezone.utc).date()
    if YEARS:
        years = sorted({int(y) for y in re.split(r"[\s,]+", YEARS) if y.strip().isdigit()})
        windows = [(date(y, 1, 1), min(date(y, 12, 31), today)) for y in years]
        windows = [(s, e) for s, e in windows if s <= e]
        if windows:
            return windows
    start = date.fromisoformat(START_DATE)
    end = date.fromisoformat(END_DATE) if END_DATE else today
    return [(start, end)]


def iter_all_logins(gh, windows, qualifiers):
    """Chain the per-window search generators into one stream of logins."""
    for s, e in windows:
        if _stop:
            return
        print(f"\n=== window {s} .. {e} ===", flush=True)
        yield from search_user_logins(gh, s, e, qualifiers)


def main():
    windows = build_windows()
    print("Collecting GitHub users — windows: "
          + ", ".join(f"{s}..{e}" for s, e in windows))
    print(f"Extra qualifiers: {EXTRA_QUALIFIERS or '(none)'}")
    print(f"Keeping only public PERSONAL emails. Max users: {MAX_USERS or 'unlimited'}\n")

    gh = GitHub(parse_tokens(GITHUB_TOKEN))
    db = DB(DATABASE_URL)

    n_tokens = len(gh.pool)
    workers = WORKERS or min(max(n_tokens * 2, 4), 16)   # ~2 concurrent per token
    max_inflight = workers * 4                            # cap look-ahead / memory
    print(f"Concurrency: {workers} workers over {n_tokens} token(s).\n")

    kept = 0
    examined = 0
    inflight: dict = {}          # future -> login
    stop_now = False

    def persist(done):
        """Runs on the MAIN thread only — the single DB connection stays
        single-threaded. Returns True if MAX_USERS reached."""
        nonlocal kept, examined
        for fut in done:
            inflight.pop(fut, None)
            login, status, record = fut.result()
            examined += 1
            if status == "kept":
                db.upsert_developer(record)
                db.mark_seen(login, "kept")
                kept += 1
                skills = ", ".join(record["tech_skills"][:4]) or "—"
                print(f"  + {login:<22} {record['email']:<34} [{skills}]", flush=True)
                if MAX_USERS and kept >= MAX_USERS:
                    return True
            elif status == "error":
                print(f"    [skip {login}: {record}]", flush=True)
            else:
                db.mark_seen(login, status)
            if examined % 200 == 0:
                print(f"  ... examined {examined}, kept {kept}", flush=True)
        return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            for login in iter_all_logins(gh, windows, EXTRA_QUALIFIERS):
                if _stop:
                    break
                if db.already_seen(login):
                    continue
                inflight[pool.submit(process_login, gh, login)] = login
                # Backpressure: once enough are queued, drain the finished ones.
                if len(inflight) >= max_inflight:
                    done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                    if persist(done):
                        stop_now = True
                        break
            # Drain whatever is still in flight (unless MAX_USERS hit).
            while inflight and not stop_now and not _stop:
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                if persist(done):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            for fut in inflight:      # don't wait on stragglers when stopping
                fut.cancel()

    if MAX_USERS and kept >= MAX_USERS:
        print(f"\nReached MAX_USERS={MAX_USERS}.", flush=True)
    print(f"\nDone. Examined {examined} profiles this run, kept {kept}.")
    print(f"Total developers in DB: {db.count_developers()}")


if __name__ == "__main__":
    main()
