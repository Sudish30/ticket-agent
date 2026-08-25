"""Shared LLM plumbing for all agents: model factory + tolerant JSON calls.

Used by ticket_agent.graph and solver_agent.graph so model choice and the JSON retry/extraction
behaviour stay identical across the pipeline.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable

from langchain_anthropic import ChatAnthropic

MODEL = os.environ.get("TICKET_AGENT_MODEL", "claude-sonnet-4-6")


def make_llm() -> ChatAnthropic:
    return ChatAnthropic(model=MODEL, temperature=0, max_tokens=4000)


def extract_json(text: str) -> dict:
    """Tolerant JSON extraction: strips fences and grabs the outermost object."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


def call_json(prompt: str, system: str, retries: int = 2,
              llm_factory: Callable[[], ChatAnthropic] = make_llm) -> dict:
    """One JSON-returning call with up to `retries` "that was not valid JSON" nudges before failing."""
    llm = llm_factory()
    last = None
    for _ in range(retries + 1):
        out = llm.invoke([("system", system), ("user", prompt)]).content
        try:
            return extract_json(out)
        except (json.JSONDecodeError, ValueError) as e:
            last = e
            prompt = prompt + "\n\nYour previous output was not valid JSON. Output ONLY the JSON object."
    raise RuntimeError(f"Model did not return valid JSON: {last}")
