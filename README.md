# GitHub personal-email developer collector

Finds GitHub developers who **created their account on/after a date** (default
`2021-01-01`) and expose a **public personal email** (gmail/outlook/proton/…,
not a company domain), then stores them in **PostgreSQL**.

It uses GitHub's official REST Search API — not HTML scraping — so it's
reliable and respects rate limits (primary + secondary, with auto-backoff).

> ⚠️ The emails collected are public but still personal data. Using them for
> unsolicited bulk mail can breach GitHub's Terms and laws like GDPR/CAN-SPAM.
> Use the results responsibly (e.g. opt-in outreach, research).

## How it works

1. **Search** users with `created:<window> type:user [extra qualifiers]`.
   GitHub caps any search at 1000 results, so the date range is sliced into
   windows and **recursively bisected** when a window is too dense.
2. **Enrich** — the search API doesn't return emails, so each login's full
   profile is fetched (`/users/{login}`) to read `email`.
3. **Filter** — keep only profiles whose email domain is a known consumer
   provider (`personal_domains.py`). Orgs are excluded via `type:user`.
4. **Store / resume** — rows go into `developers`; every examined login is
   recorded in `seen_logins` so re-runs skip work already done.

## Setup

```bash
# 1. Python deps (in a virtualenv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Start PostgreSQL (needs Docker) — or point DATABASE_URL at your own
docker compose up -d

# 3. Configure
cp .env.example .env
#    edit .env: paste your GITHUB_TOKEN, adjust filters
```

Create a token at <https://github.com/settings/tokens> — **no scopes needed**
for public data.

## Run

### Option A — Web UI (recommended)

```bash
source .venv/bin/activate
python webapp.py        # -> http://localhost:8000
```

The dashboard lets you:
- **Launch / stop** scrape runs with filters (date range, qualifiers, max users)
  and watch the live log.
- **Browse, search and paginate** collected developers.
- See **stats** — totals, top email providers, top locations.

### Option B — CLI

```bash
source .venv/bin/activate
python github_scraper.py
```

Stop anytime with Ctrl-C; it finishes the current profile and exits cleanly.
Re-running continues where it left off. The web UI and CLI share the same
database, so you can use either interchangeably.

## Tuning the search (`.env`)

| Variable          | Meaning                                                        |
|-------------------|----------------------------------------------------------------|
| `START_DATE`      | Earliest account-creation date (default `2021-01-01`)          |
| `END_DATE`        | Latest date; blank = today                                     |
| `EXTRA_QUALIFIERS`| Extra GitHub qualifiers, e.g. `location:Berlin language:Go`    |
| `MAX_USERS`       | Stop after N kept users (`0` = unlimited)                      |

Because each kept user costs ~1 profile request and the date-only sweep is
*huge*, narrowing with `EXTRA_QUALIFIERS` (location/language/followers) is
strongly recommended for a focused run.

## Outreach — messaging developers from your own domain

Once developers are collected, the **Outreach** tab turns them into an opt-in
mailing workflow in three steps:

1. **Weekly plan** — pick a **continent**, optional **country**, and how many
   messages to send, plus the subject/body template. The form shows how many
   *not-yet-contacted* developers match. Tokens `{name} {first_name} {login}
   {country} {location}` are personalised per recipient.
2. **Auto-send** — hit **Send now** on a plan. `outreach.py` selects the newest
   eligible developers, sends over your SMTP server, throttles between messages
   (`SEND_DELAY`), and streams a live log. Stop anytime — it finishes the current
   message and marks the plan `stopped` so you can **Resume** later.
3. **History** — every attempt (`sent` / `failed` / `skipped`) is recorded on the
   **History** tab. A developer is only ever emailed **once** — a partial unique
   index guarantees no duplicate `sent` to the same address across all plans.

Configure your domain's SMTP in `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASS`, `FROM_EMAIL`, `FROM_NAME`). **If SMTP is left blank the sender runs
in DRY-RUN mode** — recipients are selected and logged as `skipped`, but nothing
is actually emailed, so you can rehearse a plan safely before going live.

> ⚠️ These are real people's personal emails. Keep volumes low, make it genuinely
> opt-in / relevant, honour unsubscribe requests, and comply with GDPR/CAN-SPAM.

## Querying results

```sql
SELECT login, name, email, location, github_created
FROM developers
ORDER BY github_created DESC
LIMIT 50;

-- breakdown by provider
SELECT email_domain, count(*) FROM developers GROUP BY 1 ORDER BY 2 DESC;
```

## Files

| File                  | Purpose                                  |
|-----------------------|------------------------------------------|
| `webapp.py`           | Flask web UI (dashboard + run control)   |
| `templates/*.html`    | UI pages (dashboard, users, outreach, …) |
| `github_scraper.py`   | Main scrape pipeline                     |
| `outreach.py`         | Message-send engine (SMTP + throttle)    |
| `personal_domains.py` | Consumer-email-domain allowlist + logic  |
| `schema.sql`          | PostgreSQL tables                        |
| `docker-compose.yml`  | Local Postgres 16                        |
| `.env.example`        | Config template                          |
