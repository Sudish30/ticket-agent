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

# Stage 2: solve the brief (patches a temp copy of the repo, runs pytest)
python -m solver_agent.main brief.json --repo demo_repo

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
| `solver_agent/` | stage 2: plans + writes exact string edits per the `TaskBrief`, applies them to a temp copy, runs pytest, emits `Solution` (solution.json/.md) |

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

## Solver agent (stage 2)

`python -m solver_agent.main brief.json --repo demo_repo` takes the confirmed brief, reads the suspected files (plus
any files the brief's `affected_areas`/`evidence` prose names, plus their local imports), plans and writes exact
`old_str → new_str` edits, applies them to a fresh temp copy of the repo, and runs pytest — up to 3 attempts, feeding
failures back into the retry. Verification is judged against a baseline run and has three outcomes: **passed** (fixed a
previously-failing test, or a newly-added regression test passes — always with zero new failures), **applied_unverified**
(patch applies cleanly and breaks nothing, but no test verifies it — stated honestly in the rationale), and **failed**
(new failures or patch errors after all retries). Bugs the brief leaves out of scope stay unfixed by design, and a patch
whose only remaining failures are out-of-scope ones stops retrying immediately.

Flow: **New ticket** → the intake agent starts automatically (status **Clarifying**) and posts grounded questions in the
ticket's Clarification thread → reply as the reporter → the agent posts its sign-off → reply `confirm` → status
**Brief ready**.

The topbar **View as** toggle (remembered in `localStorage` as `quorum-role`, default Reporter) picks the lens:
- **Reporter** — ticket details, the Clarification thread and the reply box. No solving controls, no brief, no Solutions tab.
- **Engineer** — everything, read-only thread, the Markdown brief (`TaskBrief.to_markdown()`), and **Start solving** once
  the status is **Brief ready**. After an **Agent error**, **Retry agent** calls `POST /solve` to run the intake again.
