"""The LangGraph agent.

    lookup_codebase ──► analyze ──► (nothing left to ask) ──► build_brief ──► confirm ──► END
                           ▲                                                     │
                           │ (open questions, clarification rounds left)         │ (human wants changes)
                           └──────────────── ask_human ◄─────────────────────────┘

``round`` counts every human-facing turn (ask_human and confirm). The last round is reserved for
sign-off, so ask_human only runs while ``round < max_rounds - 1``. A question is asked at most once:
if it comes back neither answered nor deferred it becomes an assumption and is never re-asked.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable

from langchain_anthropic import ChatAnthropic
from langgraph.graph import END, StateGraph

from . import prompts
from .channels import Channel
from .codebase import Codebase, MAX_FILES
from .schemas import AgentState, ClarificationTurn, Question, TaskBrief, Ticket

MODEL = os.environ.get("TICKET_AGENT_MODEL", "claude-sonnet-4-6")

# Canonical answer texts for questions the agent decides on its own (answered_by="assumption").
DEFERRED = "Deferred to agent's judgement"
ASKED_NO_ANSWER = "Asked once, not answered — agent will assume the simplest reasonable option"
NEVER_ASKED = "Not asked (no clarification rounds left) — agent will assume the simplest reasonable option"


def _llm() -> ChatAnthropic:
    return ChatAnthropic(model=MODEL, temperature=0, max_tokens=4000)


def _json(text: str) -> dict:
    """Tolerant JSON extraction: strips fences and grabs the outermost object."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


def _call(prompt: str, retries: int = 2) -> dict:
    llm = _llm()
    last = None
    for _ in range(retries + 1):
        out = llm.invoke([("system", prompts.SYSTEM), ("user", prompt)]).content
        try:
            return _json(out)
        except (json.JSONDecodeError, ValueError) as e:
            last = e
            prompt = prompt + "\n\nYour previous output was not valid JSON. Output ONLY the JSON object."
    raise RuntimeError(f"Model did not return valid JSON: {last}")


def _transcript(state: AgentState) -> str:
    return "\n".join(f"{t.role.upper()}: {t.text}" for t in state.get("transcript", [])) or "(none)"


def _qjson(qs: list[Question]) -> str:
    return json.dumps([q.model_dump() for q in qs], indent=2)


def _max_qid(qs: list[Question]) -> int:
    """Highest numeric id among existing questions (q1, q2, ...); 0 if none."""
    nums = [int(m.group(1)) for q in qs if (m := re.fullmatch(r"q(\d+)", q.id))]
    return max(nums, default=0)


def _str_list(value) -> list[str]:
    """Coerce a model-provided list (possibly a bare string or dicts) into a list of non-empty strings."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [x if isinstance(x, str) else json.dumps(x) for x in value if x]


# Blanket deferral: a short reply that hands every open question back to the agent ("confirm", "go ahead", ...)
DEFER_RE = re.compile(r"\s*(confirm(?:ed)?|go ahead|proceed|your call|use your (?:own )?judge?ment)\b", re.I)
# ...unless it references specific options/questions — "(b)", "option", "Q2", "1)" — then it carries real answers.
QREF_RE = re.compile(r"\(\s*[a-z]\s*\)|\boptions?\b|\bquestions?\b|\bq\d+\b|\b\d+\s*[).]", re.I)
MAX_DEFER_WORDS = 8


def _is_blanket_defer(reply: str) -> bool:
    """True only for a short 'go ahead'-style reply with no option/question references.

    Anything longer or more specific goes through INGEST_ANSWER, which can still defer
    individual questions via "deferred_to_agent".
    """
    return (
        bool(DEFER_RE.match(reply))
        and len(reply.split()) < MAX_DEFER_WORDS
        and not QREF_RE.search(reply)
    )


# ---------------- Nodes ----------------

def lookup_codebase(state: AgentState) -> AgentState:
    """Runs once at the start. Picks relevant files so later prompts can cite real code."""
    cb: Codebase | None = state.get("codebase")
    if cb is None or not cb.files:
        return {"code_context": "", "suspected_files": []}
    data = _call(prompts.SELECT_FILES.format(ticket=state["ticket"].as_text(), tree=cb.tree_text(), max_files=MAX_FILES))
    picks = [f for f in data.get("files", []) if isinstance(f, dict) and f.get("path") in cb.files][:MAX_FILES]
    return {"code_context": cb.context_text([f["path"] for f in picks]), "suspected_files": picks}


def analyze(state: AgentState) -> AgentState:
    ticket: Ticket = state["ticket"]
    prev_qs = state.get("questions", [])
    data = _call(
        prompts.ANALYZE.format(
            ticket=ticket.as_text(),
            code_context=state.get("code_context") or "(no codebase connected)",
            understanding=state.get("understanding") or "(none)",
            questions=_qjson(prev_qs) if prev_qs else "(none)",
            transcript=_transcript(state),
            max_questions=4 if state.get("round", 0) == 0 else 2,
        )
    )
    answered = [q for q in prev_qs if q.answer]
    new_qs = [Question.model_validate({"id": "q0", **q}) for q in data.get("questions", [])]
    # renumber new questions above the highest id ever used so they never collide with earlier ones
    for i, q in enumerate(new_qs, start=_max_qid(prev_qs) + 1):
        q.id = f"q{i}"
    # Unreported bugs the model saw in the code: suggested follow-ups, never scope. Keep the previous
    # list if this pass returned nothing so a finding is not lost between rounds.
    findings = _str_list(data.get("related_findings")) or list(state.get("related_findings") or [])
    return {"understanding": data["understanding"], "questions": answered + new_qs, "related_findings": findings}


def make_ask_human(channel: Channel) -> Callable[[AgentState], AgentState]:
    def ask_human(state: AgentState) -> AgentState:
        open_qs = [q for q in state["questions"] if not q.answer]
        first_round = state.get("round", 0) == 0
        for q in open_qs:
            q.asked_count += 1
        msg = _llm().invoke(
            [
                ("system", prompts.SYSTEM),
                ("user", prompts.ASK_HUMAN.format(
                    contact=state["contact"], key=state["ticket"].key,
                    understanding=state["understanding"], transcript=_transcript(state),
                    questions=_qjson(open_qs),
                    round_note=prompts.ROUND_NOTE_FIRST if first_round else prompts.ROUND_NOTE_LATER,
                )),
            ]
        ).content
        reply = channel.ask(state["ticket"].key, state["contact"], msg)
        turns = [ClarificationTurn(role="agent", text=msg), ClarificationTurn(role="human", text=reply)]

        if _is_blanket_defer(reply):
            # Short blanket "go ahead": every open question becomes a deferred assumption; nothing to parse.
            for q in open_qs:
                q.answer, q.answered_by = DEFERRED, "assumption"
        else:
            parsed = _call(prompts.INGEST_ANSWER.format(questions=_qjson(open_qs), reply=reply))
            by_id = {q.id: q for q in open_qs}
            for a in parsed.get("answers", []):
                q = by_id.get(a.get("id"))
                if q and a.get("answered") and a.get("answer"):
                    q.answer, q.answered_by = a["answer"], "human"
            for qid in parsed.get("deferred_to_agent", []):
                q = by_id.get(qid)
                if q and not q.answer:
                    q.answer, q.answered_by = DEFERRED, "assumption"
            if parsed.get("new_info"):
                turns.append(ClarificationTurn(role="human", text=f"(additional info) {parsed['new_info']}"))

        # Ask-once rule: whatever is still open was asked and came back neither answered nor deferred.
        # It becomes an assumption now and is never re-asked.
        for q in open_qs:
            if not q.answer:
                q.answer, q.answered_by = ASKED_NO_ANSWER, "assumption"

        return {"questions": state["questions"], "transcript": turns, "round": state.get("round", 0) + 1}

    return ask_human


def build_brief(state: AgentState) -> AgentState:
    # Every still-open question (blocking or not) becomes an assumption so the brief never carries answer: null
    for q in state["questions"]:
        if not q.answer:
            q.answer = ASKED_NO_ANSWER if q.asked_count else NEVER_ASKED
            q.answered_by = "assumption"
    suspected = state.get("suspected_files") or []
    findings = state.get("related_findings") or []
    data = _call(
        prompts.BUILD_BRIEF.format(
            ticket=state["ticket"].as_text(),
            understanding=state["understanding"],
            questions=_qjson(state["questions"]),
            transcript=_transcript(state),
            suspected_files=json.dumps(suspected, indent=2),
            related_findings=json.dumps(findings, indent=2),
            schema=json.dumps(TaskBrief.model_json_schema(), indent=1),
        )
    )
    data["ticket_id"] = state["ticket"].key
    data["clarification_contact"] = state["contact"]
    data["resolved_questions"] = [q.model_dump() for q in state["questions"]]
    # The lookup results are authoritative if the model dropped them; normalise shapes it may get wrong.
    data["suspected_files"] = [f if isinstance(f, dict) else {"path": str(f)} for f in (data.get("suspected_files") or suspected)]
    data["related_findings"] = _str_list(data.get("related_findings")) or list(findings)
    return {"brief": TaskBrief.model_validate(data)}


def make_confirm(channel: Channel) -> Callable[[AgentState], AgentState]:
    def confirm(state: AgentState) -> AgentState:
        brief = state["brief"]
        msg = _llm().invoke(
            [("system", prompts.SYSTEM),
             ("user", prompts.CONFIRM.format(contact=state["contact"], key=brief.ticket_id,
                                             brief=brief.model_dump_json(indent=2)))]
        ).content
        reply = channel.ask(brief.ticket_id, state["contact"], msg)
        ok = bool(re.match(r"\s*(confirm|confirmed|yes|lgtm|looks good|approved)\b", reply, re.I))
        turns = [ClarificationTurn(role="agent", text=msg), ClarificationTurn(role="human", text=reply)]
        return {"confirmed": ok, "transcript": turns, "round": state.get("round", 0) + 1}

    return confirm


# ---------------- Routing ----------------

def after_analyze(state: AgentState) -> str:
    open_any = [q for q in state["questions"] if not q.answer]
    open_blocking = [q for q in open_any if q.blocking]
    rnd, max_rounds = state.get("round", 0), state.get("max_rounds", 3)
    clarification_left = rnd < max_rounds - 1  # the last round is reserved for sign-off
    if open_any and clarification_left and (open_blocking or rnd == 0):
        return "ask_human"
    return "build_brief"


def after_confirm(state: AgentState) -> str:
    if state["confirmed"] or state.get("round", 0) >= state.get("max_rounds", 3) + 1:
        return END
    return "analyze"  # human asked for changes: their reply is in transcript, re-analyze


# ---------------- Build ----------------

def build_graph(channel: Channel):
    g = StateGraph(AgentState)
    g.add_node("lookup_codebase", lookup_codebase)
    g.add_node("analyze", analyze)
    g.add_node("ask_human", make_ask_human(channel))
    g.add_node("build_brief", build_brief)
    g.add_node("confirm", make_confirm(channel))

    g.set_entry_point("lookup_codebase")
    g.add_edge("lookup_codebase", "analyze")
    g.add_conditional_edges("analyze", after_analyze, {"ask_human": "ask_human", "build_brief": "build_brief"})
    g.add_edge("ask_human", "analyze")
    g.add_edge("build_brief", "confirm")
    g.add_conditional_edges("confirm", after_confirm, {END: END, "analyze": "analyze"})
    return g.compile()


def run(ticket: Ticket, channel: Channel, contact: str | None = None, max_rounds: int = 3,
        repo: str | None = None) -> TaskBrief:
    """repo: local directory path or 'owner/name[@branch]' on GitHub. None = no codebase grounding."""
    graph = build_graph(channel)
    final = graph.invoke(
        {"ticket": ticket, "contact": contact or ticket.reporter or "reporter",
         "codebase": Codebase.open(repo), "code_context": "", "suspected_files": [], "related_findings": [],
         "questions": [], "transcript": [], "round": 0, "max_rounds": max_rounds, "confirmed": False},
        config={"recursion_limit": 50},
    )
    return final["brief"]
