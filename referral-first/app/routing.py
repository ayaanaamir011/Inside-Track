"""The referral-first decision, shared by manual adds and the Gmail newsletter import.

A new internship goes to "Waiting for referral" when you have a connection at
the company, and straight to the bot's queue ("applying") when you don't.
"""
from typing import Optional

from . import db
from .companies import aliases, matches, normalize


def contact_count(company: str) -> int:
    """How many of your connections work at this company (a count only, never who)."""
    names = aliases(company)
    if not names:
        return 0
    with db.connect() as c:
        groups = c.execute(
            "SELECT company_norm, COUNT(*) n FROM connections GROUP BY company_norm"
        ).fetchall()
    return sum(g["n"] for g in groups if any(matches(q, g["company_norm"]) for q in names))


def find_existing(company: str, title: Optional[str], url: Optional[str],
                  location: Optional[str] = None):
    """An internship already tracked for this posting (any status), or None."""
    with db.connect() as c:
        if url:
            row = c.execute(
                "SELECT * FROM internships WHERE url=? OR referral_link=? LIMIT 1", (url, url)
            ).fetchone()
            if row:
                return row
        if title:
            return c.execute(
                "SELECT * FROM internships WHERE company_norm=? AND lower(title)=lower(?) "
                "AND ifnull(location,'')=ifnull(?,'') LIMIT 1",
                (normalize(company), title, location),
            ).fetchone()
    return None


def create(company: str, title: Optional[str], url: Optional[str],
           location: Optional[str] = None, source: str = "manual"):
    """Insert an internship with the right starting status and return its row.

    The caller queues the bot when the returned status is "applying".
    """
    n = contact_count(company)
    status = db.WAITING if n > 0 else db.APPLYING
    ts = db.now()
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO internships(company, company_norm, title, url, location, source, status, "
            "contact_count, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (company, normalize(company), title, url, location, source, status, n, ts, ts),
        )
        iid = cur.lastrowid
    return db.get_internship(iid)


def interrupted_ids():
    """Internships left in "applying" when the app was last closed."""
    with db.connect() as c:
        return [r["id"] for r in c.execute(
            "SELECT id FROM internships WHERE status=? ORDER BY id", (db.APPLYING,))]
