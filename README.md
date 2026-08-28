# Ticket Agent — a multi-agent Jira-to-PR pipeline

Takes a vague bug ticket and turns it into a reviewed, tested pull request — with a human in the loop at the
two moments that matter: confirming what to build, and deciding what to do with the result.

```
  reporter ──► ① INTAKE AGENT ──► TaskBrief ──► ② ORCHESTRATOR (tech lead) ──► PRPackage ──► ③ GITHUB PR
  (chat in      asks clarifying     (confirmed     plans subtasks and dispatches   (diff, tests,    branch pushed,
   Jira or      questions, gets      contract)     workers in ONE shared           review table,    real PR opened
   web UI)      sign-off                           workspace:                      PR title/desc)   with `gh`
                                                    · code_writer  (solver agent)
                                                    · test_writer  (pytest tests)
                                                    · docs_writer  (comments/changelog only)
                                                   then the mandatory REVIEW GATE
```

Every stage is honest about uncertainty: unanswered questions become explicit assumptions, an unverifiable fix is
labeled `applied_unverified` (not "passed"), and a review that still has blockers ships as `needs_human_review`
instead of silently approving. Agents don't just reason about code — they **run** it: the solver reproduces the
bug in a sandbox before patching, and the reviewer probes the fix empirically (including reverting it to prove
the new tests actually discriminate).

Built with LangGraph + the Anthropic API + Pydantic v2. Each stage is usable standalone from the CLI, or the
whole pipeline runs behind the bundled **Quorum web UI**.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
export $(grep '^ANTHROPIC_API_KEY=' .env)

# The whole pipeline in a browser (ticket → clarify → brief → solve → PR):
python -m uvicorn quorum_backend.server:app --port 8000   # open http://localhost:8000

# Or stage by stage:
python main.py mock_tickets/NOTE-157.json --repo demo_repo   # ① intake: you play the reporter → brief.json
python -m solver_agent.main brief.json --repo demo_repo      # ② (solo solver) patch + pytest → solution.json
python -m orchestrator.main brief.json --repo demo_repo      # ② full tech-lead run → pr_package.json/.md

# Unit tests (LLM calls mocked, no API key needed)
python -m unittest -v
```

## The web UI, end to end

The FastAPI backend (`quorum_backend/server.py`) stores tickets in a JSON file, runs the agents in background
threads, and serves the [Quorum](https://github.com/S-kalakota/Quorum) UI — the wired UI files are bundled in
`ui/`, so a fresh clone serves the full web app with no extra setup (set `QUORUM_UI_DIR=/path/to/Quorum` to serve
a live checkout while editing the UI).

**New ticket** → the intake agent starts automatically (status **Clarifying**) and posts grounded questions in the
ticket's Clarification thread → reply as the reporter → the agent posts its sign-off → reply `confirm` → status
**Brief ready** → the Engineer clicks **Start solving** (`POST /solve-brief`), which runs the orchestrator on the
stored brief (status **Solving**) → status **PR ready** (package complete) or **Needs human review**, the PR
markdown is posted as a comment, and the Solutions screen shows the package: subtask table, review table, and the
combined diff in a collapsible block → the Engineer clicks **Open PR on GitHub** (`POST /open-pr`), which applies
the stored diff to a fresh clone of the repo's GitHub remote (mapped in `repos.json`), pushes branch
`agent/<key>-<slug>`, opens a real PR with `gh`, posts the URL as a comment, and turns the button into a link
(status **PR opened**).

The topbar **View as** toggle (remembered in `localStorage` as `quorum-role`, default Reporter) picks the lens:
- **Reporter** — ticket details, the Clarification thread and the reply box. No solving controls, no brief, no Solutions tab.
- **Engineer** — everything: read-only thread, the Markdown brief, **Start solving** / **Open PR on GitHub**, and
  after an **Agent error**, **Retry agent** (`POST /solve`) to run the intake again.

## Stage 1 — Intake agent (`ticket_agent/`)

Reads the ticket, figures out what it doesn't understand, talks with the reporter until everything is clear, gets
sign-off, and emits a structured **TaskBrief** — the contract every later stage builds against.

```
lookup_codebase ──► analyze ──► (all clear) ──► build_brief ──► confirm ──► END
                       ▲                                          │
                       │ (open questions)                         │ (human wants changes)
                       └──────── ask_human ◄──────────────────────┘
```

- **Grounded in the codebase**: with `--repo` (local path or GitHub `owner/name[@ref]`), a `lookup_codebase` node
  picks the relevant files first, so questions cite real code and the brief carries a ranked `suspected_files` list.
- **Ask-once rule**: a question is asked at most once; anything unanswered becomes an explicit assumption in the
  brief (`DEFERRED` / `ASKED_NO_ANSWER` / `NEVER_ASKED`) — the brief never carries a silent unknown.
- **Scope discipline**: bugs the model notices that the ticket did *not* report go into `related_findings` as
  suggested follow-ups — never into acceptance criteria unless the reporter pulls them in.
- **Channels**: the human side is a swappable `Channel` (Terminal / Scripted for tests / Jira comments / the
  Quorum backend), so the graph never changes when the transport does. `--jira` talks to real Jira Cloud;
  `INTEGRATION.md` documents the 4 REST routes a custom ticket UI must implement.

`TaskBrief` fields: `problem_statement, goal, acceptance_criteria, repro_steps, affected_areas, constraints,
out_of_scope, resolved_questions, assumptions, evidence, suspected_files, related_findings, confidence` — plus
three metadata fields filled in code rather than by the model: `ticket_id`, `ticket_type`, `clarification_contact`.

Knobs: `--max-rounds` (default 3, i.e. ≤2 clarification rounds + sign-off), `--contact` (who gets asked),
`--out` (brief.json; a Markdown twin brief.md is always written), `--post-brief` (post the brief on the ticket),
`--repo` / `TICKET_AGENT_REPO`, `TICKET_AGENT_MODEL`.

```python
from ticket_agent import run, load_mock_ticket, TerminalChannel
brief = run(load_mock_ticket("mock_tickets/NOTE-157.json"), TerminalChannel(), repo="demo_repo")
brief.model_dump()   # -> the dict the solver/orchestrator consume
```

## Stage 2 — Solver agent (`solver_agent/`)

The hands-on-keyboard fixer. `python -m solver_agent.main brief.json --repo demo_repo` takes the confirmed brief,
reads the suspected files (plus any files the brief's `affected_areas`/`evidence` prose names, plus their local
imports), **investigates live** — up to 6 sandboxed shell commands in a scratch copy (running the failing test,
executing snippets, printing values) to reproduce and localize the bug before planning, with the findings
grounding the diagnosis (retries get 2 more diagnostic commands to test why the failure happened) — then plans and
writes exact `old_str → new_str` edits, applies them to a fresh temp copy of the repo, and runs pytest — up to 3
attempts, feeding failures back into the retry.

Verification is judged against a baseline pytest run and has three outcomes: **passed** (fixed a previously-failing
test, or a newly-added regression test passes — always with zero new failures), **applied_unverified** (patch
applies cleanly and breaks nothing, but no test verifies it — stated honestly in the rationale), and **failed**
(new failures or patch errors after all retries). Bugs the brief leaves out of scope stay unfixed by design, and a
patch whose only remaining failures are out-of-scope ones stops retrying immediately. Output: `solution.json` +
`solution.md` (diff, test counts, rationale, investigation log).

## Stage 3 — Orchestrator (`orchestrator/`)

The tech lead. `python -m orchestrator.main brief.json --repo demo_repo` turns the brief into subtasks
(`{id, worker, instruction, depends_on, rationale}`), each dispatched to a registered worker in ONE shared temp
workspace:

- **code_writer** — the solver agent, scoped by the subtask instruction;
- **test_writer** — writes new pytest tests for the acceptance criteria against the patched code;
- **docs_writer** — refreshes comments/docstrings the fix made stale and appends a CHANGELOG note (a deterministic
  guard rejects any edit touching executable code).

Results are evaluated **status-aware**: a `passed` fix is accepted, an `applied_unverified` fix is never retried
but gets a regression-test subtask appended to verify it, and a `failed` one is retried with feedback (max 2),
then replanned (max 1), then reported honestly. Every dispatch snapshots the workspace, and a rejected attempt is
rolled back — rejected work never leaks into later subtasks.

The run always ends at the **review gate**: an independent reviewer first probes the change empirically — up to 5
sandboxed commands in a scratch copy of the workspace (running the suite itself, writing and running its own probe
tests, and a deterministic discrimination check that reverts the fix and confirms the new tests then fail —
"empirically verified" vs "reasoned" in the review notes) — then judges the brief + diff + full changed files +
tests (never the workers' reasoning) against five checks: every acceptance criterion addressed, constraints
respected, out_of_scope untouched, no regressions/security issues, and new tests genuinely asserting the ACs.
Blocking change requests get ONE repair round (a scoped subtask, then one re-review); a still-blocked run ships as
`needs_human_review` with the change requests attached, and minors-only approves with follow-ups.

The result is `pr_package.json`/`pr_package.md`: subtask outcomes, the review table (with probe log), the solver's
investigation log, combined diff, test counts, new tests, and an LLM-written PR title + description. Adding a
worker is one `@register`-decorated function in `orchestrator/registry.py`.

## Stage 4 — Real GitHub PRs (`quorum_backend/github_pr.py`)

Once a ticket has a PR package, `POST /api/tickets/{key}/open-pr` (the Engineer's **Open PR on GitHub** button)
clones the repo's GitHub remote to a temp dir, creates branch `agent/<key>-<slug>`, applies the package's stored
combined diff, commits as **Ticket Agent**, pushes, and opens the PR with `gh pr create` — the PR URL is stored on
the ticket and posted as a comment. Requires the `gh` CLI authenticated with push rights on the remote.

`repos.json` maps each project to its local path and GitHub remote, e.g. Notely's `demo_repo` ↔
`Sudish30/notely-demo`.

## Demo repos & tickets

`demo_repo/` is **Notely**, a tiny Flask app with three planted bugs, described vaguely by `mock_tickets/NOTE-*.json`
(the point: the intake agent has to ask the right questions and find the right files).

| ticket | bug | where |
|---|---|---|
| NOTE-142 "People keep getting logged out" | session cookie SameSite=Strict breaks the OAuth return | auth/session.py |
| NOTE-151 "Signup says my email is invalid" | email regex rejects `+` and `-` | forms/validators.py |
| NOTE-157 "Password reset link never works" | TTL compares seconds to minutes | auth/tokens.py |

`mock_tickets/FIN-203.json` targets **FinPilot** (`Sudish30/Finpilot`), a larger FastAPI app: a duplicate-charge
detector whose key lost its date component, so every monthly subscription flags as a duplicate. Its backend has no
test suite — which exercises the `applied_unverified` → regression-test-verification path end to end. The FinPilot
demo expects a machine-local scratch clone at the path `repos.json` names (`/tmp/finpilot-test/Finpilot/backend`):

```bash
git clone https://github.com/Sudish30/Finpilot.git /tmp/finpilot-test/Finpilot   # or edit repos.json's "path"
# plant the FIN-203 bug: in backend/app/services/analytics.py, delete ", t.date" from the dedup key
```
`mock_tickets/PROJ-142.json` is a standalone ticket with no codebase.

## Layout

| path | role |
|---|---|
| `ticket_agent/` | stage 1: intake graph, prompts, schemas (`Ticket`, `TaskBrief`), channels, Jira client, `Codebase` |
| `ticket_agent/sandbox.py` | shared sandboxed shell (`run_cmd`): path guards, no proxies, per-run command budget |
| `solver_agent/` | stage 2: investigate → plan → patch → pytest loop, emits `Solution` (solution.json/.md) |
| `orchestrator/` | stage 3: plan subtasks → dispatch workers → evaluate → review gate, emits `PRPackage` (pr_package.json/.md) |
| `orchestrator/workers/` | test_writer, docs_writer (comment-only guard), reviewer (empirical probes + discrimination check) |
| `quorum_backend/` | FastAPI backend: ticket store, agent runners, `/solve-brief`, `/open-pr`, serves the UI |
| `quorum_backend/github_pr.py` | clone → branch → apply diff → commit → push → `gh pr create` |
| `ui/` | bundled backend-wired Quorum UI (index.html, app.js, styles.css, tokens.css) |
| `repos.json` | project name → `{path, github, default_branch, description}` mapping for open-pr |
| `demo_repo/` | Notely, the planted-bug demo app |
| `mock_tickets/` | `Ticket`-shaped JSON fixtures (PROJ-142, NOTE-142/151/157, FIN-203) |
| `main.py` | stage-1 CLI (`--repo`, `--jira`, `--post-brief`, …) |
| `tests/` | 94 unit tests, all LLM calls stubbed — intake, solver, orchestrator, sandbox, backend, github_pr |
| `INTEGRATION.md` | the Jira REST routes a custom ticket UI must implement for `--jira` mode |

## Environment

`ANTHROPIC_API_KEY` (required) · `TICKET_AGENT_MODEL` (default `claude-sonnet-4-6`) · `TICKET_AGENT_REPO`
(default repo for grounding/solving) · `GITHUB_TOKEN` (optional, for GitHub-hosted `--repo`) · `QUORUM_UI_DIR`
(UI dir to serve; default the bundled `ui/`) · `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` /
(only for `--jira`) · `JIRA_AGENT_NAME` (the agent's display name — used by `--jira` AND by the web backend to
author its ticket comments; default "Ticket Agent"). The web backend must run on port 8000 (the agent calls
itself over HTTP).

## Next steps (if time)

- Slack channel implementation (same `Channel` interface, ~30 lines).
- `reopen(brief, question)` entry point so later agents can send questions back mid-build.
