"""Data models for the Orchestrator. TaskBrief comes from ticket_agent.schemas — not duplicated."""
from __future__ import annotations

from typing import Literal, Optional, TypedDict

from pydantic import BaseModel

from ticket_agent.schemas import TaskBrief


class Subtask(BaseModel):
    """One unit of planned work, dispatched to a registered worker. Mutated in place by the graph."""

    id: str
    worker: str
    instruction: str
    depends_on: list[str] = []
    rationale: str = ""
    status: Literal["pending", "accepted", "failed", "skipped"] = "pending"
    attempts: int = 0
    summary: str = ""


class SubtaskReport(BaseModel):
    """The subtask fields that go into the PRPackage (rationale/depends_on stay internal)."""

    id: str
    worker: str
    instruction: str
    status: str
    attempts: int
    summary: str


class PRPackage(BaseModel):
    """Output contract with the downstream reviewer/shipper: everything needed to open the PR.

    status: "complete" — every subtask accepted; "partial" — some accepted, some failed/skipped
    (honest per-subtask statuses); "failed" — nothing was accepted.
    """

    ticket_id: str
    status: Literal["complete", "partial", "failed"]
    subtasks: list[SubtaskReport] = []
    combined_diff: str = ""
    files_changed: list[str] = []
    tests_passed: int = 0
    tests_failed: int = 0
    new_tests_added: list[str] = []
    pr_title: str = ""
    pr_description: str = ""
    duration_seconds: float = 0.0

    def to_markdown(self) -> str:
        """The PR package as Markdown: written to pr_package.md next to pr_package.json."""
        icon = {"complete": "✅", "partial": "🟡", "failed": "❌"}.get(self.status, "❌")
        out = [
            f"# {self.pr_title or self.ticket_id}",
            "",
            f"_{icon} {self.status} · {self.tests_passed} passed · {self.tests_failed} failed · "
            f"{len(self.new_tests_added)} new test(s) · {self.duration_seconds:.0f}s_",
            "",
            self.pr_description or "_No description._",
            "",
            "## Subtasks",
            "| id | worker | status | attempts | summary |",
            "|---|---|---|---|---|",
        ]
        for s in self.subtasks:
            summary = s.summary.replace("|", " ").replace(chr(10), " ")[:200]
            out.append(f"| {s.id} | {s.worker} | {s.status} | {s.attempts} | {summary} |")
        out += ["", "## Files changed", *([f"- `{p}`" for p in self.files_changed] or ["_None._"])]
        if self.new_tests_added:
            out += ["", "## New tests", *[f"- `{t}`" for t in self.new_tests_added]]
        out += ["", "## Combined diff", "```diff", self.combined_diff.rstrip() or "(empty)", "```"]
        return "\n".join(out) + "\n"


class OrchestratorState(TypedDict, total=False):
    brief_raw: dict              # graph input; validated by load_brief
    brief: TaskBrief
    repo: str                    # original repository (never touched)
    workspace: str               # THE shared temp copy: workers patch it, every pytest runs in it
    plan_json: list              # the plan as planned (plus orchestrator-appended verification subtasks)
    subtasks: list               # list[Subtask], mutated in place
    results: dict                # subtask id -> the worker's latest output dict
    feedback: dict               # subtask id -> evaluator feedback for the next retry
    current_id: str              # subtask just dispatched ("" when nothing was runnable)
    replans: int
    max_retries: int             # retries per subtask after its first attempt (default 2)
    max_replans: int             # whole-plan replans (default 1)
    replan_requested: bool
    pending_verification: list   # code subtask ids accepted as applied_unverified, awaiting a passing regression test
    started: float
    pr: Optional[PRPackage]
