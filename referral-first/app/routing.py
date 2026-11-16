"""The referral-first decision, shared by manual adds and the Gmail newsletter import.

A new internship goes to "Waiting for referral" when you have a connection at
the company, and straight to the bot's queue ("applying") when you don't.
"""
from typing import List, Optional, Tuple

from . import companies, db


def employers() -> List[Tuple[str, int]]:
    """Every employer name in your connections, as LinkedIn wrote it, with a head count."""
    with db.connect() as c:
        return [(r["company"], r["n"]) for r in c.execute(
            "SELECT company, COUNT(*) n FROM connections "
            "WHERE company IS NOT NULL AND TRIM(company) != '' GROUP BY company")]


def connections_loaded() -> bool:
    with db.connect() as c:
        return c.execute("SELECT 1 FROM connections LIMIT 1").fetchone() is not None


def user_groups() -> List[List[str]]:
    """Your own "these names are the same company" groups (Connections tab)."""
    return db.get_setting("company_groups") or []


def contacts_at(company: str, _employers=None) -> List[dict]:
    """Where your contacts for this company are listed: [{"company": employer name, "count": n}].

    Employer names and counts only, never who.
    """
    hits = companies.find(company, employers() if _employers is None else _employers,
                          companies.normalize_groups(user_groups()))
    return [{"company": name, "count": n} for name, n in hits]


def contact_count(company: str, _employers=None) -> int:
    return sum(h["count"] for h in contacts_at(company, _employers))


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
                (companies.normalize(company), title, location),
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
            (company, companies.normalize(company), title, url, location, source, status, n, ts, ts),
        )
        iid = cur.lastrowid
    return db.get_internship(iid)


def recount() -> int:
    """Re-run the contact check after your connections or name groups change.

    Internships nothing has been submitted for yet (still queued for the bot, or
    handed back in "Needs attention") move to "Waiting for referral" when a
    contact turns up, unless you already chose to apply to them without one.
    Returns how many moved.
    """
    known = employers()
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, company, status, contact_count, referral_decided FROM internships "
            "WHERE status IN (?,?,?)", (db.WAITING, db.APPLYING, db.NEEDS_ATTENTION)).fetchall()
    moved = 0
    for r in rows:
        n = contact_count(r["company"], known)
        if n > 0 and r["status"] != db.WAITING and not r["referral_decided"]:
            db.update_internship(r["id"], status=db.WAITING, contact_count=n, error=None)
            moved += 1
        elif n != r["contact_count"]:
            with db.connect() as c:    # a count changing isn't activity; leave updated_at alone
                c.execute("UPDATE internships SET contact_count=? WHERE id=?", (n, r["id"]))
    return moved


def interrupted_ids():
    """Internships left in "applying" when the app was last closed."""
    with db.connect() as c:
        return [r["id"] for r in c.execute(
            "SELECT id FROM internships WHERE status=? ORDER BY id", (db.APPLYING,))]
