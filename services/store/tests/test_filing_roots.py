from pathlib import Path

import pytest
from coppermind_store.eligibility import FilingRoots

from coppermind.settings import ProductSettings


def roots(tmp_path: Path, *existing: str) -> FilingRoots:
    for name in existing:
        (tmp_path / name).mkdir()
    return FilingRoots(ProductSettings(), tmp_path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["hobby"], "hobby"),
        ([5, "hobby", "craft"], "hobby"),
        ([5], "Notes"),
        ([], "Notes"),
        (5, "Notes"),
        (None, "Notes"),
        ("", "Notes"),
        ("  ", "Notes"),
        ("Hobby", "Hobby"),
    ],
)
def test_a_type_value_resolves_to_its_first_string_or_the_notes_folder(tmp_path, value, expected):
    assert roots(tmp_path).resolve(value)[0] == expected


def test_a_reserved_type_uses_the_fallback_and_names_the_reserved_type(tmp_path):
    assert roots(tmp_path).resolve("Review") == ("Notes", "Review")
    assert roots(tmp_path).resolve("people") == ("Notes", "people")


@pytest.mark.parametrize("existing", [["work"], ["Work"], ["WORK"]])
def test_an_existing_folder_spelling_is_reused_case_insensitively(tmp_path, existing):
    assert roots(tmp_path, *existing).resolve("wOrK")[0] == existing[0]


@pytest.mark.parametrize("order", [("Work", "work"), ("work", "Work")])
def test_case_variants_that_already_exist_resolve_to_the_lowercase_one(tmp_path, order):
    assert roots(tmp_path, *order).resolve("WORK")[0] == "work"


def test_capitalised_case_variants_resolve_in_codepoint_order(tmp_path):
    assert roots(tmp_path, "WORK", "Work").resolve("work")[0] == "WORK"
