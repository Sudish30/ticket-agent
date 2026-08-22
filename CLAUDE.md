# CLAUDE.md — Ticket Understanding Agent

First agent in a multi-agent Jira pipeline. Reads a ticket (optionally grounded in a codebase), asks the
reporter clarifying questions — each at most once — gets sign-off, and emits a structured `TaskBrief`
(Pydantic) for downstream planner/coder/tester agents.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate   # this machine has python3 only, no `python`
pip install -r requirements.txt                       # langgraph, langchain-anthropic, anthropic, pydantic v2, requests (+ pytest, flask for demo_repo)
cp .env.example .env && export $(grep '^ANTHROPIC_API_KEY=' .env)

python main.py mock_tickets/PROJ-142.json             # interactive: you play the reporter at the keyboard
python main.py mock_tickets/NOTE-157.json --repo demo_repo   # ...grounded in the demo codebase (or TICKET_AGENT_REPO=...)
PYTHONPATH=. python examples/scripted_demo.py         # no-keyboard end-to-end run with canned replies
python main.py PROJ-142 --jira                        # real Jira Cloud; questions posted as comments
python main.py PROJ-142 --jira --terminal             # real ticket, answer in terminal
python main.py PROJ-142 --jira --post-brief           # ...and post brief.md's Markdown as a final comment
python -m unittest -v                                 # unit tests (tests/, LLM calls mocked, no API key needed)
python -m pytest demo_repo/tests                      # the demo app's own suite: 2 fail, 1 pass (planted bugs)
python -m uvicorn quorum_backend.server:app --port 8000   # Quorum web UI + backend at http://localhost:8000 (UI from ../Quorum)
```

`examples/` is not a package — it needs `PYTHONPATH=.` to import `ticket_agent`.
Tests live in `tests/` (stdlib `unittest`, also runnable with `pytest tests/`); they patch `graph._llm` / `graph._call`
and use `ScriptedChannel` (records `.sent`, feeds canned replies) as the human-in-the-loop seam.

Env vars: `ANTHROPIC_API_KEY` (required), `TICKET_AGENT_MODEL` (default `claude-sonnet-4-6`),
`TICKET_AGENT_REPO` (default for `--repo`; the Quorum backend defaults it to `demo_repo`), `GITHUB_TOKEN` (optional,
for `owner/name[@branch]` repos), `QUORUM_UI_DIR` (Quorum checkout to serve; default `../Quorum`),
`JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` / `JIRA_AGENT_NAME` (only for `--jira`).

## Architecture

```
lookup_codebase ──► analyze ──► (nothing left to ask) ──► build_brief ──► confirm ──► END
                       ▲                                                     │
                       │ (open questions, clarification rounds left)         │ (human wants changes)
                       └──────────────── ask_human ◄─────────────────────────┘
```

| file | role |
|---|---|
| `ticket_agent/graph.py` | LangGraph nodes, routing, `build_graph(channel)`, `run(ticket, channel, contact, max_rounds, repo) -> TaskBrief` |
| `ticket_agent/schemas.py` | `Ticket`/`Comment` (input), `Question`, `ClarificationTurn`, `TaskBrief` (output contract; `.to_markdown()` = brief.md / posted comment, `.as_comment()` = short plain text), `AgentState` |
| `ticket_agent/prompts.py` | `SYSTEM` + 6 task prompts: `SELECT_FILES`, `ANALYZE`, `ASK_HUMAN`, `INGEST_ANSWER`, `BUILD_BRIEF`, `CONFIRM`; `ROUND_NOTE_FIRST` / `ROUND_NOTE_LATER` slot into `ASK_HUMAN` |
| `ticket_agent/codebase.py` | `Codebase.open(spec)` — local dir or GitHub `owner/name[@ref]`; `files`, `tree_text()`, `read()`, `context_text(paths)`; `MAX_FILES=12`, `MAX_FILE_CHARS=6000` |
| `ticket_agent/channels.py` | `Channel` Protocol `ask(key, contact, message) -> str`; `TerminalChannel`, `ScriptedChannel`, `JiraCommentChannel` |
| `ticket_agent/jira_client.py` | `JiraClient` (Jira Cloud REST v3), `FakeJiraClient` (same duck type, against the Quorum backend's `/api/tickets` routes), `adf_to_text` (ADF → plain text), `load_mock_ticket` |
| `quorum_backend/server.py` | FastAPI: JSON ticket store (`quorum_backend/tickets.json`, gitignored), comment thread, `POST /solve` runs the agent in a thread, serves the Quorum UI at `/` |
| `main.py` | argparse CLI; `--repo`; writes `brief.json` + `brief.md` (`--out` path with `.md`); `--post-brief` (with `--jira`) posts `brief.to_markdown()` on the ticket |
| `tests/test_graph.py` | `_is_blanket_defer` cases + `ask_human` ingest-vs-defer paths with mocked LLM |
| `tests/test_rounds.py` | ask-once rule, sign-off round reservation, `build_brief` assumptions, `analyze` grounding, `lookup_codebase` |
| `tests/test_markdown.py` | `TaskBrief.to_markdown()` section order, checklist, table escaping, collapsed Q&A, empty sections |
| `tests/test_quorum_backend.py` | backend routes + `FakeJiraClient` via FastAPI `TestClient` (temp store, `requests` shimmed into the client); UI-serving test skips without `../Quorum` |
| `mock_tickets/*.json` | `Ticket`-shaped fixtures: `PROJ-142` (standalone), `NOTE-142/151/157` (describe bugs planted in `demo_repo/`) |
| `demo_repo/` | Notely, a tiny Flask app: NOTE-142 → `auth/session.py` SameSite=Strict; NOTE-151 → `forms/validators.py` email regex; NOTE-157 → `auth/tokens.py` TTL in minutes compared to seconds |
| `INTEGRATION.md` | the 4 Jira REST routes a fake Jira / custom ticket UI must implement for `--jira` mode |

### State (`AgentState`, TypedDict)

`ticket`, `understanding` (running plain-text model of the ticket), `questions` (list[Question], open
and answered), `transcript` (list[ClarificationTurn], **append reducer** — nodes return only new turns),
`round`, `max_rounds`, `brief`, `confirmed`, `contact` (who gets asked; defaults to ticket reporter),
`codebase` (`Codebase` or None), `code_context` (selected file contents), `suspected_files`
(`[{path, reason, confidence}]`), `related_findings` (list[str]: unreported bugs seen in the code).

### Nodes

- **lookup_codebase** — entry point. No-op (`code_context=""`) without a codebase. Otherwise one `SELECT_FILES`
  call over the file tree picks ≤ `MAX_FILES` files (paths not in `cb.files` are dropped); their contents become
  `code_context`, the picks become `suspected_files`.
- **analyze** — one LLM call over ticket text + `code_context` + previous understanding + prior questions (with answers /
  assumptions, so the model never re-asks them) + transcript, with `max_questions` = 4 on round 0, 2 after.
  Returns a fresh `understanding`, `related_findings` (kept from the previous pass if this one returns none) and the
  *still-open* questions (each may carry `context` = "path · function"). Already-answered questions are kept; new ones
  are renumbered from `_max_qid(prev) + 1` (highest id ever used) so ids never collide.
- **ask_human** (closure over `channel`) — bumps `asked_count` on every open question, then the LLM drafts ONE message;
  the prompt gets the transcript plus a `round_note` (`ROUND_NOTE_FIRST`: open with a 1–2 sentence read of the ticket;
  `ROUND_NOTE_LATER`: no summary, acknowledge answers, ask what's left) → `channel.ask()` blocks for the human reply.
  If `_is_blanket_defer(reply)` — starts with `confirm` / `go ahead` / `proceed` / `your call` / `use your judgement`
  (`DEFER_RE`), is under `MAX_DEFER_WORDS` (8) words, AND has no option/question reference (`QREF_RE`: `(b)`, `option`,
  `question`, `Q2`, `1)`) — every open question becomes `DEFERRED` and `INGEST_ANSWER` is skipped. Otherwise the LLM
  ingests the reply and fills `Question.answer` / `answered_by="human"`; ids in `deferred_to_agent` become `DEFERRED`.
  **Ask-once rule:** anything still open afterwards becomes `ASKED_NO_ANSWER` (`answered_by="assumption"`) and is never
  re-asked. Appends agent + human turns (plus an `(additional info)` turn if the reply had unasked facts). `round += 1`.
- **build_brief** — *every* still-open question becomes an assumption (`NEVER_ASKED` if `asked_count == 0`, else
  `ASKED_NO_ANSWER`) so the brief never carries `answer: null`; then one LLM call produces JSON matching
  `TaskBrief.model_json_schema()`, given `suspected_files` and `related_findings` as inputs. `ticket_id`,
  `clarification_contact`, `resolved_questions` are filled in code; `suspected_files` / `related_findings` fall back
  to the state values if the model drops them, and are shape-normalised.
- **confirm** (closure over `channel`) — LLM writes a sign-off message from the brief (mentions `related_findings` as
  "also noticed, not in scope unless you want it") → `channel.ask()`. Approval is a regex on the reply start:
  `confirm|confirmed|yes|lgtm|looks good|approved`. `round += 1`.

### Routing

- `after_analyze` → `ask_human` iff there are open questions AND `round < max_rounds - 1` (the last round is
  reserved for sign-off) AND (some open question is `blocking` OR this is round 0). So round 0 asks about
  everything open; later rounds only ask about new blocking questions. Otherwise → `build_brief`.
  With the default `max_rounds=3` that is at most 2 clarification rounds.
- `after_confirm` → `END` if confirmed or `round >= max_rounds + 1`; else back to `analyze`
  (the human's change request is already in the transcript, so analyze picks it up).
- `round` counts both ask_human and confirm turns. `recursion_limit=50` on invoke.

### LLM plumbing (graph.py)

- `ChatAnthropic(model=MODEL, temperature=0, max_tokens=4000)`; system prompt + single user message
  per call. No tool use / structured-output API — JSON is requested in the prompt.
- `_call()` does tolerant extraction (`_json`: strips ``` fences, takes outermost `{...}`) and retries
  up to 2× with a "previous output was not valid JSON" nudge before raising `RuntimeError`.
- `ask_human` and `confirm` call `_llm().invoke` directly (free-text message, not JSON).
- `_str_list()` coerces model-provided lists (bare string, dicts) into `list[str]`.

### Channels

All channels implement `ask(key, contact, message) -> str` and are injected at `build_graph` time,
so swapping the human-in-the-loop transport never touches the graph.
- `TerminalChannel` — prints the message, reads stdin until a blank line.
- `ScriptedChannel(replies)` — pops canned replies; returns `"confirm"` once exhausted; records `.sent`.
- `JiraCommentChannel(client, agent_name, poll_seconds=15, timeout_seconds=3600)` — posts
  `@{contact} {message}` as a comment, then polls `list_comments` for new comments not authored by
  `agent_name`; raises `TimeoutError` after the deadline.

### Quorum backend (`quorum_backend/server.py`)

`python -m uvicorn quorum_backend.server:app --port 8000` from the repo root. Routes: `GET/POST /api/tickets`,
`GET /api/tickets/{key}`, `POST …/comments {author, body}`, `POST …/status/{status}`, `POST …/solve`; `/` and
static files come from `QUORUM_UI_DIR` (default `../Quorum`, the `wire-intake-agent` checkout whose `app.js` is
wired to these routes and polls every 3 s). `/solve` flips status `Ready → Clarifying` and starts `_run_agent` in a
daemon thread, which calls `run(ticket, JiraCommentChannel(FakeJiraClient("http://127.0.0.1:8000"), poll 2 s), repo=TICKET_AGENT_REPO)`
— i.e. the agent talks to its own server over HTTP, posting questions as comments by `JIRA_AGENT_NAME` and waiting for
any other author's comment. On success: status `Brief ready`, `brief` (dict) + `brief_md` (`to_markdown()`) stored on
the ticket and the markdown posted as a final comment; on exception: status `Agent error` with the message.
Ticket keys are `QT-001`, `QT-002`, …

### Data conventions

- `Ticket.as_text()` renders comments oldest-first and the prompts state that later comments
  override earlier text — comments are treated as the most current truth.
- `Question.answered_by` ∈ `"ticket" | "human" | "assumption"`; assumption answers use the canonical texts
  `DEFERRED` / `ASKED_NO_ANSWER` / `NEVER_ASKED` from `graph.py`. `Question.asked_count` is 0 or 1 in practice.
- `TaskBrief.confidence` (0–1): prompt says 0.9+ only when acceptance criteria are explicit and every blocking
  question is answered or settled by the code. `evidence` cites sources like `"comment #2 by Dan"` or
  `"auth/tokens.py validate_reset_token()"`.
- `TaskBrief.related_findings`: bugs the model saw in the code that the ticket did not report — suggested
  follow-ups only, never acceptance criteria/questions unless the reporter pulls them in. "Fix it properly" means
  fix the reported bug properly, not widen scope.
- `Question` objects in state are mutated in place by `ask_human` / `build_brief`.

### Prompt rules worth knowing (prompts.py)

- `ANALYZE`: with code, questions must cite what was found and ask only what the code can't tell you; no re-asking
  answered or assumption-marked questions; no housekeeping questions (assignee, sprint, follow-up tickets); `text` is a
  clean question only (rationale goes in `why`); rationale questions are never blocking; max 4 on round 0, 2 after;
  scope-discipline block (unreported bugs → `related_findings`).
- `BUILD_BRIEF`: scope = reported problem + what the reporter added; `related_findings` never become AC/constraints/
  assumptions; assumptions implementation-relevant only; every list item 1–2 sentences; `affected_areas` name concrete
  files/functions when a codebase was available.

## Gotchas

- Blanket deferral is deliberately conservative: "Proceed with option (b), and for Q2 the answer is X"
  is ingested (option/question refs), and so is any reply of 8+ words, even if it starts with "go ahead".
  Partial deferrals ("Q2 is your call") are handled by `INGEST_ANSWER`'s `deferred_to_agent`.
- `examples/scripted_demo.py`'s canned replies are numbered against one particular set of model
  questions; wording/order varies run to run, so a reply can land on a different question than
  intended. Its second reply `"confirm"` blanket-defers whatever is open in round 2, and the
  sign-off comes from `ScriptedChannel`'s exhausted-queue fallback `"confirm"`.
- When driving `TerminalChannel` from piped stdin, supply spare `confirm` lines: a second clarification round
  consumes one, and `input()` raises `EOFError` once stdin is exhausted.
- `Codebase.open("owner/name")` hits the GitHub API (needs network; `GITHUB_TOKEN` avoids rate limits); a local
  path never does. `IGNORE_DIRS` / `CODE_EXT` in `codebase.py` decide what is listed.
- The Quorum backend must run on **port 8000**: `_run_agent` hard-codes `FakeJiraClient(base_url="http://127.0.0.1:8000")`
  and the UI's `app.js` falls back to `http://localhost:8000` when served from any other port. Run it as a single
  process (the store is a JSON file guarded by an in-process lock).

## Planned (from README, not implemented)

Slack channel; `reopen(brief, question)` entry point.
