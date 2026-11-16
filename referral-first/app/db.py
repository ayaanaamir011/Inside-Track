import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("RF_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "app.db"
RESUME_DIR = DATA_DIR / "resumes"
SHOT_DIR = DATA_DIR / "screenshots"

# Internship statuses
WAITING = "waiting_for_referral"
APPLYING = "applying"
APPLIED = "applied"
NEEDS_ATTENTION = "needs_attention"
ARCHIVED = "archived"
STATUSES = [WAITING, APPLYING, APPLIED, NEEDS_ATTENTION, ARCHIVED]

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY,
    first_name TEXT, last_name TEXT, url TEXT,
    company TEXT, company_norm TEXT, position TEXT, connected_on TEXT
);
CREATE INDEX IF NOT EXISTS idx_conn_company ON connections(company_norm);

CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL, path TEXT NOT NULL, uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS internships (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL, company_norm TEXT NOT NULL,
    title TEXT, url TEXT,
    status TEXT NOT NULL,
    contact_count INTEGER NOT NULL DEFAULT 0,
    resume_id INTEGER REFERENCES resumes(id),
    referral_link TEXT, notes TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, applied_at TEXT,
    location TEXT, source TEXT NOT NULL DEFAULT 'manual'
);

-- Newsletter emails read from Gmail, and every listing found in them.
CREATE TABLE IF NOT EXISTS newsletter_emails (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    subject TEXT, sender TEXT, received_at TEXT,
    processed_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS newsletter_listings (
    id INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES newsletter_emails(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    section TEXT, region TEXT,
    title TEXT NOT NULL, company TEXT NOT NULL, location TEXT, url TEXT NOT NULL,
    decision TEXT NOT NULL,          -- skipped | pending | routed | duplicate
    reason TEXT,
    internship_id INTEGER REFERENCES internships(id) ON DELETE SET NULL
);
"""

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("internships", "location", "TEXT"),
    ("internships", "source", "TEXT NOT NULL DEFAULT 'manual'"),
]

DEFAULT_SETTINGS = {
    "profile": {
        "first_name": "", "last_name": "", "email": "", "phone": "",
        "linkedin_url": "", "github_url": "", "website": "",
        "location": "", "school": "",
    },
    # "auto": fill and submit. "dry_run": fill only, never submit (for testing).
    "apply_mode": "auto",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    for d in (DATA_DIR, RESUME_DIR, SHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
            if column not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))
    try:
        os.chmod(DB_PATH, 0o600)   # your data: readable by your user account only
    except OSError:
        pass


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_setting(key, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return json.loads(row["value"])


def set_setting(key, value):
    with connect() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def delete_setting(key):
    with connect() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))


def latest_resume():
    with connect() as c:
        return c.execute("SELECT * FROM resumes ORDER BY id DESC LIMIT 1").fetchone()


def update_internship(iid, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE internships SET {cols} WHERE id=?", (*fields.values(), iid))


def get_internship(iid):
    with connect() as c:
        return c.execute("SELECT * FROM internships WHERE id=?", (iid,)).fetchone()
