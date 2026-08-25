"""Worker registry: name -> callable. Adding a worker (e.g. docs_writer) is one @register-decorated function.

Every worker is a callable(ctx) -> result dict, where ctx is:
  {"brief": TaskBrief, "repo": str, "workspace": str, "instruction": str, "feedback": str,
   "previous_result": result | None (this subtask's own last attempt, on retries),
   "review": dict | None (the latest review-gate output, present only after a review has run),
   "upstream": {dep_subtask_id: result}, "all_results": {subtask_id: result}}
and the result dict carries at least {"status": "passed" | "applied_unverified" | "failed", "summary": str}
plus worker-specific keys (diff, files_changed, tests_added, ...). Workers patch the SHARED workspace in
place and must leave it unchanged when they fail (the solver and test_writer both restore on failure).
"""
from __future__ import annotations

from typing import Callable

from solver_agent.graph import run as solver_run

WORKERS: dict[str, Callable[[dict], dict]] = {}
DESCRIPTIONS: dict[str, str] = {}


def register(name: str, description: str):
    """Decorator: `@register("docs_writer", "writes docs for ...")` adds the worker to the registry."""
    def deco(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        WORKERS[name] = fn
        DESCRIPTIONS[name] = description
        return fn
    return deco


def registry_text() -> str:
    """The registry as prompt text for plan_subtasks."""
    return "\n".join(f"- {n}: {d}" for n, d in DESCRIPTIONS.items())


@register("code_writer",
          "changes application code per the brief (wraps the solver agent): plans a fix, applies exact string "
          "edits to the shared workspace, runs pytest. Reports passed (verified by tests), applied_unverified "
          "(clean patch that no test verifies), or failed. Does not write new tests.")
def code_writer(ctx: dict) -> dict:
    instruction = ctx.get("instruction", "")
    if ctx.get("feedback"):
        instruction += "\n\nFeedback on your previous rejected attempt (address it): " + ctx["feedback"]
    solution = solver_run(ctx["brief"], ctx["repo"], task_instruction=instruction, workspace=ctx["workspace"])
    out = solution.model_dump()
    out["summary"] = f"{solution.status}: {solution.rationale}"[:300]
    return out


# test_writer registers itself on import — see orchestrator/workers/test_writer.py (imported by graph.py).
