"""Command-line client:  python -m taskrunner.cli <command> ...

  open                          start B.E.R.B if needed and open it in its own window
  shortcuts                     (Windows) put B.E.R.B icons on the Desktop and Start menu
  serve [--background]          start the server + dashboard
  add "<shell cmd>" [--name N] [--cwd DIR] [--timeout SEC]
  agent "<task in plain English>" [--name N] [--cwd DIR] [--timeout SEC]
  files <id>                    list files an agent job produced
  list [--status S]
  show <id>
  logs <id> [-f]                print (and optionally follow) output
  cancel <id> | retry <id> | rm <id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The app's address: http://berb.localhost (any name ending in ".localhost" means "this computer"
# to Edge, Chrome and Firefox). It uses the standard web port 80 when it's free, so no ":8765".
NAME = os.environ.get("TASKRUNNER_NAME", "berb")
PORTS = (80, 8765)  # first free one wins
_url: str | None = os.environ.get("TASKRUNNER_URL")


def pretty_url(port: int) -> str:
    # Safari on macOS doesn't resolve *.localhost names, so plain localhost there.
    host = "localhost" if sys.platform == "darwin" else f"{NAME}.localhost"
    return f"http://{host}" + ("" if port == 80 else f":{port}")


def pick_port(host: str) -> int:
    import socket
    for port in PORTS:
        with socket.socket() as sock:
            if os.name != "nt":
                # Match the server: ignore ports only cooling down after a previous run (TIME_WAIT).
                # (Not on Windows, where this option would let two programs share a port.)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    sys.exit(f"Ports {' and '.join(map(str, PORTS))} are both in use. "
             "Is Task Runner already running? Otherwise start it with --port 9000")


def server_url() -> str:
    """Where the CLI finds the server: TASKRUNNER_URL, else whichever usual port answers."""
    global _url
    if _url:
        return _url
    for port in PORTS:
        candidate = f"http://127.0.0.1:{port}"
        try:
            urllib.request.urlopen(candidate + "/api/status", timeout=1).close()
            _url = candidate
            return _url
        except OSError:
            continue
    sys.exit("cannot reach Task Runner. Start it with: python -m taskrunner.cli serve")


def call(method: str, path: str, body: dict | None = None):
    url = server_url()
    req = urllib.request.Request(
        url + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"{}").get("detail", e.reason)
        sys.exit(f"error: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach server at {url} ({e.reason}). Start it with: python -m taskrunner.cli serve")


def fmt(j: dict) -> str:
    kind = "🤖 " if j.get("kind") == "agent" else ""
    return f"#{j['id']:<4} {j['status']:<10} {kind}{j['name']}"


def _open_when_ready(check_url: str, open_url: str) -> None:
    import threading
    import webbrowser

    def wait_then_open():
        for _ in range(100):
            try:
                urllib.request.urlopen(check_url + "/api/status", timeout=1).close()
                webbrowser.open(open_url)
                return
            except OSError:
                time.sleep(0.2)

    threading.Thread(target=wait_then_open, daemon=True).start()


def main(argv=None):
    p = argparse.ArgumentParser(prog="taskrunner", description="Run shell jobs in the background.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=None,
                                                                                    help="default: 80 if free, else 8765")
    s.add_argument("--open", action="store_true", help="open the dashboard in your browser once it's ready")
    s.add_argument("--background", action="store_true", help="no console: write messages to data/server.log")
    o = sub.add_parser("open"); o.add_argument("--no-window", action="store_true", help="just make sure it's running")
    sub.add_parser("shortcuts")
    a = sub.add_parser("add"); a.add_argument("command"); a.add_argument("--name"); a.add_argument("--cwd"); a.add_argument("--timeout", type=float)
    ag = sub.add_parser("agent"); ag.add_argument("task"); ag.add_argument("--name"); ag.add_argument("--cwd"); ag.add_argument("--timeout", type=float)
    sub.add_parser("files").add_argument("id", type=int)
    l = sub.add_parser("list"); l.add_argument("--status")
    for name in ("show", "cancel", "retry", "rm"):
        sub.add_parser(name).add_argument("id", type=int)
    g = sub.add_parser("logs"); g.add_argument("id", type=int); g.add_argument("-f", "--follow", action="store_true")
    args = p.parse_args(argv)
    from . import launcher
    launcher.load_dotenv()  # your API key from .env, however B.E.R.B was started

    if args.cmd == "open":
        port = launcher.open_app(PORTS, pretty_url, window=not args.no_window)
        print(f"B.E.R.B is running at {pretty_url(port)}")
    elif args.cmd == "shortcuts":
        for lnk in launcher.create_shortcuts():
            print(f"made {lnk}")
    elif args.cmd == "serve":
        if launcher.running_port(PORTS) and not args.port:
            # Already running (e.g. started from the icon): just show it rather than start a second copy.
            if args.open:
                launcher.open_window(pretty_url(launcher.running_port(PORTS)))
            print("B.E.R.B is already running.")
            return
        if args.background or sys.stdout is None:  # pythonw has no console to print to
            log = Path(os.environ.get("TASKRUNNER_DATA", launcher.APP_DIR / "data")) / "server.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
        import uvicorn
        from .app import app
        port = args.port or pick_port(args.host)
        url = pretty_url(port)
        if args.open:
            _open_when_ready(f"http://127.0.0.1:{port}", url)
        print(f"{time.ctime()}  B.E.R.B is running at {url}\n"
              f"  (also http://localhost{'' if port == 80 else f':{port}'})\n"
              + ("" if args.background else "  Keep this window open; close it or press Ctrl+C to stop.\n"),
              flush=True)
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning"))
        app.state.server = server  # lets the app's "Turn off" button stop it
        server.run()
    elif args.cmd == "add":
        body = {k: v for k, v in vars(args).items() if k in ("command", "name", "cwd", "timeout") and v is not None}
        if "cwd" in body:
            body["cwd"] = os.path.abspath(body["cwd"])
        print(fmt(call("POST", "/api/jobs", body)))
    elif args.cmd == "agent":
        body = {k: v for k, v in vars(args).items()
                if k in ("task", "name", "cwd", "timeout") and v is not None}
        body["kind"] = "agent"
        if "cwd" in body:
            body["cwd"] = os.path.abspath(body["cwd"])
        print(fmt(call("POST", "/api/jobs", body)))
    elif args.cmd == "files":
        r = call("GET", f"/api/jobs/{args.id}/files")
        print(r["workspace"])
        for f in r["files"]:
            print(f"  {f['size']:>9}  {f['path']}")
    elif args.cmd == "list":
        q = f"?status={args.status}" if args.status else ""
        for j in call("GET", "/api/jobs" + q):
            print(fmt(j))
    elif args.cmd == "show":
        print(json.dumps(call("GET", f"/api/jobs/{args.id}"), indent=2))
    elif args.cmd == "logs":
        offset = 0
        while True:
            r = call("GET", f"/api/jobs/{args.id}/logs?offset={offset}")
            sys.stdout.write(r["text"]); sys.stdout.flush()
            offset = r["next_offset"]
            if not args.follow or (r["status"] not in ("queued", "running") and not r["text"]):
                break
            time.sleep(1)
    elif args.cmd in ("cancel", "retry"):
        print(fmt(call("POST", f"/api/jobs/{args.id}/{args.cmd}")))
    elif args.cmd == "rm":
        call("DELETE", f"/api/jobs/{args.id}")
        print(f"deleted #{args.id}")


if __name__ == "__main__":
    main()
