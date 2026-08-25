"""Orchestrator agent (stage 3): TaskBrief in, PRPackage out — plans subtasks, dispatches registered
workers into one shared workspace, evaluates status-aware, replans on failure, assembles the PR package."""
from .graph import build_graph, run
from .registry import DESCRIPTIONS, WORKERS, register
from .schemas import ChangeRequest, PRPackage, Review, ReviewCheck, Subtask, SubtaskReport

__all__ = ["build_graph", "run", "WORKERS", "DESCRIPTIONS", "register",
           "PRPackage", "Subtask", "SubtaskReport", "Review", "ReviewCheck", "ChangeRequest"]
