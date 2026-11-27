"""Parse LinkedIn's official data export (Connections.csv).

Get it from LinkedIn: Settings & Privacy -> Data privacy -> Get a copy of your data
-> "Download larger data archive" (the option that includes connections; some
accounts also get a "Connections" checkbox) -> Request archive. LinkedIn emails
a download link, within 24 hours. The CSV starts with a few "Notes:" lines before
the real header row, which this parser skips.

The file has one employer per connection: the company on their current
position, as they wrote it. Connections with no employer can't be matched.
LinkedIn says that from December 2026 the export no longer includes
connections' current companies; such a file is refused rather than imported,
so the list you already have is kept.

The same archive has a Jobs folder whose "Job Applications" CSV lists the jobs
you applied to on LinkedIn; parse_applications_csv reads that one.
"""
import codecs
import csv
import io
import re
from datetime import datetime
from typing import List, Optional, Tuple

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


class NoEmployers(ValueError):
    """A connections file with no employers in it (LinkedIn's export from December 2026 on)."""


NO_EMPLOYERS = ("This file has %s but no employers, so it can't tell where you have contacts. LinkedIn "
                "stopped including connections' current companies in its export in December 2026. Nothing was "
                "changed: the app keeps the connections you imported before.")


def _rows(raw: bytes, header_has, missing: str):
    """csv.DictReader from the header row on (LinkedIn puts "Notes:" lines above it)."""
    lines = re.split(r"\r\n|\r|\n", _decode(raw))
    start = next((i for i, line in enumerate(lines) if header_has(line.lower())), None)
    if start is None:
        raise ValueError(missing)
    header = lines[start]
    delimiter = max((",", ";", "\t"), key=header.count)    # a spreadsheet app may have re-saved it
    return csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=delimiter)


def parse_connections_csv(raw: bytes) -> Tuple[List[dict], int]:
    """Returns (connections that have an employer, how many were skipped for having none).

    Raises NoEmployers when the file has connections but not one employer.
    """
    reader = _rows(raw, lambda line: "first name" in line and "last name" in line,
                   "Couldn't find the header row. Upload the Connections.csv file from LinkedIn's data export.")
    has_company = any(_HEADER_MAP.get((k or "").strip().lower()) == "company" for k in reader.fieldnames or [])
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
    if not rows and (no_employer or not has_company):
        raise NoEmployers(NO_EMPLOYERS % ("%d connection%s" % (no_employer, "" if no_employer == 1 else "s")
                                          if no_employer else "connections"))
    return rows, no_employer


# ---------- Job Applications.csv ----------

_APP_HEADERS = {
    "application date": "applied_at", "applied on": "applied_at", "date applied": "applied_at",
    "company name": "company", "company": "company",
    "job title": "title", "title": "title",
    "job url": "url", "url": "url",
}
_DATE_FORMATS = [
    "%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p", "%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%B %d, %Y",
]


def parse_date(text: str) -> Optional[str]:
    """LinkedIn's application date as an ISO local time ("2026-11-14T10:31:00"), or None."""
    text = re.sub(r"\s+(UTC|GMT)$", "", (text or "").strip(), flags=re.I)
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%H" not in fmt and "%I" not in fmt:
            d = d.replace(hour=12)          # a date alone: midday, so no time zone moves it a day
        return d.isoformat(timespec="seconds")
    return None


def parse_applications_csv(raw: bytes) -> List[dict]:
    """[{company, title, url, applied_at}] from LinkedIn's Job Applications CSV.

    Only those four columns are read; the file's contact details and your
    answers to application questions are ignored.
    """
    reader = _rows(raw, lambda line: "company name" in line and "job title" in line,
                   "Couldn't find the header row. Upload the Job Applications CSV from the Jobs folder of "
                   "LinkedIn's data export.")
    out = []
    for rec in reader:
        row = {}
        for k, v in rec.items():
            key = _APP_HEADERS.get((k or "").strip().lower())
            if key and not row.get(key):
                row[key] = (v or "").strip()
        if not row.get("company") or not row.get("title"):
            continue
        out.append({"company": row["company"], "title": row["title"], "url": row.get("url") or None,
                    "applied_at": parse_date(row.get("applied_at", ""))})
    return out
