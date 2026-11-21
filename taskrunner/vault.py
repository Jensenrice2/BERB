"""Saved logins: an opt-in way for B.E.R.B to sign in to websites for the user.

- Off by default. Turned on with the "Remember passwords" switch (data/settings.json).
- Passwords live in the computer's own secure store - Windows Credential Manager or the macOS
  Keychain - via the `keyring` package. Only the list of sites and usernames is kept in
  data/logins.json; it never contains a password.
- The AI never sees a password. The browser fills it in itself, and only on the exact site
  (or its subdomains) it was saved for, and only on https:// pages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

SERVICE = "B.E.R.B"


class VaultUnavailable(RuntimeError):
    pass


def site_of(text: str) -> str:
    """'https://www.Example.com/login' or 'example.com' -> 'example.com'."""
    text = (text or "").strip()
    host = urlparse(text if "://" in text else "https://" + text).hostname or ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def site_matches(saved: str, url: str) -> bool:
    """Only the saved site and its subdomains - never a look-alike."""
    host = site_of(url)
    return bool(saved) and (host == saved or host.endswith("." + saved))


def secure_page(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" or (u.hostname or "") in ("localhost", "127.0.0.1")  # local: tests


class _KeyringBackend:
    def __init__(self):
        import keyring
        from keyring.backends import fail
        self.keyring = keyring
        if isinstance(keyring.get_keyring(), fail.Keyring):
            raise VaultUnavailable("this computer has no secure password store B.E.R.B can use")

    def set(self, key: str, password: str) -> None:
        self.keyring.set_password(SERVICE, key, password)

    def get(self, key: str) -> str | None:
        return self.keyring.get_password(SERVICE, key)

    def delete(self, key: str) -> None:
        try:
            self.keyring.delete_password(SERVICE, key)
        except self.keyring.errors.PasswordDeleteError:
            pass


class MemoryBackend:
    """For tests only."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, password):
        self.store[key] = password

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


class Vault:
    def __init__(self, data_dir: Path, backend=None):
        self.data_dir = Path(data_dir)
        self._backend = backend

    # ---------------------------------------------------------------- the on/off switch
    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def enabled(self) -> bool:
        try:
            return bool(json.loads(self.settings_path.read_text(encoding="utf-8")).get("remember_passwords"))
        except (OSError, ValueError):
            return False

    def set_enabled(self, on: bool) -> None:
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        settings["remember_passwords"] = bool(on)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- saved logins
    @property
    def backend(self):
        if self._backend is None:
            self._backend = _KeyringBackend()
        return self._backend

    def available(self) -> bool:
        try:
            self.backend
            return True
        except Exception:
            return False

    @property
    def index_path(self) -> Path:
        return self.data_dir / "logins.json"

    def logins(self) -> list[dict]:
        """[{site, username}] - never passwords."""
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _write(self, logins: list[dict]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(logins, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    @staticmethod
    def _key(site: str, username: str) -> str:
        return f"{site}|{username}"

    def save(self, site: str, username: str, password: str) -> dict:
        site, username = site_of(site), (username or "").strip()
        if not site or "." not in site and site != "localhost":
            raise ValueError("enter the website, like example.com")
        if not username or not password:
            raise ValueError("enter both the username (or email) and the password")
        self.backend.set(self._key(site, username), password)
        logins = [l for l in self.logins() if not (l["site"] == site and l["username"] == username)]
        logins.append({"site": site, "username": username})
        self._write(sorted(logins, key=lambda l: (l["site"], l["username"])))
        return {"site": site, "username": username}

    def delete(self, site: str, username: str) -> bool:
        site = site_of(site)
        logins = self.logins()
        keep = [l for l in logins if not (l["site"] == site and l["username"] == username)]
        if len(keep) == len(logins):
            return False
        try:
            self.backend.delete(self._key(site, username))
        finally:
            self._write(keep)
        return True

    def for_url(self, url: str) -> tuple[str, str] | None:
        """(username, password) to sign in on this page, if allowed - else None."""
        if not self.enabled() or not secure_page(url):
            return None
        # the most specific saved site wins (accounts.example.com before example.com)
        for login in sorted(self.logins(), key=lambda l: -len(l["site"])):
            if site_matches(login["site"], url):
                password = self.backend.get(self._key(login["site"], login["username"]))
                if password:
                    return login["username"], password
        return None

    def sites(self) -> list[str]:
        return sorted({l["site"] for l in self.logins()}) if self.enabled() else []
