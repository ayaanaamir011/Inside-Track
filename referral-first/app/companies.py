"""Company-name matching: is a posting's company the same as a connection's employer?

Missing a contact means applying without asking for a referral, which can't be
undone, so matching leans toward finding one. The employer names that matched
are always shown next to the result, so a wrong match is easy to spot and dismiss.

Two names count as the same company when, after normalizing:
  - they are equal, or one starts with the other     "Amazon" ~ "Amazon Web Services"
  - they differ only in spacing                       "JPMorganChase" ~ "JPMorgan Chase & Co."
  - one appears inside the other as whole words       "Disney" ~ "The Walt Disney Company"
  - they are a typo apart                             "Nvdia" ~ "NVIDIA"
  - they are in the same group of known equivalents   "Meta" ~ "Facebook", "Chase" ~ "J.P. Morgan"
"""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable, List, Optional, Sequence, Tuple

# Legal forms and articles carry no identity.
_LEGAL = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "gmbh", "ag", "sa", "bv", "nv", "pty", "lp", "llp", "the",
}
# Things that show up in parentheses or after a "|" but aren't a company name.
_NOT_A_NAME = {"remote", "hybrid", "usa", "us", "canada", "uk", "intern", "internship", "contract", "contractor"}
# Words too common to identify a company when they are all that two names share.
_GENERIC = {
    "of", "and", "for", "at", "in", "de", "la", "a", "an",
    "america", "american", "americas", "usa", "us", "canada", "canadian", "international", "global",
    "worldwide", "national", "north", "south", "east", "west", "central", "united", "general", "first",
    "new", "city", "state", "states", "federal", "royal", "york", "california", "texas",
    "group", "holdings", "partners", "capital", "ventures", "bank", "financial", "finance", "insurance",
    "investments", "securities", "consulting", "solutions", "services", "systems", "technologies",
    "technology", "tech", "software", "labs", "digital", "data", "cloud", "network", "networks",
    "communications", "media", "entertainment", "studios", "games", "energy", "health", "healthcare",
    "medical", "pharma", "pharmaceuticals", "sciences", "research", "institute", "university", "college",
    "school", "hospital", "foundation", "association", "department", "government", "agency", "office",
    "center", "centre", "enterprises", "industries", "airlines", "foods", "motors", "one",
}

# Names that mean the same employer for referral purposes. Add your own on the Connections tab.
KNOWN_GROUP_NAMES = [
    ("Google", "Alphabet", "YouTube", "Google DeepMind", "DeepMind"),
    ("Meta", "Meta Platforms", "Facebook", "Instagram", "WhatsApp", "Reality Labs"),
    ("Amazon", "Amazon.com", "AWS", "Amazon Web Services"),
    ("JPMorganChase", "JPMorgan Chase", "JPMorgan", "JP Morgan", "Chase"),
    ("Bank of America", "BofA", "BofA Securities", "Merrill Lynch", "Merrill"),
    ("Citi", "Citigroup", "Citibank"),
    ("EY", "Ernst & Young"),
    ("PwC", "PricewaterhouseCoopers"),
    ("General Motors", "GM"),
    ("General Electric", "GE"),
    ("American Express", "Amex"),
    ("Johnson & Johnson", "J&J", "JNJ"),
    ("Procter & Gamble", "P&G"),
    ("Boston Consulting Group", "BCG"),
    ("TikTok", "ByteDance"),
    ("RTX", "Raytheon", "Raytheon Technologies"),
    ("IBM", "International Business Machines"),
    ("AT&T", "ATT"),
    ("BNY", "BNY Mellon", "Bank of New York Mellon"),
    ("RBC", "Royal Bank of Canada"),
    ("TD", "TD Bank", "Toronto-Dominion"),
    ("BMO", "Bank of Montreal"),
    ("CIBC", "Canadian Imperial Bank of Commerce"),
    ("Scotiabank", "Bank of Nova Scotia"),
    ("NASA Jet Propulsion Laboratory", "Jet Propulsion Laboratory", "JPL", "NASA JPL"),
    ("UnitedHealth Group", "UnitedHealthcare", "Optum"),
    ("Snap", "Snapchat"),
    ("Block", "Square", "Cash App"),
    ("PayPal", "Venmo", "Braintree"),
    ("Salesforce", "Slack", "Tableau", "MuleSoft"),
    ("Cisco", "Cisco Meraki", "Meraki", "Webex"),
    ("UPS", "United Parcel Service"),
    ("FedEx", "Federal Express"),
    ("The New York Times", "NYT"),
]


def fold(s: str) -> str:
    """Lowercase and strip accents: 'Nestlé' -> 'nestle'."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


@lru_cache(maxsize=50000)
def normalize(name: Optional[str]) -> str:
    if not name:
        return ""
    s = fold(name).replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)          # parentheticals are handled as aliases
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in _LEGAL]
    while tokens and tokens[-1] == "and":   # "Bain & Company" -> "bain"
        tokens.pop()
    while tokens and tokens[0] == "and":
        tokens.pop(0)
    return " ".join(tokens)


@lru_cache(maxsize=50000)
def aliases(name: Optional[str]) -> Tuple[str, ...]:
    """Every normalized name a company string may go by.

    "EQ Bank | Equitable Bank"   -> ("eq bank equitable bank", "eq bank", "equitable bank")
    "Amazon Web Services (AWS)"  -> ("amazon web services", "aws")
    """
    if not name:
        return ()
    candidates = [name] + re.split(r"\s+[|/]\s+", name) + re.findall(r"\(([^)]+)\)", name)
    out = []
    for c in candidates:
        n = normalize(c)
        if len(n) >= 2 and n not in _NOT_A_NAME and n not in out:
            out.append(n)
    return tuple(out)


def _joined_prefixes(tokens: Sequence[str]) -> set:
    return {"".join(tokens[:k]) for k in range(1, len(tokens) + 1)}


def _distinctive(tokens: Sequence[str]) -> bool:
    """Enough to identify a company when found in the middle or at the end of a longer name."""
    if len(tokens) >= 2:
        return any(t not in _GENERIC and len(t) >= 3 for t in tokens)
    return len(tokens[0]) >= 5 and tokens[0] not in _GENERIC


@lru_cache(maxsize=500000)
def same(a: str, b: str) -> bool:
    """True if two normalized names refer to the same company."""
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if long[: len(short)] == short:                         # "amazon" ~ "amazon web services"
        return True
    sa, sb = "".join(ta), "".join(tb)
    if sa in _joined_prefixes(tb) or sb in _joined_prefixes(ta):    # "jpmorganchase" ~ "jpmorgan chase"
        return True
    n = len(short)
    if n < len(long) and _distinctive(short) and any(         # "disney" ~ "walt disney"
            long[i:i + n] == short for i in range(1, len(long) - n + 1)):
        return True
    if min(len(sa), len(sb)) >= 5 and abs(len(sa) - len(sb)) <= 2 and sa[0] == sb[0]:    # a typo apart
        m = SequenceMatcher(None, sa, sb)
        if m.real_quick_ratio() >= 0.9 and m.quick_ratio() >= 0.9 and m.ratio() >= 0.9:
            return True
    return False


def matches(query_norm: str, candidate_norm: str) -> bool:
    """Kept for callers that already hold normalized names."""
    return same(query_norm, candidate_norm)


def parse_groups(text: str) -> List[List[str]]:
    """Turn 'Meta = Facebook = Instagram' lines into groups of names (two or more per line)."""
    groups = []
    for line in (text or "").splitlines():
        names = [" ".join(part.split()) for part in line.split("=")]
        names = [n for n in names if n]
        if len(names) >= 2:
            groups.append(names)
    return groups


def normalize_groups(groups: Iterable[Iterable[str]]) -> List[Tuple[str, ...]]:
    out = []
    for g in groups:
        names = []
        for raw in g:
            for n in aliases(raw):
                if n not in names:
                    names.append(n)
        if len(names) >= 2:
            out.append(tuple(names))
    return out


KNOWN_GROUPS = normalize_groups(KNOWN_GROUP_NAMES)


def expand(names: Sequence[str], groups: Sequence[Tuple[str, ...]]) -> Tuple[str, ...]:
    """Add every equivalent name from any group one of the names belongs to."""
    out = list(names)
    for g in groups:
        if any(same(n, m) for n in names for m in g):
            out += [m for m in g if m not in out]
    return tuple(out)


def find(company: Optional[str], employers: Iterable[Tuple[str, int]],
         extra_groups: Sequence[Tuple[str, ...]] = ()) -> List[Tuple[str, int]]:
    """Which employers (as written in the connections export, with head counts) are this company."""
    wanted = expand(aliases(company), list(KNOWN_GROUPS) + list(extra_groups))
    if not wanted:
        return []
    hits = [(employer, n) for employer, n in employers
            if any(same(w, e) for e in aliases(employer) for w in wanted)]
    return sorted(hits, key=lambda h: (-h[1], h[0].lower()))
