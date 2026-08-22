"""Runs the agent end-to-end with a canned reporter, no keyboard needed."""
from ticket_agent import run, ScriptedChannel, load_mock_ticket

ticket = load_mock_ticket("mock_tickets/PROJ-142.json")
reporter = ScriptedChannel([
    "1) By 'logged out' I mean their session is gone and they land on the login page, no error. "
    "2) Only mobile Safari as far as we know, desktop is fine. "
    "3) Yes Dan is right, it's very likely the SameSite change. Go with the workaround for now, proper fix is a separate ticket. "
    "4) Use your judgement on tests.",
    "confirm",
])
brief = run(ticket, reporter, max_rounds=3)
print("\n--- MESSAGES THE AGENT SENT ---")
for m in reporter.sent:
    print(m, "\n" + "-" * 40)
print("\n--- FINAL BRIEF ---")
print(brief.model_dump_json(indent=2))
