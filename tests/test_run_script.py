"""run.sh --start-at-login / --stop-starting-at-login / --background, with the system tools faked.

Each test runs a copy of run.sh in a temporary folder with a fake .venv/bin/python
(it records how it was called) and fake uname, launchctl, systemctl, curl and
open on the PATH, so nothing is installed on the machine running the tests.
"""
import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LABEL = "local.internship-app"


def _exe(path: Path, body: str):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def sandbox(tmp_path):
    app = tmp_path / "My Apps" / "inside-track"           # a space in the path, like ~/My Apps
    (app / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "run.sh", app / "run.sh")
    (app / "requirements.txt").write_text("")
    log = tmp_path / "calls.log"
    _exe(app / ".venv" / "bin" / "python", 'echo "python $*" >> "%s"' % log)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    def run(*args, system="Darwin", app_running=False, systemd=True):
        _exe(bin_dir / "uname", "echo %s" % system)
        _exe(bin_dir / "curl", "exit %d" % (0 if app_running else 7))
        _exe(bin_dir / "open", 'echo "open $*" >> "%s"' % log)
        _exe(bin_dir / "xdg-open", 'echo "xdg-open $*" >> "%s"' % log)
        _exe(bin_dir / "launchctl", 'echo "launchctl $*" >> "%s"' % log)
        _exe(bin_dir / "nohup", 'echo "nohup $*" >> "%s"' % log)
        _exe(bin_dir / "systemctl",
             'echo "systemctl $*" >> "%s"; [ "$2" = show-environment ] && exit %d; exit 0'
             % (log, 0 if systemd else 1))
        env = {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "HOME": str(home)}
        out = subprocess.run(["bash", str(app / "run.sh"), *args], env=env, capture_output=True, text=True,
                             timeout=60)
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return out, calls

    run.app, run.home = app, home
    return run


def test_mac_login_item(sandbox):
    out, calls = sandbox("--start-at-login")
    assert out.returncode == 0, out.stdout + out.stderr
    plist_path = sandbox.home / "Library" / "LaunchAgents" / (LABEL + ".plist")
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == ["/bin/bash", str(sandbox.app / "run.sh"), "--background"]
    assert plist["WorkingDirectory"] == str(sandbox.app)
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == str(sandbox.app / "data" / "app.log")
    assert "launchctl load -w %s" % plist_path in calls
    assert any(c.startswith("python -m pip install") for c in calls)      # set up first
    assert "starts in the background whenever you log in" in out.stdout

    out, calls = sandbox("--stop-starting-at-login")
    assert out.returncode == 0 and not plist_path.exists()
    assert "launchctl unload -w %s" % plist_path in calls


def test_linux_systemd_service(sandbox):
    out, calls = sandbox("--start-at-login", system="Linux")
    assert out.returncode == 0, out.stdout + out.stderr
    unit = sandbox.home / ".config" / "systemd" / "user" / (LABEL + ".service")
    text = unit.read_text()
    assert 'ExecStart=/bin/bash "%s" --background' % (sandbox.app / "run.sh") in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert "systemctl --user enable --now %s.service" % LABEL in calls

    out, calls = sandbox("--stop-starting-at-login", system="Linux")
    assert not unit.exists()
    assert "systemctl --user disable --now %s.service" % LABEL in calls


def test_linux_without_systemd_uses_autostart(sandbox):
    out, calls = sandbox("--start-at-login", system="Linux", systemd=False)
    assert out.returncode == 0, out.stdout + out.stderr
    desktop = sandbox.home / ".config" / "autostart" / (LABEL + ".desktop")
    assert 'Exec=/bin/bash "%s" --background' % (sandbox.app / "run.sh") in desktop.read_text()
    assert any(c.startswith("nohup /bin/bash") and c.endswith("--background") for c in calls)   # started now

    out, _ = sandbox("--stop-starting-at-login", system="Linux", systemd=False)
    assert not desktop.exists() and "stops when you log out" in out.stdout


def test_background_start_skips_setup_and_browser(sandbox):
    out, calls = sandbox("--background")
    assert out.returncode == 0, out.stdout + out.stderr
    assert calls == ["python -m uvicorn app.main:app --port 8000"]       # no pip, no browser


def test_background_start_when_already_running_does_nothing(sandbox):
    out, calls = sandbox("--background", app_running=True)
    assert out.returncode == 0 and "Already running" in out.stdout
    assert calls == []


def test_background_start_before_setup_exits_cleanly(sandbox):
    shutil.rmtree(sandbox.app / ".venv")
    out, calls = sandbox("--background")
    assert out.returncode == 0 and "Not set up yet" in out.stdout      # a clean exit: no retry loop


def test_opening_it_again_while_it_runs_just_opens_the_page(sandbox):
    out, calls = sandbox(app_running=True)
    assert out.returncode == 0 and "already running" in out.stdout
    assert not any("uvicorn" in c for c in calls)


def test_unknown_option(sandbox):
    out, _ = sandbox("--bogus")
    assert out.returncode == 2 and "Usage" in out.stdout
