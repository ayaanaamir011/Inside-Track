"""Never applying twice (LinkedIn's Job Applications CSV), the referral wait limit,
and refusing a connections file without employers."""
from datetime import datetime, timedelta, timezone

from app.linkedin_import import parse_applications_csv, parse_date

APPLICATIONS = (
    "Application Date,Contact Email,Contact Phone Number,Company Name,Job Title,Job Url,Resume Name,"
    "Question And Answers\n"
    '"{recent}",me@example.com,555-0100,Airbnb,Software Engineering Intern (BS/MS),'
    "https://www.linkedin.com/jobs/view/111,resume.pdf,Are you authorized?:Yes\n"
    '"{old}",me@example.com,555-0100,Notion,Software Engineer Intern,'
    "https://www.linkedin.com/jobs/view/222,resume.pdf,\n"
)


def _linkedin_date(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%m/%d/%y, %I:%M %p")


def _upload_applications(client, recent_days=10, old_days=400):
    text = APPLICATIONS.format(recent=_linkedin_date(recent_days), old=_linkedin_date(old_days))
    return client.post("/api/linkedin/applications/import",
                       files={"file": ("Job Applications.csv", text.encode(), "text/csv")})


def _set_wait(client, days):
    s = client.get("/api/settings").json()
    r = client.put("/api/settings", json={"profile": s["profile"], "apply_mode": s["apply_mode"],
                                          "referral_wait_days": days})
    assert r.status_code == 200, r.text
    return r.json()


def _backdate_wait(iid, days):
    from app import db
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute("UPDATE internships SET waiting_since=? WHERE id=?", (when, iid))


# ---------- reading LinkedIn's file ----------

def test_reads_only_company_title_link_and_date():
    rows = parse_applications_csv(APPLICATIONS.format(recent="11/14/26, 10:31 AM", old="").encode())
    assert rows[0] == {"company": "Airbnb", "title": "Software Engineering Intern (BS/MS)",
                       "url": "https://www.linkedin.com/jobs/view/111", "applied_at": "2026-11-14T10:31:00"}
    assert rows[1]["applied_at"] is None
    assert "me@example.com" not in str(rows) and "authorized" not in str(rows)


def test_application_dates():
    assert parse_date("11/14/26, 10:31 AM") == "2026-11-14T10:31:00"
    assert parse_date("2026-11-14 22:05:00 UTC") == "2026-11-14T22:05:00"
    assert parse_date("Nov 14, 2026") == "2026-11-14T12:00:00"
    assert parse_date("sometime") is None


# ---------- never applying twice ----------

def test_import_adds_to_applied_once(client):
    r = _upload_applications(client).json()
    assert (r["imported"], r["already_had"], r["undated"]) == (2, 0, 0)
    applied = client.get("/api/internships", params={"status": "applied"}).json()
    assert {(a["company"], a["source"]) for a in applied} == {("Airbnb", "linkedin"), ("Notion", "linkedin")}
    assert _upload_applications(client).json()["imported"] == 0        # same file again: nothing new
    bad = client.post("/api/linkedin/applications/import", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert bad.status_code == 400 and "Job Applications" in bad.json()["detail"]


def test_manual_add_of_a_job_already_applied_to(client):
    _upload_applications(client)
    r = client.post("/api/internships", json={"company": "Airbnb", "title": "Software Engineer Intern"})
    assert r.status_code == 409 and "already applied" in r.json()["detail"] and "LinkedIn" in r.json()["detail"]
    # same company, different job: fine
    ok = client.post("/api/internships", json={"company": "Airbnb", "title": "Data Science Intern"})
    assert ok.status_code == 200
    # an application from over 6 months ago is most likely last year's posting
    assert client.post("/api/internships", json={"company": "Notion", "title": "Software Engineer Intern"}
                       ).status_code == 200


def test_queued_internships_already_applied_to_are_archived(client):
    queued = client.post("/api/internships", json={"company": "Airbnb", "title": "Software Engineering Intern",
                                                   "url": "https://careers.airbnb.com/positions/8360478"}).json()
    chosen = client.post("/api/internships", json={"company": "Airbnb",
                                                   "title": "Software Engineering Intern (BS/MS)"}).json()
    client.post(f"/api/internships/{chosen['id']}/status", json={"status": "needs_attention"})
    client.post(f"/api/internships/{chosen['id']}/apply", json={})       # Retry bot: your choice, left alone
    r = _upload_applications(client).json()
    assert r["archived"] == 1
    from app import db
    it = db.get_internship(queued["id"])
    assert it["status"] == "archived" and "already applied" in it["error"]
    assert db.get_internship(chosen["id"])["status"] == "applying"


def test_newsletter_listing_already_applied_to_is_skipped(client):
    from app import db, inbox
    _upload_applications(client)
    listing = {"company": "Airbnb", "title": "Software Engineering Intern", "location": "San Francisco, CA"}
    decision, reason, iid, apply_now = inbox._route(listing, "https://careers.airbnb.com/positions/1")
    assert decision == "duplicate" and reason.startswith("You already applied on ") and not apply_now
    assert db.get_internship(iid)["source"] == "linkedin"


def test_bot_applications_count_too(client):
    it = client.post("/api/internships", json={"company": "Figma", "title": "Software Engineer Intern",
                                               "url": "https://boards.greenhouse.io/figma/jobs/1"}).json()
    client.post(f"/api/internships/{it['id']}/status", json={"status": "applied"})
    r = client.post("/api/internships", json={"company": "Figma", "title": "Software Engineering Intern"})
    assert r.status_code == 409


# ---------- the referral wait limit ----------

def test_waits_a_week_by_default_then_applies(client):
    it = client.post("/api/internships", json={"company": "Stripe", "title": "Software Engineer Intern",
                                               "url": "https://stripe.com/jobs/1"}).json()
    assert it["status"] == "waiting_for_referral"
    listed = client.get("/api/internships", params={"status": "waiting_for_referral"}).json()[0]
    due = datetime.fromisoformat(listed["applies_on"]) - datetime.fromisoformat(listed["waiting_since"])
    assert due == timedelta(days=7)

    from app import routing
    assert routing.apply_after_wait() == []                # not yet
    _backdate_wait(it["id"], 8)
    assert routing.apply_after_wait() == [it["id"]]
    row = client.get("/api/internships", params={"status": "applying"}).json()[0]
    assert row["referral_decided"] == 1 and row["applies_on"] is None
    routing.recount()                                       # a contact check won't pull it back
    assert client.get("/api/internships", params={"status": "applying"}).json()[0]["id"] == it["id"]


def test_shorter_wait_applies_at_once_and_zero_means_never(client):
    it = client.post("/api/internships", json={"company": "Stripe", "title": "Software Engineer Intern",
                                               "url": "https://stripe.com/jobs/2"}).json()
    _backdate_wait(it["id"], 3)
    assert _set_wait(client, 0)["referral_wait_days"] == 0
    from app import routing
    assert routing.apply_after_wait() == []
    assert client.get("/api/internships", params={"status": "waiting_for_referral"}).json()[0]["applies_on"] is None
    _set_wait(client, 2)                                    # already waited 3 days: queued right away
    assert client.apply_calls == [it["id"]]
    s = client.get("/api/settings").json()
    assert client.put("/api/settings", json={"profile": s["profile"], "apply_mode": "auto",
                                             "referral_wait_days": 99}).status_code == 400


def test_restoring_to_waiting_restarts_the_clock(client):
    it = client.post("/api/internships", json={"company": "Stripe", "title": "Software Engineer Intern",
                                               "url": "https://stripe.com/jobs/3"}).json()
    _backdate_wait(it["id"], 30)
    client.post(f"/api/internships/{it['id']}/status", json={"status": "archived"})
    client.post(f"/api/internships/{it['id']}/status", json={"status": "waiting_for_referral"})
    from app import routing
    assert routing.apply_after_wait() == []


def test_upgrade_gives_waiting_internships_a_full_wait(tmp_path, monkeypatch):
    """Internships already waiting before this version get the full wait from the upgrade on."""
    import importlib
    import sqlite3
    monkeypatch.setenv("RF_DATA_DIR", str(tmp_path))
    from app import db
    importlib.reload(db)
    db.init()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute("INSERT INTO internships(company,company_norm,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                  ("Stripe", "stripe", db.WAITING, old, old))
    con = sqlite3.connect(db.DB_PATH)
    con.execute("ALTER TABLE internships DROP COLUMN waiting_since")     # the database as it was
    con.commit()
    con.close()
    db.init()
    from app import routing
    importlib.reload(routing)
    assert routing.apply_after_wait(7) == []


# ---------- connections without employers ----------

def test_connections_file_without_employers_is_refused_and_nothing_lost(client):
    before = client.get("/api/summary").json()["connections"]
    assert before > 0
    december = b"Notes:\n\"...\"\n\nFirst Name,Last Name,URL,Email Address,Connected On\nA,B,https://x,,01 Dec 2026\n"
    r = client.post("/api/connections/import", files={"file": ("Connections.csv", december, "text/csv")})
    assert r.status_code == 400 and "December 2026" in r.json()["detail"]
    blank = b"First Name,Last Name,URL,Company,Position\nA,B,https://x,,\nC,D,https://y,,\n"
    r = client.post("/api/connections/import", files={"file": ("Connections.csv", blank, "text/csv")})
    assert r.status_code == 400 and "2 connections" in r.json()["detail"]
    assert client.get("/api/summary").json()["connections"] == before
