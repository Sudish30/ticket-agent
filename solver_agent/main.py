"""Solver Agent CLI.

  python -m solver_agent.main brief.json --repo demo_repo

Reads the intake agent's TaskBrief JSON, fixes the repo in a temp copy, and writes
solution.json + solution.md next to each other.
"""
import argparse, json, os
from pathlib import Path

from .graph import run

p = argparse.ArgumentParser()
p.add_argument("brief", help="path to a TaskBrief JSON (the intake agent's brief.json)")
p.add_argument("--repo", default=os.environ.get("TICKET_AGENT_REPO", "demo_repo"),
               help="local repository directory to fix (default: $TICKET_AGENT_REPO or demo_repo)")
p.add_argument("--max-attempts", type=int, default=3)
p.add_argument("--out", default="solution.json", help="JSON output path; the Markdown twin goes next to it with .md")
a = p.parse_args()

raw = json.loads(Path(a.brief).read_text())
solution = run(raw, a.repo, max_attempts=a.max_attempts)

out_json = Path(a.out)
out_md = out_json.with_suffix(".md") if out_json.suffix != ".md" else out_json.with_name(out_json.name + ".md")
out_json.write_text(solution.model_dump_json(indent=2))
out_md.write_text(solution.to_markdown())

icon = {"passed": "✅", "applied_unverified": "⚠️"}.get(solution.status, "❌")
print(f"\n{icon} {solution.ticket_id}: {solution.status} after {solution.attempts} attempt(s) — "
      f"{solution.tests_passed} passed, {solution.tests_failed} failed ({solution.duration_seconds:.0f}s)")
print(f"Solution written to {out_json} and {out_md}; files changed: {', '.join(solution.files_changed) or 'none'}")
