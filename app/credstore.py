"""Where the Gmail app password is kept.

Preferred: the operating system's keychain (macOS Keychain, Windows Credential
Locker). If there isn't one, it falls back to the app's local database file,
which only your user account can read. It is never sent anywhere except to
Gmail when signing in, and no API endpoint returns it.
"""
from typing import Optional

from . import db

# The app was first called "Referral First". The keychain entry keeps that name so a
# password saved before the rename is still found.
SERVICE = "referral-first-gmail"
_DB_KEY = "gmail_app_password"


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def save(address: str, password: str) -> str:
    """Store the password and say where it went: "keychain" or "file"."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, address, password)
            if kr.get_password(SERVICE, address) == password:
                db.delete_setting(_DB_KEY)
                return "keychain"
        except Exception:
            pass
    db.set_setting(_DB_KEY, {"address": address, "password": password})
    return "file"


def load(address: str) -> Optional[str]:
    kr = _keyring()
    if kr is not None:
        try:
            password = kr.get_password(SERVICE, address)
            if password:
                return password
        except Exception:
            pass
    stored = db.get_setting(_DB_KEY)
    if stored and stored.get("address") == address:
        return stored.get("password")
    return None


def forget(address: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, address)
        except Exception:
            pass
    db.delete_setting(_DB_KEY)
