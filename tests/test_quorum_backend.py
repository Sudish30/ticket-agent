"""quorum_backend.server routes + FakeJiraClient, through FastAPI's TestClient. No network, no LLM, temp store.

_run_agent is patched out: creating a ticket starts it in a thread, and the real one calls the model. The
solve-brief tests patch _run_orchestrator (route) or run_orchestrator + FakeJiraClient (runner) the same way.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from quorum_backend import server
from ticket_agent.jira_client import FakeJiraClient


class _RequestsShim:
    """Routes the requests.get/post calls FakeJiraClient makes into the in-process TestClient."""

    def __init__(self, tc: TestClient):
        self.tc = tc

    @staticmethod
    def _path(url: str) -> str:
        return url.split("http://testserver", 1)[1]

    def get(self, url, **kw):
        return self.tc.get(self._path(url))

    def post(self, url, json=None, **kw):
        return self.tc.post(self._path(url), json=json)


class QuorumBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db = patch.object(server, "DB", Path(self.tmp.name) / "tickets.json")
        self._db.start()
        self._agent = patch.object(server, "_run_agent")
        self.run_agent = self._agent.start()
        self.tc = TestClient(server.app)

    def tearDown(self):
        self._agent.stop()
        self._db.stop()
        self.tmp.cleanup()

    def _wait_for_agent_calls(self, n: int) -> None:
        for _ in range(300):                      # the agent is started on a daemon thread
            if self.run_agent.call_count >= n:
                return
            time.sleep(0.01)
        self.fail(f"_run_agent called {self.run_agent.call_count} times, expected {n}")

    def test_create_starts_the_agent_and_sets_clarifying(self):
        t = self.tc.post("/api/tickets", json={"title": "Reset link never works", "reporter": "Priya"}).json()
        self.assertEqual((t["key"], t["status"], t["error"]), ("QT-001", "Clarifying", ""))
        self._wait_for_agent_calls(1)
        self.run_agent.assert_called_once_with("QT-001")
        self.assertEqual(self.tc.get("/api/tickets/QT-001").json()["status"], "Clarifying")

    def test_solve_is_only_a_retry(self):
        self.assertEqual(self.tc.post("/api/tickets/QT-404/solve").status_code, 404)
        self.tc.post("/api/tickets", json={"title": "t"})
        self._wait_for_agent_calls(1)
        self.assertEqual(self.tc.post("/api/tickets/QT-001/solve").status_code, 409)     # already clarifying
        self.tc.post("/api/tickets/QT-001/status/Agent%20error")
        r = self.tc.post("/api/tickets/QT-001/solve")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "Clarifying"))
        self._wait_for_agent_calls(2)
        self.assertEqual(self.run_agent.call_args.args, ("QT-001",))

    def test_ticket_lifecycle_through_fake_jira_client(self):
        self.assertEqual(self.tc.get("/api/tickets").json(), [])
        t = self.tc.post("/api/tickets", json={"title": "Reset link never works", "description": "d", "reporter": "Priya"}).json()
        self.assertEqual((t["key"], t["comments"]), ("QT-001", []))
        self.tc.post("/api/tickets/QT-001/comments", json={"author": "Dan", "body": "timing thing?"})

        with patch("ticket_agent.jira_client.requests", _RequestsShim(self.tc)):
            client = FakeJiraClient(base_url="http://testserver", agent_name="Ticket Agent")
            ticket = client.get_ticket("QT-001")                       # extra backend keys are ignored
            self.assertEqual((ticket.key, ticket.summary, ticket.reporter), ("QT-001", "Reset link never works", "Priya"))
            self.assertEqual([c.author for c in client.list_comments("QT-001")], ["Dan"])
            client.add_comment("QT-001", "@Priya quick question")       # default author = agent
            client.add_comment("QT-001", "final brief", author="Ticket Agent")
            self.assertEqual([(c.author, c.body) for c in client.list_comments("QT-001")][1:],
                             [("Ticket Agent", "@Priya quick question"), ("Ticket Agent", "final brief")])
            client.transition("QT-001", "Brief ready")
        self.assertEqual(self.tc.get("/api/tickets/QT-001").json()["status"], "Brief ready")
        self.assertEqual(self.tc.get("/api/tickets/nope").status_code, 404)

    def test_solve_brief_requires_brief_ready(self):
        self.assertEqual(self.tc.post("/api/tickets/QT-404/solve-brief").status_code, 404)
        self.tc.post("/api/tickets", json={"title": "t"})
        self._wait_for_agent_calls(1)
        self.assertEqual(self.tc.post("/api/tickets/QT-001/solve-brief").status_code, 409)   # still clarifying

    def test_solve_brief_starts_orchestrator_and_sets_solving(self):
        self.tc.post("/api/tickets", json={"title": "t"})
        self._wait_for_agent_calls(1)
        server._update("QT-001", status="Brief ready", brief={"ticket_id": "QT-001"})
        with patch.object(server, "_run_orchestrator") as run_orch:
            r = self.tc.post("/api/tickets/QT-001/solve-brief")
            self.assertEqual((r.status_code, r.json()["status"]), (200, "Solving"))
            for _ in range(300):                  # started on a daemon thread
                if run_orch.call_count:
                    break
                time.sleep(0.01)
            run_orch.assert_called_once_with("QT-001")
        self.assertEqual(self.tc.get("/api/tickets/QT-001").json()["status"], "Solving")

    def test_run_orchestrator_stores_package_and_posts_comment(self):
        from orchestrator.schemas import PRPackage
        self.tc.post("/api/tickets", json={"title": "t"})
        self._wait_for_agent_calls(1)
        server._update("QT-001", status="Solving", brief={"ticket_id": "QT-001"})
        pkg = PRPackage(ticket_id="QT-001", status="complete", pr_title="QT-001: fix it", pr_description="What/Why")
        fake_client = MagicMock()
        with patch.object(server, "run_orchestrator", return_value=pkg) as run_orch, \
             patch.object(server, "FakeJiraClient", return_value=fake_client):
            server._run_orchestrator("QT-001")
        run_orch.assert_called_once_with({"ticket_id": "QT-001"}, server.REPO)
        t = self.tc.get("/api/tickets/QT-001").json()
        self.assertEqual(t["status"], "PR ready")
        self.assertEqual(t["pr_package"]["pr_title"], "QT-001: fix it")
        self.assertIn("QT-001: fix it", t["pr_package_md"])
        fake_client.add_comment.assert_called_once()
        self.assertEqual(fake_client.add_comment.call_args.args[0], "QT-001")
        self.assertIn("QT-001: fix it", fake_client.add_comment.call_args.args[1])

    def test_run_orchestrator_maps_non_complete_to_needs_human_review(self):
        from orchestrator.schemas import PRPackage
        self.tc.post("/api/tickets", json={"title": "t"})
        self._wait_for_agent_calls(1)
        server._update("QT-001", status="Solving", brief={"ticket_id": "QT-001"})
        pkg = PRPackage(ticket_id="QT-001", status="needs_human_review")
        with patch.object(server, "run_orchestrator", return_value=pkg), \
             patch.object(server, "FakeJiraClient", return_value=MagicMock()):
            server._run_orchestrator("QT-001")
        self.assertEqual(self.tc.get("/api/tickets/QT-001").json()["status"], "Needs human review")

    @unittest.skipUnless(server.UI_DIR.is_dir(), "Quorum UI checkout not present next to this repo")
    def test_serves_the_quorum_ui(self):
        r = self.tc.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<script src="app.js"', r.text)
        self.assertIn("const API", self.tc.get("/app.js").text)        # the backend-wired app.js


if __name__ == "__main__":
    unittest.main()
