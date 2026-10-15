"""History reads stay bounded even when unrelated deleted notes accumulate."""

import subprocess

from coppermind_store import note_history
from coppermind_store.fs import content_hash
from coppermind_store.note_history import GitHistory

from coppermind import frontmatter as fm
from coppermind.ids import new_id


def test_history_loads_bounded_batches_and_keeps_only_requested_identity(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        ).stdout

    git("init", "-q")
    wanted = new_id()
    contents = {}
    for number in range(12):
        note_id = wanted if number == 0 else new_id()
        data = fm.compose({"id": note_id}, "x" * 1000).encode()
        path = tmp_path / f"{number:02}.md"
        path.write_bytes(data)
        contents[path.name] = data
    git("add", "-A")
    git("commit", "-qm", "Before")
    for path in tmp_path.glob("*.md"):
        path.unlink()
    git("add", "-A")
    git("commit", "-qm", "Removed")
    monkeypatch.setattr(note_history, "_BLOB_BATCH_BYTES", 2200)
    history = GitHistory(tmp_path)
    original = history._blob_batch
    sizes = []

    def batch(names):
        data = list(original(names))
        sizes.append(sum(len(content) for _, content in data))
        yield from data

    monkeypatch.setattr(history, "_blob_batch", batch)
    history.refresh("id", True, {wanted})
    assert history.status.available
    assert list(history.removed) == [wanted]
    assert max(sizes) <= 2200
    assert history.evidence(wanted, "00.md", content_hash(contents["00.md"]))
    # The Admin health read must not walk or materialize history.
    monkeypatch.setattr(history, "_load", lambda *args: (_ for _ in ()).throw(AssertionError()))
    history.refresh("id", True)
    assert history.status.available
