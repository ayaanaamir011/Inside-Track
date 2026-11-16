import importlib
import re
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SENDER = "hi@interninsider.me"


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    """Tests never touch the real OS keychain."""
    from app import credstore
    monkeypatch.setattr(credstore, "_keyring", lambda: None)


def _new_client(tmp_path, monkeypatch):
    monkeypatch.setenv("RF_DATA_DIR", str(tmp_path))
    from app import db, inbox, main
    importlib.reload(db)
    importlib.reload(main)
    inbox._lock = None
    calls = []

    async def fake_apply(iid):
        calls.append(iid)

    monkeypatch.setattr(main.applier, "run_application", fake_apply)
    from fastapi.testclient import TestClient
    c = TestClient(main.app, base_url="http://127.0.0.1:8000")
    c.apply_calls = calls
    return c


@pytest.fixture()
def fresh_client(tmp_path, monkeypatch):
    """The app as first opened: no LinkedIn connections imported yet."""
    return _new_client(tmp_path, monkeypatch)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The app with the sample connections imported (contacts at Stripe, AWS and Google)."""
    c = _new_client(tmp_path, monkeypatch)
    with open(FIXTURES / "Connections.csv", "rb") as f:
        assert c.post("/api/connections/import",
                      files={"file": ("Connections.csv", f, "text/csv")}).status_code == 200
    return c


def make_email(html=None, sender=SENDER, name="Intern Insider", auth="pass", days_ago=1,
               subject="[JUST RELEASED] 55+ New Internships in the USA and Canada", message_id=None):
    """A raw email the way Gmail stores it, with Gmail's authentication verdict on top."""
    if html is None:
        html = (FIXTURES / "newsletter_email.html").read_text(encoding="utf-8")
    msg = EmailMessage()
    msg["From"] = "%s <%s>" % (name, sender)
    msg["To"] = "me@gmail.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))
    msg["Message-ID"] = message_id or "<%s-%s@mail.beehiiv.com>" % (abs(hash((html, sender, days_ago, subject))), auth)
    msg.set_content("Open this email in a browser to see this week's internships.")
    msg.add_alternative(html, subtype="html")
    domain = sender.rsplit("@", 1)[1]
    verdicts = {
        # Same shape as the header Gmail puts on the real newsletter.
        "pass": "mx.google.com;\r\n dkim=pass header.i=@%s header.s=1ef header.b=AbCdEf12;\r\n"
                " dkim=pass header.i=@sendgrid.info header.s=smtpapi header.b=GhIjKl34;\r\n"
                " spf=pass (google.com: domain of bounces+1-d161-me=gmail.com@em1.%s designates 1.2.3.4 as"
                " permitted sender) smtp.mailfrom=\"bounces+1-d161-me=gmail.com@em1.%s\";\r\n"
                " dmarc=pass (p=QUARANTINE sp=QUARANTINE dis=NONE) header.from=%s"
                % (domain, domain, domain, domain),
        "fail": "mx.google.com;\r\n dkim=fail header.i=@%s header.s=k1;\r\n spf=softfail smtp.mailfrom=x@evil.example;\r\n"
                " dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=%s" % (domain, domain),
    }
    head = b""
    if auth in verdicts:
        head = ("Delivered-To: me@gmail.com\r\nAuthentication-Results: %s\r\n" % verdicts[auth]).encode()
    return head + msg.as_bytes()


class FakeGmail:
    """Just enough of an IMAP server for the app to sign in, search by sender and fetch."""

    def __init__(self, password, messages):
        self.password, self.messages, self.commands = password, list(messages), []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def send(self, text):
                self.wfile.write(text.encode("utf-8") + b"\r\n")

            def handle(self):
                self.send("* OK Gimap ready")
                authed = False
                while True:
                    line = self.rfile.readline().decode("utf-8", "replace").strip()
                    if not line:
                        return
                    tag, _, rest = line.partition(" ")
                    cmd = rest.upper()
                    outer.commands.append(rest if not cmd.startswith("LOGIN") else "LOGIN ...")
                    if cmd.startswith("CAPABILITY"):
                        self.send("* CAPABILITY IMAP4rev1")
                    elif cmd.startswith("LOGIN"):
                        if rest.split(" ", 2)[2].strip('"') != outer.password:
                            self.send(tag + " NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
                            continue
                        authed = True
                    elif cmd.startswith("LOGOUT"):
                        self.send("* BYE logging out")
                        self.send(tag + " OK LOGOUT completed")
                        return
                    elif not authed:
                        self.send(tag + " BAD not signed in")
                        continue
                    elif cmd.startswith("LIST"):
                        self.send('* LIST (\\HasNoChildren) "/" "INBOX"')
                        self.send('* LIST (\\All \\HasNoChildren) "/" "[Gmail]/All Mail"')
                    elif cmd.startswith("EXAMINE"):
                        self.send("* %d EXISTS" % len(outer.messages))
                    elif cmd.startswith("SELECT"):
                        self.send(tag + " NO this test server is read-only")
                        continue
                    elif cmd.startswith("UID SEARCH"):
                        wanted = rest.split('FROM "', 1)[1].split('"', 1)[0].lower()
                        hits = [str(i + 1) for i, raw in enumerate(outer.messages)
                                if wanted in re.search(rb"^From:(.*)$", raw, re.M).group(1).decode().lower()]
                        self.send("* SEARCH " + " ".join(hits))
                    elif cmd.startswith("UID FETCH"):
                        uid = int(rest.split()[2])
                        raw = outer.messages[uid - 1]
                        part = "BODY[]"
                        if "HEADER" in cmd:
                            raw, part = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0] + b"\r\n\r\n", "BODY[HEADER]"
                        self.wfile.write(("* %d FETCH (UID %d %s {%d}\r\n" % (uid, uid, part, len(raw))).encode())
                        self.wfile.write(raw + b")\r\n")
                    else:
                        self.send(tag + " BAD unknown command")
                        continue
                    self.send(tag + " OK done")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake_gmail(monkeypatch):
    """Factory: fake_gmail([raw emails]) starts a fake Gmail and points the app at it."""
    servers = []

    def start(messages, password="abcdefghijklmnop"):
        from app import gmail
        srv = FakeGmail(password, messages)
        servers.append(srv)
        monkeypatch.setattr(gmail, "HOST", "127.0.0.1")
        monkeypatch.setattr(gmail, "PORT", srv.port)
        monkeypatch.setattr(gmail, "_TEST_PLAINTEXT", True)
        return srv

    yield start
    for srv in servers:
        srv.close()
