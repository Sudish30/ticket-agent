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


class ReviewCheck(BaseModel):
    name: str
    result: Literal["pass", "fail", "warn"] = "warn"
    note: str = ""


class ChangeRequest(BaseModel):
    file: str = ""
    issue: str = ""
    suggestion: str = ""
    severity: Literal["blocker", "minor"] = "minor"


class Review(BaseModel):
    """The mandatory final review gate's output. verdict is normalized from the blockers
    (minors-only → approve with follow-ups); rounds counts review passes (2 after a repair round)."""

    verdict: Literal["approve", "request_changes"]
    checks: list[ReviewCheck] = []
    change_requests: list[ChangeRequest] = []
    rounds: int = 1
    probe_log: list[dict] = []   # sandbox commands the reviewer ran in its scratch copy (incl. the revert-check)


class PRPackage(BaseModel):
    """Output contract with the downstream shipper: everything needed to open the PR.

    status: "complete" — every subtask accepted and the review approved; "partial" — some accepted, some
    failed/skipped (honest per-subtask statuses); "failed" — nothing was accepted;
    "needs_human_review" — the review gate still has blocking change requests after the one repair round.
    """

    ticket_id: str
    status: Literal["complete", "partial", "failed", "needs_human_review"]
    subtasks: list[SubtaskReport] = []
    combined_diff: str = ""
    files_changed: list[str] = []
    tests_passed: int = 0
    tests_failed: int = 0
    new_tests_added: list[str] = []
    pr_title: str = ""
    pr_description: str = ""
    review: Optional[Review] = None
    investigation: list[dict] = []   # per code_writer subtask: {subtask, reproduced, observed_error, evidence, commands}
    duration_seconds: float = 0.0

    def to_markdown(self) -> str:
        """The PR package as Markdown: written to pr_package.md next to pr_package.json."""
        icon = {"complete": "✅", "partial": "🟡", "failed": "❌",
                "needs_human_review": "🛑"}.get(self.status, "❌")
        out = [
            f"# {self.pr_title or self.ticket_id}",
            "",
            f"_{icon} {self.status} · {self.tests_passed} passed · {self.tests_failed} failed · "
            f"{len(self.new_tests_added)} new test(s) · {self.duration_seconds:.0f}s_",
            "",
            self.pr_description or "_No description._",
        ]
        if self.review:
            ricon = {"approve": "✅", "request_changes": "🛑"}.get(self.review.verdict, "🛑")
            out += ["", "## Review", f"_{ricon} {self.review.verdict} · {self.review.rounds} round(s)_", ""]
            if self.review.checks:
                out += ["| check | result | note |", "|---|---|---|"]
                ci = {"pass": "✅", "fail": "❌", "warn": "⚠️"}
                for c in self.review.checks:
                    note = c.note.replace("|", " ").replace(chr(10), " ")[:300]
                    out.append(f"| {c.name} | {ci.get(c.result, '')} {c.result} | {note} |")
            blockers = [c for c in self.review.change_requests if c.severity == "blocker"]
            minors = [c for c in self.review.change_requests if c.severity == "minor"]
            if blockers:
                out += ["", "### Change requests (blocking)",
                        *[f"- `{c.file or '—'}`: {c.issue} — _{c.suggestion}_" for c in blockers]]
            if minors:
                out += ["", "### Follow-ups (minor)",
                        *[f"- `{c.file or '—'}`: {c.issue} — _{c.suggestion}_" for c in minors]]
            if self.review.probe_log:
                out += ["", f"<details><summary>Reviewer probes ({len(self.review.probe_log)} command(s))</summary>",
                        "", "```"]
                for r in self.review.probe_log:
                    out.append(f"$ {r.get('cmd')}  -> exit {r.get('exit_code')}")
                    tail = str(r.get("stdout") or r.get("stderr") or "").strip().splitlines()
                    out += [f"    {ln}" for ln in tail[-3:]]
                out += ["```", "", "</details>"]
        if self.investigation:
            out += ["", "## Investigation"]
            for inv in self.investigation:
                cmds = inv.get("commands") or []
                out += ["", f"**{inv.get('subtask', '?')}** — reproduced: {inv.get('reproduced', 'no')} · "
                            f"{inv.get('evidence', '') or '(no findings)'}"]
                if inv.get("observed_error"):
                    out.append(f"Observed: {str(inv['observed_error'])[:300]}")
                if cmds:
                    out += ["", f"<details><summary>Command log ({len(cmds)} command(s))</summary>", "", "```"]
                    for r in cmds:
                        out.append(f"[{r.get('phase', '')}] $ {r.get('cmd')}  -> exit {r.get('exit_code')}")
                        tail = str(r.get("stdout") or r.get("stderr") or "").strip().splitlines()
                        out += [f"    {ln}" for ln in tail[-3:]]
                    out += ["```", "", "</details>"]
        out += [
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
    attempt_snapshot: str        # pre-dispatch workspace copy; evaluate restores it when the attempt is rejected
    replans: int
    max_retries: int             # retries per subtask after its first attempt (default 2)
    max_replans: int             # whole-plan replans (default 1)
    replan_requested: bool
    pending_verification: list   # code subtask ids accepted as applied_unverified, awaiting a passing regression test
    repo_baseline: list          # tests already failing on the untouched repo (computed once in assemble_pr)
    failing_now: list            # tests failing in the workspace at the last assembly (exact ids, for the reviewer)
    review: dict                 # raw reviewer output of the latest review round (normalized in finalize)
    review_rounds: int           # review passes run (1, or 2 after the repair round)
    repair_done: bool            # the single repair round has been spent (or is pointless — reviewer crashed)
    started: float
    pr: Optional[PRPackage]
