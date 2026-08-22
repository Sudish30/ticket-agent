"""TaskBrief.to_markdown(): section order, checklist, table escaping, collapsed Q&A, empty sections."""
import unittest

from ticket_agent.schemas import Question, TaskBrief


def _brief(**over) -> TaskBrief:
    base = dict(
        ticket_id="NOTE-157", ticket_type="bug",
        problem_statement="Reset tokens expire after ~30 seconds.",
        goal="Reset tokens stay valid for 30 minutes.",
        acceptance_criteria=["A token clicked at 29 minutes works", "A token clicked at 31 minutes is rejected"],
        repro_steps=["Request a reset", "Wait two minutes", "Click the link"],
        suspected_files=[{"path": "auth/tokens.py", "confidence": 0.97, "reason": "age in seconds | TTL in minutes"}],
        affected_areas=["auth/tokens.py validate_reset_token()"],
        constraints=["Keep TOKEN_TTL_MINUTES = 30"],
        out_of_scope=["Persistent _used store"],
        assumptions=["30 minutes is the intended lifetime"],
        related_findings=["auth/tokens.py · _used is in-memory · not reported"],
        evidence=["description", "comment #1 by Priya"],
        resolved_questions=[Question(id="q1", text="Does it fail every time?", why="w", blocking=True,
                                     context="auth/tokens.py · validate_reset_token()",
                                     answer="Yes", answered_by="human", asked_count=1)],
        confidence=0.93, clarification_contact="Priya",
    )
    base.update(over)
    return TaskBrief(**base)


SECTIONS = ["## Problem", "## Goal", "## Acceptance criteria", "## Repro steps", "## Suspected files",
            "## Affected areas", "## Constraints", "## Out of scope", "## Assumptions", "## Related findings",
            "## Evidence"]


class ToMarkdown(unittest.TestCase):
    def test_title_and_section_order(self):
        md = _brief().to_markdown()
        headings = [l for l in md.splitlines() if l.startswith("#")]
        self.assertEqual(headings[0], "# NOTE-157 — Task brief (confidence 0.93)")
        self.assertEqual([h for h in headings if h.startswith("## ")], SECTIONS)
        self.assertLess(md.index("## Evidence"), md.index("<details>"))          # transcript comes last

    def test_checklist_table_and_collapsed_transcript(self):
        md = _brief().to_markdown()
        self.assertIn("- [ ] A token clicked at 29 minutes works", md)
        self.assertIn("| Path | Confidence | Reason |", md)
        self.assertIn("| `auth/tokens.py` | 0.97 | age in seconds \\| TTL in minutes |", md)   # pipe escaped
        self.assertIn("<details>\n<summary>Q&amp;A transcript (1 question)</summary>", md)
        self.assertIn("### q1 · blocking · `auth/tokens.py · validate_reset_token()`", md)
        self.assertIn("**Q:** Does it fail every time?", md)
        self.assertIn("**A:** Yes _(human)_", md)
        self.assertTrue(md.rstrip().endswith("</details>"))

    def test_empty_sections_are_explicit(self):
        md = _brief(repro_steps=[], suspected_files=[], constraints=[], related_findings=[],
                    resolved_questions=[]).to_markdown()
        self.assertIn("## Repro steps\n_None._", md)
        self.assertIn("## Suspected files\n_None (no codebase connected)._", md)
        self.assertIn("## Constraints\n_None._", md)
        self.assertIn("<summary>Q&amp;A transcript (0 questions)</summary>\n\n_No clarification questions were needed._", md)


if __name__ == "__main__":
    unittest.main()
