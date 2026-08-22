"""Quorum backend: ticket store + comment thread + agent runner. Serves the Quorum UI too.

    python -m uvicorn quorum_backend.server:app --port 8000
    open http://localhost:8000

Creating a ticket starts the intake agent immediately (status "Clarifying"); POST /solve is a manual retry.

Env: ANTHROPIC_API_KEY (required), TICKET_AGENT_REPO (default: demo_repo), QUORUM_UI_DIR (default: ../Quorum)
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

from ticket_agent import run
from ticket_agent.channels import JiraCommentChannel
from ticket_agent.jira_client import FakeJiraClient
from ticket_agent.schemas import Ticket

AGENT_NAME = os.environ.get("JIRA_AGENT_NAME", "Ticket Agent")
REPO = os.environ.get("TICKET_AGENT_REPO", str(Path(__file__).resolve().parent.parent / "demo_repo"))
UI_DIR = Path(os.environ.get("QUORUM_UI_DIR", Path(__file__).resolve().parent.parent.parent / "Quorum"))
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


# ---------------- serve the Quorum UI ----------------

if UI_DIR.is_dir():
    @app.get("/")
    def index():
        return FileResponse(UI_DIR / "index.html")
    app.mount("/", StaticFiles(directory=UI_DIR), name="ui")
