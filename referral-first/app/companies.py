"""Company-name normalization and matching.

Company strings are messy ("Google", "Google LLC", "JPMorganChase",
"EQ Bank | Equitable Bank"). We normalize both sides and treat two names as
the same company when one's word sequence is a prefix of the other's
("Amazon" ~ "Amazon Web Services"), ignoring where the spaces fall
("JPMorganChase" ~ "JPMorgan Chase & Co.").
"""
import re
from typing import List, Optional

_SUFFIXES = {
    "inc", "incorporated", "llc", "l.l.c", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "sa", "bv", "pty", "lp", "llp", "the",
}
# Things that show up in parentheses after a company name but aren't a name.
_NOT_A_NAME = {"remote", "hybrid", "usa", "us", "canada", "uk"}


def normalize(name: Optional[str]) -> str:
    if not name:
        return ""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)          # drop parentheticals like "(AWS)"
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in _SUFFIXES]
    return " ".join(tokens)


def aliases(name: Optional[str]) -> List[str]:
    """Every normalized name a company string may go by.

    "EQ Bank | Equitable Bank" -> ["eq bank", "equitable bank"]
    "AXIS (AXIS Capital)"      -> ["axis", "axis capital"]
    """
    if not name:
        return []
    candidates = [name] + re.split(r"\s+[|/]\s+", name) + re.findall(r"\(([^)]+)\)", name)
    out = []
    for c in candidates:
        n = normalize(c)
        if len(n) >= 2 and n not in _NOT_A_NAME and n not in out:
            out.append(n)
    return out


def _joined_prefixes(tokens):
    return {"".join(tokens[:k]) for k in range(1, len(tokens) + 1)}


def matches(query_norm: str, candidate_norm: str) -> bool:
    """True if the two normalized names refer to the same company."""
    if not query_norm or not candidate_norm:
        return False
    if query_norm == candidate_norm:
        return True
    q, c = query_norm.split(), candidate_norm.split()
    shorter, longer = (q, c) if len(q) <= len(c) else (c, q)
    if longer[: len(shorter)] == shorter:
        return True
    # Same words, different spacing: "jpmorganchase" ~ "jpmorgan chase and", "pay pal" ~ "paypal"
    return "".join(q) in _joined_prefixes(c) or "".join(c) in _joined_prefixes(q)
