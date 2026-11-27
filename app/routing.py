"""The referral-first decision, shared by manual adds and the Gmail newsletter import.

A new internship goes to "Waiting for referral" when you have a connection at
the company, and straight to the bot's queue ("applying") when you don't. After
a few days without a referral (your setting), the bot applies anyway. Jobs you
already applied to (here, or on LinkedIn) are never applied to again.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from . import companies, db, jobboards

# An application this recent to the same company and job title counts as "already applied".
# Older ones are most likely last year's posting with the same name.
APPLIED_WITHIN_DAYS = 180


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
                "SELECT * FROM internships WHERE url=? OR referral_link=? OR source_url=? LIMIT 1",
                (url, url, url),
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
            "contact_count, created_at, updated_at, waiting_since) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (company, companies.normalize(company), title, url, location, source, status, n, ts, ts,
             ts if status == db.WAITING else None),
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
            db.update_internship(r["id"], status=db.WAITING, contact_count=n, error=None,
                                 waiting_since=db.now())
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


# ---------- waiting for a referral, but not forever ----------

def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def wait_days() -> int:
    return int(db.get_setting("referral_wait_days") or 0)


def apply_after_wait(days: Optional[int] = None) -> List[int]:
    """Move internships that have waited `days` for a referral to the bot's queue.

    Returns their ids; the caller hands them to the bot. 0 days: never.
    """
    days = wait_days() if days is None else days
    if days <= 0:
        return []
    with db.connect() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM internships WHERE status=? AND referral_decided=0 "
            "AND COALESCE(waiting_since, created_at) <= ? ORDER BY id", (db.WAITING, _ago(days)))]
    for iid in ids:
        db.update_internship(iid, status=db.APPLYING, referral_decided=1, error=None)
    return ids


def applies_on(row, days: Optional[int] = None) -> Optional[str]:
    """When the bot will apply to a waiting internship without a referral, or None."""
    days = wait_days() if days is None else days
    if days <= 0 or row["status"] != db.WAITING or row["referral_decided"]:
        return None
    start = datetime.fromisoformat(row["waiting_since"] or row["created_at"])
    return (start + timedelta(days=days)).isoformat(timespec="seconds")


# ---------- never applying twice ----------

def already_applied(company: str, title: Optional[str], url: Optional[str] = None,
                    exclude_id: Optional[int] = None):
    """A recent application of yours to this same job (any source), or None.

    Same job: the same link, or the same company with a title that names the
    same job (same rules as the job board lookup: season, year and degree level
    must not conflict).
    """
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM internships WHERE status=? AND id != ? "
            "AND (applied_at IS NULL OR applied_at >= ?) ORDER BY applied_at DESC",
            (db.APPLIED, exclude_id or -1, _ago(APPLIED_WITHIN_DAYS)[:10])).fetchall()
    norm = companies.normalize(company)
    for r in rows:
        if url and url in (r["url"], r["source_url"], r["referral_link"]):
            return r
        if title and r["title"] and companies.same(norm, r["company_norm"]) \
                and jobboards.same_title(title, r["title"]):
            return r
    return None


def applied_note(row) -> str:
    """'on Nov 14, 2026 on LinkedIn' for an application found by already_applied."""
    when = ""
    if row["applied_at"]:
        try:
            d = datetime.fromisoformat(row["applied_at"])
            when = " on %s %d, %d" % (d.strftime("%b"), d.day, d.year)
        except ValueError:
            pass
    return when + (" on LinkedIn" if row["source"] == "linkedin" else "")


def import_applications(rows: List[dict]) -> Tuple[int, int]:
    """Record jobs you applied to elsewhere (LinkedIn's Job Applications.csv) as Applied.

    Returns (added, already there). Importing the same file twice adds nothing.
    """
    known, ts = employers(), db.now()
    added = had = 0
    with db.connect() as c:
        for r in rows:
            norm = companies.normalize(r["company"])
            if c.execute(
                    "SELECT 1 FROM internships WHERE source='linkedin' AND company_norm=? "
                    "AND lower(ifnull(title,''))=lower(?) AND ifnull(applied_at,'')=ifnull(?,'')",
                    (norm, r["title"], r.get("applied_at"))).fetchone():
                had += 1
                continue
            c.execute(
                "INSERT INTO internships(company, company_norm, title, url, status, contact_count, source, "
                "referral_decided, created_at, updated_at, applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (r["company"], norm, r["title"], r.get("url"), db.APPLIED, contact_count(r["company"], known),
                 "linkedin", 1, ts, ts, r.get("applied_at")))
            added += 1
    return added, had


def skip_already_applied() -> int:
    """Archive internships not yet submitted that you turn out to have applied to already.

    Ones you explicitly chose to apply to (Apply now, Retry bot) are left alone.
    Returns how many were archived.
    """
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, company, title, url FROM internships WHERE status IN (?,?,?) AND referral_decided=0",
            (db.WAITING, db.APPLYING, db.NEEDS_ATTENTION)).fetchall()
    archived = 0
    for r in rows:
        hit = already_applied(r["company"], r["title"], r["url"], exclude_id=r["id"])
        if hit:
            db.update_internship(r["id"], status=db.ARCHIVED, error=(
                "You already applied to this%s, so the bot won't apply again. If it's a different job, "
                "click Restore." % applied_note(hit)))
            archived += 1
    return archived
