from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IntentName = Literal["create", "update", "append", "search", "unknown", "plan"]
WebMedia = Literal["text", "text_and_images", "images"]
WEB_MEDIA: tuple[str, ...] = ("text", "text_and_images", "images")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intent(Strict):
    value: IntentName
    confidence: float = Field(ge=0.0, le=1.0)


class NotMentioned(Strict):
    status: Literal["not_mentioned"]


class ExplicitNull(Strict):
    status: Literal["explicit_null"]


class Ambiguous(Strict):
    status: Literal["ambiguous"]
    candidates: list[Any]
    source_text: str = ""


class Value(Strict):
    status: Literal["value"]
    value: Any
    confidence: float = Field(ge=0.0, le=1.0)
    source_text: str = ""


FieldValue = Annotated[
    NotMentioned | ExplicitNull | Ambiguous | Value, Field(discriminator="status")
]


class DateValue(Strict):
    start: str
    end: str | None = None


class Candidate(Strict):
    target_name: str | None = None  # "<name> [kind]", written before the key; a thinking aid only
    target: str
    confidence: float = Field(ge=0.0, le=1.0)
    item: str | None = None
    item_candidates: list[str] = Field(default_factory=list)
    item_text: str | None = None
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    content: str | None = None
    search_query: str | None = None
    # What to look up on the web before writing (the answer becomes `content`). Only offered to
    # the model when web research is available (Context.web_research).
    web_query: str | None = None
    # What the web research should bring back: text, pictures, or both (with web_query only).
    web_media: WebMedia = "text"


class Interpretation(Strict):
    intent: Intent
    candidates: list[Candidate] = Field(min_length=1, max_length=3)
    notes: str = ""
    # A question for the user when the message is contradictory or garbled (a misheard voice
    # note that says "do NOT find pictures"); the bot asks it instead of acting on a guess.
    clarify: str | None = None

    @property
    def best(self) -> Candidate:
        return max(self.candidates, key=lambda c: c.confidence)

    @property
    def second(self) -> Candidate | None:
        rest = sorted(self.candidates, key=lambda c: c.confidence, reverse=True)[1:]
        return rest[0] if rest else None
