"""reviewer worker: the mandatory final review gate.

Registered like any worker but never planned as a subtask — the graph invokes it after assemble_pr on every
run (and once more after the repair round). It judges INDEPENDENTLY: it sees the brief, the combined diff,
the final test results and the full post-change content of every changed file — never the workers' rationales.
The verdict is normalized from the blockers (blockers are authoritative over the model's raw verdict; a
minors-only review is an approve with follow-ups).
"""
from __future__ import annotations

from ticket_agent.llm import call_json, make_llm

from .. import prompts
from ..registry import register

MAX_DIFF_CHARS = 20000
MAX_FILE_CHARS = 8000


def _llm():
    return make_llm()


def _call(prompt: str, retries: int = 2) -> dict:
    return call_json(prompt, prompts.SYSTEM, retries=retries, llm_factory=_llm)


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "\n[... truncated ...]"


@register("reviewer",
          "the final review gate — runs automatically after all subtasks are resolved; NEVER plan it as a "
          "subtask. Independently checks the combined diff and changed files against the brief's acceptance "
          "criteria, constraints and out_of_scope, plus regressions/security and whether new tests really "
          "assert the ACs; returns approve | request_changes with severity-tagged change requests.")
def reviewer(ctx: dict) -> dict:
    files = ctx.get("files") or {}
    data = _call(prompts.REVIEW.format(
        brief=ctx["brief"].model_dump_json(indent=2),
        tests=f"{ctx.get('tests_passed', 0)} passed, {ctx.get('tests_failed', 0)} failed · "
              f"new tests: {', '.join(ctx.get('new_tests') or []) or '(none)'} · "
              f"already failing BEFORE any change (pre-existing): "
              f"{', '.join(ctx.get('baseline_failed') or []) or '(none)'}",
        diff=_clip(ctx.get("diff") or "(empty)", MAX_DIFF_CHARS),
        files="\n\n".join(f"===== {p} =====\n{_clip(t, MAX_FILE_CHARS)}" for p, t in files.items())
              or "(no files changed)",
    ))
    checks = [c for c in (data.get("checks") or []) if isinstance(c, dict)]
    change_requests = [c for c in (data.get("change_requests") or []) if isinstance(c, dict)]
    blockers = [c for c in change_requests if str(c.get("severity")) == "blocker"]
    verdict = "request_changes" if blockers else "approve"     # blockers are authoritative, not the raw verdict
    return {"status": "reviewed", "verdict": verdict, "checks": checks, "change_requests": change_requests,
            "summary": f"{verdict}: {len(blockers)} blocker(s), {len(change_requests) - len(blockers)} minor(s)"}
