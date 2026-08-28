"""reviewer worker: the mandatory final review gate — with empirical probing.

Registered like any worker but never planned as a subtask — the graph invokes it after assemble_pr on every
run (and once more after the repair round). It judges INDEPENDENTLY: it sees the brief, the combined diff,
the final test results and the full post-change content of every changed file — never the workers' rationales.

Before rendering its verdict it may run up to MAX_PROBE_CMDS sandbox commands in a SCRATCH COPY of the
workspace (run the suite itself, run one suspect test, write + run its own probe tests — probe files live only
in the scratch copy and never enter the diff). Then, deterministically, the DISCRIMINATION CHECK: the fix is
reverted in the scratch copy and the new tests are re-run — if they FAIL on the reverted code they genuinely
discriminate ("empirically verified"); if they still pass they prove nothing. The result is recorded in the
tests_assert_acs check note. The five checks and the verdict structure are unchanged; blockers stay
authoritative over the model's raw verdict.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from solver_agent.graph import _copy_repo
from ticket_agent.llm import call_json, make_llm
from ticket_agent.sandbox import SandboxError, run_cmd

from .. import prompts
from ..registry import register

MAX_DIFF_CHARS = 20000
MAX_FILE_CHARS = 8000
MAX_PROBE_CMDS = 4       # reviewer-chosen probes; +1 reserved for the deterministic revert-check (≤5 total)


def _llm():
    return make_llm()


def _call(prompt: str, retries: int = 2) -> dict:
    return call_json(prompt, prompts.SYSTEM, retries=retries, llm_factory=_llm)


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "\n[... truncated ...]"


def _tests_line(ctx: dict) -> str:
    return (f"{ctx.get('tests_passed', 0)} passed, {ctx.get('tests_failed', 0)} failed · "
            f"new tests: {', '.join(ctx.get('new_tests') or []) or '(none)'} · "
            f"currently FAILING (exact ids): {', '.join(ctx.get('failing_tests') or []) or '(none)'} · "
            f"already failing BEFORE any change (pre-existing): "
            f"{', '.join(ctx.get('baseline_failed') or []) or '(none)'}")


def _probe_text(log: list[dict]) -> str:
    lines = []
    for r in log:
        tail = (r.get("stdout", "") + ("\n" + r.get("stderr", "")
                                       if str(r.get("stderr", "")).strip() else "")).strip()
        tail = "\n".join(tail.splitlines()[-10:])
        lines.append(f"$ {r.get('cmd')}\n[exit {r.get('exit_code')}]\n{tail}")
    return "\n\n".join(lines) or "(none yet)"


def _probe(ctx: dict, scratch: str, log: list) -> None:
    """Up to MAX_PROBE_CMDS reviewer-chosen commands in the scratch copy; refusals are logged but not run."""
    for _ in range(MAX_PROBE_CMDS):
        data = _call(prompts.REVIEW_PROBE.format(
            brief=ctx["brief"].model_dump_json(indent=2),
            tests=_tests_line(ctx),
            diff=_clip(ctx.get("diff") or "(empty)", MAX_DIFF_CHARS),
            transcript=_probe_text(log),
            remaining=MAX_PROBE_CMDS - len(log)))
        if str(data.get("action")) != "run" or not str(data.get("cmd") or "").strip():
            return
        cmd = str(data["cmd"])
        try:
            run_cmd(scratch, cmd, log=log)
        except SandboxError as e:
            log.append({"cmd": cmd, "stdout": "", "stderr": f"sandbox refused: {e}",
                        "exit_code": -1, "duration": 0.0})


def _discrimination(ctx: dict, scratch: str, log: list) -> str:
    """Revert the fix in the scratch copy, re-run the new tests, confirm they fail (i.e. they discriminate)."""
    new_tests = [t for t in (ctx.get("new_tests") or []) if t]
    if not new_tests:
        return "reasoned (no new tests to verify empirically)"
    reverted = []
    for p in (ctx.get("files") or {}):
        if p.startswith("tests/") or p == "CHANGELOG.md":      # keep the tests; docs don't change behavior
            continue
        src, dst = Path(ctx.get("repo", ""), p), Path(scratch, p)
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)                          # restore the pre-change file
            reverted.append(p)
        elif dst.is_file():
            dst.unlink()                                       # file created by the fix: reverting removes it
            reverted.append(p)
    if not reverted:
        return "reasoned (no non-test code changes to revert)"
    try:
        res = run_cmd(scratch, "python -m pytest -q " + " ".join(new_tests), timeout=120, log=log)
    except SandboxError as e:
        return f"reasoned (revert-check refused: {e})"
    # Tag the logged entry so neither the REVIEW prompt transcript nor the stored probe_log can mistake
    # this reverted-code run for a with-fix probe: a failure HERE is the desired outcome.
    res["cmd"] = "[REVERT-CHECK — failure here means the test discriminates, which is GOOD] " + res["cmd"]
    if res["exit_code"] != 0:
        return (f"empirically verified: with the fix reverted ({', '.join(reverted)}) "
                "the new tests FAIL — they genuinely discriminate")
    return (f"empirical check FAILED: the new tests still PASS with the fix reverted ({', '.join(reverted)}) "
            "— they do not prove the fix")


@register("reviewer",
          "the final review gate — runs automatically after all subtasks are resolved; NEVER plan it as a "
          "subtask. Probes the change empirically in a scratch copy (runs the suite, writes its own probe "
          "tests, reverts the fix to confirm the new tests discriminate), then independently checks the "
          "combined diff and changed files against the brief's acceptance criteria, constraints and "
          "out_of_scope, plus regressions/security and whether new tests really assert the ACs; returns "
          "approve | request_changes with severity-tagged change requests.")
def reviewer(ctx: dict) -> dict:
    files = ctx.get("files") or {}
    probe_log: list = []
    discrimination = ""
    ws = ctx.get("workspace")
    if ws and os.path.isdir(ws):
        scratch_root = tempfile.mkdtemp(prefix="review-scratch-")
        scratch = os.path.join(scratch_root, "repo")
        _copy_repo(ws, scratch)
        try:
            try:
                _probe(ctx, scratch, probe_log)
            except Exception as e:                             # a broken probe loop degrades, never kills the review
                probe_log.append({"cmd": "(probe loop)", "stdout": "", "stderr": f"probe crashed: {e}",
                                  "exit_code": -1, "duration": 0.0})
            try:
                discrimination = _discrimination(ctx, scratch, probe_log)
            except Exception as e:
                discrimination = f"reasoned (revert-check crashed: {e})"
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)

    data = _call(prompts.REVIEW.format(
        brief=ctx["brief"].model_dump_json(indent=2),
        tests=_tests_line(ctx),
        diff=_clip(ctx.get("diff") or "(empty)", MAX_DIFF_CHARS),
        files="\n\n".join(f"===== {p} =====\n{_clip(t, MAX_FILE_CHARS)}" for p, t in files.items())
              or "(no files changed)",
        probes=(_probe_text(probe_log) if probe_log else "(no probes were run)")
               + (f"\n\nDiscrimination check: {discrimination}" if discrimination else ""),
    ))
    checks = [c for c in (data.get("checks") or []) if isinstance(c, dict)]
    if discrimination:                                         # deterministic record, not model prose
        for c in checks:
            if str(c.get("name")) == "tests_assert_acs":
                c["note"] = (str(c.get("note", "")).rstrip() + f" [{discrimination}]").strip()
                break
    change_requests = [c for c in (data.get("change_requests") or []) if isinstance(c, dict)]
    blockers = [c for c in change_requests if str(c.get("severity")) == "blocker"]
    verdict = "request_changes" if blockers else "approve"     # blockers are authoritative, not the raw verdict
    return {"status": "reviewed", "verdict": verdict, "checks": checks, "change_requests": change_requests,
            "probe_log": probe_log,
            "summary": f"{verdict}: {len(blockers)} blocker(s), {len(change_requests) - len(blockers)} minor(s)"}
