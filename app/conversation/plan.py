"""A multi-step goal in progress: what the user wants to end up with, the steps planned for it,
the step being worked on, and what every finished step did.

Each step is an ordinary one-action request ("create a page X in projects") run through the
normal pipeline, so a step can ask the user a question like any message can; the state then
rides along in the pending session and the plan carries on once the answer is in."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_STEPS = 25  # hard stop, whatever the checker says; a list of items is one step each
StepStatus = Literal["done", "failed"]


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str
    status: StepStatus
    outcome: str  # what the bot answered for the step (its "done" line, or the error)


class PlanState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    planned: list[str]
    current: str
    history: list[PlanStep] = Field(default_factory=list)
    # UndoRecord JSON of every write the plan made, oldest first: "undo all" replays them.
    undo: list[str] = Field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return len(self.history) >= MAX_STEPS
