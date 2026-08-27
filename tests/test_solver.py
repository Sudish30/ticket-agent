"""Solver Agent tests. The LLM is stubbed by patching solver_agent.graph._call; pytest genuinely runs on a
tiny fixture repo (calc.py with a planted bug), so apply/run/judge behaviour is exercised for real.

Since the investigate node, every run starts with ≥1 investigate reply (INV_DONE = "no commands, here are my
findings"), and every retry inserts one retry-investigate reply (INV_RETRY_DONE) before its write_patch.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solver_agent.graph import PatchError, apply_edits, run
from solver_agent.schemas import Edit

BRIEF = {
    "ticket_id": "QT-9", "ticket_type": "bug",
    "problem_statement": "add() subtracts.", "goal": "add() adds.",
    "acceptance_criteria": ["test_add passes"],
    "suspected_files": [{"path": "calc.py", "reason": "the bug", "confidence": 0.9}],
    "confidence": 0.9,
}

PLAN = {"diagnosis": "calc.py · add() uses '-' instead of '+'.",
        "changes": [{"path": "calc.py", "what": "use +", "why": "test_add"}], "risks": []}

INV_DONE = {"action": "done", "findings": {"reproduced": "yes", "observed_error": "test_add fails: -1 != 5",
                                           "evidence": "add() subtracts instead of adding"}}
INV_RETRY_DONE = {"action": "done", "findings": {"reproduced": "yes", "observed_error": "",
                                                 "evidence": "the edit used the wrong operator"}}
RUN_ECHO = {"action": "run", "cmd": "echo probing", "reason": "look around"}


def _edits(new_str, old_str="    return a - b"):
    return {"edits": [{"path": "calc.py", "old_str": old_str, "new_str": new_str, "reason": "fix add()"}]}


GOOD = _edits("    return a + b")
WRONG = _edits("    return a * b")
NO_MATCH = _edits("    return a + b", old_str="    return a / b")

# The plan classifies the fixture's only baseline failure as out of scope (as if the brief covered another bug).
PLAN_OOS = {**PLAN, "out_of_scope_failures": ["tests/test_calc.py::test_add"]}
COMMENT_ONLY = _edits("# reviewed\ndef add(a, b):", old_str="def add(a, b):")   # applies cleanly, flips nothing
NEW_TEST = {"edits": [{"path": "tests/test_calc.py",
                       "old_str": "def test_add():\n    assert add(2, 3) == 5",
                       "new_str": "def test_add():\n    assert add(2, 3) == 5\n\n\n"
                                  "def test_add_returns_int():\n    assert isinstance(add(1, 1), int)",
                       "reason": "regression test for the change"}]}


class SolverBase(unittest.TestCase):
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

    def _run(self, *replies):
        with patch("solver_agent.graph._call", side_effect=list(replies)) as call:
            solution = run(BRIEF, str(self.repo))
        return solution, call


class SolverGraph(SolverBase):
    def test_first_attempt_success(self):
        s, call = self._run(INV_DONE, PLAN, GOOD)
        self.assertEqual((s.status, s.attempts, s.files_changed), ("passed", 1, ["calc.py"]))
        self.assertIn("-    return a - b", s.diff)
        self.assertIn("+    return a + b", s.diff)
        self.assertIn("a/calc.py", s.diff)
        self.assertGreaterEqual(s.tests_passed, 1)
        self.assertEqual(s.tests_failed, 0)
        self.assertEqual(call.call_count, 3)                       # investigate + plan + one patch
        md = s.to_markdown()
        self.assertIn("# QT-9 — Solution (passed, 1 attempt", md)
        self.assertIn("```diff", md)
        self.assertIn("`calc.py`", md)

    def test_fail_then_pass_feeds_error_back(self):
        s, call = self._run(INV_DONE, PLAN, WRONG, INV_RETRY_DONE, GOOD)
        self.assertEqual((s.status, s.attempts), ("passed", 2))
        probe_prompt = call.call_args_list[3].args[0]              # 4th call = retry investigate
        self.assertIn("return a * b", probe_prompt)                # sees the failed edits
        self.assertIn("test_add", probe_prompt)                    # and the failure output
        retry_prompt = call.call_args_list[4].args[0]              # 5th call = write_patch attempt 2
        self.assertIn("return a * b", retry_prompt)                # previous edits included
        self.assertIn("test_add", retry_prompt)                    # test output included
        self.assertIn("APPROACH", retry_prompt)                    # fix-vs-approach diagnosis instruction
        self.assertIn("the edit used the wrong operator", retry_prompt)   # retry-probe findings ride along
        self.assertIn("+    return a + b", s.diff)                 # final diff comes from a fresh copy

    def test_patch_error_is_a_failed_attempt_and_fed_back(self):
        s, call = self._run(INV_DONE, PLAN, NO_MATCH, INV_RETRY_DONE, GOOD)
        self.assertEqual((s.status, s.attempts), ("passed", 2))
        retry_prompt = call.call_args_list[4].args[0]
        self.assertIn("PATCH ERROR", retry_prompt)
        self.assertIn("old_str not found", retry_prompt)

    def test_attempts_exhausted_is_honest(self):
        s, call = self._run(INV_DONE, PLAN, WRONG, INV_RETRY_DONE, WRONG, INV_RETRY_DONE, WRONG)
        self.assertEqual((s.status, s.attempts), ("failed", 3))
        self.assertEqual(call.call_count, 7)                       # inv + plan + 3 patches + 2 retry-invs
        self.assertIn("test_add", s.test_output_tail)              # the real failing output, no success claim
        self.assertIn("Gave up after 3 attempt(s)", s.rationale)
        self.assertEqual(s.tests_failed, 1)
        self.assertIn("(failed, 3 attempts", s.to_markdown().splitlines()[0])

    def test_new_passing_test_counts_as_verification(self):
        s, call = self._run(INV_DONE, PLAN_OOS, NEW_TEST)
        self.assertEqual((s.status, s.attempts), ("passed", 1))
        self.assertEqual(call.call_count, 3)
        self.assertIn("newly-added passing test", s.rationale)

    def test_nothing_verifiable_is_applied_unverified(self):
        s, _ = self._run(INV_DONE, PLAN_OOS, COMMENT_ONLY)
        self.assertEqual((s.status, s.attempts), ("applied_unverified", 1))
        self.assertIn("cannot verify", s.rationale)
        self.assertIn("(applied_unverified, 1 attempt", s.to_markdown().splitlines()[0])

    def test_short_circuit_no_retries_when_only_out_of_scope_failures_remain(self):
        s, call = self._run(INV_DONE, PLAN_OOS, COMMENT_ONLY, GOOD, GOOD)   # spare stubs never consumed
        self.assertEqual(call.call_count, 3)                      # inv + plan + one patch: settled, not re-solved
        self.assertEqual(s.status, "applied_unverified")

    def test_investigate_runs_commands_then_done(self):
        s, call = self._run(RUN_ECHO,
                            {"action": "run", "cmd": 'python -c "print(\'bug seen\')"', "reason": "show it"},
                            INV_DONE, PLAN, GOOD)
        self.assertEqual(s.status, "passed")
        inv = s.investigation
        self.assertEqual(inv["reproduced"], "yes")
        self.assertEqual([c["cmd"] for c in inv["commands"]],
                         ["echo probing", 'python -c "print(\'bug seen\')"'])
        self.assertEqual(inv["commands"][0]["exit_code"], 0)
        self.assertIn("probing", inv["commands"][0]["stdout"])
        self.assertEqual({c["phase"] for c in inv["commands"]}, {"initial"})
        plan_prompt = call.call_args_list[3].args[0]
        self.assertIn("add() subtracts instead of adding", plan_prompt)   # findings ground plan_fix
        self.assertIn("echo probing", plan_prompt)                        # and the command log rides along
        self.assertIn("## Investigation", s.to_markdown())

    def test_investigate_budget_exhaustion_forces_findings(self):
        wrap = {"findings": {"reproduced": "no", "observed_error": "", "evidence": "budget spent, inconclusive"}}
        s, call = self._run(*([RUN_ECHO] * 6), wrap, PLAN, GOOD)
        self.assertEqual(s.status, "passed")
        self.assertEqual(call.call_count, 9)                       # 6 runs + wrap-up + plan + patch
        self.assertEqual(len(s.investigation["commands"]), 6)      # the budget, fully spent and logged
        self.assertEqual(s.investigation["evidence"], "budget spent, inconclusive")

    def test_affected_areas_paths_are_read_into_context(self):
        (self.repo / "notes.py").write_text("x = 1\n")
        brief = {**BRIEF,
                 "suspected_files": [{"path": "notes.py", "reason": "misleading", "confidence": 0.9}],
                 "affected_areas": ["calc.py · add() subtracts instead of adding"]}
        with patch("solver_agent.graph._call", side_effect=[INV_DONE, PLAN, GOOD]) as call:
            s = run(brief, str(self.repo))
        self.assertEqual(s.status, "passed")
        plan_prompt = call.call_args_list[1].args[0]
        self.assertIn("===== calc.py =====", plan_prompt)         # pulled from affected_areas prose, not imports
        self.assertIn("return a - b", plan_prompt)

    def test_malformed_brief_fails_loudly(self):
        with self.assertRaises(ValueError) as ctx:
            run({"ticket_id": "X"}, str(self.repo))
        self.assertIn("not a valid TaskBrief", str(ctx.exception))

    def test_workspace_mode_patches_shared_dir_in_place(self):
        ws = Path(self.tmp.name) / "ws"
        shutil.copytree(self.repo, ws)
        with patch("solver_agent.graph._call", side_effect=[INV_DONE, PLAN, GOOD]):
            s = run(BRIEF, str(self.repo), workspace=str(ws))
        self.assertEqual(s.status, "passed")
        self.assertIn("return a + b", (ws / "calc.py").read_text())     # patched in the shared workspace

    def test_workspace_mode_restores_on_failure(self):
        ws = Path(self.tmp.name) / "ws"
        shutil.copytree(self.repo, ws)
        with patch("solver_agent.graph._call",
                   side_effect=[INV_DONE, PLAN, WRONG, INV_RETRY_DONE, WRONG, INV_RETRY_DONE, WRONG]):
            s = run(BRIEF, str(self.repo), workspace=str(ws))
        self.assertEqual(s.status, "failed")
        self.assertIn("return a - b", (ws / "calc.py").read_text())     # workspace left as it was found

    def test_task_instruction_reaches_plan_prompt(self):
        with patch("solver_agent.graph._call", side_effect=[INV_DONE, PLAN, GOOD]) as call:
            run(BRIEF, str(self.repo), task_instruction="only fix the TTL, do not write tests")
        self.assertIn("only fix the TTL, do not write tests", call.call_args_list[1].args[0])


class ApplyEdits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "f.py").write_text("x = 1\ny = 2\nx = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_match_errors(self):
        with self.assertRaises(PatchError) as ctx:
            apply_edits(self.tmp.name, [Edit(path="f.py", old_str="nope", new_str="x")])
        self.assertIn("not found", str(ctx.exception))

    def test_multi_match_errors(self):
        with self.assertRaises(PatchError) as ctx:
            apply_edits(self.tmp.name, [Edit(path="f.py", old_str="x = 1", new_str="x = 3")])
        self.assertIn("occurs 2 times", str(ctx.exception))

    def test_unique_match_applies_exactly_once(self):
        apply_edits(self.tmp.name, [Edit(path="f.py", old_str="y = 2", new_str="y = 9")])
        self.assertEqual(Path(self.tmp.name, "f.py").read_text(), "x = 1\ny = 9\nx = 1\n")


if __name__ == "__main__":
    unittest.main()
