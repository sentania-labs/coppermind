from datetime import date

from coppermind.naming import (
    MAX_STEM_LENGTH,
    note_stem,
    sanitize_folder,
    sanitize_stem,
    unique_stem,
)


def test_strips_characters_windows_forbids():
    assert sanitize_stem("Q3: plan/rev <draft>?") == "Q3 plan rev draft"


def test_windows_device_names_cannot_collide_with_a_device():
    assert sanitize_stem("CON") == "CON (name)"
    assert sanitize_stem("com1.md") == "com1.md (name)"


def test_normalises_to_nfc_so_macos_and_linux_agree():
    decomposed = "Café notes"
    assert sanitize_stem(decomposed) == "Café notes"


def test_trailing_dots_and_spaces_go_because_windows_drops_them_anyway():
    assert sanitize_stem("Draft. . ") == "Draft"


def test_empty_and_unusable_titles_still_produce_a_filename():
    assert sanitize_stem("") == "Untitled"
    assert sanitize_stem("///") == "Untitled"


def test_long_titles_are_capped():
    stem = sanitize_stem("A" * 400)
    assert len(stem) == MAX_STEM_LENGTH


def test_a_dated_note_keeps_its_whole_date_prefix():
    stem = note_stem("B" * 400, note_date=date(2026, 9, 8), dated=True)
    assert stem.startswith("2026-09-08 ")
    assert len(stem) <= MAX_STEM_LENGTH


def test_an_undated_type_gets_no_date_prefix():
    assert note_stem("Runbook", note_date=date(2026, 9, 8), dated=False) == "Runbook"


def test_collisions_are_compared_the_way_windows_and_macos_compare():
    assert unique_stem("Notes", ["notes"]) == "Notes (2)"
    assert unique_stem("Notes", ["Notes", "Notes (2)"]) == "Notes (3)"
    assert unique_stem("Notes", ["Other"]) == "Notes"


def test_a_collision_suffix_stays_inside_the_stem_limit():
    stem = "A" * MAX_STEM_LENGTH
    candidate = unique_stem(stem, [stem])
    assert candidate.endswith(" (2)")
    assert len(candidate) == MAX_STEM_LENGTH


def test_a_folder_cannot_escape_the_notes_filesystem():
    assert sanitize_folder("../../etc") == "etc"
    assert sanitize_folder("Work/Customers/Ameren") == "Work/Customers/Ameren"
    assert sanitize_folder("Work//Internal/") == "Work/Internal"
