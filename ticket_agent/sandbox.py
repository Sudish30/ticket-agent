"""Sandboxed-ish shell execution for agent workers (solver investigation, reviewer probing).

run_cmd() executes ONE shell command with cwd=workspace and returns
{"cmd", "stdout", "stderr", "exit_code", "duration"} — stdout/stderr truncated to ~4k chars each.

Guards (best-effort, documented honestly):
- commands referencing absolute paths outside the workspace, or any ".." traversal, are REFUSED
  (SandboxError — the command never runs, and refused commands are not logged);
- proxy env vars are cleared and NO_PROXY is set, so casual proxied HTTP(S) is disabled. TRUE network
  isolation needs containerization (namespaces / containers) and is deliberately out of scope here;
- a per-run budget of MAX_COMMANDS executions, enforced against the caller's execution log
  (pass the SAME list to every run_cmd call of one run — its length is the spent budget);
- the current interpreter's bin dir is prepended to PATH so `python` / `pytest` resolve to this venv
  without the command needing an absolute path.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

MAX_OUTPUT = 4000       # chars kept of stdout / stderr each
MAX_COMMANDS = 15       # per-run execution budget (len(log) is the spent count)

_ABS_PATH_RE = re.compile(r"""(?:^|[\s='"({:,;])(/[^\s'"(){};:,]*)""")


class SandboxError(RuntimeError):
    """A command was refused (guard violation or exhausted budget) — it was NOT executed."""


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n[... truncated ...]"


def run_cmd(workspace: str, cmd: str, timeout: int = 30, log: list | None = None) -> dict:
    """Run `cmd` (shell) inside `workspace`; append the result to `log`, the per-run execution log."""
    ws = os.path.realpath(workspace)
    if log is not None and len(log) >= MAX_COMMANDS:
        raise SandboxError(f"command budget exhausted ({MAX_COMMANDS} executions per run)")
    if ".." in cmd:
        raise SandboxError('".." path traversal is not allowed — use paths relative to the workspace root')
    for m in _ABS_PATH_RE.finditer(cmd):
        real = os.path.realpath(m.group(1))
        if not (real == ws or real.startswith(ws + os.sep)):
            raise SandboxError(f"absolute path outside the workspace: {m.group(1)} — use relative paths")

    env = {**os.environ,
           "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
           "NO_PROXY": "*", "no_proxy": "*"}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)

    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=ws, env=env,
                           capture_output=True, text=True, timeout=timeout)
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = f"[timed out after {timeout}s]"
        code = -1
    result = {"cmd": cmd, "stdout": _truncate(out), "stderr": _truncate(err),
              "exit_code": code, "duration": round(time.time() - t0, 2)}
    if log is not None:
        log.append(result)
    return result
