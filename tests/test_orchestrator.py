"""Orchestrator tests. The graph's LLM is stubbed by patching orchestrator.graph._call; the workers are
replaced through the registry (patch.dict on WORKERS), so worker outputs are fully scripted while the shared
workspace, dependency ordering, retry/replan policy and PR assembly (with a real pytest run) are exercised
for real on the calc fixture repo.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import registry
from orchestrator.graph import run

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


if __name__ == "__main__":
    unittest.main()
