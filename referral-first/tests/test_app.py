import asyncio
import functools
import http.server
import importlib
import os
import threading
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RF_DATA_DIR", str(tmp_path))
    from app import db, main
    importlib.reload(db)
    importlib.reload(main)
    calls = []

    async def fake_apply(iid):
        calls.append(iid)

    monkeypatch.setattr(main.applier, "run_application", fake_apply)
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    c.apply_calls = calls
    return c


def import_fixture(client):
    with open(FIXTURES / "Connections.csv", "rb") as f:
        r = client.post("/api/connections/import", files={"file": ("Connections.csv", f, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_normalize_and_match():
    from app.companies import matches, normalize
    assert normalize("Google LLC") == "google"
    assert normalize("Amazon Web Services (AWS)") == "amazon web services"
    assert normalize("AT&T Inc.") == "at and t"
    assert matches("amazon", "amazon web services")
    assert matches("google", "google")
    assert not matches("apple", "applebee s")
    assert not matches("meta", "metabase")


def test_import_skips_notes_and_blank_company(client):
    assert import_fixture(client) == {"imported": 4}
    s = client.get("/api/summary").json()
    assert s["connections"] == 4 and s["companies"] == 3


def test_check_returns_count_not_names(client):
    import_fixture(client)
    r = client.get("/api/check", params={"company": "google"}).json()
    assert r == {"company": "google", "has_contact": True, "contact_count": 2}
    assert "Turing" not in str(r)
    assert client.get("/api/check", params={"company": "Amazon"}).json()["contact_count"] == 1
    assert client.get("/api/check", params={"company": "Figma"}).json()["has_contact"] is False


def test_company_with_contact_waits_for_referral(client):
    import_fixture(client)
    r = client.post("/api/internships", json={"company": "Stripe, Inc.", "url": "https://jobs.example.com/1"})
    it = r.json()
    assert it["status"] == "waiting_for_referral" and it["contact_count"] == 1
    assert client.apply_calls == []
    # Got a referral -> apply through it
    r = client.post(f"/api/internships/{it['id']}/apply", json={"referral_link": "https://ref.example.com/x"})
    assert r.json()["status"] == "applying" and r.json()["referral_link"] == "https://ref.example.com/x"
    assert client.apply_calls == [it["id"]]


def test_company_without_contact_applies_immediately(client):
    import_fixture(client)
    it = client.post("/api/internships", json={"company": "Figma", "url": "https://jobs.example.com/2"}).json()
    assert it["status"] == "applying"
    assert client.apply_calls == [it["id"]]


def test_duplicate_and_bad_urls(client):
    client.post("/api/internships", json={"company": "Figma", "url": "https://jobs.example.com/3"})
    assert client.post("/api/internships", json={"company": "Figma", "url": "https://jobs.example.com/3"}).status_code == 409
    assert client.post("/api/internships", json={"company": "Figma", "url": "javascript:alert(1)"}).status_code == 400


def test_status_changes(client):
    it = client.post("/api/internships", json={"company": "Figma"}).json()
    r = client.post(f"/api/internships/{it['id']}/status", json={"status": "applied"}).json()
    assert r["status"] == "applied" and r["applied_at"]
    assert client.post(f"/api/internships/{it['id']}/status", json={"status": "bogus"}).status_code == 400


def test_resume_upload_latest_wins(client):
    client.post("/api/resumes", files={"file": ("old.pdf", b"%PDF-1", "application/pdf")})
    client.post("/api/resumes", files={"file": ("new.pdf", b"%PDF-2", "application/pdf")})
    assert client.get("/api/summary").json()["resume"] == "new.pdf"
    assert client.post("/api/resumes", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400


# ---------- real browser tests against local fixture forms ----------

def _have_browser():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture()
def fixture_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _setup_for_bot(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("RF_DATA_DIR", str(tmp_path))
    from app import applier, db
    importlib.reload(db)
    importlib.reload(applier)
    db.init()
    db.set_setting("profile", {**db.DEFAULT_SETTINGS["profile"], "first_name": "Sam", "last_name": "Lee",
                               "email": "sam@example.com", "phone": "555-0100",
                               "linkedin_url": "https://linkedin.com/in/sam"})
    db.set_setting("apply_mode", mode)
    res = tmp_path / "resume.pdf"
    res.write_bytes(b"%PDF-1.4 test")
    with db.connect() as c:
        c.execute("INSERT INTO resumes(filename,path,uploaded_at) VALUES(?,?,?)", ("resume.pdf", str(res), db.now()))
    return applier, db


def _add(db, url):
    with db.connect() as c:
        return c.execute(
            "INSERT INTO internships(company,company_norm,url,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("Figma", "figma", url, db.APPLYING, db.now(), db.now()),
        ).lastrowid


@pytest.mark.skipif(not _have_browser(), reason="playwright not installed")
def test_bot_fills_and_submits(tmp_path, monkeypatch, fixture_server):
    applier, db = _setup_for_bot(tmp_path, monkeypatch, "auto")
    iid = _add(db, fixture_server + "/greenhouse_like.html")
    asyncio.run(applier.run_application(iid))
    it = db.get_internship(iid)
    assert it["status"] == db.APPLIED, it["error"]
    assert (db.SHOT_DIR / f"{iid}.png").exists()


@pytest.mark.skipif(not _have_browser(), reason="playwright not installed")
def test_bot_stops_on_unanswered_required_question(tmp_path, monkeypatch, fixture_server):
    applier, db = _setup_for_bot(tmp_path, monkeypatch, "auto")
    iid = _add(db, fixture_server + "/custom_question.html")
    asyncio.run(applier.run_application(iid))
    it = db.get_internship(iid)
    assert it["status"] == db.NEEDS_ATTENTION
    assert "Why do you want to work here" in it["error"]


@pytest.mark.skipif(not _have_browser(), reason="playwright not installed")
def test_bot_dry_run_does_not_submit(tmp_path, monkeypatch, fixture_server):
    applier, db = _setup_for_bot(tmp_path, monkeypatch, "dry_run")
    iid = _add(db, fixture_server + "/greenhouse_like.html")
    asyncio.run(applier.run_application(iid))
    it = db.get_internship(iid)
    assert it["status"] == db.NEEDS_ATTENTION
    assert "Dry run" in it["error"] and "first_name" in it["error"] and "resume" in it["error"]
