import re
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import applier, db
from .companies import matches, normalize
from .linkedin_import import parse_connections_csv

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Referral First")
db.init()


# ---------- contact lookup (counts only, never names) ----------

def contact_count(company: str) -> int:
    q = normalize(company)
    if not q:
        return 0
    first = q.split()[0]
    with db.connect() as c:
        rows = c.execute(
            "SELECT company_norm, COUNT(*) n FROM connections "
            "WHERE company_norm LIKE ? OR ? LIKE company_norm || '%' GROUP BY company_norm",
            (first + "%", q),
        ).fetchall()
    return sum(r["n"] for r in rows if matches(q, r["company_norm"]))


@app.get("/api/check")
def check(company: str):
    n = contact_count(company)
    return {"company": company, "has_contact": n > 0, "contact_count": n}


# ---------- internships ----------

class NewInternship(BaseModel):
    company: str
    title: str | None = None
    url: str | None = None


class ApplyRequest(BaseModel):
    referral_link: str | None = None


class StatusChange(BaseModel):
    status: str


class NotesChange(BaseModel):
    notes: str


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

    n = contact_count(company)
    status = db.WAITING if n > 0 else db.APPLYING
    ts = db.now()
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO internships(company, company_norm, title, url, status, contact_count, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (company, normalize(company), (body.title or "").strip() or None, url, status, n, ts, ts),
        )
        iid = cur.lastrowid
    if status == db.APPLYING:
        background.add_task(applier.run_application, iid)
    return _row(db.get_internship(iid))


@app.get("/api/internships")
def list_internships(status: str | None = None):
    with db.connect() as c:
        if status:
            rows = c.execute("SELECT * FROM internships WHERE status=? ORDER BY updated_at DESC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM internships ORDER BY updated_at DESC").fetchall()
    return [_row(r) for r in rows]


@app.get("/api/summary")
def summary():
    with db.connect() as c:
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM internships GROUP BY status")}
        conns = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT company_norm) k FROM connections").fetchone()
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
def apply_now(iid: int, background: BackgroundTasks, body: ApplyRequest | None = None):
    it = _require(iid)
    if it["status"] == db.APPLYING:
        raise HTTPException(409, "Already applying.")
    link = (body.referral_link or "").strip() if body else ""
    if link and not re.match(r"https?://", link):
        raise HTTPException(400, "Referral link must start with http:// or https://")
    if link:
        db.update_internship(iid, referral_link=link)
    db.update_internship(iid, status=db.APPLYING, error=None)
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
        rows = parse_connections_csv(await file.read())
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
    # Re-score internships still waiting / not yet applied with fresh data.
    with db.connect() as c:
        for it in c.execute("SELECT id, company FROM internships WHERE status=?", (db.WAITING,)).fetchall():
            c.execute("UPDATE internships SET contact_count=? WHERE id=?", (contact_count(it["company"]), it["id"]))
    return {"imported": len(rows)}


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


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
