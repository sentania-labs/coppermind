"""Every published example of a note's frontmatter validates against the default schema."""

import json
import re
from pathlib import Path
from typing import Any

from coppermind import frontmatter as fm
from coppermind.schema import default_schema

ROOT = Path(__file__).resolve().parents[1]


def complete(example: dict[str, Any]) -> dict[str, Any]:
    """What the store adds to a draft: the defaults and the identity."""
    schema = default_schema()
    return {**schema.defaults(), "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2", **example}


def json_blocks(path: str) -> list[dict[str, Any]]:
    text = (ROOT / path).read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    return [json.loads(block) for block in blocks]


def test_the_plaud_ingest_sample_validates():
    sample = json.loads((ROOT / "examples/ingest/plaud-sample.json").read_text(encoding="utf-8"))
    frontmatter = sample["note"]["frontmatter"]
    assert frontmatter["account"] == ["Acme"]
    assert "time" not in frontmatter and "timezone" not in frontmatter
    assert "T" in frontmatter["date"]
    assert default_schema().validate_frontmatter(complete(frontmatter)) == []


def test_the_n8n_intake_example_validates():
    examples = [
        block["note"]["frontmatter"]
        for block in json_blocks("docs/n8n-intake.md")
        if isinstance(block.get("note"), dict) and "frontmatter" in block["note"]
    ]
    assert examples
    for frontmatter in examples:
        assert "time" not in frontmatter and "timezone" not in frontmatter
        assert default_schema().validate_frontmatter(complete(frontmatter)) == []


def test_the_note_file_example_in_data_and_settings_validates():
    text = (ROOT / "docs/data-and-settings.md").read_text(encoding="utf-8")
    match = re.search(r"```markdown\n(---\n.*?\n---\n.*?)```", text, re.S)
    assert match
    frontmatter, _ = fm.parse(match.group(1))
    assert isinstance(frontmatter["account"], list)
    assert "time" not in frontmatter
    assert default_schema().validate_frontmatter(dict(frontmatter)) == []
