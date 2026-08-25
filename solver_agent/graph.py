"""The Solver Agent graph: TaskBrief in, Solution out.

    load_brief ─► read_files ─► plan_fix ─► write_patch ─► apply_patch ─► run_tests ─► emit_solution ─► END
                                                ▲                                │ (retry: new failures, patch
                                                └────────────────────────────────┘  errors, or in-scope failures
                                                                                    remain; max 3 attempts)

Verification is judged against a baseline pytest run on the pristine repo (done once, in read_files, so plan_fix
can classify each pre-existing failure as in scope or out of scope for the brief). Three outcomes:

- **passed** — zero new failures AND (≥1 baseline failure fixed OR ≥1 newly-added test passes — credit for
  self-written regression tests when the existing suite doesn't cover the bug).
- **applied_unverified** — the patch applies cleanly, zero new failures, but nothing flipped green and no new
  test verifies it; every remaining failure is a baseline one the plan classified out of scope. Terminal: a
  settled patch is not re-solved (no retry burn), and the rationale says the suite cannot verify the fix.
- **failed** — new failures, patch errors, or in-scope baseline failures still failing after all retries.

Flipping an unrelated failing test never counts as success. Every attempt applies its FULL edit set to a fresh
copy of the repo (edits never stack across attempts).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from ticket_agent.codebase import Codebase
from ticket_agent.llm import call_json, make_llm
from ticket_agent.schemas import TaskBrief

from . import prompts
from .schemas import Edit, Solution, SolverState

MAX_CONTEXT_FILES = 15
TEST_TIMEOUT = 120
COPY_IGNORE = (".git", "__pycache__", ".venv", "venv", ".pytest_cache", "node_modules", "*.pyc")


def _llm():
    return make_llm()


def _call(prompt: str, retries: int = 2) -> dict:
    return call_json(prompt, prompts.SYSTEM, retries=retries, llm_factory=_llm)


def _files_block(files: dict[str, str]) -> str:
    return "\n\n".join(f"===== {p} =====\n{text}" for p, text in files.items())


class PatchError(ValueError):
    """An edit could not be applied: missing file, or old_str matched zero or multiple times."""


# ---------------- Nodes ----------------

def load_brief(state: SolverState) -> SolverState:
    try:
        brief = TaskBrief.model_validate(state["brief_raw"])
    except ValidationError as e:
        raise ValueError(f"Input is not a valid TaskBrief — refusing to guess what was meant:\n{e}") from e
    repo = state["repo"]
    if not os.path.isdir(repo):
        raise ValueError(f"repo must be a local directory (got {repo!r}): the solver copies it to apply patches")
    ws = state.get("workspace")
    if ws and not os.path.isdir(ws):
        raise ValueError(f"workspace must be an existing directory (got {ws!r})")
    src = ws or repo         # orchestrator mode: read, patch, baseline and diff against the shared workspace
    return {"brief": brief, "codebase": Codebase.open(src), "src": src, "started": time.time()}


IMPORT_RE = re.compile(r"^\s*(?:from\s+([.\w]+)\s+import|import\s+([\w.]+))", re.M)


def _import_paths(path: str, text: str, known: set[str]) -> list[str]:
    """Repo-local files imported by `path` (absolute `import a.b` / `from a.b import` or relative `from .x import`)."""
    out = []
    here = PurePosixPath(path).parent
    for m in IMPORT_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name.startswith("."):
            dots = len(name) - len(name.lstrip("."))
            base = here
            for _ in range(dots - 1):
                base = base.parent
            mod = name.lstrip(".")
        else:
            base, mod = PurePosixPath("."), name
        rel = mod.replace(".", "/")
        for cand in ((f"{rel}.py", f"{rel}/__init__.py") if rel else ("__init__.py",)):
            p = str(base / cand)
            p = p[2:] if p.startswith("./") else p
            if p in known:
                out.append(p)
                break
    return out


def _prose_paths(brief: TaskBrief, known: set[str]) -> list[str]:
    """Repo files named in the brief's affected_areas / evidence prose (e.g. "auth/tokens.py · validate_reset_token()").

    Matched as whole paths (no partial-name hits), so the solver reads what the brief talks about even when
    suspected_files misses or misleads — it must never write edits for a file it has not read.
    """
    out = []
    for s in list(brief.affected_areas) + list(brief.evidence):
        for p in sorted(known):
            if p not in out and re.search(rf"(?<![\w/.-]){re.escape(p)}(?!\w)", s):
                out.append(p)
    return out


def _baseline(repo: str) -> tuple[list[str], dict[str, int]]:
    """Which tests fail on the pristine repo, before any patch — computed once on a throwaway copy."""
    tmp = tempfile.mkdtemp(prefix="solver-baseline-")
    pristine = os.path.join(tmp, "repo")
    _copy_repo(repo, pristine)
    _, _, failed, counts = _pytest(pristine)
    shutil.rmtree(tmp, ignore_errors=True)
    return sorted(failed), counts


def read_files(state: SolverState) -> SolverState:
    """Context = suspected files + repo files named in affected_areas/evidence prose + the local modules they
    import (breadth-first), capped at MAX_CONTEXT_FILES. Also runs the baseline pytest once (pristine copy) so
    plan_fix can classify the pre-existing failures.

    Files are read from disk in full (Codebase.read truncates for prompt use; edits must match the real file).
    """
    cb: Codebase = state["codebase"]
    known = set(cb.files)
    src = Path(state["src"])

    ordered: list[str] = []
    for f in state["brief"].suspected_files:
        p = f.get("path") if isinstance(f, dict) else None
        if p and p in known and p not in ordered:
            ordered.append(p)
    for p in _prose_paths(state["brief"], known):          # read what the brief's prose names — no blind edits
        if p not in ordered and len(ordered) < MAX_CONTEXT_FILES:
            ordered.append(p)
    if not ordered:
        raise ValueError(f"Nothing to read: no suspected_files (or files named in affected_areas/evidence) "
                         f"exist in {state['src']!r}")

    queue = list(ordered)
    while queue and len(ordered) < MAX_CONTEXT_FILES:
        current = queue.pop(0)
        for dep in _import_paths(current, (src / current).read_text(errors="replace"), known):
            if dep not in ordered and len(ordered) < MAX_CONTEXT_FILES:
                ordered.append(dep)
                queue.append(dep)

    baseline_failed, baseline_counts = _baseline(state["src"])
    return {"files": {p: (src / p).read_text(errors="replace") for p in ordered},
            "baseline_failed": baseline_failed, "baseline_counts": baseline_counts}


def plan_fix(state: SolverState) -> SolverState:
    baseline = state.get("baseline_failed") or []
    ti = state.get("task_instruction") or ""
    data = _call(prompts.PLAN_FIX.format(
        brief=state["brief"].model_dump_json(indent=2),
        instruction=prompts.TASK_INSTRUCTION_NOTE.format(instruction=ti) if ti else "",
        files=_files_block(state["files"]),
        baseline="\n".join(f"- {t}" for t in baseline) or "(none — the suite is green before any patch)",
    ))
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError(f"plan_fix produced no changes: {data!r}")
    return {"plan": {"diagnosis": str(data.get("diagnosis", "")), "changes": changes,
                     "risks": data.get("risks") or [],
                     "out_of_scope_failures": [str(x) for x in (data.get("out_of_scope_failures") or [])]}}


def write_patch(state: SolverState) -> SolverState:
    brief = state["brief"]
    attempt = state.get("attempts", 0) + 1
    history = state.get("history") or []
    retry_note = ""
    if history:
        prev = history[-1]
        retry_note = prompts.RETRY_NOTE.format(attempt=attempt,
                                               previous_edits=json.dumps(prev["edits"], indent=2),
                                               result=prev["result"])
    data = _call(prompts.WRITE_PATCH.format(
        plan=json.dumps(state["plan"], indent=2),
        guard=json.dumps({"constraints": brief.constraints, "out_of_scope": brief.out_of_scope,
                          "related_findings (never fix here)": brief.related_findings}, indent=2),
        files=_files_block(state["files"]),
        retry_note=retry_note,
    ))
    edits = [Edit.model_validate(e) for e in data.get("edits") or []]
    if not edits:
        raise ValueError(f"write_patch produced no edits: {data!r}")
    for e in edits:
        if not e.old_str or e.old_str == e.new_str:
            raise ValueError(f"write_patch produced an empty or no-op edit for {e.path}")
    return {"edits": edits, "attempts": attempt}


def _copy_repo(repo: str, dst: str) -> None:
    shutil.copytree(repo, dst, ignore=shutil.ignore_patterns(*COPY_IGNORE))


def apply_edits(workdir: str, edits: list[Edit]) -> None:
    """Exact unique string replacements. PatchError on a missing file, zero matches, or multiple matches."""
    for e in edits:
        f = Path(workdir, e.path)
        if not f.is_file():
            raise PatchError(f"{e.path}: no such file in the repo")
        text = f.read_text(errors="replace")
        n = text.count(e.old_str)
        if n == 0:
            raise PatchError(f"{e.path}: old_str not found — it must be copied exactly, whitespace included")
        if n > 1:
            raise PatchError(f"{e.path}: old_str occurs {n} times — add surrounding context so it is unique")
        f.write_text(text.replace(e.old_str, e.new_str, 1))


def apply_patch(state: SolverState) -> SolverState:
    ws = state.get("workspace")
    if ws:
        # Orchestrator mode: patch the shared workspace in place. A snapshot of its pre-solver state is taken
        # once; every attempt resets the workspace to that snapshot before applying its full edit set.
        snap = state.get("snapshot")
        extra: SolverState = {}
        if not snap:
            snap = os.path.join(tempfile.mkdtemp(prefix=f"solver-snap-{state['brief'].ticket_id}-"), "repo")
            _copy_repo(ws, snap)
            extra = {"snapshot": snap}
        shutil.rmtree(ws, ignore_errors=True)
        _copy_repo(snap, ws)
        try:
            apply_edits(ws, state["edits"])
            return {"workdir": ws, "patch_error": "", **extra}
        except PatchError as e:
            return {"workdir": ws, "patch_error": str(e), **extra}

    old = state.get("workdir")
    if old:
        shutil.rmtree(os.path.dirname(old), ignore_errors=True)   # drop the previous attempt's copy
    tmp = tempfile.mkdtemp(prefix=f"solver-{state['brief'].ticket_id}-")
    workdir = os.path.join(tmp, "repo")
    _copy_repo(state["repo"], workdir)
    try:
        apply_edits(workdir, state["edits"])
        return {"workdir": workdir, "patch_error": ""}
    except PatchError as e:
        return {"workdir": workdir, "patch_error": str(e)}


SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error)")
FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


def _pytest(workdir: str) -> tuple[bool, str, set[str], dict[str, int]]:
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=workdir,
                           capture_output=True, text=True, timeout=TEST_TIMEOUT)
        out = (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
        ok = p.returncode == 0
    except subprocess.TimeoutExpired as e:
        out = f"{e.stdout or ''}\n{e.stderr or ''}\n[pytest timed out after {TEST_TIMEOUT}s]".strip()
        ok = False
    last = out.splitlines()[-1] if out else ""
    counts = {kind: int(n) for n, kind in SUMMARY_RE.findall(last)}
    return ok, out, set(FAILED_RE.findall(out)), counts


def _covered(test_id: str, out_of_scope: list[str]) -> bool:
    """Is this failing test one the plan classified as out of scope for the brief? (tolerant id matching)"""
    return any(o and (o == test_id or o in test_id or test_id in o) for o in out_of_scope)


def run_tests(state: SolverState) -> SolverState:
    baseline_failed = state.get("baseline_failed")
    baseline_counts = state.get("baseline_counts") or {}
    if baseline_failed is None:                            # fallback — read_files normally computed it
        baseline_failed, baseline_counts = _baseline(state["src"])

    history = list(state.get("history") or [])
    edits_dump = [e.model_dump() for e in state.get("edits", [])]
    common = {"baseline_failed": baseline_failed, "baseline_counts": baseline_counts}

    if state.get("patch_error"):
        out = f"PATCH ERROR — the edits were not applied: {state['patch_error']}"
        history.append({"edits": edits_dump, "result": out})
        return {**common, "outcome": "retry", "test_output": out,
                "tests_passed": 0, "tests_failed": 0, "fixed_tests": [], "new_failures": [],
                "remaining_failures": list(baseline_failed), "new_tests_passed": 0, "history": history}

    _, out, failed_now, counts = _pytest(state["workdir"])
    base = set(baseline_failed)
    fixed = sorted(base - failed_now)
    new = sorted(failed_now - base)
    remaining = sorted(failed_now & base)
    total_now = sum(counts.values())
    base_total = sum(baseline_counts.values()) if baseline_counts else total_now
    new_tests_passed = max(0, (total_now - base_total) - len(new))   # tests added by the patch that pass

    oos = (state.get("plan") or {}).get("out_of_scope_failures") or []
    rem_in = [r for r in remaining if not _covered(r, oos)]
    rem_oos = [r for r in remaining if _covered(r, oos)]
    # Three outcomes, always requiring zero new failures for success:
    # passed              — something verifies the fix: a baseline failure flipped green, or a newly-added test passes.
    # applied_unverified  — nothing verifies it, but nothing in scope is failing either. Terminal: don't re-solve.
    # retry               — new failures, or an in-scope baseline failure the patch was supposed to fix still fails.
    if not new and (fixed or new_tests_passed):
        outcome = "passed"
    elif not new and not rem_in:
        outcome = "applied_unverified"
    else:
        outcome = "retry"

    tail = "\n".join(out.splitlines()[-30:])
    history.append({"edits": edits_dump, "result": (
        f"fixed: {', '.join(fixed) or 'none'} · new failures: {', '.join(new) or 'none'}"
        f" · newly added passing tests: {new_tests_passed}"
        f" · still failing IN SCOPE (your patch must fix these): {', '.join(rem_in) or 'none'}"
        f" · still failing out of scope (leave them alone): {', '.join(rem_oos) or 'none'}\n{tail}")})
    return {**common, "outcome": outcome, "test_output": out,
            "tests_passed": counts.get("passed", 0),
            "tests_failed": counts.get("failed", 0) + counts.get("error", 0),
            "fixed_tests": fixed, "new_failures": new, "remaining_failures": remaining,
            "new_tests_passed": new_tests_passed, "history": history}


def _unified_diff(repo: str, workdir: str, paths: list[str]) -> str:
    chunks = []
    for p in paths:
        a = Path(repo, p).read_text(errors="replace").splitlines(keepends=True)
        b = Path(workdir, p).read_text(errors="replace").splitlines(keepends=True)
        chunks.append("".join(difflib.unified_diff(a, b, fromfile=f"a/{p}", tofile=f"b/{p}")))
    return "".join(chunks)


def emit_solution(state: SolverState) -> SolverState:
    applied = not state.get("patch_error")
    outcome = state.get("outcome", "retry")
    status = outcome if outcome in ("passed", "applied_unverified") else "failed"
    edits = state.get("edits", [])
    files_changed = sorted({e.path for e in edits}) if applied else []
    base = state.get("snapshot") or state["src"]              # workspace mode diffs against the pre-solver snapshot
    diff = _unified_diff(base, state["workdir"], files_changed) if applied and state.get("workdir") else ""

    ws, snap = state.get("workspace"), state.get("snapshot")

    oos = (state.get("plan") or {}).get("out_of_scope_failures") or []
    remaining = state.get("remaining_failures") or []
    rem_oos = [r for r in remaining if _covered(r, oos)]
    rem_in = [r for r in remaining if not _covered(r, oos)]

    bits = [state.get("plan", {}).get("diagnosis", "")]
    if state.get("fixed_tests"):
        bits.append(f"Fixed: {', '.join(state['fixed_tests'])}.")
    if state.get("new_tests_passed"):
        bits.append(f"Verified by {state['new_tests_passed']} newly-added passing test(s).")
    if rem_oos:
        bits.append(f"Left failing (pre-existing, outside this brief's scope): {', '.join(rem_oos)}.")
    if rem_in:
        bits.append(f"Still failing and in scope for this brief: {', '.join(rem_in)}.")
    if state.get("new_failures"):
        bits.append(f"New failures introduced: {', '.join(state['new_failures'])}.")
    if state.get("patch_error"):
        bits.append(f"Final attempt could not be applied: {state['patch_error']}")
    if status == "applied_unverified":
        bits.append("The patch applied cleanly and introduced no new failures, but the test suite cannot verify "
                    "this fix: no covered test flipped green and no newly-added test exercises it.")
    if status == "failed":
        bits.append(f"Gave up after {state.get('attempts', 0)} attempt(s); this output reports the failure honestly.")

    if ws and snap:
        if status == "failed":                                 # leave the shared workspace as the solver found it
            shutil.rmtree(ws, ignore_errors=True)
            _copy_repo(snap, ws)
        shutil.rmtree(os.path.dirname(snap), ignore_errors=True)   # the snapshot is no longer needed

    solution = Solution(
        ticket_id=state["brief"].ticket_id,
        status=status,
        attempts=state.get("attempts", 0),
        edits=edits,
        diff=diff,
        files_changed=files_changed,
        tests_passed=state.get("tests_passed", 0),
        tests_failed=state.get("tests_failed", 0),
        test_output_tail="\n".join(state.get("test_output", "").splitlines()[-30:]),
        rationale=" ".join(b for b in bits if b).strip(),
        duration_seconds=round(time.time() - state.get("started", time.time()), 1),
    )
    return {"solution": solution}


# ---------------- Routing / build ----------------

def after_run_tests(state: SolverState) -> str:
    if state["outcome"] in ("passed", "applied_unverified"):   # applied_unverified is terminal: don't re-solve
        return "emit_solution"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "emit_solution"
    return "write_patch"


def build_graph():
    g = StateGraph(SolverState)
    for name, fn in [("load_brief", load_brief), ("read_files", read_files), ("plan_fix", plan_fix),
                     ("write_patch", write_patch), ("apply_patch", apply_patch), ("run_tests", run_tests),
                     ("emit_solution", emit_solution)]:
        g.add_node(name, fn)
    g.set_entry_point("load_brief")
    g.add_edge("load_brief", "read_files")
    g.add_edge("read_files", "plan_fix")
    g.add_edge("plan_fix", "write_patch")
    g.add_edge("write_patch", "apply_patch")
    g.add_edge("apply_patch", "run_tests")
    g.add_conditional_edges("run_tests", after_run_tests,
                            {"write_patch": "write_patch", "emit_solution": "emit_solution"})
    g.add_edge("emit_solution", END)
    return g.compile()


def run(brief: dict | TaskBrief, repo: str, max_attempts: int = 3,
        task_instruction: str = "", workspace: str | None = None) -> Solution:
    """brief: TaskBrief or its dict form. repo: local directory.

    task_instruction: optional orchestrator note appended to the plan prompt to scope this run
    (e.g. "only fix the token TTL, do not write tests").
    workspace: optional existing shared directory to patch IN PLACE (orchestrator mode): the solver snapshots
    it once, resets it to the snapshot per attempt, leaves the final patch in it on success and restores it on
    failure. Standalone (None) keeps the temp-copy behavior; the final patched copy stays on disk (workdir).
    """
    raw = brief.model_dump() if isinstance(brief, TaskBrief) else brief
    init: SolverState = {"brief_raw": raw, "repo": repo, "max_attempts": max_attempts,
                         "task_instruction": task_instruction or ""}
    if workspace:
        init["workspace"] = workspace
    final = build_graph().invoke(init, config={"recursion_limit": 50})
    return final["solution"]
