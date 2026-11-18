"""The weekly newsletter import: Gmail -> verify sender -> parse -> filter -> route.

Each new Intern Insider email is read once. Every listing in it is recorded
with what was decided and why (shown on the Gmail tab). Listings that match
your filters go through the same referral-first routing as a manual add:
"Waiting for referral" if you have a contact at the company, otherwise the bot
applies.
"""
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from . import credstore, db, gmail, newsletter, routing

SENDER_HINTS = ["Intern Insider", "interninsider"]
DEFAULT_SENDER = "hi@interninsider.me"   # where the weekly issue comes from (their notices use another address)
LOOKBACK_DAYS = 7          # only act on newsletters from the past week
REGIONS = ["USA", "Canada"]
MODES = ["auto", "review"]

_lock = None


class InboxError(Exception):
    """A problem the user can act on. The message is shown in the app."""


# ---------- settings ----------

def default_filters() -> dict:
    return {
        "regions": [],
        "keywords": list(newsletter.DEFAULT_KEYWORDS),
        "exclude": list(newsletter.DEFAULT_EXCLUDE),
        "mode": "auto",
    }


def filters() -> dict:
    f = default_filters()
    f.update(db.get_setting("newsletter_filters") or {})
    return f


def account() -> Optional[dict]:
    return db.get_setting("gmail_account")


def todo() -> Optional[str]:
    """What still has to be set up before newsletters can be handled; None when ready."""
    if not account():
        return "Connect Gmail first."
    if not filters()["regions"]:
        return "Choose USA and/or Canada under “What to apply to” and save."
    if not routing.connections_loaded():
        return ("Import your LinkedIn connections on the Connections tab. Until then the app can't tell "
                "where you have contacts, so it won't apply to anything.")
    if db.latest_resume() is None or not (db.get_setting("profile") or {}).get("email"):
        return ("Upload your resume and fill in your details on the Resume & profile tab, "
                "so the bot has something to apply with.")
    return None


def ready() -> bool:
    return todo() is None


def state() -> dict:
    acct = account() or {}
    return {
        "connected": bool(acct),
        "address": acct.get("address"),
        "sender": acct.get("sender"),
        "storage": acct.get("storage"),
        "ready": ready(),
        "todo": todo(),
        "filters": filters(),
        "defaults": {"keywords": newsletter.DEFAULT_KEYWORDS, "exclude": newsletter.DEFAULT_EXCLUDE},
        "status": db.get_setting("gmail_status") or {},
    }


def _clean_list(values) -> List[str]:
    out = []
    for v in values or []:
        v = " ".join(str(v).split())[:60]
        if v and v.lower() not in [o.lower() for o in out]:
            out.append(v)
    return out[:200]


def save_filters(regions, keywords, exclude, mode) -> dict:
    regions = [r for r in REGIONS if r in (regions or [])]
    if not regions:
        raise InboxError("Choose at least one country.")
    if mode not in MODES:
        raise InboxError("Unknown mode.")
    keywords = _clean_list(keywords)
    if not keywords:
        raise InboxError("Add at least one keyword, or nothing will ever match.")
    db.set_setting("newsletter_filters", {
        "regions": regions, "keywords": keywords, "exclude": _clean_list(exclude), "mode": mode,
    })
    return state()


def connect(address: str, app_password: str, sender: Optional[str] = None) -> dict:
    """Sign in to Gmail, find the newsletter's sender address, and remember both. (Blocking.)"""
    address = (address or "").strip().lower()
    password = re.sub(r"\s+", "", app_password or "")   # Google shows app passwords with spaces
    sender = (sender or "").strip().lower() or None
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", address):
        raise InboxError("Enter your full Gmail address.")
    if not password:
        raise InboxError("Paste the app password from Google.")
    if sender and not re.match(r"^[\x21-\x7e]+@[\x21-\x7e]+\.[\x21-\x7e]+$", sender):
        raise InboxError("That newsletter sender doesn't look like an email address.")
    try:
        if sender:
            gmail.check_login(address, password)
        else:
            sender = gmail.find_sender(address, password, SENDER_HINTS) or DEFAULT_SENDER
    except gmail.GmailError as e:
        raise InboxError(str(e))
    old = account()
    if old and old.get("address") != address:
        credstore.forget(old["address"])
    storage = credstore.save(address, password)
    db.set_setting("gmail_account", {"address": address, "sender": sender, "storage": storage})
    db.delete_setting("gmail_status")
    return state()


def disconnect() -> dict:
    acct = account()
    if acct:
        credstore.forget(acct["address"])
    db.delete_setting("gmail_account")
    db.delete_setting("gmail_status")
    return state()


# ---------- routing one listing ----------

def _route(listing: dict, url: str) -> Tuple[str, Optional[str], Optional[int], bool]:
    """Send one listing through the referral check.

    Returns (decision, reason override, internship id, should the bot apply now?).
    """
    if not newsletter.is_public_url(url):
        return "skipped", "The link doesn't go to a public web page", None, False
    existing = routing.find_existing(listing["company"], listing["title"], url, listing.get("location"))
    if existing:
        return "duplicate", "Already in your list", existing["id"], False
    row = routing.create(listing["company"], listing["title"], url, listing.get("location"),
                         source="newsletter")
    return "routed", None, row["id"], row["status"] == db.APPLYING


def _resolve_all(urls: List[str]) -> dict:
    """Follow the newsletter's click-tracking links to the real postings, a few at a time."""
    urls = list(dict.fromkeys(urls))
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(urls, pool.map(newsletter.resolve_url, urls)))


# ---------- processing an email ----------

def process_message(raw: bytes, sender: str, f: dict, cutoff: Optional[str] = None) -> Optional[dict]:
    """Read one email and record every listing. Returns a summary, or None if the
    email was already processed (or is older than the cutoff). (Blocking.)"""
    info = gmail.read_message(raw)
    if cutoff and info["date"] and info["date"] < cutoff:
        return None
    with db.connect() as c:
        if c.execute("SELECT 1 FROM newsletter_emails WHERE message_id=?", (info["message_id"],)).fetchone():
            return None

    note, listings = None, []
    if info["sender"] != sender or not info["verified"]:
        note = "Ignored: Gmail couldn't confirm that this email really came from %s." % sender
    elif not info["html"]:
        note = "Couldn't read this email (it has no HTML version)."
    else:
        listings = newsletter.parse_listings(info["html"])
        if not listings:
            note = "No internship listings found in this email."

    with db.connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO newsletter_emails(message_id, subject, sender, received_at, processed_at, note) "
            "VALUES(?,?,?,?,?,?)",
            (info["message_id"], info["subject"], info["sender"], info["date"], db.now(), note),
        )
        if cur.rowcount == 0:
            return None
        email_id = cur.lastrowid

    decided = [(l,) + newsletter.classify(l, f) for l in listings]
    auto = f.get("mode") == "auto"
    resolved = _resolve_all([l["url"] for l, ok, _ in decided if ok]) if auto else {}

    summary = {"email_id": email_id, "subject": info["subject"], "listings": len(listings), "matched": 0,
               "waiting": 0, "applying": 0, "pending": 0, "apply_ids": []}
    rows = []
    for pos, (l, ok, reason) in enumerate(decided):
        decision, iid = "skipped", None
        url = l["url"]
        if ok:
            summary["matched"] += 1
            if auto:
                url = resolved.get(l["url"], l["url"])
                decision, why, iid, apply_now = _route(l, url)
                reason = why or reason
                if decision == "routed":
                    summary["applying" if apply_now else "waiting"] += 1
                    if apply_now:
                        summary["apply_ids"].append(iid)
            else:
                decision = "pending"
                summary["pending"] += 1
        rows.append((email_id, pos, l.get("section"), l.get("region"), l["title"], l["company"],
                     l.get("location"), url, decision, reason, iid))
    with db.connect() as c:
        c.executemany(
            "INSERT INTO newsletter_listings(email_id, position, section, region, title, company, location, "
            "url, decision, reason, internship_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    return summary


def _check_blocking() -> dict:
    missing = todo()
    if missing:
        raise InboxError(missing)
    acct, f = account(), filters()
    password = credstore.load(acct["address"])
    if not password:
        raise InboxError("The saved app password is missing. Disconnect and connect Gmail again.")

    status = db.get_setting("gmail_status") or {}
    status.update({"last_checked": db.now(), "last_error": None})
    try:
        raws = gmail.fetch_from_sender(acct["address"], password, acct["sender"], LOOKBACK_DAYS + 1)
    except gmail.GmailError as e:
        status["last_error"] = str(e)
        db.set_setting("gmail_status", status)
        raise InboxError(str(e))

    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat(timespec="seconds")
    total = {"found": len(raws), "new_emails": 0, "listings": 0, "matched": 0, "waiting": 0, "applying": 0,
             "pending": 0, "apply_ids": []}
    for raw in raws:
        s = process_message(raw, acct["sender"], f, cutoff)
        if s is None:
            continue
        total["new_emails"] += 1
        for k in ("listings", "matched", "waiting", "applying", "pending"):
            total[k] += s[k]
        total["apply_ids"] += s["apply_ids"]
    if total["new_emails"]:
        status["last_new"] = {k: v for k, v in total.items() if k != "apply_ids"}
        status["last_new"]["at"] = status["last_checked"]
    db.set_setting("gmail_status", status)
    return total


async def check() -> dict:
    """Look for new newsletter emails and process them. Returns counts plus
    `apply_ids`: internships the caller should hand to the bot."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        return await asyncio.to_thread(_check_blocking)


# ---------- the Gmail tab ----------

def recent_emails(limit: int = 6) -> List[dict]:
    with db.connect() as c:
        emails = [dict(r) for r in c.execute(
            "SELECT * FROM newsletter_emails ORDER BY COALESCE(received_at, processed_at) DESC, id DESC LIMIT ?",
            (limit,))]
        for e in emails:
            e["listings"] = [dict(r) for r in c.execute(
                "SELECT l.*, i.status AS internship_status, i.contact_count AS contact_count "
                "FROM newsletter_listings l LEFT JOIN internships i ON i.id = l.internship_id "
                "WHERE l.email_id=? ORDER BY l.position", (e["id"],))]
    return emails


def apply_listing(listing_id: int) -> Tuple[Optional[dict], Optional[int]]:
    """Route a listing that was skipped or is waiting for approval. (Blocking.)

    Returns (the listing, internship id for the bot or None).
    """
    with db.connect() as c:
        row = c.execute("SELECT * FROM newsletter_listings WHERE id=?", (listing_id,)).fetchone()
    if row is None:
        return None, None
    l = dict(row)
    if l["decision"] in ("routed", "duplicate"):
        return l, None
    url = newsletter.resolve_url(l["url"])
    decision, why, iid, apply_now = _route(l, url)
    with db.connect() as c:
        c.execute("UPDATE newsletter_listings SET decision=?, reason=?, internship_id=?, url=? WHERE id=?",
                  (decision, why or "You chose to apply", iid, url if decision != "skipped" else l["url"],
                   listing_id))
        l = dict(c.execute("SELECT * FROM newsletter_listings WHERE id=?", (listing_id,)).fetchone())
    return l, (iid if apply_now else None)


def apply_all_pending(email_id: int) -> List[int]:
    """Route every listing of an email that is waiting for approval. (Blocking.)"""
    with db.connect() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM newsletter_listings WHERE email_id=? AND decision='pending' ORDER BY position",
            (email_id,))]
    apply_ids = []
    for lid in ids:
        _, iid = apply_listing(lid)
        if iid:
            apply_ids.append(iid)
    return apply_ids
