"""Company-name normalization and matching.

LinkedIn company strings are messy ("Google", "Google LLC", "Alphabet Inc. / Google").
We normalize both sides and treat two names as the same company when one's
word sequence is a prefix of the other's ("Amazon" ~ "Amazon Web Services").
"""
import re

_SUFFIXES = {
    "inc", "incorporated", "llc", "l.l.c", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "sa", "bv", "pty", "lp", "llp", "the",
}


def normalize(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)          # drop parentheticals like "(AWS)"
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in _SUFFIXES]
    return " ".join(tokens)


def matches(query_norm: str, candidate_norm: str) -> bool:
    """True if the two normalized names refer to the same company."""
    if not query_norm or not candidate_norm:
        return False
    if query_norm == candidate_norm:
        return True
    q, c = query_norm.split(), candidate_norm.split()
    shorter, longer = (q, c) if len(q) <= len(c) else (c, q)
    return longer[: len(shorter)] == shorter
