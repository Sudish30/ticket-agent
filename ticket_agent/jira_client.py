"""Fetch tickets from Jira Cloud, or from local mock JSON files."""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from .schemas import Comment, Ticket


# ---------- ADF (Atlassian Document Format) -> plain text ----------

def adf_to_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "mention":
        return "@" + node.get("attrs", {}).get("text", "").lstrip("@")
    if t == "hardBreak":
        return "\n"
    children = "".join(adf_to_text(c) for c in node.get("content", []))
    if t in {"paragraph", "heading", "listItem", "codeBlock", "blockquote"}:
        return children + "\n"
    if t in {"bulletList", "orderedList"}:
        return "".join("- " + adf_to_text(c).rstrip("\n") + "\n" for c in node.get("content", []))
    return children


# ---------- Real Jira ----------

class JiraClient:
    def __init__(self, base_url: str | None = None, email: str | None = None, token: str | None = None):
        self.base = (base_url or os.environ["JIRA_BASE_URL"]).rstrip("/")
        self.auth = (email or os.environ["JIRA_EMAIL"], token or os.environ["JIRA_API_TOKEN"])

    def _get(self, path: str, **params):
        r = requests.get(f"{self.base}/rest/api/3{path}", auth=self.auth, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict):
        r = requests.post(f"{self.base}/rest/api/3{path}", auth=self.auth, json=payload, timeout=30)
        r.raise_for_status()
        return r.json() if r.text else {}

    def get_ticket(self, key: str) -> Ticket:
        data = self._get(f"/issue/{key}")
        f = data["fields"]
        comments = [
            Comment(
                author=c["author"]["displayName"],
                body=adf_to_text(c.get("body")).strip(),
                created=c.get("created", ""),
            )
            for c in f.get("comment", {}).get("comments", [])
        ]
        links = []
        for l in f.get("issuelinks", []):
            other = l.get("outwardIssue") or l.get("inwardIssue")
            if other:
                links.append(other["key"])
        return Ticket(
            key=data["key"],
            summary=f.get("summary", ""),
            description=adf_to_text(f.get("description")).strip(),
            issue_type=f.get("issuetype", {}).get("name", "Task"),
            priority=(f.get("priority") or {}).get("name", ""),
            labels=f.get("labels", []),
            components=[c["name"] for c in f.get("components", [])],
            reporter=(f.get("reporter") or {}).get("displayName", ""),
            assignee=(f.get("assignee") or {}).get("displayName", ""),
            linked_issues=links,
            epic=(f.get("parent") or {}).get("key", ""),
            comments=comments,
        )

    def add_comment(self, key: str, text: str) -> dict:
        body = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.split("\n") if line] or
                       [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }
        return self._post(f"/issue/{key}/comment", {"body": body})

    def list_comments(self, key: str) -> list[Comment]:
        return self.get_ticket(key).comments

    def transition(self, key: str, status_name: str) -> None:
        trans = self._get(f"/issue/{key}/transitions")["transitions"]
        match = next((t for t in trans if t["name"].lower() == status_name.lower()), None)
        if not match:
            raise ValueError(f"No transition named {status_name!r}; available: {[t['name'] for t in trans]}")
        self._post(f"/issue/{key}/transitions", {"transition": {"id": match["id"]}})


# ---------- Quorum backend (quorum_backend/server.py) ----------

class FakeJiraClient:
    """Duck-typed twin of JiraClient for the Quorum backend's flat ticket API.

    Routes: GET /api/tickets/{key} (flat ticket dict incl. comments[{author, body, created}]),
    POST /api/tickets/{key}/comments {author, body}, POST /api/tickets/{key}/status/{status}.
    Comments the agent posts are stamped with ``agent_name`` so JiraCommentChannel can skip them.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", agent_name: str | None = None):
        self.base = base_url.rstrip("/")
        self.agent_name = agent_name or os.environ.get("JIRA_AGENT_NAME", "Ticket Agent")

    def _url(self, path: str) -> str:
        return f"{self.base}/api/tickets{path}"

    def get_ticket(self, key: str) -> Ticket:
        r = requests.get(self._url(f"/{key}"), timeout=30)
        r.raise_for_status()
        return Ticket.model_validate(r.json())  # extra backend keys (status, brief, ...) are ignored

    def list_comments(self, key: str) -> list[Comment]:
        return self.get_ticket(key).comments

    def add_comment(self, key: str, text: str, author: str | None = None) -> dict:
        r = requests.post(self._url(f"/{key}/comments"), json={"author": author or self.agent_name, "body": text}, timeout=30)
        r.raise_for_status()
        return r.json() if r.text else {}

    def transition(self, key: str, status_name: str) -> None:
        r = requests.post(self._url(f"/{key}/status/{status_name}"), timeout=30)
        r.raise_for_status()


# ---------- Mock ----------

def load_mock_ticket(path: str | Path) -> Ticket:
    return Ticket.model_validate(json.loads(Path(path).read_text()))
