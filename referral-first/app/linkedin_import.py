"""Parse LinkedIn's official data export (Connections.csv).

Get it from LinkedIn: Settings & Privacy -> Data privacy -> Get a copy of your data
-> "Download larger data archive" (the option that includes connections; some
accounts also get a "Connections" checkbox) -> Request archive. LinkedIn emails
a download link, within 24 hours. The CSV starts with a few "Notes:" lines before
the real header row, which this parser skips.

The file has one employer per connection: the company on their current
position, as they wrote it. Connections with no employer can't be matched.
"""
import codecs
import csv
import io
import re
from typing import List, Tuple

from .companies import normalize

_HEADER_MAP = {
    "first name": "first_name",
    "last name": "last_name",
    "url": "url",
    "company": "company",
    "company name": "company",
    "organization": "company",
    "position": "position",
    "title": "position",
    "connected on": "connected_on",
}


def _decode(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):   # re-saved from a spreadsheet app
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def parse_connections_csv(raw: bytes) -> Tuple[List[dict], int]:
    """Returns (connections that have an employer, how many were skipped for having none)."""
    lines = re.split(r"\r\n|\r|\n", _decode(raw))
    start = next(
        (i for i, line in enumerate(lines)
         if "first name" in line.lower() and ("company" in line.lower() or "organization" in line.lower())),
        None,
    )
    if start is None:
        raise ValueError("Couldn't find the header row. Upload the Connections.csv file from LinkedIn's data export.")

    header = lines[start]
    delimiter = max((",", ";", "\t"), key=header.count)    # a spreadsheet app may have re-saved it
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=delimiter)
    rows, no_employer = [], 0
    for rec in reader:
        row = {}
        for k, v in rec.items():
            if k is None:
                continue
            key = _HEADER_MAP.get(k.strip().lower())
            if key and not row.get(key):
                row[key] = (v or "").strip()
        if not any(row.values()):
            continue                      # blank line
        if not row.get("company"):
            no_employer += 1
            continue
        row["company_norm"] = normalize(row["company"])
        rows.append(row)
    return rows, no_employer
