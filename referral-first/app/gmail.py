"""Read-only access to Gmail over IMAP, signed in with a Google app password.

The mailbox is opened read-only and messages are fetched with BODY.PEEK, so
nothing is ever marked as read, moved, deleted or sent.
"""
import email
import email.policy
import email.utils
import hashlib
import imaplib
import re
import ssl
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import List, Optional

try:
    import certifi
    _CAFILE = certifi.where()
except ImportError:  # pragma: no cover
    _CAFILE = None

HOST, PORT = "imap.gmail.com", 993
# The tests point HOST/PORT at a fake server on this machine and switch this on.
# Real connections are always encrypted and certificate-checked.
_TEST_PLAINTEXT = False

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class GmailError(Exception):
    """A problem the user can act on. The message is shown in the app."""


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(d: datetime) -> str:
    return "%02d-%s-%04d" % (d.day, _MONTHS[d.month - 1], d.year)


def _open(address: str, password: str):
    try:
        if _TEST_PLAINTEXT and HOST == "127.0.0.1":
            conn = imaplib.IMAP4(HOST, PORT, timeout=30)
        else:
            ctx = ssl.create_default_context(cafile=_CAFILE)   # imaplib doesn't verify certificates by default
            conn = imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ctx, timeout=30)
    except (OSError, imaplib.IMAP4.error) as e:
        raise GmailError("Couldn't reach Gmail (%s). Check your internet connection and try again." % e)
    try:
        conn.login(address, password)
    except imaplib.IMAP4.error as e:
        _close(conn)
        text = str(e)
        if "pplication-specific password" in text:
            raise GmailError("Google needs an app password here, not your normal Google password. "
                             "Create one at myaccount.google.com/apppasswords and paste it in.")
        if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text:
            raise GmailError("Gmail didn't accept that address and app password. Check the address, and paste "
                             "the 16-letter app password (not your normal Google password).")
        raise GmailError("Gmail refused the sign-in: %s" % text)
    except OSError as e:
        _close(conn)
        raise GmailError("Lost the connection to Gmail (%s). Try again." % e)
    return conn


def _close(conn):
    try:
        conn.logout()
    except Exception:
        pass


def _select_all_mail(conn):
    """Open "All Mail" (so archived or tabbed emails are found too), falling back to the inbox."""
    name = '"INBOX"'
    typ, data = conn.list()
    if typ == "OK":
        for item in data or []:
            if not isinstance(item, bytes):
                continue
            m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$',
                         item.decode("ascii", "replace"))
            if m and re.search(r"\\All\b", m.group("flags")):
                name = m.group("name").strip()
                if not name.startswith('"'):
                    name = _quote(name)
                break
    typ, _ = conn.select(name, readonly=True)
    if typ != "OK":
        raise GmailError("Signed in, but couldn't open the mailbox.")


def _search(conn, *criteria: str) -> List[bytes]:
    typ, data = conn.uid("SEARCH", *criteria)
    if typ != "OK":
        raise GmailError("Gmail couldn't run the search.")
    return data[0].split() if data and data[0] else []


def _fetch(conn, uid: bytes, what: str) -> Optional[bytes]:
    typ, data = conn.uid("FETCH", uid.decode("ascii"), what)
    if typ != "OK":
        return None
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2:
            return part[1]
    return None


def _since(days: int) -> str:
    return _imap_date(datetime.now(timezone.utc) - timedelta(days=days))


def fetch_from_sender(address: str, password: str, sender: str, days: int) -> List[bytes]:
    """Raw emails from `sender` received in roughly the last `days` days, oldest first."""
    conn = _open(address, password)
    try:
        _select_all_mail(conn)
        uids = _search(conn, "FROM", _quote(sender), "SINCE", _since(days))
        return [raw for raw in (_fetch(conn, u, "(BODY.PEEK[])") for u in uids) if raw]
    except (imaplib.IMAP4.error, OSError) as e:
        raise GmailError("Gmail connection problem: %s" % e)
    finally:
        _close(conn)


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_sender(address: str, password: str, hints: List[str], days: int = 120) -> Optional[str]:
    """Sign in and work out which address the newsletter comes from.

    Looks at recent emails whose sender matches one of the hints (for example
    "Intern Insider"), keeps the ones Gmail authenticated, and returns the most
    common sender address. Returns None if there are no such emails.
    """
    conn = _open(address, password)
    try:
        _select_all_mail(conn)
        uids = []
        for hint in hints:
            for u in _search(conn, "FROM", _quote(hint), "SINCE", _since(days)):
                if u not in uids:
                    uids.append(u)
        keys = [_squash(h) for h in hints]
        counts = Counter()
        for u in sorted(uids, key=int)[-12:]:
            raw = _fetch(conn, u, "(BODY.PEEK[HEADER])")
            if not raw:
                continue
            info = read_message(raw)
            who = _squash(info["sender_name"]) + " " + _squash(info["sender"])
            if info["verified"] and any(k in who for k in keys):
                counts[info["sender"]] += 1
        return counts.most_common(1)[0][0] if counts else None
    except (imaplib.IMAP4.error, OSError) as e:
        raise GmailError("Gmail connection problem: %s" % e)
    finally:
        _close(conn)


def check_login(address: str, password: str) -> None:
    """Raise GmailError unless the address and app password can open the mailbox."""
    conn = _open(address, password)
    try:
        _select_all_mail(conn)
    except (imaplib.IMAP4.error, OSError) as e:
        raise GmailError("Gmail connection problem: %s" % e)
    finally:
        _close(conn)


# ---------- reading a message ----------

def _is_verified(msg, address: str) -> bool:
    """Did Gmail itself authenticate the From address (DMARC, or an aligned DKIM signature)?

    Gmail stamps its verdict in an Authentication-Results header when the email
    arrives. Receiving servers add headers at the top, so the first such header
    is Gmail's own and can't be forged by the sender.
    """
    results = msg.get_all("Authentication-Results") or []
    if not results or "@" not in address:
        return False
    top = " ".join(str(results[0]).split())
    if not top.lower().startswith("mx.google.com"):
        return False
    if re.search(r"\bdmarc=pass\b", top, re.I):
        return True
    domain = address.rsplit("@", 1)[1]
    for d in re.findall(r"\bdkim=pass\b[^;]*?header\.[id]=(?:[^@\s;]*@)?([\w.-]+)", top, re.I):
        d = d.lower().rstrip(".")
        if domain == d or domain.endswith("." + d) or d.endswith("." + domain):
            return True
    return False


def read_message(raw: bytes) -> dict:
    """The parts of an email we use: who sent it (and whether that's verified), when, and its HTML."""
    info = {
        "message_id": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "subject": "", "sender": "", "sender_name": "", "verified": False, "date": None, "html": None,
    }
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        name, addr = email.utils.parseaddr(str(msg.get("From", "")))
        info["sender"], info["sender_name"] = addr.strip().lower(), name.strip()
        info["subject"] = " ".join(str(msg.get("Subject", "")).split())
        mid = str(msg.get("Message-ID", "")).strip()
        if mid:
            info["message_id"] = mid
        info["verified"] = _is_verified(msg, info["sender"])
        try:
            when = email.utils.parsedate_to_datetime(str(msg.get("Date", "")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            info["date"] = when.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
        body = msg.get_body(preferencelist=("html",))
        if body is not None:
            info["html"] = body.get_content() or None
    except Exception:
        pass   # a malformed email is reported as unreadable rather than crashing the check
    return info
