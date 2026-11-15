"""Parse LinkedIn's official data export (Connections.csv).

Get it from LinkedIn: Settings & Privacy -> Data privacy -> Get a copy of your data
-> select "Connections" -> Request archive. LinkedIn emails a download link
(usually within ~10 minutes). The CSV starts with a few "Notes:" lines before
the real header row, which this parser skips.
"""
import csv
import io

from .companies import normalize

_HEADER_MAP = {
    "first name": "first_name",
    "last name": "last_name",
    "url": "url",
    "company": "company",
    "position": "position",
    "connected on": "connected_on",
}


def parse_connections_csv(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if "first name" in line.lower() and "company" in line.lower()),
        None,
    )
    if start is None:
        raise ValueError("Couldn't find the header row. Upload the Connections.csv file from LinkedIn's data export.")

    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows = []
    for rec in reader:
        row = {}
        for k, v in rec.items():
            if k is None:
                continue
            key = _HEADER_MAP.get(k.strip().lower())
            if key:
                row[key] = (v or "").strip()
        if not row.get("company"):
            continue  # can't match a connection with no company
        row["company_norm"] = normalize(row["company"])
        rows.append(row)
    return rows
