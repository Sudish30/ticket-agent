"""test_writer worker: a small LangGraph that writes pytest tests for the brief's acceptance criteria.

    write_tests ─► apply_and_run ─► END

It reads the code writer's diff (from its dependency's result) plus the workspace's existing test files, asks
the LLM for test edits (modify an existing tests/ file with an exact unique old_str, or create a new file with
old_str ""), applies them in the SHARED workspace, runs pytest before and after, and reports pass/fail plus the
tests it added. It only touches tests/, and on any failure it restores every file it touched — a rejected
attempt leaves the workspace exactly as it found it.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from solver_agent.graph import PatchError, _pytest, apply_edits
from solver_agent.schemas import Edit
from ticket_agent.llm import call_json, make_llm

from .. import prompts
from ..registry import register


def _llm():
    return make_llm()


def _call(prompt: str, retries: int = 2) -> dict:
    return call_json(prompt, prompts.SYSTEM, retries=retries, llm_factory=_llm)


class TestWriterState(TypedDict, total=False):
    ctx: dict      # the registry worker context
    data: dict     # LLM output: {edits, new_tests}
    result: dict   # the worker result


def _test_files(ws: str) -> dict[str, str]:
    out = {}
    root = Path(ws)
    tdir = root / "tests"
    if tdir.is_dir():
        for p in sorted(tdir.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if "__pycache__" not in rel:
                out[rel] = p.read_text(errors="replace")
    return out


def write_tests(state: TestWriterState) -> TestWriterState:
    ctx = state["ctx"]
    diff = "\n\n".join(r.get("diff", "") for r in (ctx.get("upstream") or {}).values() if r.get("diff"))
    if not diff:   # dependency carried no diff — fall back to any code change made earlier in the run
        diff = "\n\n".join(r.get("diff", "") for r in (ctx.get("all_results") or {}).values() if r.get("diff"))
    tests = _test_files(ctx["workspace"])
    prev = ctx.get("previous_result") or {}
    previous = ""
    if prev:       # solver-style retry note: the worker's own previous edits + what the suite said
        previous = prompts.TW_RETRY_NOTE.format(
            edits=json.dumps(prev.get("edits") or [], indent=2),
            result=(str(prev.get("summary", "")) + "\n" + str(prev.get("test_output_tail", "")))[:3000])
    data = _call(prompts.WRITE_TESTS.format(
        brief=ctx["brief"].model_dump_json(indent=2),
        instruction=ctx.get("instruction", ""),
        diff=diff or "(none)",
        tests="\n\n".join(f"===== {p} =====\n{t}" for p, t in tests.items()) or "(no test files yet)",
        previous=previous,
        feedback=prompts.FEEDBACK_NOTE.format(feedback=ctx["feedback"]) if ctx.get("feedback") else "",
    ))
    return {"data": data}


def apply_and_run(state: TestWriterState) -> TestWriterState:
    ctx = state["ctx"]
    ws = ctx["workspace"]
    edits = state["data"].get("edits") or []
    new_tests = [str(t) for t in (state["data"].get("new_tests") or [])]

    _, _, pre_failed, pre_counts = _pytest(ws)
    touched: dict[str, Optional[str]] = {}                     # path -> original content (None = created)

    def _restore() -> None:
        for rel, text in touched.items():
            p = Path(ws, rel)
            if text is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(text)

    def _fail(reason: str, tail: str = "") -> TestWriterState:
        _restore()
        return {"result": {
            "status": "failed", "summary": f"rejected: {reason} (workspace restored)"[:300],
            "tests_added": [], "diff": "", "new_failures": [], "files_changed": [],
            "edits": [dict(e) for e in edits if isinstance(e, dict)],   # so the retry can see what was tried
            "test_output_tail": tail,
            "tests_passed": pre_counts.get("passed", 0),
            "tests_failed": pre_counts.get("failed", 0) + pre_counts.get("error", 0),
        }}

    try:
        if not edits:
            raise PatchError("no edits returned — at least one test must be added")
        for e in edits:
            rel = str(e.get("path", ""))
            if not rel.startswith("tests/"):
                raise PatchError(f"{rel}: test_writer may only touch files under tests/")
            p = Path(ws, rel)
            touched.setdefault(rel, p.read_text(errors="replace") if p.is_file() else None)
            if not e.get("old_str"):
                if p.exists():
                    raise PatchError(f"{rel}: already exists — modify it with an exact old_str, not old_str \"\"")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(e.get("new_str", "")))
            else:
                apply_edits(ws, [Edit.model_validate(e)])
    except PatchError as err:
        return _fail(f"patch error: {err}")

    _, post_out, post_failed, post_counts = _pytest(ws)
    new_failures = sorted(post_failed - pre_failed)
    added = max(0, sum(post_counts.values()) - sum(pre_counts.values()))
    if new_failures:
        return _fail(f"its tests introduced failures: {', '.join(new_failures)}",
                     tail="\n".join(post_out.splitlines()[-30:]))
    if added < 1:
        return _fail("no new tests were collected — the edits added nothing the suite runs")

    diff = ""
    for rel, before in touched.items():
        after = Path(ws, rel).read_text(errors="replace")
        diff += "".join(difflib.unified_diff((before or "").splitlines(keepends=True),
                                             after.splitlines(keepends=True),
                                             fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return {"result": {
        "status": "passed",
        "summary": f"added {added} passing test(s): {', '.join(new_tests) or ', '.join(sorted(touched))}"[:300],
        "tests_added": new_tests,
        "diff": diff,
        "new_failures": [],
        "files_changed": sorted(touched),
        "tests_passed": post_counts.get("passed", 0),
        "tests_failed": post_counts.get("failed", 0) + post_counts.get("error", 0),
        "test_output_tail": "\n".join(post_out.splitlines()[-30:]),
    }}


def build_graph():
    g = StateGraph(TestWriterState)
    g.add_node("write_tests", write_tests)
    g.add_node("apply_and_run", apply_and_run)
    g.set_entry_point("write_tests")
    g.add_edge("write_tests", "apply_and_run")
    g.add_edge("apply_and_run", END)
    return g.compile()


@register("test_writer",
          "writes new pytest tests under tests/ that verify the brief's acceptance criteria against the current "
          "patched code (it reads the code writer's diff), runs the suite in the shared workspace, and reports "
          "pass/fail plus the exact tests it added. Does not change application code.")
def test_writer(ctx: dict) -> dict:
    final = build_graph().invoke({"ctx": ctx}, config={"recursion_limit": 10})
    return final["result"]
