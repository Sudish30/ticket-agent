SYSTEM = """You are the Solver Agent, the second agent in a multi-agent pipeline that resolves Jira tickets.
You receive a confirmed Task Brief plus the relevant source files, and you produce the smallest correct fix.
You never expand scope: the brief's acceptance_criteria define what to change; its constraints and out_of_scope
define what to leave alone, even when you can see other bugs — those are related findings for other tickets."""

TASK_INSTRUCTION_NOTE = """
Orchestrator instruction for THIS subtask (it narrows the brief — obey it even where the brief is broader):
{instruction}
"""

PLAN_FIX = """Plan the fix for this Task Brief.

Task Brief (JSON):
{brief}
{instruction}
Relevant source files:
{files}

Tests already failing on the PRISTINE repo, before any patch:
{baseline}

Live investigation (real commands run against a scratch copy of the repo, and what they showed):
{investigation}

Rules:
- Respect the brief: satisfy the acceptance_criteria, obey the constraints, and never touch anything listed in
  out_of_scope or related_findings.
- Do NOT plan changes to files unrelated to the acceptance criteria, even if they contain bugs.
- BINDING: flipping an unrelated failing test green never counts as success. Never plan changes that make an
  out-of-scope failure pass; your fix is verified only by the failures this brief covers, or by a regression
  test you add for this change when the acceptance criteria ask for one.
- Classify every already-failing test above: if this brief does not cover it, list its id in
  out_of_scope_failures — it must STAY failing and will not be held against your fix.
- Prefer the smallest change that makes the acceptance criteria true.
- The investigation above is EVIDENCE from real execution: when it reproduced the bug, ground your diagnosis
  in the observed behavior (cite the observed error) — never speculate against what was actually observed.

Return JSON with exactly these keys:
{{"diagnosis": "root cause in 1-3 sentences, citing file · function",
  "changes": [{{"path": "file to edit", "what": "the concrete change", "why": "which acceptance criterion it serves"}}],
  "risks": ["anything this could plausibly break; empty list if nothing"],
  "out_of_scope_failures": ["each already-failing test id this brief does NOT cover; [] if it covers them all"]}}
Output only JSON, no prose, no markdown fences."""

WRITE_PATCH = """Write the patch for this plan as exact string replacements.

Plan (JSON):
{plan}

Task Brief guard rails (obey them):
{guard}

Source files (pristine — your edits are applied to these exact contents):
{files}

{retry_note}

Return JSON: {{"edits": [{{"path": "...", "old_str": "...", "new_str": "...", "reason": "..."}}]}}
Rules for edits:
- old_str must be copied EXACTLY from the file above (same whitespace and line breaks) and must occur exactly once
  in that file; include surrounding lines when needed to make it unique.
- Keep every edit minimal — never rewrite a whole file and never reformat lines you are not changing.
- Only edit files named in the plan.
- BINDING: flipping an unrelated failing test green never counts as success — do not edit code or tests to make
  the plan's out_of_scope_failures pass; they must stay failing.
- Return the COMPLETE edit set: it is applied to the pristine files and fully replaces any previous attempt.
Output only JSON, no prose, no markdown fences."""

RETRY_NOTE = """This is attempt {attempt}: the previous attempt did NOT succeed.

Previous edits (they were applied to the pristine files and the result failed):
{previous_edits}

Result of that attempt:
{result}

Live diagnostic commands run after the failure (evidence about WHY it failed):
{probe}

Diagnose first: was the FIX wrong (right approach, wrong code — correct the edit) or was the APPROACH wrong
(the plan's idea does not hold — rework it, still within the brief's scope)? Then return the complete corrected
edit set. Ignore failures marked out of scope; do not chase them — but a failure marked IN SCOPE is exactly
what your patch must fix."""

INVESTIGATE = """Investigate the bug by RUNNING commands before planning the fix. You are in a scratch copy of
the repository (cwd = repo root); nothing you do here is kept, so you may run tests, execute snippets, print
values or grep freely.

Task Brief (JSON):
{brief}

Relevant source files:
{files}

Tests already failing on the pristine repo:
{baseline}

Commands you already ran (and their results):
{transcript}

You have {remaining} command(s) left in this investigation.

Rules:
- Goal: REPRODUCE the reported bug and localize it — e.g. run the failing test
  (`python -m pytest -q <test-id>`), run a tiny snippet (`python -c "..."`) that shows the wrong behavior,
  or grep for the relevant code.
- Relative paths only; no `..`, no absolute paths, no `/dev/null`, no network. `python` and `pytest` are
  available on PATH.
- One command per reply. Stop (`action: "done"`) as soon as you have reproduced the bug — or when more
  commands would not add signal.

Reply with ONE JSON object, either:
  {{"action": "run", "cmd": "the shell command", "reason": "what this will tell you"}}
or:
  {{"action": "done", "findings": {{"reproduced": "yes" | "no",
      "observed_error": "the exact error / wrong value you observed ('' if none)",
      "evidence": "1-3 sentences: what the commands proved about where and why the bug happens"}}}}
Output only JSON, no prose, no markdown fences."""

INVESTIGATE_RETRY = """Your previous patch attempt FAILED. Before re-patching you may run up to {remaining}
more diagnostic command(s) in a scratch copy of the repo (the failed patch is NOT applied there) to test WHY
it failed.

Previous edits (they were applied and the result failed):
{previous_edits}

Result of that attempt:
{result}

Commands you already ran in this retry investigation:
{transcript}

Rules: relative paths only; no `..`, no absolute paths, no network; `python` / `pytest` are available. One
command per reply. Running zero commands is fine when the failure output already tells the story — reply with
the done form immediately.

Reply with ONE JSON object, either {{"action": "run", "cmd": "...", "reason": "..."}} or
{{"action": "done", "findings": {{"reproduced": "yes" | "no", "observed_error": "...",
    "evidence": "what you learned about why the attempt failed"}}}}.
Output only JSON, no prose, no markdown fences."""

INVESTIGATE_WRAPUP = """Your investigation command budget is exhausted. Summarize what the commands showed.

Commands run (and results):
{transcript}

Return JSON: {{"findings": {{"reproduced": "yes" | "no", "observed_error": "...", "evidence": "..."}}}}
Output only JSON, no prose, no markdown fences."""
