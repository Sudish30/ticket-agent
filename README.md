# Ticket Understanding Agent

First agent in the pipeline. Reads a Jira ticket, figures out what it doesn't understand,
talks back and forth with the reporter until everything is clear, gets sign-off, and
hands a structured **Task Brief** to the next agent.

```
analyze ──► (all clear) ──► build_brief ──► confirm ──► END
   ▲                                          │
   │ (open questions)                         │ (human wants changes)
   └──────── ask_human ◄──────────────────────┘
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
export $(cat .env | xargs)

# Demo: you play the reporter in the terminal
python main.py mock_tickets/PROJ-142.json

# No-keyboard run with canned replies
PYTHONPATH=. python examples/scripted_demo.py

# Real Jira: questions posted as comments, waits for a reply
python main.py PROJ-142 --jira

# ...and post the Markdown brief (same content as brief.md) as a final comment
python main.py PROJ-142 --jira --post-brief

# Quorum web UI + backend (needs the Quorum checkout next to this repo, see below)
python -m uvicorn quorum_backend.server:app --port 8000   # open http://localhost:8000

# Unit tests (LLM calls mocked, no API key needed)
python -m unittest -v
```

## Handing off to the next agent

```python
from ticket_agent import run, load_mock_ticket, TerminalChannel
brief = run(load_mock_ticket("mock_tickets/PROJ-142.json"), TerminalChannel())
brief.model_dump()   # -> dict your planner/coder agent consumes
```

`TaskBrief` fields: `problem_statement, goal, acceptance_criteria, repro_steps, affected_areas,
constraints, out_of_scope, resolved_questions, assumptions, evidence, confidence`.

## Layout

| file | role |
|---|---|
| `schemas.py` | `Ticket`, `Question`, `TaskBrief`, graph state |
| `prompts.py` | the 5 prompts (analyze / ask / ingest reply / brief / confirm) |
| `graph.py` | LangGraph nodes + routing |
| `channels.py` | Terminal, Scripted (tests), JiraComment (polls for replies) |
| `jira_client.py` | Jira Cloud REST + ADF→text + mock loader |
| `codebase.py` | `Codebase`: file tree + file reads for a local dir or GitHub repo (used by `--repo`) |
| `demo_repo/` | Notely, a tiny Flask app with planted bugs that `mock_tickets/NOTE-*.json` describe |

## Knobs
- `--max-rounds` (default 3): human-facing rounds including sign-off, so at most `max-rounds - 1` clarification
  rounds. A question is asked at most once; anything unanswered becomes an explicit assumption.
- `--contact`: override who gets asked (default: ticket reporter).
- `--out` (default `brief.json`): every run also writes the Markdown twin (`brief.md`) next to it — `TaskBrief.to_markdown()`.
- `--post-brief` (with `--jira`): after sign-off, post that Markdown brief back on the ticket as a comment.
- `--repo` (or `TICKET_AGENT_REPO` env var): ground questions in a codebase — see below.
- `TICKET_AGENT_MODEL` env var to change the model.

## Next steps (if time)
- Slack channel implementation (same `Channel` interface, ~30 lines).
- `reopen(brief, question)` entry point so later agents can send questions back mid-build.

## Codebase grounding (demo repo)

`demo_repo/` is Notely, a small Flask app with three planted bugs. `mock_tickets/NOTE-*.json` describe them vaguely.

```bash
python main.py mock_tickets/NOTE-142.json --repo demo_repo          # local path
python main.py mock_tickets/NOTE-142.json --repo team-org/notely@buggy   # or a GitHub repo (GITHUB_TOKEN optional)
```

With `--repo`, a `lookup_codebase` node runs first: it lists the repo, asks the model to pick the relevant files, and feeds
their contents into every later prompt. Questions then cite real files (`Question.context`) and the brief carries a ranked
`suspected_files` list. Without `--repo` the agent works from the ticket alone.

| ticket | bug | where |
|---|---|---|
| NOTE-142 "People keep getting logged out" | session cookie SameSite=Strict breaks the OAuth return | auth/session.py |
| NOTE-151 "Signup says my email is invalid" | email regex rejects `+` and `-` | forms/validators.py |
| NOTE-157 "Password reset link never works" | TTL compares seconds to minutes | auth/tokens.py |

Bugs the model notices in the code that the ticket did **not** report go into `TaskBrief.related_findings` as suggested
follow-ups — they never become acceptance criteria or questions unless the reporter pulls them in.

## Quorum web UI

`quorum_backend/server.py` is a small FastAPI app that stores tickets in `quorum_backend/tickets.json`, runs the agent
on demand, and serves the [Quorum](https://github.com/S-kalakota/Quorum) UI (branch `wire-intake-agent`, with the
backend-wired `app.js`).

```bash
git clone https://github.com/S-kalakota/Quorum ../Quorum      # or set QUORUM_UI_DIR=/path/to/Quorum
export ANTHROPIC_API_KEY=...                                   # TICKET_AGENT_REPO defaults to demo_repo
python -m uvicorn quorum_backend.server:app --port 8000
open http://localhost:8000
```

Flow: **New ticket** → **Tickets** → expand → **Solve ticket** → the agent posts grounded questions in the Clarification
thread → reply as the reporter → the agent posts its sign-off → reply `confirm` → status **Brief ready** and the
Markdown brief (`TaskBrief.to_markdown()`) appears on the ticket.
