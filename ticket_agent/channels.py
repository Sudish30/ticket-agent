"""Where the agent talks to the human. Swap implementations without touching the graph."""
from __future__ import annotations

import time
from typing import Protocol

from .jira_client import JiraClient


class Channel(Protocol):
    def ask(self, key: str, contact: str, message: str) -> str: ...


class TerminalChannel:
    """Demo mode: a teammate plays the reporter at the keyboard."""

    def ask(self, key: str, contact: str, message: str) -> str:
        print(f"\n┌─ Agent → {contact} ({key}) " + "─" * 40)
        print(message)
        print("└" + "─" * 60)
        print(f"[{contact}] reply (end with an empty line):")
        lines = []
        while True:
            line = input()
            if line.strip() == "" and lines:
                break
            lines.append(line)
        return "\n".join(lines)


class ScriptedChannel:
    """Tests: canned replies in order."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.sent: list[str] = []

    def ask(self, key: str, contact: str, message: str) -> str:
        self.sent.append(message)
        return self.replies.pop(0) if self.replies else "confirm"


class JiraCommentChannel:
    """Posts the question as a Jira comment and polls for a new comment from anyone but the agent."""

    def __init__(self, client: JiraClient, agent_name: str, poll_seconds: int = 15, timeout_seconds: int = 3600):
        self.client = client
        self.agent_name = agent_name
        self.poll = poll_seconds
        self.timeout = timeout_seconds

    def ask(self, key: str, contact: str, message: str) -> str:
        before = len(self.client.list_comments(key))
        self.client.add_comment(key, f"@{contact} {message}")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.poll)
            comments = self.client.list_comments(key)
            new = [c for c in comments[before + 1:] if c.author != self.agent_name]
            if new:
                return "\n".join(c.body for c in new)
        raise TimeoutError(f"No reply on {key} within {self.timeout}s")
