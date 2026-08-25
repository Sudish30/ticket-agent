"""docs_writer worker: documentation for a change the run already made — and nothing else.

One registry entry, as designed. Input: the brief, the combined diff of the run so far, the review result when
one exists (as a planned subtask it usually runs BEFORE the review gate — then it sees "(not yet reviewed)"),
and the current contents of every file the run changed. Output: edits restricted to comments and docstrings in
those changed files, plus a CHANGELOG-style note appended to CHANGELOG.md under a "## <ticket>" heading.

It must never touch executable code: `comment_only_change` compares the "code skeletons" of old_str/new_str
(triple-quoted strings collapsed, #-comments stripped quote-aware, blank lines dropped) and rejects any edit
whose skeletons differ. On any rejection or a broken test run it restores every file it touched.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Optional

from solver_agent.graph import PatchError, _pytest, apply_edits
from solver_agent.schemas import Edit
from ticket_agent.llm import call_json, make_llm

from .. import prompts
from ..registry import register


def _llm():
    return make_llm()


def _call(prompt: str, retries: int = 2) -> dict:
    return call_json(prompt, prompts.SYSTEM, retries=retries, llm_factory=_llm)


TRIPLE_DQ = re.compile(r'"""(?:.|\n)*?"""')
TRIPLE_SQ = re.compile(r"'''(?:.|\n)*?'''")


def _strip_comment(line: str) -> str:
    """Cut the line at the first # that is outside a quoted string (quote-aware, escape-aware)."""
    out, quote, i = [], "", 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                out.append(line[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
            out.append(ch)
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _skeleton(text: str) -> str:
    """The text with docstrings and comments removed — what must stay identical across a docs edit.

    A line that is nothing but a (collapsed) triple-quoted string is a bare string expression — a runtime
    no-op — so it is dropped entirely: ADDING a missing docstring is a legitimate docs edit, not a code change.
    """
    text = TRIPLE_DQ.sub('"""DOC"""', text)
    text = TRIPLE_SQ.sub("'''DOC'''", text)
    lines = [_strip_comment(line).rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines
                     if line.strip() and line.strip() not in ('"""DOC"""', "'''DOC'''"))


def comment_only_change(old_str: str, new_str: str) -> bool:
    """True iff old_str → new_str changes nothing outside comments and (triple-quoted) docstrings."""
    return _skeleton(old_str) == _skeleton(new_str)


@register("docs_writer",
          "updates comments/docstrings made stale by the change, in files this run already changed, and appends "
          "a CHANGELOG.md note. NEVER touches executable code (a deterministic guard rejects any edit that "
          "changes code outside comments/docstrings). Plan it as the final subtask, depends_on the code fix.")
def docs_writer(ctx: dict) -> dict:
    ws = ctx["workspace"]
    results = {**(ctx.get("all_results") or {}), **(ctx.get("upstream") or {})}
    diff = "\n\n".join(r.get("diff", "") for r in results.values() if isinstance(r, dict) and r.get("diff"))
    changed: list[str] = []
    for r in results.values():
        if isinstance(r, dict):
            for p in r.get("files_changed") or []:
                if p not in changed:
                    changed.append(p)
    files = {p: Path(ws, p).read_text(errors="replace") for p in changed if Path(ws, p).is_file()}
    review = ctx.get("review")
    review_txt = (json.dumps(review, indent=2, default=str) if isinstance(review, dict)
                  else "(not yet reviewed — this subtask runs before the review gate)")

    data = _call(prompts.WRITE_DOCS.format(
        brief=ctx["brief"].model_dump_json(indent=2),
        instruction=ctx.get("instruction", ""),
        diff=diff or "(none)",
        review=review_txt,
        files="\n\n".join(f"===== {p} =====\n{t}" for p, t in files.items()) or "(no files changed yet)",
        feedback=prompts.FEEDBACK_NOTE.format(feedback=ctx["feedback"]) if ctx.get("feedback") else "",
    ))
    edits = [e for e in (data.get("edits") or []) if isinstance(e, dict)]
    changelog = str(data.get("changelog") or "").strip()

    _, _, pre_failed, pre_counts = _pytest(ws)
    touched: dict[str, Optional[str]] = {}                     # path -> original content (None = created)

    def _restore() -> None:
        for rel, text in touched.items():
            p = Path(ws, rel)
            if text is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(text)

    def _fail(reason: str, tail: str = "") -> dict:
        _restore()
        return {"status": "failed", "summary": f"rejected: {reason} (workspace restored)"[:300],
                "edits": edits, "diff": "", "files_changed": [], "changelog": "",
                "test_output_tail": tail,
                "tests_passed": pre_counts.get("passed", 0),
                "tests_failed": pre_counts.get("failed", 0) + pre_counts.get("error", 0)}

    try:
        if not edits and not changelog:
            raise PatchError("nothing produced — return comment/docstring edits and/or a changelog note")
        for e in edits:
            rel = str(e.get("path", ""))
            if rel not in files:
                raise PatchError(f"{rel}: docs_writer may only touch files this run changed "
                                 f"({', '.join(changed) or 'none'})")
            if not comment_only_change(str(e.get("old_str", "")), str(e.get("new_str", ""))):
                raise PatchError(f"{rel}: the edit changes executable code — docs_writer may only change "
                                 "comments and docstrings")
            touched.setdefault(rel, Path(ws, rel).read_text(errors="replace"))
            apply_edits(ws, [Edit.model_validate(e)])
        if changelog:
            cl = Path(ws, "CHANGELOG.md")
            touched.setdefault("CHANGELOG.md", cl.read_text(errors="replace") if cl.is_file() else None)
            body = cl.read_text(errors="replace") if cl.is_file() else "# Changelog\n"
            cl.write_text(body.rstrip() + f"\n\n## {ctx['brief'].ticket_id}\n{changelog}\n")
    except PatchError as err:
        return _fail(str(err))

    _, post_out, post_failed, post_counts = _pytest(ws)
    new_failures = sorted(post_failed - pre_failed)
    if new_failures:                                           # documentation must never change behavior
        return _fail(f"documentation edits broke tests: {', '.join(new_failures)}",
                     tail="\n".join(post_out.splitlines()[-30:]))

    doc_diff = ""
    for rel, before in touched.items():
        after = Path(ws, rel).read_text(errors="replace")
        doc_diff += "".join(difflib.unified_diff((before or "").splitlines(keepends=True),
                                                 after.splitlines(keepends=True),
                                                 fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return {"status": "passed",
            "summary": (f"documented: {len(edits)} comment/docstring edit(s)"
                        + (" + CHANGELOG.md note" if changelog else ""))[:300],
            "edits": edits, "diff": doc_diff, "files_changed": sorted(touched), "changelog": changelog,
            "tests_passed": post_counts.get("passed", 0),
            "tests_failed": post_counts.get("failed", 0) + post_counts.get("error", 0)}
