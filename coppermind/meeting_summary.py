"""Reviewable meeting prose. Empty sections are deliberately dropped by the owner.

The parts a meeting summary has are defined once, here. The request to the model, the check
of what comes back, the Markdown written into a note and the page where the directions are
edited all follow SECTIONS, so a section added here appears everywhere without another change.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationInfo, model_validator

SECTION_MAX = 12000


@dataclass(frozen=True)
class Section:
    """One part of a meeting summary: its key, the name its owner sees, and its directions."""

    key: str
    name: str
    default: str


SECTIONS: tuple[Section, ...] = (
    Section(
        "summary",
        "Summary",
        "Three to six bullets covering the outcomes and the open risks, in plain language. "
        "Include case numbers, versions, product names and figures as they were spoken. "
        "Skip small talk.",
    ),
    Section(
        "decisions",
        "Decisions",
        "Only decisions that were actually made, each stated as a decision.",
    ),
    Section(
        "action_items",
        "Action items",
        "Group them by the person who made the commitment, with the note's owner first. "
        "For each, say what was promised, and the date only if one was stated. Use the "
        "speaker label when a name is not established, or 'Owner unknown'. Suggestions and "
        "ideas are not action items.",
    ),
)


class MeetingSummary(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _known_parts_only(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict):
            return data
        keys = [section.key for section in SECTIONS]
        if set(data) - set(keys):
            raise ValueError("a meeting summary has only these parts: " + ", ".join(keys))
        if (info.context or {}).get("complete") and set(data) != set(keys):
            raise ValueError("a meeting summary has exactly these parts: " + ", ".join(keys))
        return {**dict.fromkeys(keys, ""), **data}

    @model_validator(mode="after")
    def _text_of_a_sensible_size(self) -> "MeetingSummary":
        given: dict[str, Any] = self.model_extra or {}
        for section in SECTIONS:
            if not isinstance(given[section.key], str):
                raise ValueError(f"{section.key} must be text")
            if len(given[section.key]) > SECTION_MAX:
                raise ValueError(f"{section.key} is longer than {SECTION_MAX:,} characters")
        return self

    @classmethod
    def from_answer(cls, data: Any) -> "MeetingSummary":
        """A summary written now: every part must be there, not only the ones that are known."""
        return cls.model_validate(data, context={"complete": True})

    def text(self, key: str) -> str:
        return str((self.model_extra or {}).get(key, ""))

    def parts(self) -> list[tuple[str, str]]:
        """Each section's name and text, in order, leaving out the empty ones."""
        return [
            (section.name, self.text(section.key).strip())
            for section in SECTIONS
            if self.text(section.key).strip()
        ]

    def markdown(self) -> str:
        return "\n\n".join(f"## {name}\n\n{text}" for name, text in self.parts())
