import asyncio
import os
import re
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import applier, companies, db, inbox, routing
from .linkedin_import import parse_connections_csv

STATIC = Path(__file__).parent / "static"
GMAIL_CHECK_MINUTES = max(5, int(os.environ.get("RF_GMAIL_CHECK_MINUTES", "30")))

db.init()

_tasks = set()


def _spawn(coro):
    """Run a coroutine in the background and keep it alive until it finishes."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _gmail_loop():
    """While the app is running, look for a new newsletter every half hour."""
    await asyncio.sleep(10)
    while True:
        try:
            if inbox.ready():
                summary = await inbox.check()
                for iid in summary["apply_ids"]:
                    _spawn(applier.run_application(iid))
        except inbox.InboxError:
            pass   # already recorded for the Gmail tab
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(GMAIL_CHECK_MINUTES * 60)


@asynccontextmanager
async def lifespan(_app):
    # Re-run the contact check first (the matching rules may be newer than the stored results),
    # then pick up applications that were still queued when the app was last closed.
    if routing.connections_loaded():
        routing.recount()
    for iid in routing.interrupted_ids():
        _spawn(applier.run_application(iid))
    _spawn(_gmail_loop())
    yield
    for task in list(_tasks):
        task.cancel()


class LocalOnly:
    """Only answer pages this app served, on this computer.

    The app can read a mailbox and submit applications, and it has no login. So a
    request is refused when it is addressed to anything but localhost (another
    machine, or a website using a DNS trick), or when a browser says it was sent
    by a page from somewhere else (the Origin header).
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            host = headers.get("host", "")
            name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
            origin = headers.get("origin")
            if name not in ("127.0.0.1", "localhost", "[::1]") or (
                    origin is not None and urlsplit(origin).netloc != host):
                refusal = JSONResponse({"detail": "This app only answers on the computer it runs on."},
                                       status_code=403)
                await refusal(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app = FastAPI(title="Referral First", lifespan=lifespan)
app.add_middleware(LocalOnly)


# ---------- contact lookup (counts only, never names) ----------

@app.get("/api/check")
def check(company: str):
    listed_as = routing.contacts_at(company)
    n = sum(h["count"] for h in listed_as)
    return {"company": company, "has_contact": n > 0, "contact_count": n, "listed_as": listed_as,
            "connections_loaded": routing.connections_loaded()}


# ---------- internships ----------

class NewInternship(BaseModel):
    company: str
    title: Optional[str] = None
    url: Optional[str] = None


class ApplyRequest(BaseModel):
    referral_link: Optional[str] = None


class StatusChange(BaseModel):
    status: str


class NotesChange(BaseModel):
    notes: str


NO_CONNECTIONS = ("Import your LinkedIn connections first (Connections tab). Until then the app can't tell "
                  "where you have contacts, so it won't apply to anything.")


def _row(r):
    d = dict(r)
    d["has_screenshot"] = (db.SHOT_DIR / f"{d['id']}.png").exists()
    return d


@app.post("/api/internships")
def add_internship(body: NewInternship, background: BackgroundTasks):
    company = body.company.strip()
    if not company:
        raise HTTPException(400, "Company is required.")
    url = (body.url or "").strip() or None
    if url and not re.match(r"https?://", url):
        raise HTTPException(400, "The posting link must start with http:// or https://")

    if url:
        with db.connect() as c:
            dup = c.execute(
                "SELECT id, status FROM internships WHERE url=? AND status != ?", (url, db.ARCHIVED)
            ).fetchone()
        if dup:
            raise HTTPException(409, f"You already added this posting (status: {dup['status'].replace('_', ' ')}).")

    if not routing.connections_loaded():
        raise HTTPException(400, NO_CONNECTIONS)

    row = routing.create(company, (body.title or "").strip() or None, url)
    if row["status"] == db.APPLYING:
        background.add_task(applier.run_application, row["id"])
    d = _row(row)
    d["listed_as"] = routing.contacts_at(company)
    return d


@app.get("/api/internships")
def list_internships(status: Optional[str] = None):
    with db.connect() as c:
        if status:
            rows = c.execute("SELECT * FROM internships WHERE status=? ORDER BY updated_at DESC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM internships ORDER BY updated_at DESC").fetchall()
    out = [_row(r) for r in rows]
    known = routing.employers()
    for d in out:
        if d["status"] != db.ARCHIVED:     # which employer names your contacts there are listed under
            d["listed_as"] = routing.contacts_at(d["company"], known)
    return out


@app.get("/api/summary")
def summary():
    with db.connect() as c:
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM internships GROUP BY status")}
        conns = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT company) k FROM connections").fetchone()
    r = db.latest_resume()
    return {
        "counts": {s: counts.get(s, 0) for s in db.STATUSES},
        "connections": conns["n"],
        "companies": conns["k"],
        "resume": r["filename"] if r else None,
    }


def _require(iid):
    it = db.get_internship(iid)
    if it is None:
        raise HTTPException(404, "Not found")
    return it


@app.post("/api/internships/{iid}/apply")
def apply_now(iid: int, background: BackgroundTasks, body: Optional[ApplyRequest] = None):
    it = _require(iid)
    if it["status"] == db.APPLYING:
        raise HTTPException(409, "Already applying.")
    link = (body.referral_link or "").strip() if body else ""
    if link and not re.match(r"https?://", link):
        raise HTTPException(400, "Referral link must start with http:// or https://")
    if link:
        db.update_internship(iid, referral_link=link)
    # referral_decided: you chose to go ahead, so a later contact check won't pull it back.
    db.update_internship(iid, status=db.APPLYING, error=None, referral_decided=1)
    background.add_task(applier.run_application, iid)
    return _row(db.get_internship(iid))


@app.post("/api/internships/{iid}/status")
def set_status(iid: int, body: StatusChange):
    _require(iid)
    if body.status not in db.STATUSES or body.status == db.APPLYING:
        raise HTTPException(400, "Invalid status")
    extra = {"applied_at": db.now()} if body.status == db.APPLIED else {}
    db.update_internship(iid, status=body.status, **extra)
    return _row(db.get_internship(iid))


@app.post("/api/internships/{iid}/notes")
def set_notes(iid: int, body: NotesChange):
    _require(iid)
    db.update_internship(iid, notes=body.notes)
    return _row(db.get_internship(iid))


@app.delete("/api/internships/{iid}")
def delete_internship(iid: int):
    _require(iid)
    with db.connect() as c:
        c.execute("DELETE FROM internships WHERE id=?", (iid,))
    (db.SHOT_DIR / f"{iid}.png").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/internships/{iid}/screenshot")
def screenshot(iid: int):
    p = db.SHOT_DIR / f"{iid}.png"
    if not p.exists():
        raise HTTPException(404, "No screenshot")
    return FileResponse(p, headers={"Cache-Control": "no-store"})


# ---------- connections ----------

@app.post("/api/connections/import")
async def import_connections(file: UploadFile = File(...), replace: bool = Form(True)):
    try:
        rows, no_employer = parse_connections_csv(await file.read())
    except ValueError as e:
        raise HTTPException(400, str(e))
    with db.connect() as c:
        if replace:
            c.execute("DELETE FROM connections")
        c.executemany(
            "INSERT INTO connections(first_name,last_name,url,company,company_norm,position,connected_on) "
            "VALUES(:first_name,:last_name,:url,:company,:company_norm,:position,:connected_on)",
            [{k: r.get(k) for k in ("first_name", "last_name", "url", "company", "company_norm",
                                    "position", "connected_on")} for r in rows],
        )
    return {"imported": len(rows), "no_employer": no_employer, "moved": routing.recount()}


@app.get("/api/connections/companies")
def connection_companies(q: str = ""):
    """How employers are written in your connections (names and head counts, never people)."""
    known = routing.employers()
    raw, squashed = companies.fold(q).strip(), companies.normalize(q).replace(" ", "")
    hits = [(name, n) for name, n in known
            if not raw or raw in companies.fold(name)
            or (squashed and squashed in companies.normalize(name).replace(" ", ""))]
    hits.sort(key=lambda h: (-h[1], h[0].lower()))
    return {"companies": len(known), "matching": len(hits),
            "results": [{"company": name, "count": n} for name, n in hits[:60]]}


class CompanyGroups(BaseModel):
    text: str


@app.get("/api/connections/groups")
def get_company_groups():
    return {"text": "\n".join(" = ".join(g) for g in routing.user_groups()),
            "built_in": [" = ".join(g) for g in companies.KNOWN_GROUP_NAMES]}


@app.put("/api/connections/groups")
def put_company_groups(body: CompanyGroups):
    """Your own "same company" lines, e.g. "Meta = Facebook = Instagram"."""
    groups = companies.parse_groups(body.text)
    db.set_setting("company_groups", groups)
    return {"groups": len(groups), "moved": routing.recount()}


# ---------- resumes ----------

@app.post("/api/resumes")
async def upload_resume(file: UploadFile = File(...)):
    name = Path(file.filename or "resume.pdf").name
    if not re.search(r"\.(pdf|docx?)$", name, re.I):
        raise HTTPException(400, "Resume must be a PDF or Word file.")
    ts = db.now()
    dest = db.RESUME_DIR / f"{ts.replace(':', '-')}_{name}"
    dest.write_bytes(await file.read())
    with db.connect() as c:
        c.execute("INSERT INTO resumes(filename, path, uploaded_at) VALUES(?,?,?)", (name, str(dest), ts))
    return {"filename": name, "uploaded_at": ts}


@app.get("/api/resumes")
def list_resumes():
    with db.connect() as c:
        return [dict(id=r["id"], filename=r["filename"], uploaded_at=r["uploaded_at"])
                for r in c.execute("SELECT * FROM resumes ORDER BY id DESC")]


# ---------- settings ----------

class Settings(BaseModel):
    profile: dict
    apply_mode: str = "auto"


@app.get("/api/settings")
def get_settings():
    return {"profile": db.get_setting("profile"), "apply_mode": db.get_setting("apply_mode")}


@app.put("/api/settings")
def put_settings(body: Settings):
    if body.apply_mode not in ("auto", "dry_run"):
        raise HTTPException(400, "apply_mode must be auto or dry_run")
    allowed = db.DEFAULT_SETTINGS["profile"].keys()
    db.set_setting("profile", {k: str(body.profile.get(k, "")).strip() for k in allowed})
    db.set_setting("apply_mode", body.apply_mode)
    return get_settings()


# ---------- Gmail newsletter import ----------

class GmailConnect(BaseModel):
    address: str
    app_password: str
    sender: Optional[str] = None


class NewsletterFilters(BaseModel):
    regions: List[str]
    keywords: List[str]
    exclude: List[str] = []
    mode: str = "auto"


def _gmail_state():
    s = inbox.state()
    s["check_minutes"] = GMAIL_CHECK_MINUTES
    return s


@app.get("/api/gmail")
def gmail_state():
    return _gmail_state()


@app.post("/api/gmail/connect")
async def gmail_connect(body: GmailConnect):
    try:
        await asyncio.to_thread(inbox.connect, body.address, body.app_password, body.sender)
    except inbox.InboxError as e:
        raise HTTPException(400, str(e))
    return _gmail_state()


@app.post("/api/gmail/disconnect")
def gmail_disconnect():
    inbox.disconnect()
    return _gmail_state()


@app.put("/api/gmail/filters")
def gmail_filters(body: NewsletterFilters):
    try:
        inbox.save_filters(body.regions, body.keywords, body.exclude, body.mode)
    except inbox.InboxError as e:
        raise HTTPException(400, str(e))
    return _gmail_state()


@app.post("/api/gmail/check")
async def gmail_check(background: BackgroundTasks):
    try:
        result = await inbox.check()
    except inbox.InboxError as e:
        raise HTTPException(400, str(e))
    for iid in result.pop("apply_ids"):
        background.add_task(applier.run_application, iid)
    return result


@app.get("/api/newsletter/emails")
def newsletter_emails():
    return inbox.recent_emails()


@app.post("/api/newsletter/listings/{listing_id}/apply")
async def newsletter_apply(listing_id: int, background: BackgroundTasks):
    listing, iid = await asyncio.to_thread(inbox.apply_listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Not found")
    if iid:
        background.add_task(applier.run_application, iid)
    return listing


@app.post("/api/newsletter/emails/{email_id}/apply-all")
async def newsletter_apply_all(email_id: int, background: BackgroundTasks):
    ids = await asyncio.to_thread(inbox.apply_all_pending, email_id)
    for iid in ids:
        background.add_task(applier.run_application, iid)
    return {"queued": len(ids)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
