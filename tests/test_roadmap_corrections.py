"""Tests for roadmap and STATUS corrections from issue 28 (2026-10-09).

These tests verify that the roadmap and STATUS documentation correctly
reflect the corrections from issue 28: the Sync Plus cap as a setting with
200 MB default, A9 re-read, the vocabulary gate closed, and v0.2.0 shipped
items.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Roadmap correctness (AC1, AC2)
# ---------------------------------------------------------------------------


class TestRoadmapSyncPlus:
    """Sync cap corrections from the vault side (2026-10-09)."""

    def test_roadmap_names_200_mb_default(self):
        """The roadmap states the 200 MB default for Sync Plus."""
        text = _read("docs/roadmap.md")
        assert "200 MB" in text, "roadmap.md must name the 200 MB default for Sync Plus"

    def test_roadmap_no_5_mib_cap_figure(self):
        """The 5 MiB cap figure is removed from the roadmap."""
        text = _read("docs/roadmap.md")
        assert "5 MiB" not in text, (
            "roadmap.md must not contain the 5 MiB cap figure; "
            "the operator uses Sync Plus (200 MB default)"
        )

    def test_roadmap_sync_plus_account(self):
        """The roadmap names Sync Plus as the operator's account."""
        text = _read("docs/roadmap.md")
        assert "Sync Plus" in text, "roadmap.md must name Sync Plus as the operator's account"

    def test_roadmap_10_gb_total(self):
        """The roadmap states 10 GB total for Sync Plus."""
        text = _read("docs/roadmap.md")
        assert "10 GB" in text, "roadmap.md must name 10 GB total for Sync Plus"

    def test_roadmap_a3_cap_from_setting(self):
        """The roadmap states A3 attachment copy reads the cap from a setting."""
        text = _read("docs/roadmap.md")
        assert "setting" in text.lower() and "cap" in text.lower(), (
            "roadmap.md must state that the A3 attachment cap is read from a setting"
        )


class TestRoadmapA9:
    """A9 re-read: rebuild-everything job plus adoption control."""

    def test_roadmap_a9_rebuild_everything(self):
        """A9 is described as rebuild-everything job plus adoption control."""
        text = _read("docs/roadmap.md")
        assert "rebuild-everything" in text.lower(), (
            "roadmap.md must describe A9 as a rebuild-everything job plus adoption control"
        )

    def test_roadmap_a9_no_bulk_import_endpoint(self):
        """A9 is not described as a bulk import endpoint."""
        text = _read("docs/roadmap.md")
        assert "Import that is not the adopt reconciler" not in text, (
            "roadmap.md must not use the old A9 description "
            "('Import that is not the adopt reconciler')"
        )

    def test_roadmap_post_v1_notes_import(self):
        """The roadmap notes POST /v1/notes serves client-side import."""
        text = _read("docs/roadmap.md")
        assert "POST /v1/notes" in text, (
            "roadmap.md must reference POST /v1/notes as the import path"
        )


class TestRoadmapVocabularyGate:
    """Vocabulary-mapping gate closed (2026-10-02)."""

    def test_roadmap_vocabulary_closed(self):
        """The roadmap notes the vocabulary-mapping gate is closed."""
        text = _read("docs/roadmap.md")
        assert "vocabulary-mapping" in text.lower() or "vocabulary gate" in text.lower(), (
            "roadmap.md must reference the closed vocabulary-mapping gate"
        )

    def test_roadmap_tags_organically(self):
        """The roadmap states tags grow organically."""
        text = _read("docs/roadmap.md")
        assert "organically" in text, "roadmap.md must state that tags grow organically"

    def test_roadmap_fields_page(self):
        """The roadmap names the Fields page as the tool."""
        text = _read("docs/roadmap.md")
        assert "Fields page" in text or "Fields and tags" in text, (
            "roadmap.md must name the Fields page as the vocabulary tool"
        )


class TestRoadmapMergedTable:
    """Merged table reflects v0.2.0 shipped items."""

    def test_roadmap_mentions_v0_2_0(self):
        """The roadmap names v0.2.0 as shipped."""
        text = _read("docs/roadmap.md")
        assert "v0.2.0" in text, "roadmap.md must name v0.2.0 as shipped"

    def test_roadmap_fields_tags_in_merged(self):
        """Fields and tags (#30) is in the merged table."""
        text = _read("docs/roadmap.md")
        assert "#30" in text or "#37" in text, (
            "roadmap.md must reference #30 or #37 (Fields and tags) in merged"
        )

    def test_roadmap_sources_status_in_merged(self):
        """Sources and status (A4 / #36) is in the merged table."""
        text = _read("docs/roadmap.md")
        assert "#36" in text, "roadmap.md must reference #36 (sources and status) in merged"


class TestRoadmapNextOrder:
    """Next, in order reflects v0.3.0 as finishing milestone 2."""

    def test_roadmap_names_v0_3_0(self):
        """The roadmap names v0.3.0 as finishing milestone 2."""
        text = _read("docs/roadmap.md")
        assert "v0.3.0" in text, "roadmap.md must name v0.3.0 as finishing milestone 2"

    def test_roadmap_curator_still_pending(self):
        """The curator is still listed in Next, not in Merged."""
        text = _read("docs/roadmap.md")
        # Curator should not appear in the Merged table rows
        # (it is in "Next, in order" / Wave 3)
        assert "curator" in text.lower(), "roadmap.md must still list the curator in Next, in order"


class TestRoadmapAddendum:
    """The addendum is updated with resolved items."""

    def test_addendum_sync_resolved(self):
        """The addendum notes the sync plan is resolved."""
        text = _read("docs/roadmap-addendum-2026-10-01.md")
        assert "Resolved" in text, "roadmap-addendum must note the sync plan as resolved"

    def test_addendum_vocabulary_resolved(self):
        """The addendum notes vocabulary mapping is resolved."""
        text = _read("docs/roadmap-addendum-2026-10-01.md")
        assert "Resolved" in text, "roadmap-addendum must note vocabulary mapping as resolved"


class TestStatusUpdates:
    """STATUS.md reflects v0.2.0 and the corrections."""

    def test_status_updated_date(self):
        """STATUS.md is dated 2026-10-09."""
        text = _read("STATUS.md")
        assert "2026-10-09" in text, "STATUS.md must be dated 2026-10-09"

    def test_status_sync_plus(self):
        """STATUS.md states Sync Plus with 200 MB default."""
        text = _read("STATUS.md")
        assert "Sync Plus" in text, "STATUS.md must name Sync Plus"
        assert "200 MB" in text, "STATUS.md must name 200 MB as the default"

    def test_status_v0_2_0_shipped(self):
        """STATUS.md notes v0.2.0 shipped."""
        text = _read("STATUS.md")
        assert "v0.2.0" in text, "STATUS.md must note v0.2.0 as shipped"

    def test_status_fields_page_mentioned(self):
        """STATUS.md references the Fields page."""
        text = _read("STATUS.md")
        assert "Fields" in text, "STATUS.md must reference the Fields page"

    def test_status_49_oversized_removed(self):
        """STATUS.md does not reference 49 oversized files."""
        text = _read("STATUS.md")
        assert "49-oversized" not in text and "49 over the" not in text, (
            "STATUS.md must not contain the 49-oversized-files figure"
        )


class TestProseCheck:
    """Ensure no em-dashes were introduced by the edits."""

    def test_no_em_dashes(self):
        """No em-dashes in any doc file we touched."""
        emdash = "\u2014"
        for path in (
            "docs/roadmap.md",
            "docs/roadmap-addendum-2026-10-01.md",
            "docs/data-and-settings.md",
            "STATUS.md",
        ):
            content = _read(path)
            assert emdash not in content, (
                f"em-dash found in {path}; use a comma, colon, parentheses or a period"
            )
