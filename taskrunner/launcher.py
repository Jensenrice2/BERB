"""Opening B.E.R.B like a normal app: no terminal window, no commands.

`python -m taskrunner.cli open`  starts the server quietly in the background if it isn't running,
                                 then opens the dashboard in its own app window.
`python -m taskrunner.cli shortcuts`  (Windows) puts a B.E.R.B icon on the Desktop and Start menu
                                 that does exactly that.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent   # the folder you downloaded (has .env, data/, .venv/)


def _app_version() -> str:
    """A fingerprint of this copy's code, so an older copy still running can be spotted and replaced."""
    import hashlib
    h = hashlib.sha256()
    pkg = Path(__file__).resolve().parent
    for f in sorted([*pkg.glob("*.py"), *(pkg / "static").glob("*.*")]):
        h.update(f.name.encode()); h.update(f.read_bytes())
    return h.hexdigest()[:12]


APP_VERSION = os.environ.get("TASKRUNNER_FAKE_VERSION") or _app_version()  # (the override is for tests)
ICON = Path(__file__).resolve().parent / "static" / "icons" / "berb.ico"


def load_dotenv(path: Path = APP_DIR / ".env") -> None:
    """Read KEY=value lines from .env into the environment (existing variables win)."""
    try:
        text = path.read_text(encoding="utf-8-sig")  # -sig: ignore a byte-order mark
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def running_info(ports) -> tuple[int, dict] | None:
    """(port, status) of a running B.E.R.B, if any (checks it really is B.E.R.B)."""
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
                status = json.loads(r.read() or b"{}")
                if "concurrency" in status:
                    return port, status
        except (OSError, ValueError):
            continue
    return None


def running_port(ports) -> int | None:
    info = running_info(ports)
    return info[0] if info else None


def is_this_copy(status: dict) -> bool:
    """Is the running B.E.R.B this exact copy (same folder, same version)?"""
    return status.get("app_dir") == str(APP_DIR) and status.get("version") == APP_VERSION


def stop_running(port: int, ports, wait: float = 20) -> bool:
    """Ask a running B.E.R.B (e.g. an older copy) to turn off, and wait until it has."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", method="POST",
                                 headers={"Origin": "http://berb.localhost"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except OSError:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if running_port(ports) is None:
            return True
        time.sleep(0.3)
    return False


def tell_user(message: str) -> None:
    """Show a message even when there's no console window (pythonw on Windows)."""
    print(message, flush=True)
    if os.name == "nt" and (sys.stdout is None or "pythonw" in sys.executable.lower()):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "B.E.R.B", 0x40)
        except Exception:
            pass


def replace_if_outdated(ports) -> bool:
    """If an older copy (or one from another folder) is running, turn it off. True if none is left running."""
    info = running_info(ports)
    if info is None:
        return True
    port, status = info
    if is_this_copy(status):
        return False  # it's us: nothing to replace
    if stop_running(port, ports):
        return True
    tell_user("An older copy of B.E.R.B is still running and couldn't be turned off automatically. "
              "Turn it off (click Turn off in it, or restart your computer), then open B.E.R.B again.")
    raise SystemExit(1)


def _pythonw() -> str:
    """On Windows, pythonw.exe runs Python without opening a console window."""
    exe = Path(sys.executable)
    if os.name == "nt":
        quiet = exe.with_name("pythonw.exe")
        if quiet.exists():
            return str(quiet)
    return str(exe)


def start_background() -> None:
    """Start the server detached from this process, with no window; output goes to data/server.log."""
    cmd = [_pythonw(), "-m", "taskrunner.cli", "serve", "--background"]
    kwargs: dict = dict(cwd=APP_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def _app_browser() -> list[str] | None:
    """A Chromium browser that can show a page as its own app window (no tabs or address bar)."""
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for root in filter(None, roots):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                if (Path(root) / rel).exists():
                    return [str(Path(root) / rel)]
    elif sys.platform == "darwin":
        for app in ("/Applications/Google Chrome.app", "/Applications/Microsoft Edge.app"):
            if Path(app).exists():
                return ["open", "-na", app, "--args"]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            if shutil.which(name):
                return [shutil.which(name)]
    return None


def open_window(url: str) -> None:
    browser = _app_browser()
    if browser:
        try:
            subprocess.Popen(browser + [f"--app={url}"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url)


def open_app(ports, pretty_url, window: bool = True, wait: float = 60) -> int:
    """Make sure B.E.R.B is running, then show it. Returns the port."""
    replace_if_outdated(ports)  # an older copy still running would otherwise be shown instead
    port = running_port(ports)
    if port is None:
        start_background()
        deadline = time.time() + wait
        while port is None and time.time() < deadline:
            time.sleep(0.3)
            port = running_port(ports)
        if port is None:
            log = Path(os.environ.get("TASKRUNNER_DATA", APP_DIR / "data")) / "server.log"
            raise SystemExit(f"B.E.R.B didn't start. Details are in {log}")
    if window:
        open_window(pretty_url(port))
    return port


def create_shortcuts(dest_dirs: list[Path] | None = None) -> list[Path]:
    """Windows: B.E.R.B shortcuts (with the logo) that open the app with no console window."""
    if os.name != "nt":
        raise SystemExit("Shortcuts are made on Windows. On a Mac, use start-mac.command.")
    if dest_dirs is None:
        home = Path(os.environ.get("USERPROFILE", Path.home()))
        desktop = _known_folder("Desktop") or home / "Desktop"
        programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        dest_dirs = [desktop, programs]
    icon = ICON  # berb.ico, made from the logo
    made = []
    for folder in dest_dirs:
        folder.mkdir(parents=True, exist_ok=True)
        lnk = folder / "B.E.R.B.lnk"
        ps = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:BERB_LNK);"
            "$s.TargetPath = $env:BERB_TARGET; $s.Arguments = '-m taskrunner.cli open';"
            "$s.WorkingDirectory = $env:BERB_DIR; $s.IconLocation = $env:BERB_ICON;"
            "$s.Description = 'B.E.R.B - give Claude tasks to do'; $s.Save()"
        )
        env = {**os.environ, "BERB_LNK": str(lnk), "BERB_TARGET": _pythonw(),
               "BERB_DIR": str(APP_DIR), "BERB_ICON": str(icon)}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                       stdout=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        made.append(lnk)
    return made


def _known_folder(name: str) -> Path | None:
    """The real Desktop folder (it may be moved into OneDrive)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{name}')"],
                             capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        path = out.stdout.strip()
        return Path(path) if path else None
    except OSError:
        return None
