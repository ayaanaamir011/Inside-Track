"""Find an internship on the company's own job board: Greenhouse, Lever or Ashby.

Used when a listing's link is one the bot can't use (Handshake makes you sign
in) or there is no link at all. These three systems publish every open job as
public JSON, so the lookup needs no account and never opens the listing site.

It is deliberately strict. A posting is only used when its title matches
closely and nothing else on the board matches nearly as well, because applying
to the wrong job is worse than handing the listing back to you.
"""
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from . import companies, newsletter

try:
    import certifi
    _CAFILE = certifi.where()
except ImportError:  # pragma: no cover
    _CAFILE = None

TIMEOUT = 15
CACHE_SECONDS = 3600
USER_AGENT = "Mozilla/5.0 (compatible; internship-tracker)"

THRESHOLD = 0.8   # share of title words two titles must have in common
MARGIN = 0.15     # how far ahead of any other title the best one must be

# Words dropped from a company name to guess its board's address ("Palantir Technologies" -> "palantir").
_NAME_SUFFIXES = {"technologies", "technology", "tech", "labs", "software", "systems", "group", "holdings",
                  "global", "usa", "us"}

_SEASONS = {"summer": "summer", "fall": "fall", "autumn": "fall", "winter": "winter", "spring": "spring"}
_DEGREES = {"bs": "bs", "bachelor": "bs", "bachelors": "bs", "undergrad": "bs", "undergraduate": "bs",
            "ms": "ms", "master": "ms", "masters": "ms", "phd": "phd", "doctoral": "phd", "mba": "mba"}
_STEMS = {"internship": "intern", "internships": "intern", "interns": "intern",
          "engineering": "engineer", "engineers": "engineer"}
_STOP = {"and", "of", "the", "a", "an", "for", "to", "in", "at", "with", "on"}

_cache: Dict[Tuple[str, str], Tuple[float, Optional[list]]] = {}


class BoardError(Exception):
    """The board couldn't be reached or answered with something unreadable."""


# ---------- fetching ----------

def _get_json(url: str):
    """Parsed JSON, or None when there is no board at that address (404)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    ctx = ssl.create_default_context(cafile=_CAFILE)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise BoardError("%s answered %s" % (url, e.code))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BoardError("%s: %s" % (url, e))


def _greenhouse(slug: str) -> Optional[list]:
    data = _get_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug)
    if not isinstance(data, dict):
        return None
    return [{"title": j.get("title") or "", "url": j.get("absolute_url"),
             "location": (j.get("location") or {}).get("name"), "company": j.get("company_name")}
            for j in data.get("jobs") or []]


def _lever(slug: str) -> Optional[list]:
    data = _get_json("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    if not isinstance(data, list):
        return None
    return [{"title": p.get("text") or "", "url": p.get("applyUrl") or p.get("hostedUrl"),
             "location": (p.get("categories") or {}).get("location"), "company": None}
            for p in data]


def _ashby(slug: str) -> Optional[list]:
    data = _get_json("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    if not isinstance(data, dict):
        return None
    return [{"title": j.get("title") or "", "url": j.get("applyUrl") or j.get("jobUrl"),
             "location": j.get("location"), "company": None}
            for j in data.get("jobs") or [] if j.get("isListed", True)]


BOARDS: List[Tuple[str, Callable[[str], Optional[list]]]] = [
    ("Greenhouse", _greenhouse), ("Lever", _lever), ("Ashby", _ashby),
]


def _postings(board: str, fetch, slug: str) -> Optional[list]:
    key = (board, slug)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    postings = fetch(slug)
    _cache[key] = (time.time(), postings)
    return postings


# ---------- which board ----------

def slugs(company: str) -> List[Tuple[str, bool]]:
    """Board addresses to try for a company, as (slug, from the full name?)."""
    out: List[Tuple[str, bool]] = []
    for name in companies.aliases(company):
        words = name.split()
        short = list(words)
        while len(short) > 1 and short[-1] in _NAME_SUFFIXES:
            short.pop()
        for ws, full in ((words, True), (short, short == words)):
            for slug in ("".join(ws), "-".join(ws)):
                if len(slug) >= 2 and slug not in [s for s, _ in out]:
                    out.append((slug, full))
    return out[:6]


# ---------- which posting ----------

class Title(NamedTuple):
    words: frozenset
    seasons: frozenset
    years: frozenset
    degrees: frozenset


def parse_title(title: str) -> Title:
    tokens = re.findall(r"[a-z0-9]+", companies.fold(title or "").replace("&", " and "))
    words, seasons, years, degrees = set(), set(), set(), set()
    for t in tokens:
        if t in _SEASONS:
            seasons.add(_SEASONS[t])
        elif re.fullmatch(r"20\d\d", t):
            years.add(t)
        elif t in _DEGREES:
            degrees.add(_DEGREES[t])
        elif t not in _STOP:
            words.add(_STEMS.get(t, t))
    return Title(frozenset(words), frozenset(seasons), frozenset(years), frozenset(degrees))


def _compatible(a: Title, b: Title) -> bool:
    """Neither title rules the other out: Summer vs Winter, 2026 vs 2027, PhD vs BS, intern vs full time."""
    for x, y in ((a.seasons, b.seasons), (a.years, b.years), (a.degrees, b.degrees)):
        if x and y and not x & y:
            return False
    return ("intern" in a.words) == ("intern" in b.words)


def similarity(a: Title, b: Title) -> float:
    if not a.words or not b.words:
        return 0.0
    return len(a.words & b.words) / len(a.words | b.words)


def same_title(a: str, b: str) -> bool:
    """Two job titles that name the same job (see best_match for the rules)."""
    ta, tb = parse_title(a), parse_title(b)
    return _compatible(ta, tb) and similarity(ta, tb) >= THRESHOLD


def _city(location: Optional[str]) -> str:
    first = (location or "").split(",")[0]
    return " ".join(re.findall(r"[a-z0-9]+", companies.fold(first)))


_US_STATE_NAMES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
    "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
    "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
    "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
    "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|"
    "District of Columbia"
)


def _country(location: Optional[str]) -> Optional[str]:
    """'usa', 'canada', another country's name as written ('south korea'), or None if unclear."""
    region = newsletter.infer_region(location)
    if region:
        return region.lower()
    if re.search(r"\b(%s)\b" % _US_STATE_NAMES, location or "", re.I):
        return "usa"
    parts = [p.strip() for p in (location or "").split(",")]
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z .'-]{3,}", parts[-1]) and "remote" not in parts[-1].lower():
        return companies.fold(parts[-1])
    return None


def _same_country(a: Optional[str], b: Optional[str]) -> bool:
    ca, cb = _country(a), _country(b)
    return not (ca and cb and ca != cb)


def best_match(title: str, location: Optional[str], postings: List[dict]) -> Optional[dict]:
    """The one posting that is clearly this job, or None."""
    want = parse_title(title)
    scored = []
    for p in postings:
        have = parse_title(p["title"])
        if p.get("url") and _compatible(want, have):
            s = similarity(want, have)
            if s >= THRESHOLD:
                scored.append((s, have.words, p))
    if not scored:
        return None
    top = max(s for s, _, _ in scored)
    top_words = {w for s, w, _ in scored if s == top}
    if len(top_words) > 1:
        return None                                  # two different titles fit equally well
    words = top_words.pop()
    if any(w != words and top - s < MARGIN for s, w, _ in scored):
        return None                                  # another title is nearly as close
    same = [p for s, w, p in scored if w == words and _same_country(location, p.get("location"))]
    if not same:
        return None                                  # only posted in another country
    if len(same) > 1 and location:                   # the same job posted once per city
        city = _city(location)
        same = [p for p in same if city and _city(p.get("location")) == city] or same
    if len({p["url"] for p in same}) > 1:
        return None
    return same[0]


def find_posting(company: str, title: str, location: Optional[str] = None) -> Optional[dict]:
    """This internship on the company's own Greenhouse, Lever or Ashby board.

    Returns {"url", "title", "location", "board"}, or None if it isn't there or
    can't be told apart from other jobs. Boards that can't be reached count as
    not having it.
    """
    if not (company or "").strip() or not (title or "").strip():
        return None
    for slug, full_name in slugs(company):
        for board, fetch in BOARDS:
            # Lever and Ashby don't say whose board it is, so a shortened name must still be distinctive.
            if board != "Greenhouse" and not full_name and len(slug.replace("-", "")) < 5:
                continue
            try:
                postings = _postings(board, fetch, slug)
            except BoardError:
                continue
            if not postings:
                continue
            if board == "Greenhouse":   # Greenhouse names the company: skip someone else's board
                postings = [p for p in postings if not p["company"] or
                            companies.same(companies.normalize(p["company"]), companies.normalize(company))]
            found = best_match(title, location, postings)
            if found:
                return {"url": found["url"], "title": found["title"], "location": found.get("location"),
                        "board": board}
    return None
