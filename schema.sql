-- Schema for collected GitHub developers with public personal emails.

CREATE TABLE IF NOT EXISTS developers (
    id              BIGINT PRIMARY KEY,            -- GitHub numeric user id
    login           TEXT   NOT NULL UNIQUE,        -- username
    name            TEXT,
    email           TEXT   NOT NULL,
    email_domain    TEXT   NOT NULL,
    company         TEXT,
    location        TEXT,
    country         TEXT,
    continent       TEXT,
    tech_skills     TEXT[] DEFAULT '{}',     -- languages across the user's repos
    bio             TEXT,
    blog            TEXT,
    twitter         TEXT,
    public_repos    INTEGER,
    public_gists    INTEGER,
    followers       INTEGER,
    following       INTEGER,
    hireable        BOOLEAN,
    github_created  TIMESTAMPTZ NOT NULL,          -- account creation (the > 2021 filter)
    html_url        TEXT,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dev_created       ON developers (github_created);
CREATE INDEX IF NOT EXISTS idx_dev_email_domain  ON developers (email_domain);
CREATE INDEX IF NOT EXISTS idx_dev_location      ON developers (location);
CREATE INDEX IF NOT EXISTS idx_dev_continent     ON developers (continent);

CREATE INDEX IF NOT EXISTS idx_dev_tech_skills   ON developers USING GIN (tech_skills);

CREATE INDEX IF NOT EXISTS idx_dev_country       ON developers (country);

-- Add columns to pre-existing tables (no-op once present).
ALTER TABLE developers ADD COLUMN IF NOT EXISTS continent TEXT;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS country TEXT;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS tech_skills TEXT[] DEFAULT '{}';

-- Tracks every login we've already examined (even if rejected) so re-runs
-- don't re-fetch the same profiles.  Lets the job resume safely.
CREATE TABLE IF NOT EXISTS seen_logins (
    login        TEXT PRIMARY KEY,
    status       TEXT NOT NULL,        -- 'kept' | 'no_email' | 'business_email'
    examined_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------- --
-- Outreach: weekly plans and the message-send history.
-- --------------------------------------------------------------------------- --

-- A "weekly plan": who to reach out to (continent / country) and how many, plus
-- the message template.  Sending is driven off one of these rows.
CREATE TABLE IF NOT EXISTS outreach_plans (
    id            SERIAL PRIMARY KEY,
    week_start    DATE   NOT NULL,               -- Monday of the target week
    continent     TEXT,                          -- NULL = any
    country       TEXT,                          -- NULL = any
    send_count    INTEGER NOT NULL DEFAULT 0,    -- how many messages to send
    per_day       INTEGER,                       -- connections (messages) per day; NULL = no cap
    start_date    DATE,                          -- campaign window start (NULL = unset)
    end_date      DATE,                          -- campaign window end   (NULL = unset)
    subject       TEXT   NOT NULL,
    body          TEXT   NOT NULL,               -- supports {name} {login} {country} {location} tokens
    status        TEXT   NOT NULL DEFAULT 'draft', -- draft | sending | done | stopped
    sent          INTEGER NOT NULL DEFAULT 0,    -- messages actually sent so far
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Additive migrations for databases created before these columns existed.
ALTER TABLE outreach_plans ADD COLUMN IF NOT EXISTS per_day    INTEGER;
ALTER TABLE outreach_plans ADD COLUMN IF NOT EXISTS start_date DATE;
ALTER TABLE outreach_plans ADD COLUMN IF NOT EXISTS end_date   DATE;

CREATE INDEX IF NOT EXISTS idx_plan_week ON outreach_plans (week_start DESC);

-- One row per attempted send.  The partial unique index guarantees a developer
-- is only ever successfully emailed once (across all plans).
CREATE TABLE IF NOT EXISTS outreach_messages (
    id            SERIAL PRIMARY KEY,
    plan_id       INTEGER REFERENCES outreach_plans(id) ON DELETE SET NULL,
    login         TEXT   NOT NULL,
    email         TEXT   NOT NULL,
    name          TEXT,
    country       TEXT,
    subject       TEXT,
    body          TEXT,
    status        TEXT   NOT NULL,               -- sent | failed | skipped
    error         TEXT,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_msg_sent_at ON outreach_messages (sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_plan    ON outreach_messages (plan_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_email_once
    ON outreach_messages (lower(email)) WHERE status = 'sent';
