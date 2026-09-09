from datetime import date

from coppermind.naming import (
    MAX_COMPONENT_BYTES,
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


# A filename component is limited in bytes, so a title in a script whose
# characters cost more than one byte each is where a character-counted cap
# fails. These use real multi-byte text rather than asserting that some
# constant was consulted.
NOTE_SUFFIX = ".md"


def _component_bytes(stem: str) -> int:
    return len((stem + NOTE_SUFFIX).encode("utf-8"))


def test_a_cjk_title_fits_the_byte_limit_a_character_count_would_miss():
    """120 CJK characters are 360 bytes, which no filesystem here will take."""
    stem = sanitize_stem("会" * 200)
    assert _component_bytes(stem) <= MAX_COMPONENT_BYTES
    assert len(stem) < MAX_STEM_LENGTH  # the byte budget bound first


def test_a_four_byte_emoji_title_fits_too():
    stem = sanitize_stem("😀" * 200)
    assert _component_bytes(stem) <= MAX_COMPONENT_BYTES


def test_an_ascii_title_still_gets_the_full_character_cap():
    stem = sanitize_stem("A" * 200)
    assert len(stem) == MAX_STEM_LENGTH


def test_truncation_never_splits_a_character():
    for title in ("会" * 200, "😀" * 200, "café" * 100):
        stem = sanitize_stem(title)
        # Round trips cleanly, so no half character was left at the end.
        assert stem.encode("utf-8").decode("utf-8") == stem


def test_a_dated_multibyte_title_fits_with_its_date_prefix():
    stem = note_stem("会" * 200, note_date=date(2026, 9, 8), dated=True)
    assert stem.startswith("2026-09-08 ")
    assert _component_bytes(stem) <= MAX_COMPONENT_BYTES


def test_the_collision_suffix_still_fits_after_byte_truncation():
    """The suffix has to survive truncation, not be pushed past the limit."""
    taken = sanitize_stem("会" * 200)
    candidate = unique_stem(taken, [taken])
    assert candidate.endswith(" (2)")
    assert _component_bytes(candidate) <= MAX_COMPONENT_BYTES


def test_a_written_file_with_a_multibyte_title_actually_lands(tmp_path):
    """The end the cap exists for: the file is creatable on a real filesystem."""
    stem = note_stem("会" * 200, note_date=date(2026, 9, 8), dated=True)
    target = tmp_path / f"{stem}{NOTE_SUFFIX}"
    target.write_text("x", encoding="utf-8")
    assert target.is_file()
