"""CLI entry point.

  python main.py mock_tickets/PROJ-142.json            # demo: you play the reporter in the terminal
  python main.py mock_tickets/NOTE-157.json --repo demo_repo   # ...grounded in a local codebase
  python main.py PROJ-142 --jira                       # real Jira, clarification via comments
  python main.py PROJ-142 --jira --terminal            # real Jira ticket, but answer in the terminal
  python main.py PROJ-142 --jira --post-brief          # ...and post the Markdown brief as a final comment

Every run writes brief.json (TaskBrief.model_dump) and brief.md (TaskBrief.to_markdown) next to each other.
"""
import argparse, json, os
from pathlib import Path
from ticket_agent import run, TerminalChannel, JiraCommentChannel, JiraClient, load_mock_ticket

p = argparse.ArgumentParser()
p.add_argument("ticket", help="path to mock JSON, or Jira key with --jira")
p.add_argument("--jira", action="store_true", help="real Jira Cloud")
p.add_argument("--terminal", action="store_true", help="force terminal channel even with --jira")
p.add_argument("--contact", help="who to ask (default: reporter)")
p.add_argument("--max-rounds", type=int, default=3,
               help="human-facing rounds including sign-off; clarification stops after max-rounds-1 (default 3)")
p.add_argument("--repo", help="codebase to ground questions in: local path or owner/name[@branch] on GitHub "
                              "(default: $TICKET_AGENT_REPO)")
p.add_argument("--out", default="brief.json", help="JSON output path; the Markdown twin goes next to it with .md")
p.add_argument("--post-brief", action="store_true",
               help="after sign-off, post the Markdown brief (same content as brief.md) as a final comment "
                    "on the ticket (requires --jira)")
a = p.parse_args()
if a.post_brief and not a.jira:
    p.error("--post-brief requires --jira (mock tickets have nowhere to post a comment)")

if a.jira:
    client = JiraClient()
    ticket = client.get_ticket(a.ticket)
    channel = TerminalChannel() if a.terminal else JiraCommentChannel(client, os.environ.get("JIRA_AGENT_NAME", "Ticket Agent"))
else:
    ticket = load_mock_ticket(a.ticket)
    channel = TerminalChannel()

brief = run(ticket, channel, contact=a.contact, max_rounds=a.max_rounds,
            repo=a.repo or os.environ.get("TICKET_AGENT_REPO"))

out_json = Path(a.out)
out_md = out_json.with_suffix(".md") if out_json.suffix != ".md" else out_json.with_name(out_json.name + ".md")
out_json.write_text(json.dumps(brief.model_dump(), indent=2))
out_md.write_text(brief.to_markdown())
print(f"\n✅ Task brief written to {out_json} and {out_md} (confidence {brief.confidence:.2f})")
if a.post_brief:
    client.add_comment(ticket.key, brief.to_markdown())
    print(f"📝 Markdown brief posted as a comment on {ticket.key}")
print(brief.model_dump_json(indent=2))
