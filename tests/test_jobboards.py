"""Finding a listing on the company's own Greenhouse, Lever or Ashby board.

The boards are faked here with the same JSON shapes their public APIs return.
"""
import pytest

from app import jobboards as jb


@pytest.fixture(autouse=True)
def boards(monkeypatch):
    """Fake boards: {url: JSON} (None = no board there). Records every URL asked for."""
    data, asked = {}, []
    jb._cache.clear()

    def fake_get(url):
        asked.append(url)
        value = data.get(url)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(jb, "_get_json", fake_get)
    data["asked"] = asked
    yield data
    jb._cache.clear()


def gh(slug):
    return "https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug


def lever(slug):
    return "https://api.lever.co/v0/postings/%s?mode=json" % slug


def ashby(slug):
    return "https://api.ashbyhq.com/posting-api/job-board/%s" % slug


def gh_job(title, jid, location="San Francisco, California", company="Airbnb"):
    return {"absolute_url": "https://careers.airbnb.com/positions/%d?gh_jid=%d" % (jid, jid), "id": jid,
            "location": {"name": location}, "title": title, "company_name": company}


def lever_job(title, uid, location):
    return {"text": title, "categories": {"location": location, "commitment": "Internship"},
            "hostedUrl": "https://jobs.lever.co/palantir/%s" % uid,
            "applyUrl": "https://jobs.lever.co/palantir/%s/apply" % uid}


def match(title, location, *titles_and_locations):
    postings = [{"title": t, "url": "https://example.com/%d" % i, "location": loc}
                for i, (t, loc) in enumerate(titles_and_locations)]
    found = jb.best_match(title, location, postings)
    return found and found["title"]


def test_board_addresses_to_try():
    assert jb.slugs("Airbnb") == [("airbnb", True)]
    assert jb.slugs("Jane Street") == [("janestreet", True), ("jane-street", True)]
    assert jb.slugs("Palantir Technologies") == [
        ("palantirtechnologies", True), ("palantir-technologies", True), ("palantir", False)]
    assert ("aws", True) in jb.slugs("Amazon Web Services (AWS)")


def test_titles_that_are_the_same_job():
    sf = "San Francisco, CA"
    assert match("Software Engineer Intern", sf, ("Software Engineering Intern (BS/MS)", sf))
    assert match("Software Engineering Internship", sf, ("Software Engineer Intern (Summer 2027)", sf))
    assert match("Software Engineer Intern - Connectivity Platform - Product", None,
                 ("Software Engineer, Internship - Connectivity Platform and Product", "New York, NY"))


def test_titles_that_rule_each_other_out():
    sf = "San Francisco, CA"
    assert not match("Software Engineering Intern (PhD)", sf, ("Software Engineering Intern (BS/MS)", sf))
    assert not match("Software Engineer Intern (Summer 2027)", sf, ("Software Engineer Intern (Winter 2027)", sf))
    assert not match("Software Engineer Intern 2026", sf, ("Software Engineer Intern 2027", sf))
    assert not match("Software Engineer Intern", sf, ("Software Engineer", sf))          # full-time job
    assert not match("Software Engineer Intern", sf, ("Software Engineer Intern, Infrastructure", sf))
    assert not match("Data Science Intern", sf, ("Software Engineering Intern", sf))


def test_two_close_titles_mean_nobody_is_picked():
    assert not match("Software Engineer Intern - Product", None,
                     ("Software Engineer Intern - Product Platform", None),
                     ("Software Engineer Intern - Product Growth", None))
    # an exact title wins over one that only shares most words
    assert match("Software Engineer Intern", None,
                 ("Software Engineer Intern (Summer 2027)", None),
                 ("Software Engineering Intern, Frontend (Summer 2027)", None)) == "Software Engineer Intern (Summer 2027)"


def test_same_job_in_several_cities_needs_the_city():
    jobs = [("Forward Deployed Software Engineer, Internship - US Government", "Washington, D.C."),
            ("Forward Deployed Software Engineer, Internship - US Government", "New York, NY"),
            ("Forward Deployed Software Engineer, Internship - US Government", "Palo Alto, CA")]
    title = "Forward Deployed Software Engineer, Internship - US Government"
    assert match(title, None, *jobs) is None
    assert match(title, "Seattle, WA", *jobs) is None
    found = jb.best_match(title, "New York, NY", [
        {"title": t, "url": "https://example.com/%d" % i, "location": loc} for i, (t, loc) in enumerate(jobs)])
    assert found["url"] == "https://example.com/1"


def test_never_a_posting_in_another_country():
    seoul = ("Forward Deployed Software Engineer, Internship - Commercial", "Seoul, South Korea")
    assert match(seoul[0], "New York, NY", seoul) is None
    assert match(seoul[0], "Toronto, ON", seoul) is None
    assert match(seoul[0], None, seoul) == seoul[0]                 # no location to compare
    assert match(seoul[0], "Remote", seoul) == seoul[0]             # unclear: not ruled out
    # one posting, another US city: postings often cover several cities
    assert match("Software Engineering Intern", "Seattle, WA",
                 ("Software Engineering Intern", "San Francisco, California")) == "Software Engineering Intern"


def test_found_on_greenhouse(boards):
    boards[gh("airbnb")] = {"jobs": [gh_job("Software Engineering Intern (BS/MS)", 8360478),
                                     gh_job("Software Engineering Intern (PhD)", 8360479)]}
    found = jb.find_posting("Airbnb", "Software Engineering Intern (BS/MS)", "San Francisco, CA")
    assert found == {"url": "https://careers.airbnb.com/positions/8360478?gh_jid=8360478",
                     "title": "Software Engineering Intern (BS/MS)", "location": "San Francisco, California",
                     "board": "Greenhouse"}


def test_found_on_lever_under_a_shorter_name(boards):
    boards[lever("palantir")] = [lever_job("Software Engineer, Internship - Connectivity Platform and Product",
                                           "130ee501-bd22-4d44-b651-39581ef97743", "New York, NY")]
    found = jb.find_posting("Palantir Technologies", "Software Engineer Intern - Connectivity Platform - Product",
                            "New York, NY")
    assert found["board"] == "Lever"
    assert found["url"] == "https://jobs.lever.co/palantir/130ee501-bd22-4d44-b651-39581ef97743/apply"


def test_found_on_ashby_listed_only(boards):
    job = {"title": "Software Engineer Intern (Summer 2027)", "location": "San Francisco, California",
           "jobUrl": "https://jobs.ashbyhq.com/notion/3fba", "applyUrl": "https://jobs.ashbyhq.com/notion/3fba/application"}
    boards[ashby("notion")] = {"jobs": [dict(job, isListed=False)]}
    assert jb.find_posting("Notion", "Software Engineer Intern", None) is None
    jb._cache.clear()
    boards[ashby("notion")] = {"jobs": [dict(job, isListed=True)]}
    assert jb.find_posting("Notion", "Software Engineer Intern", None)["url"].endswith("/application")


def test_someone_elses_greenhouse_board_is_ignored(boards):
    boards[gh("dell")] = {"jobs": [gh_job("Corporate Strategy Undergraduate Intern", 1, "Austin, TX",
                                          company="Dell Bakery Supply")]}
    assert jb.find_posting("Dell Technologies", "Corporate Strategy Undergraduate Intern", "Austin, TX") is None


def test_short_guessed_names_are_not_tried_on_lever_or_ashby(boards):
    jb.find_posting("Dell Technologies", "Corporate Strategy Undergraduate Intern", "Austin, TX")
    asked = boards["asked"]
    assert gh("dell") in asked
    assert lever("dell") not in asked and ashby("dell") not in asked
    assert lever("delltechnologies") in asked


def test_unreachable_boards_count_as_not_found(boards):
    for board in (gh, lever, ashby):
        boards[board("airbnb")] = jb.BoardError("timed out")
    assert jb.find_posting("Airbnb", "Software Engineering Intern", None) is None


def test_needs_a_company_and_a_title(boards):
    assert jb.find_posting("", "Software Engineering Intern") is None
    assert jb.find_posting("Airbnb", "") is None
    assert boards["asked"] == []
