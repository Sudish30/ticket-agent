"""quorum_backend.github_pr with the `_run` subprocess seam patched — no network, no real git/gh."""
import os
import tempfile
import unittest
from unittest.mock import patch

from quorum_backend import github_pr


class Slug(unittest.TestCase):
    def test_slug_normalises_and_caps(self):
        self.assertEqual(github_pr._slug(" Accept '+' plus-addressed emails! "), "accept-plus-addressed-emails")
        self.assertEqual(github_pr._slug("!!!"), "fix")                    # nothing usable -> stable fallback
        self.assertLessEqual(len(github_pr._slug("word " * 30)), 40)


class RetargetNewFiles(unittest.TestCase):
    def test_created_files_get_dev_null_existing_files_kept(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "forms"))
            with open(os.path.join(d, "forms", "validators.py"), "w") as f:
                f.write("x = 1\n")
            diff = ("--- a/forms/validators.py\n+++ b/forms/validators.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
                    "--- a/CHANGELOG.md\n+++ b/CHANGELOG.md\n@@ -0,0 +1 @@\n+## QT-002\n")
            out = github_pr._retarget_new_files(diff, d)
            self.assertIn("--- a/forms/validators.py\n", out)              # exists in clone: untouched
            self.assertIn("--- /dev/null\n+++ b/CHANGELOG.md\n", out)      # created by the run: /dev/null
            self.assertNotIn("--- a/CHANGELOG.md", out)


class OpenPRFromPackage(unittest.TestCase):
    PKG = {"combined_diff": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n",
           "pr_title": "QT-002: Accept plus-addressed emails",
           "pr_description": "What / why / how tested"}

    def test_happy_path_commands_and_url(self):
        calls = []

        def fake_run(cmd, cwd=None, input_text=None):
            calls.append((cmd, input_text))
            if cmd[:3] == ["gh", "pr", "create"]:
                return "\nhttps://github.com/Sudish30/notely-demo/pull/1\n"
            return ""

        with patch.object(github_pr, "_run", side_effect=fake_run):
            url = github_pr.open_pr_from_package(self.PKG, "Sudish30/notely-demo", "QT-002")
        self.assertEqual(url, "https://github.com/Sudish30/notely-demo/pull/1")

        self.assertEqual(calls[0][0][:2], ["git", "clone"])
        self.assertIn("https://github.com/Sudish30/notely-demo.git", calls[0][0])
        branch = calls[1][0][-1]                                           # git checkout -b <branch>
        self.assertEqual(branch, "agent/QT-002-accept-plus-addressed-emails")
        apply_call = next(c for c in calls if "apply" in c[0])
        self.assertIn("+b\n", apply_call[1])                               # the diff went in via stdin
        commit = next(c[0] for c in calls if "commit" in c[0])
        self.assertIn("user.name=Ticket Agent", commit)                    # author per spec
        self.assertIn(self.PKG["pr_title"], commit)
        push = next(c[0] for c in calls if "push" in c[0])
        self.assertIn(branch, push)
        gh = next(c[0] for c in calls if c[0][0] == "gh")
        for expected in ("--repo", "Sudish30/notely-demo", "--title", self.PKG["pr_title"],
                         "--body", self.PKG["pr_description"]):
            self.assertIn(expected, gh)

    def test_empty_diff_fails_loudly(self):
        with self.assertRaises(github_pr.PROpenError):
            github_pr.open_pr_from_package({"combined_diff": "", "pr_title": "t"}, "o/r", "QT-9")

    def test_gh_without_url_fails_loudly(self):
        with patch.object(github_pr, "_run", return_value="no url in output"):
            with self.assertRaises(github_pr.PROpenError):
                github_pr.open_pr_from_package(self.PKG, "o/r", "QT-9")


if __name__ == "__main__":
    unittest.main()
