"""Gmail newsletter import: parsing, filtering, sender checks and the weekly routing flow."""
import functools
import http.server
import importlib
import sqlite3
import threading
from collections import Counter

from conftest import FIXTURES, SENDER, make_email

from app import newsletter as nl

HTML = (FIXTURES / "newsletter_email.html").read_text(encoding="utf-8")
T = "https://link.mail.beehiiv.com/ss/c/u001."     # the newsletter's click-tracking links

# Where the tracking links "redirect" in these tests (the resolver is stubbed; no network).
FINAL = {
    T + "t02/fav1": "https://paypal.eightfold.ai/careers/job/274922982814?domain=paypal.com",
    T + "t05/swe1": "https://jobs.lever.co/palantir/00000000-0000-4000-8000-000000000001",
    T + "t06/swe2": "https://job-boards.greenhouse.io/thenewyorktimes/jobs/1000001",
    T + "t09/data1": "https://jobs.lever.co/zoox/00000000-0000-4000-8000-000000000002",
    T + "t10/data2": "https://nuro.example/careers/1000002",
    T + "t14/fin1": "https://citi.wd5.myworkdayjobs.com/2/job/New-York/Analyst_1000003",
    T + "t15/fin2": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1000004",
    T + "t19/mkt2": "https://jobs.ashbyhq.com/campfire/00000000-0000-4000-8000-000000000003/application",
    T + "next/zoox": "https://jobs.lever.co/zoox/00000000-0000-4000-8000-000000000002",
}

CONNECTIONS = (
    b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    b"Ada,One,https://www.linkedin.com/in/a1,,Palantir Technologies,Engineer,01 Jan 2025\n"
    b"Bo,Two,https://www.linkedin.com/in/b2,,JPMorgan Chase & Co.,Analyst,01 Jan 2025\n"
    b"Cy,Three,https://www.linkedin.com/in/c3,,JPMorgan Chase & Co.,VP,01 Jan 2025\n"
    b"Di,Four,https://www.linkedin.com/in/d4,,PayPal,PM,01 Jan 2025\n"
    b"Ed,Five,https://www.linkedin.com/in/e5,,Equitable Bank,Teller,01 Jan 2025\n"
)

USA_MATCHES = {
    "Software Engineer Intern", "Software Engineer, Internship - Sovereign AI Systems",
    "Backend Engineering Intern", "Data Science Intern", "Research Scientist Intern, Perception",
    "Markets Sales and Trading Summer Analyst", "Markets Analyst Program Research",
    "Marketing Analytics Intern",
}
CANADA_MATCHES = {
    "AI Developer Intern", "Software Developer Co-op", "Software Engineering Intern",
    "Capital Markets, Global Equities, AI Engineer", "Mortgage Operations Analyst Intern",
}


def filters(regions=("USA",), mode="auto"):
    return {"regions": list(regions), "keywords": nl.DEFAULT_KEYWORDS, "exclude": nl.DEFAULT_EXCLUDE,
            "mode": mode}


def connect(client, password="abcd efgh ijkl mnop"):
    return client.post("/api/gmail/connect", json={"address": "Me@Gmail.com", "app_password": password})


def ready_to_apply(client):
    client.post("/api/resumes", files={"file": ("resume.pdf", b"%PDF-1.4", "application/pdf")})
    client.put("/api/settings", json={"apply_mode": "auto", "profile": {
        "first_name": "Sam", "last_name": "Lee", "email": "sam@example.com"}})


# ---------- reading the email ----------

def test_parses_every_listing_and_nothing_else():
    ls = nl.parse_listings(HTML)
    assert len(ls) == 19      # not the header, footer, buttons, hidden Outlook block or prose with an "@"
    assert ls[0] == {
        "title": "AI Developer Intern", "company": "Patry Group", "location": "Kingston, ON",
        "url": T + "t01/feat", "section": "Hiring through Intern Insider", "region": "Canada",
    }
    by = {l["title"]: l for l in ls}
    paypal = by["Software Engineer Intern"]
    assert (paypal["company"], paypal["location"]) == ("PayPal", "San Jose, CA | Chicago, IL + 3 more")
    assert (paypal["section"], paypal["region"]) == ("Our favourite jobs", "USA")      # country from the location
    palantir = by["Software Engineer, Internship - Sovereign AI Systems"]              # hyphen inside the title
    assert (palantir["company"], palantir["section"], palantir["region"]) == (
        "Palantir Technologies", "Software Engineering", "USA")
    brave = by["Software Engineering Intern"]
    assert (brave["company"], brave["location"], brave["region"]) == ("Brave", "Remote", "Canada")   # from the heading
    eq = by["Intern - Product Management"]
    assert (eq["company"], eq["location"], eq["section"]) == ("EQ Bank | Equitable Bank", "Toronto, ON", "PM & UI/UX")
    assert by["Research Scientist Intern, Perception"]["section"] == "Data & AI/ML"
    assert by["Markets Analyst Program Research"]["section"] == "Finance & Accounting"


def test_parsing_does_not_depend_on_the_markup():
    html = """<div><p><b>Software Engineering</b></p><p><strong>USA</strong></p>
      <div><a href="https://x.example/1">Backend Intern</a> @ Acme – Austin, TX<br>
      <a href="https://x.example/2">Data Intern @ Beta Corp - Remote</a></div>
      <p><b>Canada</b></p><p>• <a href="https://x.example/3">Web Developer Co-op</a> @ Gamma — Toronto, ON</p>
      <p><a href="http://localhost:8000/api/gmail">Sneaky Intern</a> @ Nowhere – Remote</p></div>"""
    got = [(l["title"], l["company"], l["location"], l["section"], l["region"]) for l in nl.parse_listings(html)]
    assert got == [
        ("Backend Intern", "Acme", "Austin, TX", "Software Engineering", "USA"),
        ("Data Intern", "Beta Corp", "Remote", "Software Engineering", "USA"),
        ("Web Developer Co-op", "Gamma", "Toronto, ON", "Software Engineering", "Canada"),
    ]


def test_country_is_guessed_from_the_location():
    assert nl.infer_region("Toronto, ON") == "Canada"
    assert nl.infer_region("Vancouver, BC") == "Canada"
    assert nl.infer_region("San Jose, CA | Chicago, IL + 3 more") == "USA"
    assert nl.infer_region("Washington, DC") == "USA"
    assert nl.infer_region("Ontario, CA") == "USA"            # the city in California
    assert nl.infer_region("Ontario, Canada") == "Canada"
    assert nl.infer_region("Remote") is None
    assert nl.infer_region(None) is None


# ---------- deciding what to apply to ----------

def test_default_filters_pick_the_roles_asked_for():
    ls = nl.parse_listings(HTML)
    usa = {l["title"] for l in ls if nl.classify(l, filters())[0]}
    assert usa == USA_MATCHES
    both = {l["title"] for l in ls if nl.classify(l, filters(("USA", "Canada")))[0]}
    assert both == USA_MATCHES | CANADA_MATCHES

    why = {l["title"]: nl.classify(l, filters())[1] for l in ls}
    assert why["Markets Sales and Trading Summer Analyst"] == 'Title matches "sales analy"'
    assert why["Markets Analyst Program Research"] == 'In "Finance & Accounting", which matches "financ analy"'
    assert why["Research Scientist Intern, Perception"] == 'In "Data & AI/ML", which matches "data"'
    assert why["Human Resources Intern"] == "Doesn't match your keywords"
    assert why["Fund Accounting Intern"] == "Doesn't match your keywords"
    assert why["UX Designer Intern"] == "Canada isn't one of your regions"


def test_keyword_rules():
    def hit(keyword, text):
        return nl.keyword_hit(keyword, nl._tokens(text))

    assert hit("AI", "AI Developer Intern") and hit("AI", "Data & AI/ML")
    assert not hit("AI", "Retail Aide Intern") and not hit("ML", "HTML Email Intern")   # short words match whole words only
    assert hit("analy", "Business Analytics Intern") and hit("analy", "Summer Analyst")
    assert hit("financ analy", "Financial Planning & Analysis Intern")
    assert not hit("financ analy", "Financial Advisor Intern")                          # every word must appear
    assert hit("full stack", "Full-Stack Engineering Intern")
    assert not hit("developer", "Business Development Intern")
    assert not hit("", "anything") and not hit(" , ", "anything")

    entry = {"title": "Data Entry Intern", "section": None, "region": "USA"}
    assert nl.classify(entry, filters()) == (False, 'Excluded by "data entry"')
    unknown_country = {"title": "Data Intern", "section": None, "region": None}
    assert nl.classify(unknown_country, filters())[0] is True


# ---------- links ----------

def test_tracking_parameters_are_stripped_and_private_links_refused():
    assert nl.clean_url("https://jobs.lever.co/x/1?utm_source=a&lever-source=b&_bhlid=z#frag") == \
        "https://jobs.lever.co/x/1?lever-source=b"
    assert nl.clean_url("https://stripe.com/jobs/search?gh_jid=8332683&utm_medium=newsletter") == \
        "https://stripe.com/jobs/search?gh_jid=8332683"
    assert nl.clean_url("https://a.example/job/1") == "https://a.example/job/1"

    assert nl.is_public_url("https://jobs.lever.co/x")
    for bad in ("http://localhost:8000/api", "http://127.0.0.1/x", "http://192.168.1.5/", "http://intranet/",
                "mailto:x@y.z", "javascript:alert(1)", "", None):
        assert not nl.is_public_url(bad), bad


class _Redirects(http.server.BaseHTTPRequestHandler):
    ROUTES = {
        "/click/1": (302, "/hop"),
        "/hop": (302, "/jobs/42?gh_jid=42&utm_source=newsletter&utm_medium=email"),
        "/jobs/42?gh_jid=42&utm_source=newsletter&utm_medium=email": (200, None),
        "/jobs/42?gh_jid=42": (200, None),
        "/click/refused": (302, "/forbidden?utm_campaign=x"),
        "/forbidden?utm_campaign=x": (403, None),
        "/click/internal": (302, "http://169.254.169.254/latest/meta-data"),
    }

    def do_GET(self):
        code, location = self.ROUTES.get(self.path, (404, None))
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_resolving_a_tracking_link(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirects)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    # The test server is on this machine, so let local addresses count as public here.
    monkeypatch.setattr(nl, "is_public_url", lambda u: "169.254." not in u)
    try:
        assert nl.resolve_url(base + "/click/1") == base + "/jobs/42?gh_jid=42"       # two hops, tracking stripped
        assert nl.resolve_url(base + "/click/refused") == base + "/forbidden"         # site said 403; URL still learned
        assert nl.resolve_url(base + "/jobs/42?gh_jid=42") == base + "/jobs/42?gh_jid=42"
        assert nl.resolve_url(base + "/click/internal") == ""                         # redirect to a private address
        assert nl.resolve_url("http://127.0.0.1:1/unreachable") == "http://127.0.0.1:1/unreachable"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- who sent it ----------

def test_only_emails_gmail_authenticated_are_trusted():
    from app import gmail
    info = gmail.read_message(make_email())
    assert info["verified"] is True
    assert (info["sender"], info["sender_name"]) == (SENDER, "Intern Insider")
    assert "Tech Internships Released This Week" in info["html"]
    assert info["message_id"].startswith("<") and info["date"].endswith("+00:00")

    assert gmail.read_message(make_email(auth="fail"))["verified"] is False
    assert gmail.read_message(make_email(auth=None))["verified"] is False               # no verdict from Gmail

    plain = make_email(auth=None)
    forged = b"Authentication-Results: evil.example; dmarc=pass header.from=interninsider.me\r\n" + plain
    assert gmail.read_message(forged)["verified"] is False                             # not Gmail's verdict
    lower = make_email(auth="fail").replace(
        b"\nFrom:", b"\nAuthentication-Results: mx.google.com; dmarc=pass header.from=interninsider.me\nFrom:", 1)
    assert gmail.read_message(lower)["verified"] is False                              # only the top one is Gmail's

    aligned = b"Authentication-Results: mx.google.com; dkim=pass header.i=@interninsider.me header.s=k1\r\n" + plain
    assert gmail.read_message(aligned)["verified"] is True                             # no DMARC record, DKIM matches
    other = b"Authentication-Results: mx.google.com; dkim=pass header.i=@sendgrid.info header.s=k1\r\n" + plain
    assert gmail.read_message(other)["verified"] is False                              # signed, but by someone else

    assert gmail.read_message(b"not an email at all")["verified"] is False


# ---------- companies ----------

def test_company_names_match_despite_spacing_and_aliases(client):
    from app.companies import aliases, matches, normalize
    assert matches(normalize("JPMorganChase"), normalize("JPMorgan Chase & Co."))
    assert matches(normalize("Pay Pal Inc"), normalize("PayPal"))
    assert not matches("meta", "metabase") and not matches("apple", "applebee s")
    assert aliases("AXIS (AXIS Capital)") == ["axis", "axis capital"]
    assert aliases("Brave (Remote)") == ["brave"]
    assert "equitable bank" in aliases("EQ Bank | Equitable Bank")

    client.post("/api/connections/import", files={"file": ("Connections.csv", CONNECTIONS, "text/csv")})
    count = lambda company: client.get("/api/check", params={"company": company}).json()["contact_count"]
    assert count("JPMorganChase") == 2
    assert count("EQ Bank | Equitable Bank") == 1
    assert count("Palantir") == 1 and count("Zoox") == 0


# ---------- connecting Gmail ----------

def test_connect_signs_in_and_finds_the_newsletter_sender(client, fake_gmail):
    from app import db
    srv = fake_gmail([
        make_email(days_ago=1),
        make_email(days_ago=8, subject="Last week's issue"),
        make_email(html="<p>We updated our terms.</p>", sender="team@interninsider.me", subject="Terms update"),
    ])
    bad = connect(client, password="wrong")
    assert bad.status_code == 400 and "app password" in bad.json()["detail"]
    assert client.get("/api/gmail").json()["connected"] is False

    s = connect(client).json()
    assert s["connected"] is True and s["address"] == "me@gmail.com"
    assert s["sender"] == SENDER            # the address most of their verified emails come from
    assert s["storage"] == "file" and s["ready"] is False      # still needs a country

    for path in ("/api/gmail", "/api/settings", "/api/summary", "/api/newsletter/emails"):
        assert "abcdefghijklmnop" not in client.get(path).text          # the password never comes back out

    done = [c.upper() for c in srv.commands]
    assert any(c.startswith("EXAMINE") for c in done)                   # opened read-only
    assert not any(c.startswith(("SELECT", "STORE", "UID STORE", "APPEND", "EXPUNGE", "UID COPY")) for c in done)
    assert all("PEEK" in c for c in done if c.startswith("UID FETCH"))  # nothing gets marked as read

    assert client.post("/api/gmail/disconnect").json()["connected"] is False
    assert db.get_setting("gmail_app_password") is None


def test_connect_falls_back_to_the_known_sender_or_the_one_you_give(client, fake_gmail):
    fake_gmail([])                                                      # no newsletter in the mailbox yet
    assert connect(client).json()["sender"] == "hi@interninsider.me"
    r = client.post("/api/gmail/connect", json={"address": "me@gmail.com", "app_password": "abcdefghijklmnop",
                                                 "sender": "Jobs@Other-Newsletter.example"})
    assert r.json()["sender"] == "jobs@other-newsletter.example"
    assert client.post("/api/gmail/connect", json={"address": "not-an-address", "app_password": "x"}).status_code == 400


def test_filters_must_be_usable(client):
    put = lambda **kw: client.put("/api/gmail/filters", json={**filters(), **kw})
    assert put(regions=[]).status_code == 400
    assert put(regions=["Mars"]).status_code == 400
    assert put(mode="yolo").status_code == 400
    assert put(keywords=[" "]).status_code == 400
    ok = put(regions=["Canada", "USA"], keywords=["data", "Data", " ai "], exclude=[])
    assert ok.status_code == 200
    assert ok.json()["filters"] == {"regions": ["USA", "Canada"], "keywords": ["data", "ai"], "exclude": [],
                                    "mode": "auto"}


# ---------- the weekly check ----------

def test_check_reads_the_newsletter_and_routes_matches(client, fake_gmail, monkeypatch):
    monkeypatch.setattr(nl, "resolve_url", lambda u: FINAL.get(u, u))
    client.post("/api/connections/import", files={"file": ("Connections.csv", CONNECTIONS, "text/csv")})
    srv = fake_gmail([
        make_email(days_ago=1),                                     # this week's issue
        make_email(days_ago=20, subject="An old issue"),            # too old to act on
        make_email(days_ago=2, auth="fail", subject="Spoofed"),     # claims to be the sender; fails Gmail's check
    ])
    connect(client)

    r = client.post("/api/gmail/check")
    assert r.status_code == 400 and "USA" in r.json()["detail"]     # must choose a country first
    client.put("/api/gmail/filters", json=filters())
    r = client.post("/api/gmail/check")
    assert r.status_code == 400 and "resume" in r.json()["detail"]  # and have something to apply with
    assert client.get("/api/gmail").json()["ready"] is False
    ready_to_apply(client)
    assert client.get("/api/gmail").json()["ready"] is True

    r = client.post("/api/gmail/check").json()
    assert r == {"found": 3, "new_emails": 2, "listings": 19, "matched": 8, "waiting": 3, "applying": 5,
                 "pending": 0}

    its = {i["company"]: i for i in client.get("/api/internships").json()}
    assert len(its) == 8
    assert (its["Palantir Technologies"]["status"], its["Palantir Technologies"]["contact_count"]) == (
        "waiting_for_referral", 1)
    assert (its["JPMorganChase"]["status"], its["JPMorganChase"]["contact_count"]) == ("waiting_for_referral", 2)
    assert its["PayPal"]["status"] == "waiting_for_referral"
    zoox = its["Zoox"]
    assert zoox["status"] == "applying" and zoox["title"] == "Data Science Intern"
    assert zoox["url"] == FINAL[T + "t09/data1"]                    # the real posting, not the tracking link
    assert (zoox["location"], zoox["source"]) == ("Foster City, CA | Boston, MA", "newsletter")
    applying = sorted(i["id"] for i in its.values() if i["status"] == "applying")
    assert len(applying) == 5 and sorted(client.apply_calls) == applying       # only those went to the bot

    real, spoofed = client.get("/api/newsletter/emails").json()
    assert spoofed["subject"] == "Spoofed" and spoofed["listings"] == []
    assert "couldn't confirm" in spoofed["note"]
    assert Counter(l["decision"] for l in real["listings"]) == {"routed": 8, "skipped": 11}
    by = {l["title"]: l for l in real["listings"]}
    assert by["UX Designer Intern"]["reason"] == "Canada isn't one of your regions"
    assert by["Human Resources Intern"]["reason"] == "Doesn't match your keywords"
    jpm = by["Markets Analyst Program Research"]
    assert (jpm["internship_status"], jpm["contact_count"]) == ("waiting_for_referral", 2)
    text = client.get("/api/newsletter/emails").text + client.get("/api/internships").text
    assert "linkedin.com/in" not in text and "Ada" not in text      # counts only, never who

    again = client.post("/api/gmail/check").json()                  # nothing is handled twice
    assert again["new_emails"] == 0 and len(client.get("/api/internships").json()) == 8
    status = client.get("/api/gmail").json()["status"]
    assert status["last_error"] is None and status["last_new"]["matched"] == 8

    # "Apply anyway" on something the filters skipped
    r = client.post("/api/newsletter/listings/%d/apply" % by["Fund Accounting Intern"]["id"]).json()
    assert r["decision"] == "routed" and len(client.apply_calls) == 6

    # Next week repeats two postings: one resolves to a URL we already have, one only matches by name.
    repeat = """<h2>Data &amp; AI/ML</h2><h4>USA</h4><ul>
      <li><a href="%snext/zoox">Data Science Intern</a> @ Zoox – Foster City, CA | Boston, MA</li>
      <li><a href="%snext/nyt">Backend Engineering Intern</a> @ The New York Times – New York City, NY</li></ul>""" % (T, T)
    srv.messages.append(make_email(html=repeat, days_ago=0, subject="Next issue"))
    r = client.post("/api/gmail/check").json()
    assert (r["new_emails"], r["matched"], r["applying"], r["waiting"]) == (1, 2, 0, 0)
    newest = client.get("/api/newsletter/emails").json()[0]
    assert [l["decision"] for l in newest["listings"]] == ["duplicate", "duplicate"]
    assert len(client.get("/api/internships").json()) == 9 and len(client.apply_calls) == 6


def test_review_mode_waits_for_your_ok(client, fake_gmail, monkeypatch):
    monkeypatch.setattr(nl, "resolve_url", lambda u: FINAL.get(u, u))
    fake_gmail([make_email()])
    connect(client)
    ready_to_apply(client)
    client.put("/api/gmail/filters", json=filters(("USA", "Canada"), mode="review"))

    r = client.post("/api/gmail/check").json()
    assert (r["matched"], r["pending"], r["applying"], r["waiting"]) == (13, 13, 0, 0)
    assert client.get("/api/internships").json() == [] and client.apply_calls == []

    email = client.get("/api/newsletter/emails").json()[0]
    pending = [l for l in email["listings"] if l["decision"] == "pending"]
    assert len(pending) == 13
    one = client.post("/api/newsletter/listings/%d/apply" % pending[0]["id"]).json()
    assert one["decision"] == "routed" and len(client.apply_calls) == 1
    assert client.post("/api/newsletter/listings/%d/apply" % pending[0]["id"]).json()["decision"] == "routed"
    assert len(client.apply_calls) == 1                              # clicking twice doesn't apply twice

    assert client.post("/api/newsletter/emails/%d/apply-all" % email["id"]).json() == {"queued": 12}
    assert len(client.get("/api/internships").json()) == 13 and len(client.apply_calls) == 13
    assert client.post("/api/newsletter/listings/999999/apply").status_code == 404


def test_gmail_trouble_is_reported_not_fatal(client, fake_gmail):
    srv = fake_gmail([make_email()])
    connect(client)
    ready_to_apply(client)
    client.put("/api/gmail/filters", json=filters())
    srv.password = "revoked"                                         # the app password stops working
    r = client.post("/api/gmail/check")
    assert r.status_code == 400 and "app password" in r.json()["detail"]
    assert "app password" in client.get("/api/gmail").json()["status"]["last_error"]


# ---------- housekeeping ----------

def test_only_this_computer_can_drive_the_app(client):
    it = client.post("/api/internships", json={"company": "Figma"}).json()
    client.post("/api/internships/%d/status" % it["id"], json={"status": "waiting_for_referral"})
    client.apply_calls.clear()
    apply = "/api/internships/%d/apply" % it["id"]

    # another website open in your browser tries to make the bot apply
    assert client.post(apply, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(apply, headers={"Origin": "null"}).status_code == 403
    # a site pretending to be this app through DNS, or another machine on the network
    assert client.get("/api/internships", headers={"Host": "evil.example:8000"}).status_code == 403
    assert client.get("/", headers={"Host": "192.168.1.20:8000"}).status_code == 403
    assert client.apply_calls == []

    # the app's own page, however you opened it
    assert client.post(apply, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200
    assert client.get("/", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.apply_calls == [it["id"]]


def test_restart_picks_up_queued_applications(client):
    it = client.post("/api/internships", json={"company": "Figma", "url": "https://jobs.example.com/9"}).json()
    assert it["status"] == "applying" and client.apply_calls == [it["id"]]
    with client:                                                     # start the app again
        client.get("/api/summary")
    assert client.apply_calls == [it["id"], it["id"]]


def test_bot_skips_what_left_the_queue(client, tmp_path):
    import asyncio
    from app import applier, db
    it = client.post("/api/internships", json={"company": "Figma", "url": "https://jobs.example.com/10"}).json()
    client.post("/api/internships/%d/status" % it["id"], json={"status": "archived"})
    importlib.reload(applier)                                        # the real bot, not the test double
    asyncio.run(applier.run_application(it["id"]))
    assert db.get_internship(it["id"])["status"] == "archived"       # untouched; no browser was launched


def test_old_database_is_upgraded_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("RF_DATA_DIR", str(tmp_path))
    con = sqlite3.connect(str(tmp_path / "app.db"))
    con.executescript("""
        CREATE TABLE internships (
            id INTEGER PRIMARY KEY, company TEXT NOT NULL, company_norm TEXT NOT NULL, title TEXT, url TEXT,
            status TEXT NOT NULL, contact_count INTEGER NOT NULL DEFAULT 0, resume_id INTEGER,
            referral_link TEXT, notes TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            applied_at TEXT);
        INSERT INTO internships(company, company_norm, status, created_at, updated_at)
            VALUES('Stripe', 'stripe', 'applied', '2026-01-01', '2026-01-01');
    """)
    con.commit()
    con.close()
    from app import db
    importlib.reload(db)
    db.init()
    row = db.get_internship(1)
    assert (row["company"], row["source"], row["location"]) == ("Stripe", "manual", None)


def test_app_password_goes_to_the_keychain_when_there_is_one(client, monkeypatch):
    from app import credstore, db

    class Keychain:
        def __init__(self):
            self.items = {}

        def set_password(self, service, name, password):
            self.items[(service, name)] = password

        def get_password(self, service, name):
            return self.items.get((service, name))

        def delete_password(self, service, name):
            del self.items[(service, name)]

    kc = Keychain()
    monkeypatch.setattr(credstore, "_keyring", lambda: kc)
    assert credstore.save("me@gmail.com", "pw") == "keychain"
    assert db.get_setting("gmail_app_password") is None              # not written to the database file
    assert credstore.load("me@gmail.com") == "pw"
    credstore.forget("me@gmail.com")
    assert credstore.load("me@gmail.com") is None and kc.items == {}

    monkeypatch.setattr(credstore, "_keyring", lambda: None)         # no keychain on this machine
    assert credstore.save("me@gmail.com", "pw") == "file"
    assert credstore.load("me@gmail.com") == "pw" and credstore.load("other@gmail.com") is None
    credstore.forget("me@gmail.com")
    assert credstore.load("me@gmail.com") is None
