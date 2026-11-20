# Task Runner

Give it work, either as a shell command or as a task described in plain English for an AI agent (Claude). It queues the jobs, runs them in the background with a limited number of workers, and saves each job's status, output and files.

![dashboard](dashboard.png)

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## Install it as an app

With the server running, open http://127.0.0.1:8765 and:

- **Mac, Safari** (macOS Sonoma or later): **File → Add to Dock**.
- **Mac/Windows/Linux, Chrome or Edge**: click **Install app** in the page header, or the install icon in the address bar.

It then opens in its own window with its own icon. The server still has to be running for it to show jobs; if it isn't, the app tells you how to start it.

## AI agent setup

Agent jobs need an Anthropic API key in the environment of the **server**:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
.venv/bin/python -m taskrunner.cli serve        # dashboard at http://127.0.0.1:8765
```

## Assign work from the CLI

```bash
python -m taskrunner.cli agent "Download https://example.com/data.csv, clean it, and write a summary to report.md"
python -m taskrunner.cli files 7               # files the agent produced
python -m taskrunner.cli add "python train.py" --name train --timeout 3600 --cwd ~/proj
python -m taskrunner.cli list [--status running]
python -m taskrunner.cli logs 3 -f             # follow output live
python -m taskrunner.cli cancel 3 | retry 3 | rm 3 | show 3
```

## API

| Method | Path | |
|---|---|---|
| POST | `/api/jobs` | shell: `{command, name?, cwd?, timeout?}` · agent: `{kind: "agent", task, name?, cwd?, timeout?}` |
| GET | `/api/jobs/{id}/files` | list an agent job's workspace files |
| GET | `/api/jobs/{id}/files/{path}` | download one of them |
| GET | `/api/jobs?status=` | list |
| GET | `/api/jobs/{id}` | details |
| GET | `/api/jobs/{id}/logs?offset=` | incremental output (poll with `next_offset`) |
| POST | `/api/jobs/{id}/cancel` / `retry` | |
| DELETE | `/api/jobs/{id}` | |
| GET | `/api/status` | worker usage + counts |

Interactive docs: `/docs`.

## Config (env vars)

- `TASKRUNNER_DATA`: where `jobs.db` and `logs/` are stored (default `./data`)
- `TASKRUNNER_CONCURRENCY`: how many jobs run at once (default 2)
- `TASKRUNNER_URL`: the server the CLI talks to (default `http://127.0.0.1:8765`)

- `TASKRUNNER_AGENT_MODEL`: the agent's model (default `claude-opus-5-5`)
- `TASKRUNNER_AGENT_EFFORT`: `low` / `medium` / `high` / `xhigh` / `max` (default `high`). Lower is cheaper and faster.

## How agent jobs work

Each agent job runs `taskrunner/agent.py` as its own subprocess. Cancel, timeout, retry and live logs all work the same way as for shell jobs.

- **Tools:** Claude gets a persistent bash shell and a file editor. It works in `data/agent/<id>/workspace`, or in the working directory you give it.
- **Limits:** a run stops after 60 model turns, and each bash command stops after 5 minutes. Set a job timeout to cap wall-clock time.
- **Log:** shows Claude's reasoning summaries, its messages, every command it runs, and a final report.
- **Model settings:** adaptive thinking, prompt caching for the growing conversation, and server-side refusal fallback (`fallbacks: "default"`).
- **Isolation:** the file editor is confined to the workspace, but **bash is not sandboxed**. The agent can run any command the server's user can. Run the server as a low-privilege user or inside a container/VM if that matters to you.
- **Billing:** every agent job is billed to your Anthropic account by token use.

## Behavior

- Each job runs in its own process group, so cancelling it or hitting its timeout kills any child processes too. The runner sends SIGTERM first, then SIGKILL after 5 seconds.
- Jobs are stored in SQLite, so queued jobs survive a restart. Jobs that were running when the server stopped are marked `failed` with "interrupted by server restart".
- **Security:** jobs (and agents) run arbitrary shell commands as the server's user, and there is no login. The server binds to localhost only. Don't expose it to a network without adding authentication.
