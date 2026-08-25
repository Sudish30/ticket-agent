SYSTEM = """You are the Orchestrator, the tech-lead agent of a multi-agent pipeline that resolves Jira tickets.
You receive a confirmed Task Brief, break it into subtasks for specialist workers, judge their results, and
assemble the final pull-request package. You never expand scope beyond the brief, and you are honest about
anything that failed, was skipped, or could not be verified."""

PLAN_SUBTASKS = """Break this Task Brief into the smallest set of subtasks that satisfies its acceptance criteria.

Task Brief (JSON):
{brief}

Available workers (the registry — use only these names):
{workers}

Rules:
- Every subtask must be justified by the brief: its rationale must cite the acceptance criterion or constraint it serves.
- Typical shape: one code-change subtask, then a test subtask (depends_on the code change) verifying the acceptance criteria.
- instruction is the worker's marching orders — concrete and scoped (e.g. "only fix the token TTL comparison; do not write tests").
- depends_on lists subtask ids that must be ACCEPTED before this one runs.
- Do not invent work the brief does not ask for; out_of_scope and related_findings stay untouched.

Return JSON: {{"subtasks": [{{"id": "s1", "worker": "...", "instruction": "...", "depends_on": [], "rationale": "..."}}]}}
Output only JSON, no prose, no markdown fences."""

EVALUATE = """Judge this worker result against its subtask instruction and the Task Brief.

Subtask (attempt {attempt} of {max_attempts}):
{subtask}

Task Brief (JSON):
{brief}

Worker output (JSON, long strings truncated):
{result}

{policy}

Return JSON: {{"verdict": "accept" | "retry_with_feedback" | "replan",
  "feedback": "if retrying: concrete, actionable feedback for the worker — what was wrong and what to do instead; else empty",
  "reason": "one sentence"}}
Output only JSON, no prose, no markdown fences."""

POLICY_FAILED = ('The worker reported status "failed", so "accept" is not available: choose retry_with_feedback '
                 "(fixable by the same worker with better guidance) or replan (the plan itself is wrong). Either "
                 "way, write feedback that names the concrete failure.")

REPLAN = """Re-plan the remaining work: a subtask exhausted its retries.

Task Brief (JSON):
{brief}

Already ACCEPTED subtasks (their work is applied in the workspace and stays — do not repeat it):
{accepted}

Failed / unresolved subtasks and their history:
{failed}

Return the NEW subtasks for the remaining work only, with fresh unique ids (e.g. "r1"), same JSON shape as planning.
depends_on may reference accepted ids or new ids. Use a different approach than the one that failed.
Return JSON: {{"subtasks": [{{"id": "r1", "worker": "...", "instruction": "...", "depends_on": [], "rationale": "..."}}]}}
Output only JSON, no prose, no markdown fences."""

WRITE_TESTS = """Write pytest tests that verify this change against the Task Brief's acceptance criteria.

Task Brief (JSON):
{brief}

Your subtask instruction:
{instruction}

Code change made by the code writer (unified diff; "(none)" if none):
{diff}

Existing test files in the workspace (your edits are applied to these exact contents):
{tests}
{previous}{feedback}
Rules:
- Only touch files under tests/ — never application code.
- The tests must pass against the CURRENT patched code; reuse existing fixtures and conftest helpers.
- Do not duplicate tests that already exist; add the missing coverage for the acceptance criteria.
- Module-level globals in the application (in-memory stores, caches, used-token sets) persist ACROSS tests in
  one pytest process: an earlier test may already have consumed the exact resource your test would recreate
  (e.g. an identical token issued in the same clock second). Make every test self-contained — create your own
  users/tokens/records instead of reusing ones other tests already touched.
- To modify an existing test file: old_str must be copied EXACTLY (whitespace included) and occur exactly once in it.
- To create a NEW test file: use old_str "" and put the complete file content in new_str.
- List every test you add in new_tests as "tests/test_file.py::test_name".

Return JSON: {{"edits": [{{"path": "tests/...", "old_str": "...", "new_str": "...", "reason": "..."}}],
  "new_tests": ["tests/test_file.py::test_name"]}}
Output only JSON, no prose, no markdown fences."""

FEEDBACK_NOTE = """
Feedback from the evaluator on your previous rejected attempt (address it):
{feedback}
"""

TW_RETRY_NOTE = """
This is a RETRY: your previous attempt was rejected and rolled back — the workspace is pristine again.

Your previous edits:
{edits}

What happened:
{result}

Diagnose before writing: did the tests fail because of the change under test, or because of test-environment
interference (wrong patch target, cross-test state, fixture misuse)? Then return a corrected COMPLETE edit set.
"""

REVIEW = """You are the final review gate for this change. Judge INDEPENDENTLY — you see only the Task Brief,
the diff, the changed files and the test results, never the workers' reasoning.

Task Brief (JSON):
{brief}

Final test results: {tests}

Combined diff:
{diff}

Full post-change content of every changed file:
{files}

Run exactly these checks, in this order, one entry each in "checks":
1. "acceptance_criteria" — EVERY acceptance criterion is addressed: quote each AC and point at the diff line(s)
   or the new test that satisfies it. Any AC not addressed → result "fail".
2. "constraints" — every constraint in the brief is respected by the change.
3. "out_of_scope" — nothing listed in out_of_scope or related_findings was touched.
4. "regressions_security" — no obvious regressions or security issues in the changed code (injection, secrets,
   broken auth, resource leaks, behavior changes beyond the brief).
5. "tests_assert_acs" — the new tests genuinely assert the acceptance criteria (real setup, meaningful
   assertions on the behavior each AC describes), not trivially passing: no assert True, no tautologies,
   no tests that would pass even without the fix.

Verdict rules:
- Any "fail" check → verdict "request_changes" with at least one change request explaining it.
- severity "blocker" = must be fixed before merge (an AC unmet, a constraint violated, out-of-scope edits, a
  real regression/security problem, tests that do not prove the fix). severity "minor" = worth a follow-up but
  not blocking. The test results name the tests that were already failing BEFORE any change: those are
  pre-existing, and when the brief leaves them out of scope they are NOT blockers and do not fail an
  "existing tests keep passing" criterion — note them with a "warn" instead. An acceptance criterion that can
  only be verified manually (e.g. on a real device) is a "warn" with a note, not a blocker.
- Every change request names the file, the concrete issue, and an actionable suggestion.

Return JSON:
{{"verdict": "approve" | "request_changes",
  "checks": [{{"name": "...", "result": "pass" | "fail" | "warn", "note": "..."}}],
  "change_requests": [{{"file": "...", "issue": "...", "suggestion": "...", "severity": "blocker" | "minor"}}]}}
Output only JSON, no prose, no markdown fences."""

PR_PACKAGE = """Write the pull-request title and description for this completed run.

Task Brief (JSON):
{brief}

Subtask outcomes (JSON):
{subtasks}

Final test suite in the workspace: {tests_passed} passed, {tests_failed} failed. New tests added: {new_tests}

Combined diff (may be truncated):
{diff}

Rules:
- pr_title: one line, imperative, ticket id first (e.g. "NOTE-157: ...").
- pr_description: Markdown with sections **What**, **Why**, **How tested**, **Risks** — referencing the brief's
  problem and acceptance criteria, not restating the diff line by line.
- Be honest: name anything that failed, was skipped, or could not be verified, and any pre-existing failures
  deliberately left alone as out of scope.
Return JSON: {{"pr_title": "...", "pr_description": "..."}}
Output only JSON, no prose, no markdown fences."""
