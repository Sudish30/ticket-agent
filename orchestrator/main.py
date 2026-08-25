"""Orchestrator CLI.

  python -m orchestrator.main brief.json --repo demo_repo

Reads the intake agent's TaskBrief JSON, runs the tech-lead pipeline (plan → dispatch workers → evaluate →
assemble), and writes pr_package.json + pr_package.md next to each other.
"""
import argparse
import json
import os
from pathlib import Path

from .graph import build_graph

p = argparse.ArgumentParser()
p.add_argument("brief", help="path to a TaskBrief JSON (the intake agent's brief.json)")
p.add_argument("--repo", default=os.environ.get("TICKET_AGENT_REPO", "demo_repo"),
               help="local repository directory to fix (default: $TICKET_AGENT_REPO or demo_repo)")
p.add_argument("--out", default="pr_package.json", help="JSON output path; the Markdown twin goes next to it with .md")
a = p.parse_args()

raw = json.loads(Path(a.brief).read_text())
final = build_graph().invoke({"brief_raw": raw, "repo": a.repo}, config={"recursion_limit": 100})
pr = final["pr"]

out_json = Path(a.out)
out_md = out_json.with_suffix(".md") if out_json.suffix != ".md" else out_json.with_name(out_json.name + ".md")
out_json.write_text(pr.model_dump_json(indent=2))
out_md.write_text(pr.to_markdown())

print("\nPlan:")
print(json.dumps(final.get("plan_json", []), indent=2))
icon = {"complete": "✅", "partial": "🟡"}.get(pr.status, "❌")
print(f"\n{icon} {pr.ticket_id}: {pr.status} — {pr.tests_passed} passed, {pr.tests_failed} failed, "
      f"{len(pr.new_tests_added)} new test(s) ({pr.duration_seconds:.0f}s)")
for s in pr.subtasks:
    print(f"  [{s.status}] {s.id} · {s.worker} · {s.attempts} attempt(s): {s.summary[:110]}")
print(f"Workspace (left on disk): {final['workspace']}")
print(f"PR package written to {out_json} and {out_md}")
