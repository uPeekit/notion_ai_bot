from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IntentName = Literal["create", "update", "append", "search", "unknown"]


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
    target: str
    confidence: float = Field(ge=0.0, le=1.0)
    item: str | None = None
    item_candidates: list[str] = Field(default_factory=list)
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    content: str | None = None
    search_query: str | None = None


class Interpretation(Strict):
    intent: Intent
    candidates: list[Candidate] = Field(min_length=1, max_length=3)
    notes: str = ""

    @property
    def best(self) -> Candidate:
        return max(self.candidates, key=lambda c: c.confidence)

    @property
    def second(self) -> Candidate | None:
        rest = sorted(self.candidates, key=lambda c: c.confidence, reverse=True)[1:]
        return rest[0] if rest else None
