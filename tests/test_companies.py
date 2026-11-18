"""The contact check: does a posting's company match how a connection's employer is written?"""
import pytest

from app import companies

# (what you type or the newsletter says, how LinkedIn lists the contact's employer)
SAME = [
    ("Amazon", "Amazon Web Services (AWS)"), ("AWS", "Amazon Web Services (AWS)"),
    ("Disney", "The Walt Disney Company"), ("Schwab", "Charles Schwab"),
    ("NASA Jet Propulsion Laboratory", "Jet Propulsion Laboratory"),
    ("Chase", "JPMorganChase"), ("JPMorgan", "JPMorganChase"), ("JPMorganChase", "J.P. Morgan"),
    ("JP Morgan", "JPMorgan Chase & Co."), ("JPMorganChase", "JPMorgan Chase & Co."),
    ("Meta", "Facebook"), ("Facebook", "Meta Platforms, Inc."), ("Instagram", "Meta"), ("Google", "YouTube"),
    ("BCG", "Boston Consulting Group (BCG)"), ("EY", "Ernst & Young"), ("PwC", "PricewaterhouseCoopers"),
    ("Citi", "Citigroup"), ("General Motors", "GM"), ("GM", "General Motors"), ("American Express", "Amex"),
    ("Bank of America", "Merrill Lynch"), ("Merrill", "Bank of America"), ("TikTok", "ByteDance"),
    ("RBC", "Royal Bank of Canada"), ("Optum", "UnitedHealth Group"), ("NYT", "The New York Times"),
    ("Venmo", "PayPal"),
    ("L'Oreal", "L'Oréal"), ("Nestle", "Nestlé"),                                   # accents
    ("Nvdia", "NVIDIA"), ("Goldman Sach", "Goldman Sachs"), ("Northrup Grumman", "Northrop Grumman"),   # typos
    ("T-Mobile", "TMobile"), ("McDonald's", "McDonalds"), ("U.S. Bank", "US Bank"),  # spacing and punctuation
    ("Microsoft", "Microsoft"), ("Capital One", "Capital One"), ("Deloitte", "Deloitte Consulting"),
    ("Samsung Electronics", "Samsung Electronics America"), ("The New York Times", "New York Times"),
    ("EQ Bank | Equitable Bank", "Equitable Bank"), ("Palantir Technologies", "Palantir"),
    ("Bain", "Bain & Company"), ("McKinsey", "McKinsey & Company"), ("Citadel", "Citadel Securities"),
    ("Morgan Stanley", "Morgan Stanley Wealth Management"),
    ("Twitter", "X (formerly Twitter)"), ("Google", "Stealth Startup | ex-Google"),
]

DIFFERENT = [
    ("Meta", "Metabase"), ("Apple", "Applebee's"), ("Intel", "Intelligent Systems"),
    ("Cisco", "City and County of San Francisco"), ("Capital One", "Capital Group"), ("Capital One", "ONE"),
    ("Bank of America", "Bank of New York Mellon"), ("Bank of Montreal", "Bank of America"),
    ("Royal Bank of Canada", "Bank of Canada"), ("Morgan Stanley", "JPMorganChase"),
    ("Morgan Stanley", "J.P. Morgan"), ("JPMorgan", "Morgan Stanley"),
    ("American Express", "American Airlines"), ("United Airlines", "UnitedHealth Group"),
    ("General Motors", "General Electric"), ("New York Life", "The New York Times"),
    ("Citi", "Citizens Bank"), ("Square", "Squarespace"), ("Block", "Blockchain.com"),
    ("Stripe", "Strive Health"), ("Stripe", "Stryker"), ("Adobe", "Abode"), ("Oracle", "Coracle"),
    ("Nike", "Nikon"), ("Visa", "Vista"), ("Uber", "Ubisoft"), ("Tesla", "Tessera"),
    ("Ford", "Fordham University"), ("Dell", "Deloitte"), ("Samsung Electronics", "LG Electronics"),
    ("Northrop Grumman", "Northwestern Mutual"), ("Lockheed Martin", "Martin Marietta"),
    ("Boston Consulting Group", "Boston Scientific"), ("Boston Dynamics", "General Dynamics"),
    ("Johnson & Johnson", "Johnson Controls"), ("GE", "GEICO"), ("TD", "TDK"), ("Zoox", "Zoom"),
    ("Notion", "Motion"), ("Samsara", "Samsung"), ("Brave", "Braze"), ("Goldman Sachs", "Goldman Group"),
    ("University of Texas", "University of Toronto"), ("Affirm", "Affinity"),
    ("Delta Air Lines", "Deltek"), ("Wayfair", "Waymo"),
]


def found(company, employer, groups=()):
    return bool(companies.find(company, [(employer, 1)], groups))


@pytest.mark.parametrize("company,employer", SAME)
def test_same_company_is_found(company, employer):
    assert found(company, employer)


@pytest.mark.parametrize("company,employer", DIFFERENT)
def test_different_company_is_not(company, employer):
    assert not found(company, employer)


def test_find_reports_employer_names_and_counts():
    employers = [("JPMorganChase", 2), ("J.P. Morgan", 1), ("Morgan Stanley", 5), ("Chase", 1), ("Zoox", 3)]
    assert companies.find("JPMorgan Chase & Co.", employers) == [("JPMorganChase", 2), ("Chase", 1), ("J.P. Morgan", 1)]
    assert companies.find("Figma", employers) == []
    assert companies.find("", employers) == [] and companies.find(None, employers) == []
    assert companies.find("(Remote)", employers) == []        # nothing usable in the name


def test_your_own_groups():
    assert companies.parse_groups("Initech = Initrode Labs = Penetrode\n\nno equals sign\n= =\n A  =  B ") == [
        ["Initech", "Initrode Labs", "Penetrode"], ["A", "B"]]
    groups = companies.normalize_groups([["Initech", "Initrode Labs"]])
    assert not found("Initech", "Initrode Labs, Inc.")
    assert found("Initech", "Initrode Labs, Inc.", groups) and found("Initrode", "Initech LLC", groups)
    assert not found("Figma", "Initrode Labs", groups)


# ---------- through the app ----------

def csv(*rows):
    head = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    return (head + "".join("Zaphod,Beeblebrox,https://www.linkedin.com/in/zb%d,,%s,Engineer,01 Jan 2025\n" % (i, c)
                           for i, c in enumerate(rows))).encode("utf-8")


def import_csv(client, *rows):
    r = client.post("/api/connections/import", files={"file": ("Connections.csv", csv(*rows), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def add(client, company, n):
    r = client.post("/api/internships", json={"company": company, "url": "https://jobs.example.com/%d" % n})
    assert r.status_code == 200, r.text
    return r.json()


def by_id(client):
    return {i["id"]: i for i in client.get("/api/internships").json()}


def test_nothing_is_applied_to_before_connections_are_imported(fresh_client):
    r = fresh_client.post("/api/internships", json={"company": "Figma", "url": "https://jobs.example.com/1"})
    assert r.status_code == 400 and "Import your LinkedIn connections" in r.json()["detail"]
    assert fresh_client.get("/api/internships").json() == [] and fresh_client.apply_calls == []
    chk = fresh_client.get("/api/check", params={"company": "Figma"}).json()
    assert (chk["connections_loaded"], chk["has_contact"]) == (False, False)

    import_csv(fresh_client, "Stripe")
    assert add(fresh_client, "Figma", 1)["status"] == "applying"          # now the check means something


def test_gmail_check_waits_for_connections_too(fresh_client, fake_gmail):
    from conftest import make_email
    from test_newsletter import connect, filters
    fake_gmail([make_email()])
    connect(fresh_client)
    fresh_client.put("/api/gmail/filters", json=filters())
    r = fresh_client.post("/api/gmail/check")
    assert r.status_code == 400 and "LinkedIn connections" in r.json()["detail"]
    assert fresh_client.get("/api/newsletter/emails").json() == [] and fresh_client.apply_calls == []


def test_contacts_are_reported_by_employer_name_never_by_person(client):
    import_csv(client, "The Walt Disney Company", "The Walt Disney Company", "Disney Streaming", "Pixar")
    chk = client.get("/api/check", params={"company": "Disney"}).json()
    assert chk["contact_count"] == 3
    assert chk["listed_as"] == [{"company": "The Walt Disney Company", "count": 2},
                                {"company": "Disney Streaming", "count": 1}]
    it = add(client, "Disney", 1)
    assert (it["status"], it["contact_count"]) == ("waiting_for_referral", 3)
    assert it["listed_as"] == chk["listed_as"]
    assert by_id(client)[it["id"]]["listed_as"] == chk["listed_as"]       # shown on the Waiting tab too
    everything = (client.get("/api/check", params={"company": "Disney"}).text + client.get("/api/internships").text
                  + client.get("/api/connections/companies").text)
    assert "Zaphod" not in everything and "Beeblebrox" not in everything and "linkedin.com/in" not in everything


def test_employer_search(client):
    import_csv(client, "JPMorgan Chase & Co.", "JPMorgan Chase & Co.", "J.P. Morgan", "Morgan Stanley",
               "L'Oréal", "Stripe")
    search = lambda q: client.get("/api/connections/companies", params={"q": q}).json()
    rows = lambda q: [(h["company"], h["count"]) for h in search(q)["results"]]
    assert rows("morgan") == [("JPMorgan Chase & Co.", 2), ("J.P. Morgan", 1), ("Morgan Stanley", 1)]
    assert rows("jp morgan") == [("JPMorgan Chase & Co.", 2), ("J.P. Morgan", 1)]     # ignores dots and spacing
    assert rows("loreal") == [("L'Oréal", 1)] and rows("L'ORÉAL") == [("L'Oréal", 1)]
    assert rows("zzz") == []
    everything = search("")
    assert (everything["companies"], everything["matching"]) == (5, 5)
    assert everything["results"][0] == {"company": "JPMorgan Chase & Co.", "count": 2}


def test_finding_a_contact_later_moves_what_was_not_submitted(client):
    from app import db
    queued = add(client, "Disney", 1)                    # no contact known yet, so it went to the bot's queue
    handed_back = add(client, "Nvdia", 2)                # ...the bot gave this one back
    db.update_internship(handed_back["id"], status=db.NEEDS_ATTENTION, error="Not submitted: required fields")
    decided = add(client, "Chase", 3)                    # ...you told the bot to retry this one yourself
    db.update_internship(decided["id"], status=db.NEEDS_ATTENTION, error="Bot error")
    assert client.post("/api/internships/%d/apply" % decided["id"]).status_code == 200
    submitted = add(client, "Schwab", 4)                 # ...this one was submitted
    client.post("/api/internships/%d/status" % submitted["id"], json={"status": "applied"})
    unrelated = add(client, "Figma", 5)
    assert all(i["status"] == "applying" for i in (queued, handed_back, decided, submitted, unrelated))

    r = import_csv(client, "The Walt Disney Company", "The Walt Disney Company", "NVIDIA", "JPMorganChase",
                   "Charles Schwab")
    assert r == {"imported": 5, "no_employer": 0, "moved": 2}
    now = by_id(client)
    assert (now[queued["id"]]["status"], now[queued["id"]]["contact_count"]) == ("waiting_for_referral", 2)
    assert now[queued["id"]]["listed_as"] == [{"company": "The Walt Disney Company", "count": 2}]
    assert (now[handed_back["id"]]["status"], now[handed_back["id"]]["error"]) == ("waiting_for_referral", None)
    assert now[decided["id"]]["status"] == "applying"            # your decision stands
    assert now[submitted["id"]]["status"] == "applied"           # can't be un-submitted
    assert now[unrelated["id"]]["status"] == "applying"
    assert import_csv(client, "The Walt Disney Company", "NVIDIA")["moved"] == 0    # nothing left to move


def test_your_own_same_company_lines_through_the_app(client):
    from app import db
    it = add(client, "Initech", 1)
    db.update_internship(it["id"], status=db.NEEDS_ATTENTION, error="Not submitted")
    import_csv(client, "Initrode Labs")
    assert client.get("/api/check", params={"company": "Initech"}).json()["has_contact"] is False

    r = client.put("/api/connections/groups", json={"text": "Initech = Initrode Labs\nnot a pair\n"}).json()
    assert r == {"groups": 1, "moved": 1}
    chk = client.get("/api/check", params={"company": "Initech"}).json()
    assert chk["listed_as"] == [{"company": "Initrode Labs", "count": 1}]
    assert by_id(client)[it["id"]]["status"] == "waiting_for_referral"
    saved = client.get("/api/connections/groups").json()
    assert saved["text"] == "Initech = Initrode Labs"
    assert any(line.startswith("Meta = ") for line in saved["built_in"])


def test_restart_rechecks_contacts_before_the_bot_resumes(client):
    from app import db
    it = add(client, "Disney", 1)
    assert it["status"] == "applying" and client.apply_calls == [it["id"]]
    with db.connect() as c:      # as if this version's better matching arrived while the app was closed
        c.execute("INSERT INTO connections(first_name, company) VALUES('Zaphod', 'The Walt Disney Company')")
    with client:
        client.get("/api/summary")
    assert by_id(client)[it["id"]]["status"] == "waiting_for_referral"
    assert client.apply_calls == [it["id"]]                      # the bot did not pick it up again


def test_import_copes_with_a_file_resaved_by_a_spreadsheet_app(client):
    from app.linkedin_import import parse_connections_csv
    semicolons = ("Notes:\r\n\"Some note; with a semicolon\"\r\n\r\n"
                  "First Name;Last Name;URL;Email Address;Company;Position;Connected On\r\n"
                  "Zaphod;Beeblebrox;https://www.linkedin.com/in/zb;;Nestlé;Engineer;01 Jan 2025\r\n"
                  "Ford;Prefect;https://www.linkedin.com/in/fp;;;Student;01 Jan 2025\r\n"
                  ";;;;;;\r\n")
    for raw in (semicolons.encode("utf-8"), semicolons.encode("utf-16"), semicolons.replace(";", "\t").encode("utf-8")):
        rows, no_employer = parse_connections_csv(raw)
        assert [(r["first_name"], r["company"], r["position"]) for r in rows] == [("Zaphod", "Nestlé", "Engineer")]
        assert no_employer == 1                      # Ford has no employer; the blank row isn't a person

    odd_break = ("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                 "Zaphod,Beeblebrox,https://www.linkedin.com/in/zb,,Stripe,Engineer  Payments,01 Jan 2025\n")
    rows, _ = parse_connections_csv(odd_break.encode("utf-8"))
    assert [r["company"] for r in rows] == ["Stripe"]            # an odd line-break character inside a field

    r = client.post("/api/connections/import", files={"file": ("x.csv", b"just,some,other,file\n1,2,3,4\n", "text/csv")})
    assert r.status_code == 400 and "Connections.csv" in r.json()["detail"]
