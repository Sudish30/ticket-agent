"""Data models for the Solver Agent. The input contract (TaskBrief) lives in ticket_agent.schemas — not duplicated."""
from __future__ import annotations

from typing import Literal, Optional, TypedDict

from pydantic import BaseModel, Field

from ticket_agent.schemas import TaskBrief


class Edit(BaseModel):
    path: str
    old_str: str = Field(description="Exact text copied from the file; must occur exactly once in it.")
    new_str: str
    reason: str = ""


class Solution(BaseModel):
    """Output contract with downstream agents (reviewer / shipper).

    status: "passed" — verified: fixed ≥1 baseline failure or ≥1 newly-added test passes, with zero new failures.
    "applied_unverified" — patch applies cleanly, zero new failures, but nothing flipped green and no new test
    verifies it (the rationale says so). "failed" — new failures or patch errors after all retries.
    """

    ticket_id: str
    status: Literal["passed", "applied_unverified", "failed"]
    attempts: int
    edits: list[Edit] = []
    diff: str = Field(default="", description="Unified diff of the applied edits, generated from the temp workdir.")
    files_changed: list[str] = []
    tests_passed: int = 0
    tests_failed: int = 0
    test_output_tail: str = Field(default="", description="Last 30 lines of the final pytest run (or the patch error).")
    rationale: str = ""
    duration_seconds: float = 0.0

    def to_markdown(self) -> str:
        """The solution as Markdown: written to solution.md next to solution.json."""
        out = [
            f"# {self.ticket_id} — Solution ({self.status}, "
            f"{self.attempts} attempt{'' if self.attempts == 1 else 's'}, {self.duration_seconds:.0f}s)",
            "",
            f"_Tests: {self.tests_passed} passed · {self.tests_failed} failed_",
            "",
            "## Rationale",
            self.rationale or "_None._",
            "",
            "## Files changed",
            *([f"- `{p}`" for p in self.files_changed] or ["_None (no edits were applied)._"]),
            "",
            "## Edits",
        ]
        if self.edits:
            out += ["| Path | Reason |", "|---|---|"]
            out += [f"| `{e.path.replace('|', ' ')}` | {e.reason.replace('|', ' ').replace(chr(10), ' ')} |"
                    for e in self.edits]
        else:
            out.append("_None._")
        out += [
            "", "## Diff", "```diff", self.diff.rstrip() or "(empty)", "```",
            "", "## Test output (tail)", "```", self.test_output_tail.rstrip() or "(none)", "```",
        ]
        return "\n".join(out) + "\n"


class SolverState(TypedDict, total=False):
    brief_raw: dict               # graph input; validated by load_brief
    brief: TaskBrief
    repo: str                     # local repository directory being fixed
    workspace: str                # orchestrator mode: existing shared dir patched in place (else absent)
    src: str                      # source tree for reads/baseline/diffs: workspace if given, else repo
    snapshot: str                 # workspace mode: pre-solver copy; every attempt resets the workspace to it
    task_instruction: str         # orchestrator note appended to the plan prompt (scopes the brief)
    codebase: object              # ticket_agent.codebase.Codebase over repo
    files: dict[str, str]         # path -> full content: suspected files + their local imports (capped)
    plan: dict                    # {diagnosis, changes[], risks[]}
    edits: list[Edit]             # current attempt's full edit set (applied to a pristine copy)
    attempts: int
    max_attempts: int
    workdir: str                  # temp copy of the repo with the current edits applied
    patch_error: str              # non-empty if the current edit set could not be applied
    baseline_failed: list[str]    # test ids failing on the pristine repo (computed once, in read_files)
    baseline_counts: dict         # pristine-run counts {passed, failed, error} — detects newly-added tests
    outcome: str                  # per-attempt verdict: "passed" | "applied_unverified" | "retry"
    test_output: str
    tests_passed: int
    tests_failed: int
    fixed_tests: list[str]
    new_failures: list[str]
    new_tests_passed: int         # tests the patch added that pass (credit for self-written regression tests)
    remaining_failures: list[str] # pre-existing failures the patch (correctly) leaves alone
    history: list[dict]           # one {edits, result} entry per attempt, fed back into retries
    started: float
    solution: Optional[Solution]
