"""The Orchestrator graph: TaskBrief in, PRPackage out — the tech lead of the pipeline.

    load_brief ─► plan_subtasks ─► dispatch ─► evaluate ─► (all subtasks resolved) ─► assemble_pr ─► END
                        ▲             ▲ │          │
                        │ (replan ≤1) └─┴──────────┘ (retry ≤2 per subtask / next runnable subtask)
                        └──────────────────────────┘

One SHARED temp workspace per run: load_brief copies the repo once, workers patch it in place, and every
pytest — the workers' and the final re-check — runs in it. Status-aware evaluation (the core routing):
code_writer "passed" → accept; "applied_unverified" → accept WITHOUT retrying, ensure a test_writer subtask
exists (appended if the plan lacks one) to write the regression test for exactly that fix, and once it lands,
re-check the suite in the workspace to mark the chain verified; "failed" → retry with LLM feedback (max 2),
then one replan, then an honest permanent failure. Other workers are judged by the LLM evaluator.

After assembly, the REVIEW GATE always runs (assemble_pr → review_gate): an independent reviewer judges the
brief + diff + changed files + tests (never the workers' rationales). Blockers trigger ONE repair round (a
scoped code_writer/test_writer subtask back through dispatch, then reassemble + re-review once); a still-
blocked review finalizes as status "needs_human_review" with the change requests attached. Minors-only
normalizes to approve with follow-ups. Never loops forever, never silently approves.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from solver_agent.graph import _baseline, _copy_repo, _pytest
from ticket_agent.llm import call_json, make_llm
from ticket_agent.schemas import TaskBrief

from . import prompts, registry
from .workers import docs_writer as _docs_writer  # noqa: F401  (importing registers the worker)
from .workers import reviewer as _reviewer        # noqa: F401  (importing registers the worker)
from .workers import test_writer as _test_writer  # noqa: F401  (importing registers the worker)
from .schemas import (ChangeRequest, OrchestratorState, PRPackage, Review, ReviewCheck,
                      Subtask, SubtaskReport)

MAX_RETRIES = 2          # per subtask, after its first attempt
MAX_REPLANS = 1
DIFF_PROMPT_CHARS = 8000
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", "node_modules"}


def _llm():
    return make_llm()


def _call(prompt: str, retries: int = 2) -> dict:
    return call_json(prompt, prompts.SYSTEM, retries=retries, llm_factory=_llm)


# ---------------- Nodes ----------------

def load_brief(state: OrchestratorState) -> OrchestratorState:
    try:
        brief = TaskBrief.model_validate(state["brief_raw"])
    except ValidationError as e:
        raise ValueError(f"Input is not a valid TaskBrief — refusing to guess what was meant:\n{e}") from e
    repo = state["repo"]
    if not os.path.isdir(repo):
        raise ValueError(f"repo must be a local directory (got {repo!r}): the orchestrator copies it once")
    ws = os.path.join(tempfile.mkdtemp(prefix=f"orch-{brief.ticket_id}-"), "repo")
    _copy_repo(repo, ws)
    return {"brief": brief, "workspace": ws, "results": {}, "feedback": {}, "replans": 0,
            "pending_verification": [], "started": time.time(),
            "max_retries": state.get("max_retries", MAX_RETRIES),
            "max_replans": state.get("max_replans", MAX_REPLANS)}


def _validate_subtasks(raw, existing_ids=()) -> list[Subtask]:
    subtasks, ids = [], set(existing_ids)
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict):
            continue
        worker = str(s.get("worker", ""))
        if worker == "reviewer":
            continue                             # the review gate always runs after assembly — never planned
        if worker not in registry.WORKERS:
            raise ValueError(f"plan uses unknown worker {worker!r}; the registry has {sorted(registry.WORKERS)}")
        sid = str(s.get("id") or f"s{i + 1}")
        while sid in ids:
            sid += "x"
        ids.add(sid)
        subtasks.append(Subtask(id=sid, worker=worker, instruction=str(s.get("instruction", "")),
                                depends_on=[str(d) for d in (s.get("depends_on") or [])],
                                rationale=str(s.get("rationale", ""))))
    if not subtasks:
        raise ValueError("plan_subtasks produced no subtasks")
    return subtasks


def _plan_dump(s: Subtask) -> dict:
    return s.model_dump(include={"id", "worker", "instruction", "depends_on", "rationale"})


def plan_subtasks(state: OrchestratorState) -> OrchestratorState:
    data = _call(prompts.PLAN_SUBTASKS.format(brief=state["brief"].model_dump_json(indent=2),
                                              workers=registry.registry_text()))
    subtasks = _validate_subtasks(data.get("subtasks"))
    return {"subtasks": subtasks, "plan_json": [_plan_dump(s) for s in subtasks]}


def _runnable(subtasks: list[Subtask]) -> Subtask | None:
    """First pending subtask whose dependencies are all accepted; skips subtasks whose dependencies died."""
    done = {s.id for s in subtasks if s.status == "accepted"}
    dead = {s.id for s in subtasks if s.status in ("failed", "skipped")}
    for s in subtasks:
        if s.status != "pending":
            continue
        lost = set(s.depends_on) & dead
        if lost:
            s.status = "skipped"
            s.summary = f"skipped: dependency failed ({', '.join(sorted(lost))})"
            continue
        if set(s.depends_on) <= done:
            return s
    return None


def dispatch(state: OrchestratorState) -> OrchestratorState:
    subtasks = state["subtasks"]
    st = _runnable(subtasks)
    if st is None:
        for s in subtasks:                       # pending but never runnable = dependency cycle
            if s.status == "pending":
                s.status = "failed"
                s.summary = "failed: unsatisfiable dependencies"
        return {"current_id": "", "subtasks": subtasks}
    st.attempts += 1
    old_snap = state.get("attempt_snapshot")
    if old_snap:                                 # defensive: evaluate normally consumed it
        shutil.rmtree(os.path.dirname(old_snap), ignore_errors=True)
    snap = os.path.join(tempfile.mkdtemp(prefix="orch-attempt-"), "repo")
    _copy_repo(state["workspace"], snap)         # pre-dispatch snapshot: rejected attempts are rolled back to it
    results = state["results"]
    ctx = {"brief": state["brief"], "repo": state["repo"], "workspace": state["workspace"],
           "instruction": st.instruction, "feedback": (state.get("feedback") or {}).get(st.id, ""),
           "previous_result": results.get(st.id),             # this subtask's last attempt (None on the first)
           "review": state.get("review"),                     # the latest review, when one exists (repair runs)
           "upstream": {d: results[d] for d in st.depends_on if d in results},
           "all_results": dict(results)}
    try:
        res = registry.WORKERS[st.worker](ctx)
    except Exception as e:                       # a crashing worker is a failed attempt, not a crashed run
        res = {"status": "failed", "summary": f"worker crashed: {e}", "error": str(e)}
    results[st.id] = res
    return {"current_id": st.id, "results": results, "subtasks": subtasks, "attempt_snapshot": snap}


def _find(subtasks: list[Subtask], sid: str) -> Subtask:
    return next(s for s in subtasks if s.id == sid)


def _llm_eval(state: OrchestratorState, st: Subtask, res: dict, policy: str = "") -> dict:
    trimmed = {k: (v[:2000] if isinstance(v, str) else v) for k, v in res.items()}
    return _call(prompts.EVALUATE.format(
        attempt=st.attempts, max_attempts=state.get("max_retries", MAX_RETRIES) + 1,
        subtask=st.model_dump_json(include={"id", "worker", "instruction", "rationale"}, indent=2),
        brief=state["brief"].model_dump_json(indent=2),
        result=json.dumps(trimmed, indent=2, default=str),
        policy=policy,
    ))


def _deny(state: OrchestratorState, st: Subtask, feedback: str, updates: dict) -> None:
    """Escalation ladder for a rejected attempt: retry with feedback → one replan → honest permanent failure."""
    if st.attempts <= state.get("max_retries", MAX_RETRIES):
        fb = dict(state.get("feedback") or {})
        fb[st.id] = feedback or "the previous attempt was rejected; try a different approach within the brief's scope"
        st.status = "pending"                                  # re-dispatched with the feedback in its ctx
        updates["feedback"] = fb
    elif (not st.id.startswith("repair-")
          and state.get("replans", 0) < state.get("max_replans", MAX_REPLANS)):
        # Repair subtasks never consume the plan-level replan: the repair round is the review's own retry,
        # and its exhaustion goes back to the reviewer (round 2 → needs_human_review), not into a new plan.
        updates["replan_requested"] = True                     # subtask stays unresolved; replan() records it
    else:
        st.status = "failed"
        st.summary = f"failed after {st.attempts} attempt(s); retries and replans exhausted"[:300]


VERIFY_SUFFIX = " — awaiting regression-test verification"


def evaluate(state: OrchestratorState) -> OrchestratorState:
    sid = state.get("current_id")
    updates: OrchestratorState = {"replan_requested": False}
    if not sid:
        return updates
    subtasks = state["subtasks"]
    st = _find(subtasks, sid)
    res = state["results"][sid]
    status = str(res.get("status", ""))

    if st.worker == "code_writer" and status == "passed":
        st.status = "accepted"
        st.summary = res.get("summary") or "passed"
    elif st.worker == "code_writer" and status == "applied_unverified":
        # Core routing: a clean-but-unverified patch is never retried — it gets verified instead.
        st.status = "accepted"
        st.summary = (res.get("summary") or "applied_unverified")[:240] + VERIFY_SUFFIX
        updates["pending_verification"] = list(state.get("pending_verification") or []) + [sid]
        if not any(s.worker == "test_writer" and s.status == "pending" for s in subtasks):
            verify = Subtask(
                id=f"verify-{sid}", worker="test_writer", depends_on=[sid],
                instruction=("Write the regression test that verifies exactly this fix — the suite cannot "
                             f"currently verify it. The fix: {res.get('rationale') or res.get('summary', '')} "
                             f"(files changed: {', '.join(res.get('files_changed') or []) or 'see the diff'}). "
                             "Cover the brief's acceptance criteria for this change only."),
                rationale="appended by the orchestrator: the code change came back applied_unverified, so a new "
                          "regression test must prove it works.")
            subtasks.append(verify)
            updates["plan_json"] = list(state.get("plan_json") or []) + [
                {**_plan_dump(verify), "appended_by": "orchestrator (applied_unverified verification)"}]
    elif status == "failed":
        feedback = ""
        if st.attempts <= state.get("max_retries", MAX_RETRIES):     # only spend an LLM call if a retry is possible
            data = _llm_eval(state, st, res, prompts.POLICY_FAILED)
            feedback = str(data.get("feedback") or data.get("reason") or "")
        _deny(state, st, feedback, updates)
    else:
        data = _llm_eval(state, st, res)
        verdict = str(data.get("verdict", "accept"))
        if verdict == "accept":
            st.status = "accepted"
            st.summary = res.get("summary") or str(data.get("reason", ""))[:300]
        elif (verdict == "replan" and not st.id.startswith("repair-")
              and state.get("replans", 0) < state.get("max_replans", MAX_REPLANS)):
            updates["replan_requested"] = True
        else:
            _deny(state, st, str(data.get("feedback", "")), updates)

    # Chain verification: once a test_writer lands, re-check the suite for any applied_unverified fix.
    if st.worker == "test_writer" and st.status == "accepted" and state.get("pending_verification"):
        _, _, failing_now, _ = _pytest(state["workspace"])
        added = [str(t) for t in (res.get("tests_added") or []) if t]
        still_failing = [t for t in added if any(t in f or f in t for f in failing_now)]
        if added and not still_failing:
            for vid in state["pending_verification"]:
                v = _find(subtasks, vid)
                v.summary = v.summary.replace(VERIFY_SUFFIX, " — verified by new regression test(s) in the workspace")
            updates["pending_verification"] = []

    # Attempt snapshot: an attempt that was not accepted — even one the worker itself considered successful —
    # is rolled back, so rejected work never leaks into later subtasks or retries.
    snap = state.get("attempt_snapshot")
    if snap:
        if st.status != "accepted":
            shutil.rmtree(state["workspace"], ignore_errors=True)
            _copy_repo(snap, state["workspace"])
        shutil.rmtree(os.path.dirname(snap), ignore_errors=True)
    updates["attempt_snapshot"] = ""
    return {**updates, "subtasks": subtasks}


def replan(state: OrchestratorState) -> OrchestratorState:
    subtasks = state["subtasks"]
    accepted = [s for s in subtasks if s.status == "accepted"]
    unresolved = [s for s in subtasks if s.status not in ("accepted", "failed", "skipped")]
    history = json.dumps([{**s.model_dump(include={"id", "worker", "instruction", "status", "attempts"}),
                           "feedback_given": (state.get("feedback") or {}).get(s.id, ""),
                           "last_output": {k: (v[:1500] if isinstance(v, str) else v)
                                           for k, v in (state["results"].get(s.id) or {}).items()}}
                          for s in unresolved], indent=2, default=str)
    data = _call(prompts.REPLAN.format(
        brief=state["brief"].model_dump_json(indent=2),
        accepted=json.dumps([s.model_dump(include={"id", "worker", "instruction", "summary"})
                             for s in accepted], indent=2) or "(none)",
        failed=history))
    new = _validate_subtasks(data.get("subtasks"), existing_ids={s.id for s in subtasks})
    for s in unresolved:                                       # the old plan's open work is superseded — honestly
        if s.attempts:
            s.status = "failed"
            s.summary = f"failed after {s.attempts} attempt(s) — replanned"
        else:
            s.status = "skipped"
            s.summary = "superseded by replan"
    subtasks.extend(new)
    plan_json = list(state.get("plan_json") or []) + [{**_plan_dump(s), "appended_by": "replan"} for s in new]
    return {"subtasks": subtasks, "replans": state.get("replans", 0) + 1,
            "replan_requested": False, "plan_json": plan_json}


def _tree(root: str) -> dict[str, str]:
    out = {}
    rootp = Path(root)
    for p in sorted(rootp.rglob("*")):
        rel = p.relative_to(rootp)
        if p.is_file() and not (set(rel.parts[:-1]) & SKIP_DIRS) and p.suffix != ".pyc":
            out[rel.as_posix()] = p.read_text(errors="replace")
    return out


def assemble_pr(state: OrchestratorState) -> OrchestratorState:
    repo_baseline = state.get("repo_baseline")
    if repo_baseline is None:                    # once: which tests already failed on the UNTOUCHED repo
        repo_baseline, _ = _baseline(state["repo"])
    a, b = _tree(state["repo"]), _tree(state["workspace"])
    changed = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
    diff = ""
    for p in changed:
        diff += "".join(difflib.unified_diff((a.get(p) or "").splitlines(keepends=True),
                                             (b.get(p) or "").splitlines(keepends=True),
                                             fromfile=f"a/{p}", tofile=f"b/{p}"))
    _, _, failing_now, counts = _pytest(state["workspace"])
    tests_passed = counts.get("passed", 0)
    tests_failed = counts.get("failed", 0) + counts.get("error", 0)

    subtasks: list[Subtask] = state["subtasks"]
    accepted = [s for s in subtasks if s.status == "accepted"]
    unfinished = [s for s in subtasks if s.status in ("failed", "skipped")]
    status = "complete" if accepted and not unfinished else ("partial" if accepted else "failed")
    new_tests: list[str] = []
    for s in accepted:
        if s.worker == "test_writer":
            for t in (state["results"].get(s.id) or {}).get("tests_added") or []:
                if t not in new_tests:
                    new_tests.append(str(t))
    reports = [SubtaskReport(**s.model_dump(include={"id", "worker", "instruction", "status",
                                                     "attempts", "summary"})) for s in subtasks]
    investigation = []
    for s in subtasks:                                         # surface the solver's live investigation per fix
        if s.worker == "code_writer":
            inv = (state["results"].get(s.id) or {}).get("investigation")
            if isinstance(inv, dict) and (inv.get("commands") or inv.get("evidence")):
                investigation.append({"subtask": s.id, **inv})
    try:
        data = _call(prompts.PR_PACKAGE.format(
            brief=state["brief"].model_dump_json(indent=2),
            subtasks=json.dumps([r.model_dump() for r in reports], indent=2),
            tests_passed=tests_passed, tests_failed=tests_failed,
            new_tests=", ".join(new_tests) or "(none)",
            diff=diff[:DIFF_PROMPT_CHARS] + ("\n[... diff truncated ...]" if len(diff) > DIFF_PROMPT_CHARS else "")))
        title = str(data.get("pr_title", ""))
        description = str(data.get("pr_description", ""))
    except Exception as e:                                     # never lose the run over the description
        title, description = "", f"_PR description generation failed ({e}); see the subtask table below._"
    pr = PRPackage(
        ticket_id=state["brief"].ticket_id,
        status=status,
        subtasks=reports,
        combined_diff=diff,
        files_changed=changed,
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        new_tests_added=new_tests,
        investigation=investigation,
        pr_title=title or f"{state['brief'].ticket_id}: {state['brief'].goal[:80]}",
        pr_description=description,
        duration_seconds=round(time.time() - state.get("started", time.time()), 1),
    )
    return {"pr": pr, "repo_baseline": repo_baseline, "failing_now": sorted(failing_now)}


# ---------------- Review gate ----------------

def _blockers(review: dict) -> list[dict]:
    return [c for c in (review.get("change_requests") or [])
            if isinstance(c, dict) and str(c.get("severity")) == "blocker"]


def review_gate(state: OrchestratorState) -> OrchestratorState:
    """Mandatory final step: independent review of the assembled PR. Sees the brief, diff, tests and the full
    changed files — never the workers' rationales. A crashed review NEVER silently approves."""
    pr: PRPackage = state["pr"]
    ws = state["workspace"]
    files = {}
    for p in pr.files_changed:
        f = Path(ws, p)
        if f.is_file():
            files[p] = f.read_text(errors="replace")
    ctx = {"brief": state["brief"], "repo": state["repo"], "workspace": ws,
           "diff": pr.combined_diff, "files": files, "tests_passed": pr.tests_passed,
           "tests_failed": pr.tests_failed, "new_tests": list(pr.new_tests_added),
           "failing_tests": list(state.get("failing_now") or []),       # facts, not worker rationale:
           "baseline_failed": list(state.get("repo_baseline") or [])}   # which tests fail now vs before
    updates: OrchestratorState = {"review_rounds": state.get("review_rounds", 0) + 1}
    try:
        res = registry.WORKERS["reviewer"](ctx)
    except Exception as e:
        res = {"verdict": "request_changes",
               "checks": [{"name": "review", "result": "warn", "note": f"reviewer crashed: {e}"}],
               "change_requests": [{"file": "", "issue": f"the review could not run: {e}",
                                    "suggestion": "review this PR manually", "severity": "blocker"}]}
        updates["repair_done"] = True            # repairing code cannot fix a crashed reviewer
    return {**updates, "review": res}


def repair(state: OrchestratorState) -> OrchestratorState:
    """Turn the review's blockers into ONE scoped subtask and hand it back to the dispatch machinery."""
    blockers = _blockers(state["review"])
    worker = ("test_writer" if blockers and
              all(str(b.get("file", "")).startswith("tests/") for b in blockers) else "code_writer")
    lines = "\n".join(f"- {b.get('file') or '(unspecified file)'}: {b.get('issue', '')}"
                      f" — suggestion: {b.get('suggestion', '')}" for b in blockers)
    st = Subtask(id=f"repair-{state.get('review_rounds', 1)}", worker=worker,
                 instruction=("The reviewer blocked this PR. Fix EXACTLY these issues and nothing else:\n"
                              f"{lines}\nDo not change anything the reviewer did not flag."),
                 rationale="mandatory review gate: blocking change requests must be fixed before merge")
    subtasks = state["subtasks"]
    subtasks.append(st)
    plan_json = list(state.get("plan_json") or []) + [{**_plan_dump(st), "appended_by": "reviewer (repair round)"}]
    return {"subtasks": subtasks, "plan_json": plan_json, "repair_done": True}


def finalize(state: OrchestratorState) -> OrchestratorState:
    pr: PRPackage = state["pr"]
    raw = state.get("review") or {}
    checks = [ReviewCheck(name=str(c.get("name", "")),
                          result=str(c.get("result")) if str(c.get("result")) in ("pass", "fail", "warn") else "warn",
                          note=str(c.get("note", "")))
              for c in (raw.get("checks") or []) if isinstance(c, dict)]
    crs = [ChangeRequest(file=str(c.get("file", "")), issue=str(c.get("issue", "")),
                         suggestion=str(c.get("suggestion", "")),
                         severity="blocker" if str(c.get("severity")) == "blocker" else "minor")
           for c in (raw.get("change_requests") or []) if isinstance(c, dict)]
    if not checks and not crs:                   # an empty review is not an approval
        crs = [ChangeRequest(issue="the reviewer returned no checks and no change requests",
                             suggestion="review this PR manually", severity="blocker")]
    blockers = [c for c in crs if c.severity == "blocker"]
    probe_log = [c for c in (raw.get("probe_log") or []) if isinstance(c, dict)]
    review = Review(verdict="request_changes" if blockers else "approve",
                    checks=checks, change_requests=crs, rounds=state.get("review_rounds", 1),
                    probe_log=probe_log)
    status = pr.status
    if blockers and status != "failed":          # a blocked review outranks complete/partial — never silently approve
        status = "needs_human_review"
    return {"pr": pr.model_copy(update={"review": review, "status": status})}


# ---------------- Routing / build ----------------

def after_dispatch(state: OrchestratorState) -> str:
    return "evaluate" if state.get("current_id") else "assemble_pr"


def after_evaluate(state: OrchestratorState) -> str:
    if state.get("replan_requested"):
        return "replan"
    if any(s.status == "pending" for s in state["subtasks"]):
        return "dispatch"
    return "assemble_pr"


def after_review(state: OrchestratorState) -> str:
    if _blockers(state.get("review") or {}) and not state.get("repair_done"):
        return "repair"                          # one repair round; a second blocked review goes to finalize
    return "finalize"


def build_graph():
    g = StateGraph(OrchestratorState)
    for name, fn in [("load_brief", load_brief), ("plan_subtasks", plan_subtasks), ("dispatch", dispatch),
                     ("evaluate", evaluate), ("replan", replan), ("assemble_pr", assemble_pr),
                     ("review_gate", review_gate), ("repair", repair), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.set_entry_point("load_brief")
    g.add_edge("load_brief", "plan_subtasks")
    g.add_edge("plan_subtasks", "dispatch")
    g.add_conditional_edges("dispatch", after_dispatch, {"evaluate": "evaluate", "assemble_pr": "assemble_pr"})
    g.add_conditional_edges("evaluate", after_evaluate,
                            {"dispatch": "dispatch", "replan": "replan", "assemble_pr": "assemble_pr"})
    g.add_edge("replan", "dispatch")
    g.add_edge("assemble_pr", "review_gate")
    g.add_conditional_edges("review_gate", after_review, {"repair": "repair", "finalize": "finalize"})
    g.add_edge("repair", "dispatch")
    g.add_edge("finalize", END)
    return g.compile()


def run(brief: dict | TaskBrief, repo: str, max_retries: int = MAX_RETRIES,
        max_replans: int = MAX_REPLANS) -> PRPackage:
    """brief: TaskBrief or its dict form. repo: local directory (copied once into the shared workspace,
    which is left on disk for inspection)."""
    raw = brief.model_dump() if isinstance(brief, TaskBrief) else brief
    final = build_graph().invoke({"brief_raw": raw, "repo": repo,
                                  "max_retries": max_retries, "max_replans": max_replans},
                                 config={"recursion_limit": 100})
    return final["pr"]
