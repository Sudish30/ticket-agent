"""Data models for the Ticket Understanding Agent."""
from __future__ import annotations

from typing import Annotated, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


# ---------- Input: what we pull from Jira ----------

class Comment(BaseModel):
    author: str
    body: str
    created: str = ""


class Ticket(BaseModel):
    key: str
    summary: str
    description: str = ""
    issue_type: str = "Task"
    priority: str = ""
    labels: list[str] = []
    components: list[str] = []
    reporter: str = ""
    assignee: str = ""
    linked_issues: list[str] = []
    epic: str = ""
    comments: list[Comment] = []

    def as_text(self) -> str:
        lines = [
            f"KEY: {self.key}",
            f"TYPE: {self.issue_type}   PRIORITY: {self.priority}",
            f"SUMMARY: {self.summary}",
            f"REPORTER: {self.reporter}   ASSIGNEE: {self.assignee}",
            f"LABELS: {', '.join(self.labels) or '-'}   COMPONENTS: {', '.join(self.components) or '-'}",
            f"EPIC: {self.epic or '-'}   LINKED: {', '.join(self.linked_issues) or '-'}",
            "",
            "DESCRIPTION:",
            self.description or "(empty)",
            "",
            f"COMMENTS ({len(self.comments)}, oldest first — later comments override earlier ones):",
        ]
        for i, c in enumerate(self.comments, 1):
            lines.append(f"[{i}] {c.author} ({c.created}): {c.body}")
        return "\n".join(lines)


# ---------- Working objects ----------

class Question(BaseModel):
    id: str
    text: str = Field(description="Specific, answerable question. Offer options when possible.")
    why: str = Field(description="Why this matters for implementation.")
    blocking: bool = Field(description="True if work cannot sensibly start without an answer.")
    options: list[str] = []
    context: str = Field(default="", description="File / function this question is grounded in, e.g. 'auth/session.py · install_session_config()'. Empty if none.")
    asked_count: int = Field(default=0, description="How many times this was put to the human. A question is asked at most once.")
    answer: Optional[str] = None
    answered_by: Optional[str] = None  # "ticket" | "human" | "assumption"


class ClarificationTurn(BaseModel):
    role: Literal["agent", "human"]
    text: str


# ---------- Output: the contract with downstream agents ----------

class TaskBrief(BaseModel):
    ticket_id: str
    ticket_type: Literal["bug", "feature", "refactor", "chore", "unknown"]
    problem_statement: str = Field(description="Plain-language restatement of what is wrong / needed.")
    goal: str = Field(description="One sentence: what 'done' looks like.")
    acceptance_criteria: list[str]
    repro_steps: list[str] = []
    affected_areas: list[str] = Field(default=[], description="Components, modules, or files likely involved.")
    suspected_files: list[dict] = Field(default=[], description="[{path, reason, confidence 0-1}] from codebase lookup.")
    related_findings: list[str] = Field(default=[], description="Issues noticed in the code that the ticket did NOT report. Suggested follow-ups, not in scope.")
    constraints: list[str] = []
    out_of_scope: list[str] = []
    resolved_questions: list[Question] = []
    assumptions: list[str] = []
    evidence: list[str] = Field(default=[], description="Where key facts came from, e.g. 'comment #3 by Priya'.")
    confidence: float = Field(ge=0, le=1)
    clarification_contact: str = ""

    def to_markdown(self) -> str:
        """The full brief as Markdown: written to brief.md on every run and posted by --post-brief."""

        def bullets(items: list[str]) -> list[str]:
            return [f"- {x}" for x in items] or ["_None._"]

        def cell(v) -> str:
            if isinstance(v, float):
                v = f"{v:.2f}"
            return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")

        n = len(self.resolved_questions)
        out = [
            f"# {self.ticket_id} — Task brief (confidence {self.confidence:.2f})",
            "",
            f"_Type: {self.ticket_type} · clarification contact: {self.clarification_contact or '—'}_",
            "",
            "## Problem", self.problem_statement, "",
            "## Goal", self.goal, "",
            "## Acceptance criteria", *([f"- [ ] {c}" for c in self.acceptance_criteria] or ["_None._"]), "",
            "## Repro steps", *([f"{i}. {s}" for i, s in enumerate(self.repro_steps, 1)] or ["_None._"]), "",
            "## Suspected files",
        ]
        if self.suspected_files:
            out += ["| Path | Confidence | Reason |", "|---|---|---|"]
            out += [f"| `{cell(f.get('path', ''))}` | {cell(f.get('confidence', ''))} | {cell(f.get('reason', ''))} |"
                    for f in self.suspected_files]
        else:
            out.append("_None (no codebase connected)._")
        out += [
            "", "## Affected areas", *bullets(self.affected_areas),
            "", "## Constraints", *bullets(self.constraints),
            "", "## Out of scope", *bullets(self.out_of_scope),
            "", "## Assumptions", *bullets(self.assumptions),
            "", "## Related findings", *bullets(self.related_findings),
            "", "## Evidence", *bullets(self.evidence),
            "", "<details>",
            f"<summary>Q&amp;A transcript ({n} question{'' if n == 1 else 's'})</summary>", "",
        ]
        if self.resolved_questions:
            for q in self.resolved_questions:
                tags = [("blocking" if q.blocking else "non-blocking")] + ([f"`{q.context}`"] if q.context else [])
                out += [f"### {q.id} · {' · '.join(tags)}", f"**Q:** {q.text}", "",
                        f"**A:** {q.answer or '_unanswered_'} _({q.answered_by or 'open'})_", ""]
        else:
            out += ["_No clarification questions were needed._", ""]
        out.append("</details>")
        return "\n".join(out) + "\n"

    def as_comment(self) -> str:
        """Condensed plain-text brief (goal, acceptance criteria, assumptions) — a short alternative to to_markdown()."""
        lines = [
            f"Task brief for {self.ticket_id} (confidence {self.confidence:.2f})",
            "",
            f"Goal: {self.goal}",
            "",
            "Acceptance criteria:",
            *[f"{i}. {c}" for i, c in enumerate(self.acceptance_criteria, 1)],
        ]
        if self.assumptions:
            lines += ["", "Assumptions:", *[f"- {a}" for a in self.assumptions]]
        return "\n".join(lines)


# ---------- LangGraph state ----------

def _append(a: list, b: list) -> list:
    return a + b


class AgentState(TypedDict, total=False):
    ticket: Ticket
    understanding: str            # running plain-text model of the ticket
    questions: list[Question]     # current open/answered questions
    transcript: Annotated[list[ClarificationTurn], _append]
    round: int
    max_rounds: int
    brief: Optional[TaskBrief]
    confirmed: bool
    contact: str                  # who we ask
    codebase: Optional[object]    # Codebase instance or None
    code_context: str             # selected file contents, for prompts
    suspected_files: list[dict]   # [{path, reason, confidence}] from lookup_codebase
    related_findings: list[str]   # unreported bugs seen in the code; follow-ups, never scope
