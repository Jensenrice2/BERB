"""Command-line client:  python -m taskrunner.cli <command> ...

  serve                         start the server + dashboard
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

URL = os.environ.get("TASKRUNNER_URL", "http://127.0.0.1:8765")


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        URL + path, method=method,
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
        sys.exit(f"cannot reach server at {URL} ({e.reason}). Start it with: python -m taskrunner.cli serve")


def fmt(j: dict) -> str:
    kind = "🤖 " if j.get("kind") == "agent" else ""
    return f"#{j['id']:<4} {j['status']:<10} {kind}{j['name']}"


def main(argv=None):
    p = argparse.ArgumentParser(prog="taskrunner", description="Run shell jobs in the background.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8765)
    a = sub.add_parser("add"); a.add_argument("command"); a.add_argument("--name"); a.add_argument("--cwd"); a.add_argument("--timeout", type=float)
    ag = sub.add_parser("agent"); ag.add_argument("task"); ag.add_argument("--name"); ag.add_argument("--cwd"); ag.add_argument("--timeout", type=float)
    sub.add_parser("files").add_argument("id", type=int)
    l = sub.add_parser("list"); l.add_argument("--status")
    for name in ("show", "cancel", "retry", "rm"):
        sub.add_parser(name).add_argument("id", type=int)
    g = sub.add_parser("logs"); g.add_argument("id", type=int); g.add_argument("-f", "--follow", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("taskrunner.app:app", host=args.host, port=args.port)
    elif args.cmd == "add":
        body = {k: v for k, v in vars(args).items() if k in ("command", "name", "cwd", "timeout") and v is not None}
        if "cwd" in body:
            body["cwd"] = os.path.abspath(body["cwd"])
        print(fmt(call("POST", "/api/jobs", body)))
    elif args.cmd == "agent":
        body = {k: v for k, v in vars(args).items() if k in ("task", "name", "cwd", "timeout") and v is not None}
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
