"""Unit tests for clarification-reply handling in ticket_agent.graph. No network: the LLM calls are mocked.

Run from the project root:  python -m unittest -v      (or: pytest tests/)
"""
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from ticket_agent.channels import ScriptedChannel
from ticket_agent.graph import _is_blanket_defer, make_ask_human
from ticket_agent.jira_client import load_mock_ticket
from ticket_agent.schemas import Question

ROOT = Path(__file__).resolve().parents[1]
INGEST_REPLY = "Proceed with option (b), and for Q2 the answer is X"
DEFER_REPLY = "go ahead"


def _state() -> dict:
    return {
        "ticket": load_mock_ticket(ROOT / "mock_tickets" / "PROJ-142.json"),
        "contact": "Priya", "round": 1, "max_rounds": 3,
        "understanding": "u", "transcript": [],
        "questions": [
            Question(id="q1", text="Which approach?", why="w", blocking=True, options=["(a)", "(b)"]),
            Question(id="q2", text="What is the value?", why="w", blocking=False),
        ],
    }


def _fake_llm() -> MagicMock:
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="agent message")
    return llm


class BlanketDeferDetection(unittest.TestCase):
    def test_short_go_ahead_style_replies_are_blanket_defers(self):
        for reply in [DEFER_REPLY, "confirm", "Confirmed.", "your call", "proceed", "Use your judgement on tests"]:
            self.assertTrue(_is_blanket_defer(reply), reply)

    def test_replies_with_option_or_question_refs_are_not(self):
        for reply in [
            INGEST_REPLY,
            "Proceed with option (b)",
            "go ahead with (a)",
            "confirm Q1, still thinking about Q2",
            "proceed on 1) and 2)",
            "your call on question 2",
        ]:
            self.assertFalse(_is_blanket_defer(reply), reply)

    def test_long_replies_are_not(self):
        self.assertFalse(_is_blanket_defer("Go ahead but please keep SameSite=Strict on desktop and add tests"))

    def test_replies_not_starting_with_a_defer_phrase_are_not(self):
        self.assertFalse(_is_blanket_defer("1) Safari only. 2) your call."))
        self.assertFalse(_is_blanket_defer("No, keep Strict"))


class AskHumanReplyHandling(unittest.TestCase):
    @patch("ticket_agent.graph._call")
    @patch("ticket_agent.graph._llm", side_effect=_fake_llm)
    def test_specific_reply_is_ingested(self, _llm, call):
        call.return_value = {
            "answers": [{"id": "q1", "answered": True, "answer": "option (b)"},
                        {"id": "q2", "answered": True, "answer": "X"}],
            "new_info": "", "deferred_to_agent": [],
        }
        out = make_ask_human(ScriptedChannel([INGEST_REPLY]))(_state())

        call.assert_called_once()                                  # INGEST_ANSWER ran ...
        self.assertIn(INGEST_REPLY, call.call_args.args[0])        # ... on this reply
        by_id = {q.id: q for q in out["questions"]}
        self.assertEqual((by_id["q1"].answer, by_id["q1"].answered_by), ("option (b)", "human"))
        self.assertEqual((by_id["q2"].answer, by_id["q2"].answered_by), ("X", "human"))

    @patch("ticket_agent.graph._call")
    @patch("ticket_agent.graph._llm", side_effect=_fake_llm)
    def test_go_ahead_defers_everything_without_ingest(self, _llm, call):
        out = make_ask_human(ScriptedChannel([DEFER_REPLY]))(_state())

        call.assert_not_called()                                   # INGEST_ANSWER skipped
        for q in out["questions"]:
            self.assertEqual(q.answered_by, "assumption", q.id)
            self.assertTrue(q.answer)
        self.assertEqual(out["round"], 2)
        self.assertEqual([t.role for t in out["transcript"]], ["agent", "human"])

    @patch("ticket_agent.graph._call")
    @patch("ticket_agent.graph._llm", side_effect=_fake_llm)
    def test_ingest_can_still_defer_individual_questions(self, _llm, call):
        call.return_value = {
            "answers": [{"id": "q1", "answered": True, "answer": "option (b)"},
                        {"id": "q2", "answered": False, "answer": None}],
            "new_info": "", "deferred_to_agent": ["q2"],
        }
        out = make_ask_human(ScriptedChannel(["Proceed with option (b), Q2 is your call"]))(_state())

        call.assert_called_once()
        by_id = {q.id: q for q in out["questions"]}
        self.assertEqual(by_id["q1"].answered_by, "human")
        self.assertEqual(by_id["q2"].answered_by, "assumption")


if __name__ == "__main__":
    unittest.main()
