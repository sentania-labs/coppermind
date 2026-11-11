"""Reviewable meeting prose. Empty sections are deliberately dropped by the owner."""

from pydantic import BaseModel, ConfigDict, Field


class MeetingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(max_length=12000)
    decisions: str = Field(max_length=12000)
    action_items: str = Field(max_length=12000)

    def markdown(self) -> str:
        return "\n\n".join(
            f"## {title}\n\n{text.strip()}"
            for title, text in (
                ("Summary", self.summary),
                ("Decisions", self.decisions),
                ("Action items", self.action_items),
            )
            if text.strip()
        )
