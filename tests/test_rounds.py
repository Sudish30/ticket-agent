"""Ask-once rule, sign-off round reservation, scope plumbing (related_findings) and codebase lookup.

No network: LLM calls are patched. Run from the project root:  python -m unittest -v
"""
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from ticket_agent.channels import ScriptedChannel
from ticket_agent.codebase import Codebase
from ticket_agent.graph import (ASKED_NO_ANSWER, NEVER_ASKED, after_analyze, analyze, build_brief,
                                lookup_codebase, make_ask_human)
from ticket_agent.jira_client import load_mock_ticket
from ticket_agent.schemas import Question

ROOT = Path(__file__).resolve().parents[1]


def _q(i: int, blocking: bool = True, **kw) -> Question:
    return Question(id=f"q{i}", text=f"Question {i}?", why="w", blocking=blocking, **kw)


def _state(**over) -> dict:
    s = {
        "ticket": load_mock_ticket(ROOT / "mock_tickets" / "NOTE-157.json"), "contact": "Priya",
        "round": 0, "max_rounds": 3, "understanding": "u", "transcript": [],
        "code_context": "", "suspected_files": [], "related_findings": [], "questions": [],
    }
    s.update(over)
    return s


def _fake_llm() -> MagicMock:
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="agent message")
    return llm


class AskOnceRule(unittest.TestCase):
    @patch("ticket_agent.graph._call")
    @patch("ticket_agent.graph._llm", side_effect=_fake_llm)
    def test_unanswered_question_becomes_assumption_and_is_never_reasked(self, _llm, call):
        call.return_value = {
            "answers": [{"id": "q1", "answered": True, "answer": "every time"},
                        {"id": "q2", "answered": False, "answer": None}],
            "new_info": "", "deferred_to_agent": [],
        }
        out = make_ask_human(ScriptedChannel(["Yeah every time."]))(_state(questions=[_q(1), _q(2)]))

        by_id = {q.id: q for q in out["questions"]}
        self.assertEqual(by_id["q1"].answered_by, "human")
        self.assertEqual((by_id["q2"].answer, by_id["q2"].answered_by), (ASKED_NO_ANSWER, "assumption"))
        self.assertEqual([q.asked_count for q in out["questions"]], [1, 1])
        self.assertFalse([q for q in out["questions"] if not q.answer])          # nothing left open
        # ...so routing after the next analyze goes straight to the brief
        self.assertEqual(after_analyze({**_state(round=1), "questions": out["questions"]}), "build_brief")


class SignOffRoundReservation(unittest.TestCase):
    def test_round_zero_asks_about_anything_open(self):
        self.assertEqual(after_analyze(_state(questions=[_q(1, blocking=False)])), "ask_human")

    def test_later_rounds_ask_only_for_blocking(self):
        self.assertEqual(after_analyze(_state(round=1, questions=[_q(1, blocking=False)])), "build_brief")
        self.assertEqual(after_analyze(_state(round=1, questions=[_q(1, blocking=True)])), "ask_human")

    def test_last_round_is_reserved_for_sign_off(self):
        # max_rounds=3 → clarification only while round < 2, even with a blocking question open
        self.assertEqual(after_analyze(_state(round=2, questions=[_q(1, blocking=True)])), "build_brief")
        self.assertEqual(after_analyze(_state(round=0, max_rounds=1, questions=[_q(1)])), "build_brief")

    def test_nothing_open_builds_brief(self):
        self.assertEqual(after_analyze(_state(questions=[_q(1, answer="x", answered_by="human")])), "build_brief")


class BuildBriefAssumptions(unittest.TestCase):
    @patch("ticket_agent.graph._call")
    def test_every_unanswered_question_becomes_an_assumption(self, call):
        call.return_value = {"ticket_type": "bug", "problem_statement": "p", "goal": "g",
                             "acceptance_criteria": ["a"], "confidence": 0.8}
        state = _state(
            questions=[_q(1, blocking=True), _q(2, blocking=False, asked_count=1), _q(3, answer="x", answered_by="human")],
            suspected_files=[{"path": "auth/tokens.py", "reason": "r", "confidence": 0.9}],
            related_findings=["auth/tokens.py · _used set is in-memory · not reported by the ticket"],
        )
        brief = build_brief(state)["brief"]

        self.assertTrue(all(q.answer for q in brief.resolved_questions))         # never answer: null
        by_id = {q.id: q for q in brief.resolved_questions}
        self.assertEqual((by_id["q1"].answer, by_id["q1"].answered_by), (NEVER_ASKED, "assumption"))
        self.assertEqual((by_id["q2"].answer, by_id["q2"].answered_by), (ASKED_NO_ANSWER, "assumption"))
        self.assertEqual(by_id["q3"].answered_by, "human")
        # lookup results and findings reach the prompt and survive when the model omits them
        self.assertIn("_used set is in-memory", call.call_args.args[0])
        self.assertEqual(brief.suspected_files[0]["path"], "auth/tokens.py")
        self.assertEqual(brief.related_findings, state["related_findings"])


class AnalyzeGrounding(unittest.TestCase):
    @patch("ticket_agent.graph._call")
    def test_code_context_findings_and_question_context_are_carried(self, call):
        call.return_value = {
            "understanding": "u2",
            "related_findings": ["auth/tokens.py · _used is process-local · out of scope"],
            "questions": [{"text": "Every time?", "why": "w", "blocking": True, "options": [],
                           "context": "auth/tokens.py · validate_reset_token()"}],
        }
        prev = [_q(3, answer="x", answered_by="assumption", asked_count=1)]
        out = analyze(_state(round=1, questions=prev, code_context="===== auth/tokens.py =====\nTOKEN_TTL_MINUTES = 30"))

        self.assertIn("TOKEN_TTL_MINUTES = 30", call.call_args.args[0])        # code reached the prompt
        self.assertEqual(out["related_findings"], call.return_value["related_findings"])
        self.assertEqual([q.id for q in out["questions"]], ["q3", "q4"])       # kept + renumbered above max id
        self.assertEqual(out["questions"][1].context, "auth/tokens.py · validate_reset_token()")

    @patch("ticket_agent.graph._call")
    def test_previous_findings_survive_a_pass_that_returns_none(self, call):
        call.return_value = {"understanding": "u2", "questions": []}
        self.assertEqual(analyze(_state(related_findings=["keep me"]))["related_findings"], ["keep me"])


class CodebaseLookup(unittest.TestCase):
    def test_no_codebase_is_a_noop(self):
        self.assertEqual(lookup_codebase(_state(codebase=None)), {"code_context": "", "suspected_files": []})

    @patch("ticket_agent.graph._call")
    def test_selected_files_are_read_and_unknown_paths_dropped(self, call):
        call.return_value = {"files": [
            {"path": "auth/tokens.py", "reason": "reset tokens", "confidence": 0.9},
            {"path": "does/not/exist.py", "reason": "hallucinated", "confidence": 0.5},
        ]}
        out = lookup_codebase(_state(codebase=Codebase.open(str(ROOT / "demo_repo"))))

        self.assertIn("auth/tokens.py", call.call_args.args[0])               # file tree was in the prompt
        self.assertEqual([f["path"] for f in out["suspected_files"]], ["auth/tokens.py"])
        self.assertIn("===== auth/tokens.py =====", out["code_context"])
        self.assertIn("TOKEN_TTL_MINUTES", out["code_context"])


if __name__ == "__main__":
    unittest.main()
