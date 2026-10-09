"""Adoption exclusions run without PostgreSQL, including the rendered Admin forms."""

from datetime import UTC, datetime

import pytest
from coppermind_store.control import ControlState
from coppermind_store.reconciler import MirrorEntry, _scan

from coppermind.schema import default_schema
from coppermind.settings import ReconcileSettings, read_settings
from coppermind.statefiles import StateStore
from coppermind.store_protocol import ProblemInfo
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_keys_page import Inputs
from services.admin.tests.test_problems_page import ReadOnlyStore


def scan(root, *, settings=None, by_id=None, unidentified=None):
    settings = settings or ReconcileSettings()
    return _scan(
        root,
        default_schema(),
        by_id or {},
        {},
        full=False,
        quiet=None,
        unidentified=unidentified or {},
        unadoptable=frozenset(),
        excluded_folders=settings.excluded_folders,
        excluded_patterns=settings.excluded_patterns,
    )


@pytest.mark.parametrize(
    "relative,content,rule",
    [
        ("Templates/nested/one.md", "# Template\n", "folder: Templates/"),
        ("_Templates/one.md", "# Template\n", "folder: _Templates/"),
        ("Review/one.excalidraw.md", "# Drawing\n", "pattern: *.excalidraw.md"),
        ("Review/one.canvas", '{"nodes": [], "edges": []}', "pattern: *.canvas"),
        ("Review/one.kanban.md", "# Board\n", "pattern: *.kanban.md"),
        ("drawing.md", "---\nexcalidraw-plugin: parsed\n---\n", "frontmatter: excalidraw-plugin"),
        ("board.md", "---\nkanban-plugin: basic\n---\n", "frontmatter: kanban-plugin"),
        ("quoted.md", '---\n"kanban-plugin": board # marker\n---\n', "frontmatter: kanban-plugin"),
    ],
)
def test_default_exclusions_preserve_bytes_and_report_exact_rule(tmp_path, relative, content, rule):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    result = scan(tmp_path)
    assert result.adoption_candidates == []
    assert result.exclusions == {relative: rule}
    assert path.read_text() == content
    assert scan(tmp_path, unidentified=result.unidentified).exclusions == result.exclusions


def test_custom_rules_trailing_slash_and_folder_boundary(tmp_path):
    for relative in ("Archive/a.md", "Archives/b.md", "Review/a.tmp.md", "Drafts/a.md"):
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text("# Note\n")
    result = scan(
        tmp_path,
        settings=ReconcileSettings(
            excluded_folders=["Archive/"], excluded_patterns=["*.tmp.md", "Drafts/*.md"]
        ),
    )
    assert result.exclusions == {
        "Archive/a.md": "folder: Archive/",
        "Review/a.tmp.md": "pattern: *.tmp.md",
        "Drafts/a.md": "pattern: Drafts/*.md",
    }
    assert [item.path for item in result.adoption_candidates] == ["Archives/b.md"]


@pytest.mark.parametrize("relative", ["Templates/file.md", "file.excalidraw.md", "file.canvas"])
def test_known_identity_is_followed_even_with_plugin_marker(tmp_path, relative):
    note_id = "known-note"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: {note_id}\nkanban-plugin: basic\n---\n# Filed\n")
    entry = MirrorEntry(note_id, "Review/original.md", "ok", None, 0, datetime.now(UTC))
    result = scan(tmp_path, by_id={note_id: entry})
    assert result.seen == {note_id}
    assert result.observed[note_id][0].path == relative
    assert result.exclusions == {}
    assert result.adoption_candidates == []


def test_rule_removal_and_addition_apply_with_unchanged_bytes(tmp_path):
    path = tmp_path / "file.tmp.md"
    path.write_text("# Device note\n")
    settings = ReconcileSettings(excluded_patterns=["*.tmp.md"])
    first = scan(tmp_path, settings=settings)
    assert first.exclusions == {"file.tmp.md": "pattern: *.tmp.md"}
    second = scan(tmp_path, unidentified=first.unidentified)
    assert [item.path for item in second.adoption_candidates] == ["file.tmp.md"]
    # A stat cached before a new path rule must not hide that exclusion.
    stat = path.stat()
    third = scan(
        tmp_path,
        settings=settings,
        unidentified={"file.tmp.md": (stat.st_size, datetime.fromtimestamp(stat.st_mtime, UTC))},
    )
    assert third.exclusions == first.exclusions


def test_plugin_markers_only_in_frontmatter_and_non_markdown_not_adopted(tmp_path):
    (tmp_path / "note.md").write_text("# Note\nkanban-plugin: basic\n")
    (tmp_path / "board.canvas").write_text('{"nodes": []}')
    result = scan(tmp_path, settings=ReconcileSettings(excluded_patterns=[]))
    assert [item.path for item in result.adoption_candidates] == ["note.md"]
    assert result.exclusions == {}


def test_exclusion_snapshot_does_not_lose_paths_named_like_metadata(tmp_path):
    control = ControlState(tmp_path)
    assert control.read_reconciler_excluded_count() == 0
    control.write_reconciler_exclusions({"revision": "pattern: *"})
    assert control.read_reconciler_exclusions() == {"revision": "pattern: *"}
    assert control.read_reconciler_excluded_count() == 1
    control.write_reconciler_exclusions({})
    assert ControlState(tmp_path).read_reconciler_excluded_count() == 0


def test_admin_edits_exclusions_and_refuses_stale_revision(signed_in):
    client, wiring = signed_in
    form = Inputs(client.get("/admin/settings").text).values
    assert form["reconcile.excluded_folders"] == '["Templates", "_Templates"]'
    assert form["reconcile.excluded_patterns"] == '["*.excalidraw.md", "*.canvas", "*.kanban.md"]'
    response = client.post(
        "/v1/admin/settings",
        data=form
        | {
            "reconcile.excluded_folders": '["Archive/"]',
            "reconcile.excluded_patterns": '["*.tmp.md"]',
        },
    )
    assert response.status_code == 200
    settings = read_settings(StateStore(wiring.state_dir)).reconcile
    assert settings.excluded_folders == ["Archive/"]
    assert settings.excluded_patterns == ["*.tmp.md"]
    assert client.post("/v1/admin/settings", data=form).status_code == 409
    assert read_settings(StateStore(wiring.state_dir)).reconcile == settings


def test_admin_problems_shows_count_path_and_rule_escaped(signed_in):
    client, _ = signed_in
    store = ReadOnlyStore()
    store.problems = [
        ProblemInfo(kind="excluded", reference="Templates/<draft>.md", reason="folder: Templates/")
    ]
    client.app.state.store = store
    response = client.get("/admin/problems")
    assert response.status_code == 200
    assert "Files skipped by exclusion: 1" in response.text
    assert "Templates/&lt;draft&gt;.md" in response.text
    assert "folder: Templates/" in response.text


@pytest.mark.parametrize("folder", ["/Templates", "../Templates", "", "a//b", "a/../b"])
def test_invalid_excluded_folder_is_refused(folder):
    with pytest.raises(ValueError, match="relative paths"):
        ReconcileSettings(excluded_folders=[folder])


def test_malformed_excluded_file_is_counted_but_known_identity_is_followed(tmp_path):
    relative = "Templates/broken.md"
    path = tmp_path / relative
    path.parent.mkdir()
    path.write_text("---\nid: known-note\nbroken: [\n---\n")
    result = scan(tmp_path)
    assert result.exclusions == {relative: "folder: Templates/"}
    entry = MirrorEntry("known-note", "Review/original.md", "ok", None, 0, datetime.now(UTC))
    followed = scan(tmp_path, by_id={"known-note": entry})
    assert followed.seen == {"known-note"}
    assert followed.observed["known-note"][0].state == "unparsed"
    assert followed.exclusions == {}


def test_an_inflight_excluded_file_still_defers_missing_decisions(tmp_path):
    from datetime import timedelta

    from coppermind_store.reconciler import QuietWindow

    path = tmp_path / "Templates" / "arriving.md"
    path.parent.mkdir()
    path.write_text("# Identity has not arrived yet\n")
    result = _scan(
        tmp_path,
        default_schema(),
        {},
        {},
        full=False,
        quiet=QuietWindow(datetime.now(UTC) - timedelta(seconds=30), timedelta(seconds=60)),
        unidentified={},
        unadoptable=frozenset(),
        excluded_folders=["Templates"],
        excluded_patterns=[],
    )
    assert result.deferred == 1
    assert result.exclusions == {"Templates/arriving.md": "folder: Templates/"}


def test_exclusion_details_belong_to_one_scan_only(tmp_path):
    first_root = tmp_path / "first"
    first_root.mkdir()
    (first_root / "board.kanban.md").write_text("# Board\n")
    second_root = tmp_path / "second"
    second_root.mkdir()
    assert scan(first_root).exclusions == {"board.kanban.md": "pattern: *.kanban.md"}
    assert scan(second_root).exclusions == {}
