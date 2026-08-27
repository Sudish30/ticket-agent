"""Orchestrator tests. The graph's LLM is stubbed by patching orchestrator.graph._call; the workers are
replaced through the registry (patch.dict on WORKERS), so worker outputs are fully scripted while the shared
workspace, dependency ordering, retry/replan policy and PR assembly (with a real pytest run) are exercised
for real on the calc fixture repo.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import registry
from orchestrator.graph import run
from orchestrator.workers.docs_writer import comment_only_change, docs_writer
from orchestrator.workers.reviewer import reviewer as real_reviewer
from ticket_agent.schemas import TaskBrief

BRIEF = {
    "ticket_id": "QT-9", "ticket_type": "bug",
    "problem_statement": "add() subtracts.", "goal": "add() adds.",
    "acceptance_criteria": ["test_add passes"],
    "suspected_files": [{"path": "calc.py", "reason": "the bug", "confidence": 0.9}],
    "confidence": 0.9,
}

PLAN_CODE_ONLY = {"subtasks": [
    {"id": "s1", "worker": "code_writer", "instruction": "fix add()", "depends_on": [], "rationale": "AC1"}]}
# s2 listed FIRST but depends on s1 — dependency order must win over list order.
PLAN_TEST_FIRST = {"subtasks": [
    {"id": "s2", "worker": "test_writer", "instruction": "write tests", "depends_on": ["s1"], "rationale": "verify AC1"},
    {"id": "s1", "worker": "code_writer", "instruction": "fix add()", "depends_on": [], "rationale": "AC1"}]}
PLAN_TWO_INDEPENDENT = {"subtasks": [
    {"id": "s1", "worker": "code_writer", "instruction": "fix add()", "depends_on": [], "rationale": "AC1"},
    {"id": "s2", "worker": "test_writer", "instruction": "write tests", "depends_on": [], "rationale": "verify AC1"}]}
REPLAN_RESP = {"subtasks": [
    {"id": "r1", "worker": "code_writer", "instruction": "fix add() differently", "depends_on": [], "rationale": "AC1"},
    {"id": "r2", "worker": "test_writer", "instruction": "write tests", "depends_on": [], "rationale": "verify AC1"}]}
EVAL_ACCEPT = {"verdict": "accept", "feedback": "", "reason": "meets the instruction"}
EVAL_RETRY = {"verdict": "retry_with_feedback", "feedback": "use X", "reason": "wrong approach"}
PR_STUB = {"pr_title": "QT-9: make add() add", "pr_description": "**What** ... **Why** ... **How tested** ... **Risks** ..."}
CHECK_NAMES = ["acceptance_criteria", "constraints", "out_of_scope", "regressions_security", "tests_assert_acs"]
APPROVE_REVIEW = {"verdict": "approve",
                  "checks": [{"name": n, "result": "pass", "note": "ok"} for n in CHECK_NAMES],
                  "change_requests": []}
BLOCK_REVIEW = {"verdict": "request_changes",
                "checks": [{"name": "acceptance_criteria", "result": "fail", "note": "AC1 unmet"}],
                "change_requests": [{"file": "calc.py", "issue": "add() still subtracts for negatives",
                                     "suggestion": "handle negative operands", "severity": "blocker"}]}
MINOR_REVIEW = {"verdict": "request_changes",
                "checks": [{"name": "regressions_security", "result": "warn", "note": "style only"}],
                "change_requests": [{"file": "calc.py", "issue": "missing docstring",
                                     "suggestion": "add one", "severity": "minor"}]}


def scripted_worker(*results):
    """A fake registry worker that returns the scripted results in order (last one repeats) and records ctxs."""
    calls = []

    def worker(ctx):
        calls.append(ctx)
        return dict(results[min(len(calls) - 1, len(results) - 1)])
    worker.calls = calls
    return worker


class OrchestratorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "fixture_repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "tests" / "conftest.py").write_text(
            "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n")
        (self.repo / "tests" / "test_calc.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, workers, llm_replies):
        workers = {"reviewer": scripted_worker(APPROVE_REVIEW), **workers}   # the gate runs on every path
        with patch.dict(registry.WORKERS, workers), \
             patch("orchestrator.graph._call", side_effect=list(llm_replies)) as call:
            pr = run(BRIEF, str(self.repo))
        return pr, call


class Routing(OrchestratorBase):
    def test_dependency_ordering_respected(self):
        order = []
        cw = scripted_worker({"status": "passed", "summary": "fixed"})
        tw = scripted_worker({"status": "passed", "summary": "tests added",
                              "tests_added": ["tests/test_calc.py::test_new"]})

        def cw_rec(ctx):
            order.append("code")
            return cw(ctx)

        def tw_rec(ctx):
            order.append("test")
            return tw(ctx)
        pr, call = self._run({"code_writer": cw_rec, "test_writer": tw_rec},
                             [PLAN_TEST_FIRST, EVAL_ACCEPT, PR_STUB])
        self.assertEqual(order, ["code", "test"])              # s1 before s2 despite s2 listed first
        self.assertEqual(pr.status, "complete")
        self.assertEqual({s.status for s in pr.subtasks}, {"accepted"})
        self.assertEqual(tw.calls[0]["upstream"].keys(), {"s1"})   # dependency result handed to the worker

    def test_route_passed_accepts_without_llm_judge(self):
        cw = scripted_worker({"status": "passed", "summary": "fixed"})
        pr, call = self._run({"code_writer": cw}, [PLAN_CODE_ONLY, PR_STUB])
        self.assertEqual(call.call_count, 2)                   # plan + PR: no evaluate call for "passed"
        self.assertEqual((pr.status, pr.subtasks[0].status, pr.subtasks[0].attempts), ("complete", "accepted", 1))

    def test_route_applied_unverified_appends_test_writer_and_verifies(self):
        cw = scripted_worker({"status": "applied_unverified", "summary": "applied_unverified: clean patch",
                              "rationale": "fixed the TTL comparison", "files_changed": ["calc.py"],
                              "diff": "--- a/calc.py\n+++ b/calc.py\n"})
        tw = scripted_worker({"status": "passed", "summary": "added regression test",
                              "tests_added": ["tests/test_calc.py::test_ttl_regression"]})
        pr, call = self._run({"code_writer": cw, "test_writer": tw}, [PLAN_CODE_ONLY, EVAL_ACCEPT, PR_STUB])
        self.assertEqual(len(cw.calls), 1)                     # never retried
        self.assertEqual(len(pr.subtasks), 2)                  # verification subtask was appended
        appended = pr.subtasks[1]
        self.assertEqual((appended.id, appended.worker), ("verify-s1", "test_writer"))
        self.assertIn("regression test", appended.instruction)
        self.assertIn("fixed the TTL comparison", appended.instruction)   # scoped to exactly this fix
        self.assertIn("verified by new regression test", pr.subtasks[0].summary)
        self.assertEqual(pr.status, "complete")
        self.assertEqual(pr.new_tests_added, ["tests/test_calc.py::test_ttl_regression"])

    def test_route_failed_retries_with_feedback(self):
        cw = scripted_worker({"status": "failed", "summary": "boom", "test_output_tail": "E boom"},
                             {"status": "passed", "summary": "fixed"})
        pr, call = self._run({"code_writer": cw}, [PLAN_CODE_ONLY, EVAL_RETRY, PR_STUB])
        self.assertEqual(len(cw.calls), 2)
        self.assertEqual(cw.calls[0]["feedback"], "")
        self.assertIsNone(cw.calls[0]["previous_result"])
        self.assertEqual(cw.calls[1]["feedback"], "use X")     # evaluator feedback reaches the retry
        self.assertEqual(cw.calls[1]["previous_result"]["summary"], "boom")   # and so does the failed attempt
        eval_prompt = call.call_args_list[1].args[0]
        self.assertIn("boom", eval_prompt)                     # the failed output was shown to the judge
        self.assertIn("failed", eval_prompt)
        self.assertEqual((pr.subtasks[0].status, pr.subtasks[0].attempts), ("accepted", 2))
        self.assertEqual(pr.status, "complete")

    def test_retry_exhausted_one_replan_then_partial(self):
        cw = scripted_worker({"status": "failed", "summary": "boom"})   # fails forever
        tw = scripted_worker({"status": "passed", "summary": "tests added",
                              "tests_added": ["tests/test_calc.py::test_new"]})
        pr, call = self._run({"code_writer": cw, "test_writer": tw},
                             [PLAN_TWO_INDEPENDENT,                     # s1 code (fails), s2 test
                              EVAL_RETRY, EVAL_RETRY,                   # s1 attempts 1, 2 → feedback retries
                              REPLAN_RESP,                              # attempt 3 → the one replan
                              EVAL_RETRY, EVAL_RETRY,                   # r1 attempts 1, 2
                              EVAL_ACCEPT,                              # r2 accepted
                              PR_STUB])
        self.assertEqual(call.call_count, 8)                   # exactly one replan, no second
        self.assertEqual(len(cw.calls), 6)                     # 3 attempts on s1 + 3 on r1
        by_id = {s.id: s for s in pr.subtasks}
        self.assertEqual(by_id["s1"].status, "failed")
        self.assertIn("replanned", by_id["s1"].summary)
        self.assertEqual(by_id["s2"].status, "skipped")        # superseded, honestly reported
        self.assertEqual(by_id["r1"].status, "failed")
        self.assertIn("exhausted", by_id["r1"].summary)
        self.assertEqual((by_id["r2"].status, pr.status), ("accepted", "partial"))

    def test_evaluator_rejection_restores_workspace(self):
        calls, saw_changelog = [], []

        def appender(ctx):                                     # internally successful on EVERY attempt
            calls.append(ctx)
            cl = Path(ctx["workspace"], "CHANGELOG.md")
            saw_changelog.append(cl.is_file())
            cl.write_text((cl.read_text() if cl.is_file() else "# Changelog\n") + "## QT-9\n- entry\n")
            return {"status": "passed", "summary": "wrote changelog", "files_changed": ["CHANGELOG.md"]}

        plan = {"subtasks": [{"id": "s1", "worker": "test_writer", "instruction": "write the changelog",
                              "depends_on": [], "rationale": "AC"}]}
        pr, call = self._run({"test_writer": appender}, [plan, EVAL_RETRY, EVAL_ACCEPT, PR_STUB])
        self.assertEqual(pr.status, "complete")
        self.assertEqual(len(calls), 2)                        # rejected by the evaluator once, then retried
        self.assertEqual(saw_changelog, [False, False])        # the retry saw the PRE-DISPATCH workspace
        ws = Path(calls[0]["workspace"])
        self.assertEqual((ws / "CHANGELOG.md").read_text().count("## QT-9"), 1)   # no duplicate artifacts
        self.assertIn("CHANGELOG.md", pr.files_changed)        # the accepted attempt's work is kept

    def test_pr_assembly_diff_and_tests(self):
        def fixing_cw(ctx):                                    # actually patches the shared workspace
            p = Path(ctx["workspace"], "calc.py")
            p.write_text(p.read_text().replace("a - b", "a + b"))
            return {"status": "passed", "summary": "fixed add()"}
        pr, call = self._run({"code_writer": fixing_cw}, [PLAN_CODE_ONLY, PR_STUB])
        self.assertEqual(pr.files_changed, ["calc.py"])
        self.assertIn("-    return a - b", pr.combined_diff)
        self.assertIn("+    return a + b", pr.combined_diff)
        self.assertEqual((pr.tests_passed, pr.tests_failed), (1, 0))   # real pytest in the workspace
        self.assertEqual((pr.pr_title, pr.status), ("QT-9: make add() add", "complete"))
        md = pr.to_markdown()
        self.assertIn("# QT-9: make add() add", md)
        self.assertIn("```diff", md)
        self.assertIn("| s1 | code_writer | accepted | 1 |", md)


class ReviewGate(OrchestratorBase):
    @staticmethod
    def _fixing_cw(ctx):                                       # actually patches the shared workspace
        p = Path(ctx["workspace"], "calc.py")
        p.write_text(p.read_text().replace("a - b", "a + b"))
        return {"status": "passed", "summary": "fixed add()"}

    def test_approve_path(self):
        rv = scripted_worker(APPROVE_REVIEW)
        pr, call = self._run({"code_writer": self._fixing_cw, "reviewer": rv}, [PLAN_CODE_ONLY, PR_STUB])
        self.assertEqual((pr.status, pr.review.verdict, pr.review.rounds), ("complete", "approve", 1))
        self.assertEqual(len(rv.calls), 1)
        ctx = rv.calls[0]
        self.assertNotIn("all_results", ctx)                   # independent judgment: no worker rationales
        self.assertNotIn("upstream", ctx)
        self.assertIn("calc.py", ctx["files"])                 # full changed-file content, not just the diff
        self.assertIn("return a + b", ctx["files"]["calc.py"])
        self.assertIn("-    return a - b", ctx["diff"])
        self.assertEqual(ctx["baseline_failed"], ["tests/test_calc.py::test_add"])   # pre-existing facts provided
        self.assertEqual(ctx["failing_tests"], [])             # ...and the exact currently-failing ids (none: fixed)
        md = pr.to_markdown()
        self.assertIn("## Review", md)
        self.assertIn("acceptance_criteria", md)

    def test_blocker_repair_then_approve(self):
        cw = scripted_worker({"status": "passed", "summary": "fixed"})
        rv = scripted_worker(BLOCK_REVIEW, APPROVE_REVIEW)
        pr, call = self._run({"code_writer": cw, "reviewer": rv}, [PLAN_CODE_ONLY, PR_STUB, PR_STUB])
        self.assertEqual((pr.status, pr.review.verdict, pr.review.rounds), ("complete", "approve", 2))
        self.assertEqual(len(rv.calls), 2)
        self.assertEqual(len(cw.calls), 2)                     # the original subtask + the repair subtask
        self.assertIn("add() still subtracts for negatives", cw.calls[1]["instruction"])
        self.assertIn("nothing else", cw.calls[1]["instruction"])
        by_id = {s.id: s for s in pr.subtasks}
        self.assertEqual(by_id["repair-1"].status, "accepted")

    def test_blocker_repair_still_blocked_needs_human_review(self):
        cw = scripted_worker({"status": "passed", "summary": "fixed"})
        rv = scripted_worker(BLOCK_REVIEW)                     # blocks every round
        pr, call = self._run({"code_writer": cw, "reviewer": rv}, [PLAN_CODE_ONLY, PR_STUB, PR_STUB])
        self.assertEqual(pr.status, "needs_human_review")
        self.assertEqual((pr.review.verdict, pr.review.rounds), ("request_changes", 2))
        self.assertEqual(len(rv.calls), 2)                     # exactly one repair round — never loops
        self.assertEqual(pr.review.change_requests[0].severity, "blocker")
        md = pr.to_markdown()
        self.assertIn("🛑", md)
        self.assertIn("Change requests (blocking)", md)

    def test_minors_only_is_approve_with_followups(self):
        cw = scripted_worker({"status": "passed", "summary": "fixed"})
        rv = scripted_worker(MINOR_REVIEW)
        pr, call = self._run({"code_writer": cw, "reviewer": rv}, [PLAN_CODE_ONLY, PR_STUB])
        self.assertEqual((pr.status, pr.review.verdict), ("complete", "approve"))   # normalized from minors-only
        self.assertEqual(len(rv.calls), 1)                     # no repair round for minors
        self.assertEqual(rv.calls[0]["failing_tests"], ["tests/test_calc.py::test_add"])   # exact failing ids
        self.assertEqual(pr.review.change_requests[0].severity, "minor")
        self.assertIn("Follow-ups (minor)", pr.to_markdown())

    def test_repair_failure_does_not_consume_replan(self):
        cw = scripted_worker({"status": "passed", "summary": "fixed"},      # s1 passes...
                             {"status": "failed", "summary": "cannot"})     # ...the repair attempts all fail
        rv = scripted_worker(BLOCK_REVIEW)                     # blocks both rounds
        pr, call = self._run({"code_writer": cw, "reviewer": rv},
                             [PLAN_CODE_ONLY, PR_STUB, EVAL_RETRY, EVAL_RETRY, PR_STUB])
        self.assertEqual(call.call_count, 5)                   # plan + 2 PRs + 2 retry-feedbacks — NO replan call
        self.assertEqual(pr.status, "needs_human_review")
        by_id = {s.id: s for s in pr.subtasks}
        self.assertEqual(by_id["repair-1"].status, "failed")
        self.assertIn("exhausted", by_id["repair-1"].summary)
        self.assertEqual(len(rv.calls), 2)                     # still exactly one re-review

    def test_planned_reviewer_subtasks_are_dropped(self):
        plan = {"subtasks": [
            {"id": "s0", "worker": "reviewer", "instruction": "review it", "depends_on": [], "rationale": "?"},
            {"id": "s1", "worker": "code_writer", "instruction": "fix add()", "depends_on": [], "rationale": "AC1"}]}
        cw = scripted_worker({"status": "passed", "summary": "fixed"})
        pr, call = self._run({"code_writer": cw}, [plan, PR_STUB])
        self.assertEqual([s.worker for s in pr.subtasks], ["code_writer"])   # the gate is never a planned subtask
        self.assertEqual(pr.status, "complete")


class InvestigationSurface(OrchestratorBase):
    def test_investigation_surfaces_in_pr_package(self):
        cw = scripted_worker({"status": "passed", "summary": "fixed",
                              "investigation": {"reproduced": "yes", "observed_error": "1 failed",
                                                "evidence": "ran the failing test and saw it",
                                                "commands": [{"cmd": "python -m pytest -q", "exit_code": 1,
                                                              "stdout": "1 failed", "stderr": "",
                                                              "duration": 0.1, "phase": "initial"}]}})
        pr, call = self._run({"code_writer": cw}, [PLAN_CODE_ONLY, PR_STUB])
        self.assertEqual(len(pr.investigation), 1)
        self.assertEqual(pr.investigation[0]["subtask"], "s1")
        self.assertEqual(pr.investigation[0]["reproduced"], "yes")
        md = pr.to_markdown()
        self.assertIn("## Investigation", md)
        self.assertIn("reproduced: yes", md)
        self.assertIn("<details><summary>Command log", md)
        self.assertIn("python -m pytest -q", md)


class ReviewerProbe(OrchestratorBase):
    """The REAL reviewer worker, with only its LLM stubbed: probes and the revert-check genuinely run."""

    def _ctx(self, new_tests=()):
        ws = Path(self.tmp.name) / "ws"
        shutil.copytree(self.repo, ws)
        p = ws / "calc.py"
        p.write_text(p.read_text().replace("a - b", "a + b"))          # the "fix" is applied in the workspace
        files = {"calc.py": p.read_text()}
        if new_tests:
            (ws / "tests" / "test_new.py").write_text(
                "from calc import add\n\n\ndef test_add_regression():\n    assert add(2, 3) == 5\n")
            files["tests/test_new.py"] = (ws / "tests" / "test_new.py").read_text()
        return {"brief": TaskBrief.model_validate(BRIEF), "repo": str(self.repo), "workspace": str(ws),
                "diff": "--- a/calc.py\n+++ b/calc.py\n-    return a - b\n+    return a + b\n",
                "files": files, "tests_passed": 2, "tests_failed": 0,
                "new_tests": list(new_tests), "failing_tests": [], "baseline_failed": ["tests/test_calc.py::test_add"]}

    def test_probe_commands_run_in_scratch_and_are_logged(self):
        ctx = self._ctx()
        replies = [{"action": "run", "cmd": "python -m pytest -q", "reason": "run the suite myself"},
                   {"action": "done"},
                   APPROVE_REVIEW]
        with patch("orchestrator.workers.reviewer._call", side_effect=replies) as call:
            res = real_reviewer(ctx)
        self.assertEqual(res["verdict"], "approve")
        self.assertEqual(len(res["probe_log"]), 1)                     # one probe; no revert-check without new tests
        self.assertEqual(res["probe_log"][0]["cmd"], "python -m pytest -q")
        self.assertEqual(res["probe_log"][0]["exit_code"], 0)          # the fixed scratch suite is green
        review_prompt = call.call_args_list[-1].args[0]
        self.assertIn("python -m pytest -q", review_prompt)            # probe transcript shown to the reviewer
        note = next(c["note"] for c in res["checks"] if c["name"] == "tests_assert_acs")
        self.assertIn("reasoned", note)                                # no new tests → no empirical check

    def test_discrimination_check_verified_empirically(self):
        ctx = self._ctx(new_tests=["tests/test_new.py::test_add_regression"])
        replies = [{"action": "done"}, APPROVE_REVIEW]
        with patch("orchestrator.workers.reviewer._call", side_effect=replies):
            res = real_reviewer(ctx)
        self.assertEqual(len(res["probe_log"]), 1)                     # just the deterministic revert-check
        self.assertIn("pytest", res["probe_log"][0]["cmd"])
        self.assertNotEqual(res["probe_log"][0]["exit_code"], 0)       # the new test FAILS on reverted code
        note = next(c["note"] for c in res["checks"] if c["name"] == "tests_assert_acs")
        self.assertIn("empirically verified", note)
        self.assertIn("a + b", (Path(ctx["workspace"]) / "calc.py").read_text())   # real workspace untouched

    def test_discrimination_check_catches_non_discriminating_tests(self):
        ctx = self._ctx(new_tests=["tests/test_new.py::test_trivial"])
        (Path(ctx["workspace"]) / "tests" / "test_new.py").write_text("def test_trivial():\n    assert True\n")
        replies = [{"action": "done"}, APPROVE_REVIEW]
        with patch("orchestrator.workers.reviewer._call", side_effect=replies):
            res = real_reviewer(ctx)
        note = next(c["note"] for c in res["checks"] if c["name"] == "tests_assert_acs")
        self.assertIn("still PASS", note)                              # trivial test survives the revert = no proof
        self.assertIn("empirical check FAILED", note)


class DocsGuard(unittest.TestCase):
    def test_rejects_code_change(self):
        self.assertFalse(comment_only_change("    return a - b", "    return a + b"))

    def test_rejects_string_value_change(self):
        self.assertFalse(comment_only_change('mode = "Strict"', 'mode = "Lax"'))

    def test_rejects_code_change_hidden_behind_same_comment(self):
        self.assertFalse(comment_only_change("x = 1  # note", "x = 2  # note"))

    def test_accepts_comment_addition(self):
        self.assertTrue(comment_only_change("x = 1", "x = 1  # tuned in QT-9"))

    def test_accepts_comment_text_change(self):
        self.assertTrue(comment_only_change('x = "Lax"  # hardened in PROJ-130',
                                            'x = "Lax"  # Lax since NOTE-142: allows OAuth callbacks'))

    def test_accepts_docstring_change(self):
        self.assertTrue(comment_only_change('"""Old text."""', '"""New, much better text."""'))

    def test_accepts_added_full_comment_line(self):
        self.assertTrue(comment_only_change("def f():\n    pass", "def f():\n    # explains why\n    pass"))

    def test_accepts_adding_a_missing_docstring(self):
        self.assertTrue(comment_only_change("def f():\n    return 1",
                                            'def f():\n    """Explains f, added later."""\n    return 1'))

    def test_accepts_adding_a_multiline_docstring(self):
        self.assertTrue(comment_only_change(
            "def f():\n    return 1",
            'def f():\n    """Explains f.\n\n    In detail, over lines.\n    """\n    return 1'))

    def test_rejects_code_smuggled_next_to_a_docstring(self):
        self.assertFalse(comment_only_change("def f():\n    pass",
                                             'def f():\n    """doc"""; x = 1\n    pass'))

    def test_hash_inside_string_is_not_a_comment(self):
        self.assertFalse(comment_only_change('x = "a#b"', 'x = "a#c"'))   # string value change, not a comment


class DocsWorker(OrchestratorBase):
    def _ctx(self):
        ws = Path(self.tmp.name) / "ws"
        shutil.copytree(self.repo, ws)
        ctx = {"brief": TaskBrief.model_validate(BRIEF), "workspace": str(ws),
               "instruction": "document the fix", "feedback": "", "review": None, "upstream": {},
               "all_results": {"s1": {"diff": "--- a/calc.py\n+++ b/calc.py\n", "files_changed": ["calc.py"]}}}
        return ctx, ws

    def test_accepts_comment_edit_and_writes_changelog(self):
        ctx, ws = self._ctx()
        reply = {"edits": [{"path": "calc.py", "old_str": "def add(a, b):",
                            "new_str": "# adds two numbers (fixed in QT-9)\ndef add(a, b):", "reason": "clarify"}],
                 "changelog": "- QT-9: add() now adds instead of subtracting."}
        with patch("orchestrator.workers.docs_writer._call", return_value=reply):
            res = docs_writer(ctx)
        self.assertEqual(res["status"], "passed")
        self.assertIn("# adds two numbers", (ws / "calc.py").read_text())
        changelog = (ws / "CHANGELOG.md").read_text()
        self.assertIn("## QT-9", changelog)
        self.assertIn("- QT-9: add() now adds instead of subtracting.", changelog)
        self.assertEqual(res["files_changed"], ["CHANGELOG.md", "calc.py"])
        self.assertIn("a/CHANGELOG.md", res["diff"])

    def test_rejects_code_edit_and_restores(self):
        ctx, ws = self._ctx()
        reply = {"edits": [{"path": "calc.py", "old_str": "    return a - b",
                            "new_str": "    return a + b", "reason": "sneaky fix disguised as docs"}],
                 "changelog": "- fix"}
        with patch("orchestrator.workers.docs_writer._call", return_value=reply):
            res = docs_writer(ctx)
        self.assertEqual(res["status"], "failed")
        self.assertIn("executable code", res["summary"])
        self.assertIn("return a - b", (ws / "calc.py").read_text())     # untouched
        self.assertFalse((ws / "CHANGELOG.md").exists())                # nothing half-applied

    def test_rejects_edits_outside_changed_files(self):
        ctx, ws = self._ctx()
        reply = {"edits": [{"path": "tests/test_calc.py", "old_str": "from calc import add",
                            "new_str": "from calc import add  # note", "reason": "not a changed file"}],
                 "changelog": ""}
        with patch("orchestrator.workers.docs_writer._call", return_value=reply):
            res = docs_writer(ctx)
        self.assertEqual(res["status"], "failed")
        self.assertIn("only touch files this run changed", res["summary"])


if __name__ == "__main__":
    unittest.main()
