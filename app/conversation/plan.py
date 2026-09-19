"""A multi-step goal in progress: what the user wants to end up with, the steps planned for it,
the step being worked on, and what every finished step did.

A step is usually *structured* — the planner already knows it means "a row in Books titled
Omon Ra, Status Read" — and `app.conversation.steps` turns that into the same answer shape
the LLM would have produced, with no model call at all. Anything it cannot express (or that
does not resolve against the workspace) falls back to `text`, an ordinary request the LLM reads
as if the user had sent it. Either way the step goes through the usual validator, policy and
executor, so it can still ask the user something; the state then rides along in the pending
session and the plan carries on once the answer is in."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_STEPS = 25  # hard stop, whatever the checker says; a list of items is one step each
StepStatus = Literal["done", "failed"]
StepAction = Literal["create", "append", "free"]
WebMedia = Literal["text", "text_and_images", "images"]


class StepField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str  # the property's name in Notion ("Tags"), matched case-insensitively
    value: str  # option names as written in Notion; dates as YYYY-MM-DD; yes/no for a tick


class StepSpec(BaseModel):
    """One action of a plan. `text` is what the user sees and what the LLM falls back to."""

    model_config = ConfigDict(extra="forbid")
    text: str
    action: StepAction = "free"
    target: str = ""  # the page or database by name, as the workspace lists it
    title: str = ""
    content: str = ""
    fields: list[StepField] = Field(default_factory=list)
    web_query: str = ""  # look this up on the web first; the result becomes the content
    web_media: WebMedia = "text"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str
    status: StepStatus
    outcome: str  # what the bot answered for the step (its "done" line, or the error)


class PlanState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    steps: list[StepSpec]
    index: int = 0
    history: list[PlanStep] = Field(default_factory=list)
    # UndoRecord JSON of every write the plan made, oldest first: "undo all" replays them.
    undo: list[str] = Field(default_factory=list)
    # What the user answered to the plan's questions in their own words: later steps and the
    # progress check see it too, not only the step that asked.
    answers: list[str] = Field(default_factory=list)

    @property
    def current(self) -> StepSpec | None:
        return self.steps[self.index] if self.index < len(self.steps) else None

    @property
    def exhausted(self) -> bool:
        return len(self.history) >= MAX_STEPS

    def add(self, text: str) -> None:
        """A step the checker asked for after the planned ones (a retry, or something new)."""
        self.steps.append(StepSpec(text=text))
        self.index = len(self.steps) - 1
