"""Quorum backend: ticket store + comment thread + agent runner. Serves the Quorum UI too.

    python -m uvicorn quorum_backend.server:app --port 8000
    open http://localhost:8000

Creating a ticket starts the intake agent immediately (status "Clarifying"); POST /solve is a manual retry.
Once a brief is confirmed ("Brief ready"), POST /solve-brief runs the orchestrator (stage 3) on it in a
background thread: status "Solving" → "PR ready" (package complete) or "Needs human review" (anything else),
with pr_package + pr_package_md stored on the ticket and the PR markdown posted as a comment.

Env: ANTHROPIC_API_KEY (required), TICKET_AGENT_REPO (default: demo_repo), QUORUM_UI_DIR (default: the bundled
ui/ folder — or the Quorum repo root when this code lives in Quorum/agent/; point it at a live Quorum checkout
when editing the UI)
"""
from __future__ import annotations

import json, os, threading, traceback
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from orchestrator import run as run_orchestrator
from ticket_agent import run
from ticket_agent.channels import JiraCommentChannel
from ticket_agent.jira_client import FakeJiraClient
from ticket_agent.schemas import Ticket

AGENT_NAME = os.environ.get("JIRA_AGENT_NAME", "Ticket Agent")
_HOME = Path(__file__).resolve().parents[1]          # folder holding quorum_backend/: Quorum/agent/ or ticket-agent/
REPO = os.environ.get("TICKET_AGENT_REPO", str(_HOME / "demo_repo"))
# Vendored inside the Quorum repo (Quorum/agent/quorum_backend/), the UI is the repo root one level up from agent/;
# standalone, it is the wired copy bundled at ui/ — so a fresh clone serves the full web app with zero extra setup.
_DEFAULT_UI = _HOME.parent if (_HOME.parent / "index.html").is_file() else _HOME / "ui"
UI_DIR = Path(os.environ.get("QUORUM_UI_DIR", _DEFAULT_UI))
DB = Path(__file__).parent / "tickets.json"
_lock = threading.RLock()

app = FastAPI(title="Quorum backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------- storage ----------------

def _load() -> dict:
    return json.loads(DB.read_text()) if DB.exists() else {"next": 1, "tickets": {}}

def _save(d: dict) -> None:
    DB.write_text(json.dumps(d, indent=2))

def _get(key: str) -> dict:
    t = _load()["tickets"].get(key)
    if not t:
        raise HTTPException(404, f"no ticket {key}")
    return t

def _update(key: str, **fields) -> dict:
    with _lock:
        d = _load(); t = d["tickets"].get(key)
        if not t:
            raise HTTPException(404, f"no ticket {key}")
        t.update(fields); _save(d); return t


class NewTicket(BaseModel):
    title: str
    description: str = ""
    reporter: str = "Reporter"
    repository: str = ""
    issue_type: str = "Bug"
    priority: str = "Medium"

class NewComment(BaseModel):
    author: str
    body: str


# ---------------- ticket API (Jira-shaped enough for FakeJiraClient) ----------------

@app.get("/api/tickets")
def list_tickets():
    return sorted(_load()["tickets"].values(), key=lambda t: t["created"], reverse=True)

@app.post("/api/tickets")
def create_ticket(t: NewTicket):
    with _lock:
        d = _load(); key = f"QT-{d['next']:03d}"; d["next"] += 1
        d["tickets"][key] = {
            "key": key, "summary": t.title, "description": t.description, "reporter": t.reporter,
            "repository": t.repository, "issue_type": t.issue_type, "priority": t.priority,
            "assignee": "", "labels": [], "components": [], "linked_issues": [], "epic": "",
            "status": "Clarifying", "created": datetime.now().isoformat(timespec="seconds"),
            "comments": [], "brief": None, "brief_md": "", "error": "",
            "pr_package": None, "pr_package_md": "",
        }
        _save(d)
    # The intake agent starts automatically on creation; /solve stays as a manual retry after "Agent error".
    _start_agent(key)
    return _get(key)

@app.get("/api/tickets/{key}")
def get_ticket(key: str):
    return _get(key)

@app.post("/api/tickets/{key}/comments")
def add_comment(key: str, c: NewComment):
    with _lock:
        d = _load(); t = d["tickets"].get(key)
        if not t:
            raise HTTPException(404)
        t["comments"].append({"author": c.author, "body": c.body, "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        _save(d); return t["comments"][-1]

@app.post("/api/tickets/{key}/status/{status}")
def set_status(key: str, status: str):
    return _update(key, status=status)


# ---------------- agent runner ----------------

def _run_agent(key: str) -> None:
    try:
        client = FakeJiraClient(base_url="http://127.0.0.1:8000")
        ticket = Ticket.model_validate(_get(key))
        channel = JiraCommentChannel(client, AGENT_NAME, poll_seconds=2, timeout_seconds=3600)
        brief = run(ticket, channel, repo=REPO if os.path.isdir(REPO) else REPO or None)
        md = brief.to_markdown()
        _update(key, status="Brief ready", brief=brief.model_dump(), brief_md=md)
        client.add_comment(key, md, author=AGENT_NAME)
    except Exception as e:  # surface failures in the UI instead of dying silently
        traceback.print_exc()
        _update(key, status="Agent error", error=f"{type(e).__name__}: {e}")

def _start_agent(key: str) -> None:
    threading.Thread(target=_run_agent, args=(key,), daemon=True).start()

@app.post("/api/tickets/{key}/solve")
def solve(key: str):
    """Manual retry. New tickets start the agent automatically; this re-runs it after "Agent error"
    (or for legacy tickets still in "Ready")."""
    t = _get(key)
    if t["status"] not in ("Ready", "Agent error"):
        raise HTTPException(409, f"ticket is {t['status']}")
    _update(key, status="Clarifying", error="")
    _start_agent(key)
    return {"ok": True, "status": "Clarifying"}


# ---------------- orchestrator runner (stage 3) ----------------

def _run_orchestrator(key: str) -> None:
    try:
        brief = _get(key).get("brief")
        if not brief:
            raise ValueError("ticket has no stored brief")
        pkg = run_orchestrator(brief, REPO)
        md = pkg.to_markdown()
        status = "PR ready" if pkg.status == "complete" else "Needs human review"
        _update(key, status=status, pr_package=pkg.model_dump(), pr_package_md=md)
        FakeJiraClient(base_url="http://127.0.0.1:8000").add_comment(key, md, author=AGENT_NAME)
    except Exception as e:  # surface failures in the UI instead of dying silently
        traceback.print_exc()
        _update(key, status="Agent error", error=f"{type(e).__name__}: {e}")


@app.post("/api/tickets/{key}/solve-brief")
def solve_brief(key: str):
    """Engineer's "Start solving": runs the orchestrator on the ticket's stored brief in a background
    thread. Valid from "Brief ready" (or as a retry from "Agent error" when a brief already exists)."""
    t = _get(key)
    if not (t["status"] == "Brief ready" or (t["status"] == "Agent error" and t.get("brief"))):
        raise HTTPException(409, f"ticket is {t['status']}" + ("" if t.get("brief") else " and has no brief"))
    _update(key, status="Solving", error="")
    threading.Thread(target=_run_orchestrator, args=(key,), daemon=True).start()
    return {"ok": True, "status": "Solving"}


# ---------------- serve the Quorum UI ----------------

if UI_DIR.is_dir():
    @app.get("/")
    def index():
        return FileResponse(UI_DIR / "index.html")
    app.mount("/", StaticFiles(directory=UI_DIR), name="ui")
