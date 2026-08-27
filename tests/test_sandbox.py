"""ticket_agent.sandbox guards and execution. Commands genuinely run (cat/python) in a temp workspace."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_agent.sandbox import MAX_COMMANDS, MAX_OUTPUT, SandboxError, run_cmd


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        Path(self.ws, "hello.txt").write_text("hi\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_runs_and_logs(self):
        log = []
        res = run_cmd(self.ws, "cat hello.txt", log=log)
        self.assertEqual((res["exit_code"], res["stdout"].strip()), (0, "hi"))
        self.assertIn("duration", res)
        self.assertEqual(log, [res])                           # appended to the execution log

    def test_python_resolves_to_this_interpreter(self):
        res = run_cmd(self.ws, 'python -c "print(6*7)"')
        self.assertEqual((res["exit_code"], res["stdout"].strip()), (0, "42"))

    def test_rejects_absolute_path_outside_workspace(self):
        with self.assertRaises(SandboxError) as ctx:
            run_cmd(self.ws, "cat /etc/passwd")
        self.assertIn("outside the workspace", str(ctx.exception))

    def test_allows_absolute_path_inside_workspace(self):
        res = run_cmd(self.ws, f"cat {self.ws}/hello.txt")
        self.assertEqual(res["exit_code"], 0)

    def test_rejects_dotdot_traversal(self):
        with self.assertRaises(SandboxError) as ctx:
            run_cmd(self.ws, "cat ../secrets.txt")
        self.assertIn("traversal", str(ctx.exception))

    def test_budget_enforced_and_refusal_not_logged(self):
        log = [{"cmd": "x"}] * MAX_COMMANDS
        with self.assertRaises(SandboxError) as ctx:
            run_cmd(self.ws, "echo over", log=log)
        self.assertIn("budget", str(ctx.exception))
        self.assertEqual(len(log), MAX_COMMANDS)               # the refused command was never logged

    def test_output_truncated(self):
        res = run_cmd(self.ws, f"python -c \"print('x' * {MAX_OUTPUT * 2})\"")
        self.assertLessEqual(len(res["stdout"]), MAX_OUTPUT + 30)
        self.assertIn("truncated", res["stdout"])

    def test_nonzero_exit_reported_not_raised(self):
        res = run_cmd(self.ws, 'python -c "import sys; sys.exit(3)"')
        self.assertEqual(res["exit_code"], 3)

    def test_proxies_disabled_in_env(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://proxy:1", "https_proxy": "http://proxy:1"}):
            res = run_cmd(self.ws, 'python -c "import os; '
                                   "print(os.environ.get('HTTP_PROXY'), os.environ.get('https_proxy'))\"")
        self.assertEqual(res["stdout"].split(), ["None", "None"])


if __name__ == "__main__":
    unittest.main()
