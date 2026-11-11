import pytest
from coppermind_store.recording_merge import append_summary

from coppermind.meeting_summary import MeetingSummary


def test_summary_follows_unmodified_owner_text_and_links_transcript():
    owner = "Hand-written words.\n\n  Keep spacing.\n"
    result = append_summary(
        owner,
        MeetingSummary(summary="We agreed.", decisions="", action_items=""),
        "_Sources/Transcript.md",
    )
    assert result.startswith(owner)
    assert "## Recording summary\n\n### Summary\n\nWe agreed." in result
    assert "[[_Sources/Transcript|Transcript]]" in result


@pytest.mark.parametrize("path", ["", "bad]]path.md", "bad|path.md", "bad\npath.md", "bad#path.md"])
def test_invalid_transcript_link_cannot_be_appended(path):
    with pytest.raises(ValueError, match="transcript link"):
        append_summary(
            "Owner", MeetingSummary(summary="Summary", decisions="", action_items=""), path
        )
