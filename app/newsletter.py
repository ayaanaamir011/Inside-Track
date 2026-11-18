"""Turn an internship newsletter email into listings and decide which to apply to.

Built against Intern Insider's format, where every listing is one line:

    <a href="...">Role title</a> @ Company – Location

under headings for the category ("Software Engineering", "Data & AI/ML",
"Finance & Accounting", ...) and the country ("🇺🇸 USA", "🇨🇦 Canada").
Listings are found by that text pattern rather than by specific HTML tags, so
the parser doesn't depend on how the email client or sender lays out the markup.
"""
import ipaddress
import re
import ssl
import urllib.request
from typing import List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PreformattedString

try:
    import certifi
    _CAFILE = certifi.where()
except ImportError:  # pragma: no cover
    _CAFILE = None

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCKS = {
    "p", "div", "li", "ul", "ol", "table", "tbody", "thead", "tr", "td", "th", "section",
    "article", "blockquote", "header", "footer", "hr", "center", "dl", "dt", "dd", "pre",
}
SKIP = {"script", "style", "head", "title", "noscript", "template", "svg"}
BOLD = {"b", "strong"}

LISTING_RE = re.compile(r"^(?P<title>.{2,200}?)\s+@\s+(?P<rest>.{1,200})$")
DASH_RE = re.compile(r"\s+[–—]\s+")
FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")

REGION_NAMES = {
    "usa": "USA", "us": "USA", "u.s.": "USA", "u.s.a.": "USA", "united states": "USA",
    "canada": "Canada", "uk": "UK", "united kingdom": "UK", "europe": "Europe",
    "international": "International", "global": "Global",
}
CA_PROVINCES = r"ON|BC|QC|AB|MB|SK|NS|NB|NL|PE|PEI|YT|NT|NU"
US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|"
    "NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
)
CA_NAMES = (
    "Canada|Ontario|Quebec|Québec|British Columbia|Alberta|Manitoba|Saskatchewan|Nova Scotia|"
    "New Brunswick|Newfoundland"
)

DEFAULT_KEYWORDS = [
    "software", "developer", "programmer", "full stack", "fullstack", "frontend", "front end",
    "backend", "back end", "devops", "cloud engineer", "security engineer", "cyber",
    "computer science", "data", "AI", "ML", "machine learning", "artificial intelligence",
    "deep learning", "NLP", "LLM", "computer vision", "quant", "analytics",
    "sales analy", "financ analy",
]
DEFAULT_EXCLUDE = ["data entry"]


# ---------- parsing ----------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("​", "").replace("﻿", "")).strip()


def _strip_decor(s: str) -> str:
    """Drop leading emoji/bullets and trailing colons/dashes: '🔥 Our jobs:' -> 'Our jobs'."""
    s = re.sub(r"^[^\w(\[]+", "", s)
    return re.sub(r"[\s:–—-]+$", "", s)


def _render(soup) -> List[dict]:
    """Flatten the HTML into visual lines: {text, links, heading, bold}."""
    lines, buf, links = [], [], []
    state = {"bold_only": True}

    def flush(heading=False):
        text = _norm("".join(buf))
        if text:
            lines.append({"text": text, "links": list(links), "heading": heading,
                          "bold": state["bold_only"]})
        del buf[:]
        del links[:]
        state["bold_only"] = True

    def walk(node, href, bold):
        for ch in node.children:
            if isinstance(ch, PreformattedString):      # comments, doctype, CDATA
                continue
            if isinstance(ch, NavigableString):
                s = str(ch)
                buf.append(s)
                if s.strip():
                    if not bold:
                        state["bold_only"] = False
                    if href and href not in links:
                        links.append(href)
                continue
            name = ch.name
            if name in SKIP:
                continue
            if name == "br":
                flush()
            elif name in HEADINGS:
                flush()
                walk(ch, href, True)
                flush(heading=True)
            elif name in BLOCKS:
                flush()
                walk(ch, href, bold)
                flush()
            else:
                link = ch.get("href") if name == "a" else None
                walk(ch, link or href, bold or name in BOLD)

    walk(soup, None, False)
    flush()
    return lines


def _region_of_heading(text: str) -> Optional[str]:
    name = _strip_decor(text)
    known = REGION_NAMES.get(name.lower())
    if known:
        return known
    if FLAG_RE.search(text) and 0 < len(name) <= 30:
        return name
    return None


def infer_region(location: Optional[str]) -> Optional[str]:
    """Best guess at the country from a location like 'Toronto, ON' or 'Austin, TX | Remote'."""
    if not location:
        return None
    if re.search(r"\b(%s)\b" % CA_PROVINCES, location):
        return "Canada"
    if re.search(r"\b(%s)\b" % US_STATES, location):
        return "USA"
    if re.search(r"\b(%s)\b" % CA_NAMES, location, re.I):
        return "Canada"
    if re.search(r"\b(USA|United States)\b|\bU\.S\.", location, re.I):
        return "USA"
    return None


def _parse_line(text: str) -> Optional[Tuple[str, str, Optional[str]]]:
    m = LISTING_RE.match(text)
    if not m:
        return None
    title = _strip_decor(m.group("title"))
    rest = m.group("rest").strip()
    parts = DASH_RE.split(rest, maxsplit=1)
    if len(parts) == 1:
        parts = re.split(r"\s+-\s+", rest, maxsplit=1)
    company = parts[0].strip(" -–—|,")
    location = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    if not title or not company:
        return None
    return title, company, location


def parse_listings(html: str) -> List[dict]:
    """Every 'Title @ Company – Location' line that carries a link, in order."""
    soup = BeautifulSoup(html, "html.parser")
    lines = _render(soup)
    has_real_headings = any(l["heading"] for l in lines)
    section, region = None, None
    out, seen = [], set()
    for line in lines:
        text = line["text"]
        url = next((u for u in line["links"] if is_public_url(u)), None)
        parsed = _parse_line(text) if url and len(text) <= 300 else None
        if parsed:
            title, company, location = parsed
            if url in seen:
                continue
            seen.add(url)
            out.append({
                "title": title, "company": company, "location": location, "url": url,
                "section": section, "region": region or infer_region(location),
            })
            continue
        # Headings: real <h1>-<h6>, or (if the email has none) short all-bold lines.
        is_heading = line["heading"] or (
            not has_real_headings and line["bold"] and not line["links"] and len(text) <= 60
        )
        if is_heading:
            r = _region_of_heading(text)
            if r:
                region = r
            else:
                section, region = _strip_decor(text) or None, None
    return out


# ---------- deciding what to apply to ----------

def _tokens(s: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def keyword_hit(keyword: str, tokens: List[str]) -> bool:
    """All words of the keyword appear. Words of 4+ letters also match longer
    words that start with them ('analy' ~ analyst, analytics, analysis)."""
    words = _tokens(keyword)
    return bool(words) and all(
        any(t == w if len(w) <= 3 else t.startswith(w) for t in tokens) for w in words
    )


def classify(listing: dict, filters: dict) -> Tuple[bool, str]:
    """(apply?, reason) for one listing under the user's filters."""
    region, regions = listing.get("region"), filters.get("regions") or []
    if region and regions and region not in regions:
        return False, "%s isn't one of your regions" % region
    title_tokens = _tokens(listing["title"])
    section = listing.get("section") or ""
    all_tokens = _tokens(section) + title_tokens
    for kw in filters.get("exclude") or []:
        if keyword_hit(kw, all_tokens):
            return False, 'Excluded by "%s"' % kw
    for kw in filters.get("keywords") or []:
        if keyword_hit(kw, title_tokens):
            return True, 'Title matches "%s"' % kw
    for kw in filters.get("keywords") or []:
        if keyword_hit(kw, all_tokens):
            return True, 'In "%s", which matches "%s"' % (section, kw)
    return False, "Doesn't match your keywords"


# ---------- links ----------

def is_public_url(url: Optional[str]) -> bool:
    """http(s) link to a public host (not localhost or a private address)."""
    try:
        p = urlsplit(url or "")
        host = (p.hostname or "").lower()
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or "." not in host:
        return False
    if host.endswith((".local", ".localhost", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def clean_url(url: str) -> str:
    """Drop newsletter tracking parameters (utm_*, _bhlid) and the fragment."""
    p = urlsplit(url)
    kept = [q for q in p.query.split("&")
            if q and not q.lower().startswith(("utm_", "_bhlid=", "bhlid="))]
    return urlunsplit((p.scheme, p.netloc, p.path, "&".join(kept), ""))


class _RedirectLog(urllib.request.HTTPRedirectHandler):
    """Remembers where each redirect pointed, and refuses to follow one off the public web."""

    def __init__(self):
        self.targets, self.refused = [], False

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_url(newurl):
            self.refused = True
            return None
        self.targets.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def resolve_url(url: str, timeout: float = 12) -> str:
    """Follow a newsletter's click-tracking redirect to the real posting URL.

    Job sites often refuse or stall on anything that isn't a browser, so the last
    redirect target seen counts even if that site then errors. If nothing can be
    learned, the link comes back unchanged (the bot's browser can still open it).
    A link that redirects to a private address comes back as "" so it is skipped.
    """
    log = _RedirectLog()
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "text/html,*/*",
    })
    try:
        ctx = ssl.create_default_context(cafile=_CAFILE)
        opener = urllib.request.build_opener(log, urllib.request.HTTPSHandler(context=ctx))
        with opener.open(req, timeout=timeout) as resp:
            final = resp.geturl()
    except Exception:
        final = log.targets[-1] if log.targets else url
    if log.refused:
        return ""
    return clean_url(final) if final != url else url
