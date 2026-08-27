"""Open a real GitHub PR from a stored PRPackage.

Clones the repo's GitHub remote into a temp dir, creates branch agent/<key>-<slug>, applies the
package's combined diff, commits as "Ticket Agent", pushes, and runs `gh pr create` — returning
the PR URL. All git/gh calls go through the patchable `_run` seam so tests never touch the network.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


class PROpenError(RuntimeError):
    """A git/gh step failed; the message carries the command and its stderr."""


def _run(cmd: list[str], cwd: str | None = None, input_text: str | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise PROpenError(f"`{' '.join(cmd)}` failed: {(r.stderr or r.stdout).strip()[:800]}")
    return r.stdout


def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "fix"


def _retarget_new_files(diff: str, repo_dir: str) -> str:
    """The orchestrator's difflib diff writes `--- a/<path>` even for files the run CREATED;
    git apply needs `--- /dev/null` for those. Rewrite headers whose path is absent in the clone."""
    out = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("--- a/"):
            path = line[6:].rstrip("\n")
            if not (Path(repo_dir) / path).exists():
                line = "--- /dev/null" + ("\n" if line.endswith("\n") else "")
        out.append(line)
    return "".join(out)


def open_pr_from_package(pkg: dict, remote: str, key: str) -> str:
    """pkg = a PRPackage dict (needs combined_diff / pr_title / pr_description); remote = owner/name."""
    diff = pkg.get("combined_diff") or ""
    if not diff.strip():
        raise PROpenError("package has no combined_diff to apply")
    title = pkg.get("pr_title") or f"{key}: agent fix"
    branch = f"agent/{key}-{_slug(title.split(':', 1)[-1])}"

    repo_dir = os.path.join(tempfile.mkdtemp(prefix=f"pr-{key}-"), "repo")
    _run(["git", "clone", "--depth", "1", f"https://github.com/{remote}.git", repo_dir])
    _run(["git", "checkout", "-b", branch], cwd=repo_dir)
    _run(["git", "apply", "--whitespace=nowarn", "-"], cwd=repo_dir,
         input_text=_retarget_new_files(diff, repo_dir))
    _run(["git", "add", "-A"], cwd=repo_dir)
    _run(["git", "-c", "user.name=Ticket Agent", "-c", "user.email=ticket-agent@users.noreply.github.com",
          "commit", "-m", title], cwd=repo_dir)
    _run(["git", "push", "-u", "origin", branch], cwd=repo_dir)
    out = _run(["gh", "pr", "create", "--repo", remote, "--head", branch, "--title", title,
                "--body", pkg.get("pr_description") or "Automated fix by the ticket agent pipeline."],
               cwd=repo_dir)
    urls = [w.strip() for w in out.split() if w.startswith("https://github.com/")]
    if not urls:
        raise PROpenError(f"gh pr create returned no URL: {out.strip()[:300]}")
    return urls[-1]
