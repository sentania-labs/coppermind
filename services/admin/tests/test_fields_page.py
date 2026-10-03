"""Fields and tags: rendered, saved by revision, stale and invalid forms refused."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

import pytest

from coppermind.schema import FrontmatterSchema, default_schema
from coppermind.statefiles import StateStore
from coppermind.store_protocol import StoreUnavailable, TagCount
from services.admin.tests.browser import admin_browser as admin_browser


class Form(HTMLParser):
    """What a browser would submit for the page's form, controls left as rendered."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.values: dict[str, str] = {}
        self.checkboxes: set[str] = set()
        self._textarea: str | None = None
        self._select: str | None = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name:
            if attrs.get("type") == "checkbox":
                self.checkboxes.add(name)
                if "checked" in attrs:
                    self.values[name] = attrs.get("value", "on")
            else:
                self.values[name] = attrs.get("value", "")
        elif tag == "textarea" and name:
            self._textarea = name
            self.values[name] = ""
        elif tag == "select" and name:
            self._select = name
        elif tag == "option" and self._select and "selected" in attrs:
            self.values[self._select] = attrs["value"]

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._textarea = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._textarea:
            self.values[self._textarea] += data


def form(client) -> dict[str, str]:
    return Form(client.get("/admin/fields").text).values


def index_of(values: dict[str, str], kind: str, name: str) -> int:
    for field, value in values.items():
        match = re.fullmatch(rf"{kind}-(\d+)-name", field)
        if match and value == name:
            return int(match[1])
    raise AssertionError(f"no {kind} row named {name}")


def blank_index(values: dict[str, str], kind: str) -> int:
    for field, value in values.items():
        match = re.fullmatch(rf"{kind}-(\d+)-new", field)
        if match and value == "1":
            return int(match[1])
    raise AssertionError(f"no blank {kind} row")


def unchecked(values: dict[str, str], name: str) -> dict[str, str]:
    return {key: value for key, value in values.items() if key != name}


def schema_of(store: StateStore) -> FrontmatterSchema:
    body = {k: v for k, v in store.read("schema").body.items() if k != "revision"}
    return FrontmatterSchema.model_validate(body)


def errors(text: str) -> str:
    match = re.search(r'<div class="error">(.*?)</div>', text, re.S)
    assert match, text[:600]
    return unescape(match[1])


@pytest.fixture
def page(signed_in):
    client, wiring = signed_in
    counts = [TagCount(tag="work", count=3), TagCount(tag="job", count=2)]

    async def counter() -> list[TagCount]:
        return counts

    client.app.state.tag_counts = counter
    store = StateStore(wiring.state_dir)
    store.ensure("schema", default_schema().model_dump(mode="json"))
    return client, store


def test_the_page_renders_every_field_and_the_tags_in_use(page):
    client, store = page
    response = client.get("/admin/fields")
    assert response.status_code == 200
    assert "Revision: 1" in response.text
    values = Form(response.text).values
    assert values["revision"] == "1"
    for definition in default_schema().keys:
        row = index_of(values, "key", definition.name)
        assert values[f"key-{row}-kind"] == definition.kind
        assert values[f"key-{row}-description"] == definition.description
        assert f"key-{row}-guidance" in values
    context = index_of(values, "key", "context")
    assert values[f"key-{context}-values"] == "customer\ninternal\nexternal\npersonal"
    account = index_of(values, "key", "account")
    assert values[f"key-{account}-required_when"] == "context"
    assert values[f"key-{account}-required_when_values"] == "customer"
    assert "Role: context_key." in response.text
    assert values["tag_mode"] == "open"
    work = index_of(values, "tag", "work")
    assert values[f"tag-{work}-meaning"] == ""
    assert f"tag-{work}-listed" not in values
    assert "Notes: 3." in response.text
    assert "Notes: 2." in response.text
    assert 'href="/admin/fields"' in client.get("/admin").text


def test_the_page_still_edits_when_the_store_cannot_count(page):
    client, _ = page

    async def unavailable() -> list[TagCount]:
        raise StoreUnavailable("connection refused")

    client.app.state.tag_counts = unavailable
    response = client.get("/admin/fields")
    assert response.status_code == 200
    assert "Note counts are unavailable" in response.text
    assert "connection refused" not in response.text
    assert "key-0-kind" in response.text


def test_a_save_writes_the_next_revision_and_keeps_the_role_map(page):
    client, store = page
    values = form(client)
    context = index_of(values, "key", "context")
    work = index_of(values, "tag", "work")
    new_key = blank_index(values, "key")
    new_tag = blank_index(values, "tag")
    roles_before = dict(store.read("schema").body["roles"])
    response = client.post(
        "/v1/admin/fields",
        data=values
        | {
            f"key-{context}-values": (
                "customer: Work for a paying customer.\ninternal\n"
                "external: Work with a partner.\npersonal: Not work at all."
            ),
            f"key-{context}-guidance": "Choose where the note files once reviewed.",
            f"key-{new_key}-name": "project",
            f"key-{new_key}-kind": "enum",
            f"key-{new_key}-values": "alpha: The first project.\nbeta",
            f"key-{new_key}-required_when": "context",
            f"key-{new_key}-required_when_values": "customer, internal",
            f"key-{new_key}-default": "alpha",
            f"tag-{work}-listed": "on",
            f"tag-{work}-meaning": "The day job.",
            f"tag-{work}-aliases": "job, office",
            f"tag-{new_tag}-name": "home",
            f"tag-{new_tag}-meaning": "Life outside work.",
            "tag_mode": "closed",
        },
    )
    assert response.status_code == 200, errors(response.text)
    assert "Saved revision 2." in response.text
    assert store.read("schema").revision == 2
    saved = schema_of(store)
    assert saved.roles == roles_before
    context_key = next(k for k in saved.keys if k.name == "context")
    assert context_key.vocabulary == ["customer", "internal", "external", "personal"]
    assert context_key.vocabulary_meanings == {
        "customer": "Work for a paying customer.",
        "external": "Work with a partner.",
        "personal": "Not work at all.",
    }
    assert context_key.guidance == "Choose where the note files once reviewed."
    project = saved.keys[-1]
    assert project.name == "project"
    assert project.kind == "enum"
    assert project.vocabulary == ["alpha", "beta"]
    assert project.vocabulary_meanings == {"alpha": "The first project."}
    assert project.required_when == "context"
    assert project.required_when_values == ["customer", "internal"]
    assert project.default == "alpha"
    assert saved.tags == {"work": "The day job.", "home": "Life outside work."}
    assert saved.tag_aliases == {"job": "work", "office": "work"}
    assert saved.tag_mode == "closed"
    # Every key the form did not touch is exactly what it was.
    shipped = {k.name: k for k in default_schema().keys}
    for definition in saved.keys:
        if definition.name not in {"context", "project"}:
            assert definition == shipped[definition.name]

    # The saved page shows the new revision and the alias's notes under its tag.
    after = Form(response.text).values
    assert after["revision"] == "2"
    assert "Notes: 3, and 2 still under an alias." in response.text


def test_an_added_key_can_be_retired_and_a_role_key_cannot(page):
    client, store = page
    values = form(client)
    new_key = blank_index(values, "key")
    saved = client.post("/v1/admin/fields", data=values | {f"key-{new_key}-name": "project"})
    assert saved.status_code == 200, errors(saved.text)
    values = Form(saved.text).values
    project = index_of(values, "key", "project")
    assert f"key-{project}-retire" in Form(saved.text).checkboxes
    assert f"key-{index_of(values, 'key', 'tags')}-retire" not in Form(saved.text).checkboxes

    retired = client.post("/v1/admin/fields", data=values | {f"key-{project}-retire": "on"})
    assert retired.status_code == 200, errors(retired.text)
    assert [k.name for k in schema_of(store).keys] == [k.name for k in default_schema().keys]

    before = store.path_for("schema").read_bytes()
    values = Form(retired.text).values
    tags_row = index_of(values, "key", "tags")
    refused = client.post("/v1/admin/fields", data=values | {f"key-{tags_row}-retire": "on"})
    assert refused.status_code == 422
    assert "tags: plays the role tags_key, so it cannot be retired" in errors(refused.text)
    assert store.path_for("schema").read_bytes() == before


def test_a_stale_revision_is_refused_and_the_file_kept(page):
    client, store = page
    stale = form(client)
    context = index_of(stale, "key", "context")
    first = client.post(
        "/v1/admin/fields", data=stale | {f"key-{context}-guidance": "First tab's words."}
    )
    assert first.status_code == 200, errors(first.text)
    before = store.path_for("schema").read_bytes()

    second = client.post(
        "/v1/admin/fields", data=stale | {f"key-{context}-guidance": "Second tab's words."}
    )
    assert second.status_code == 409
    assert "revision 2" in second.text
    assert store.path_for("schema").read_bytes() == before
    assert next(k for k in schema_of(store).keys if k.name == "context").guidance == (
        "First tab's words."
    )


def test_correcting_an_invalid_stale_form_cannot_overwrite_another_tab(page):
    client, store = page
    stale = form(client)
    context = index_of(stale, "key", "context")
    assert client.post("/v1/admin/fields", data=stale).status_code == 200
    invalid = client.post("/v1/admin/fields", data=stale | {f"key-{context}-values": ""})
    assert invalid.status_code == 422
    corrected = Form(invalid.text).values
    assert corrected["revision"] == stale["revision"]
    assert corrected[f"key-{context}-values"] == ""
    corrected[f"key-{context}-values"] = "customer\ninternal"
    assert client.post("/v1/admin/fields", data=corrected).status_code == 409
    assert store.read("schema").revision == 2


@pytest.mark.parametrize(
    "edit,problem",
    [
        (lambda v, k, t, n: {f"key-{k('context')}-kind": "colour"}, "context: kind must be one of"),
        (
            lambda v, k, t, n: {f"key-{k('context')}-values": ""},
            "context: an enum needs at least one allowed value",
        ),
        (
            lambda v, k, t, n: {f"key-{k('context')}-values": "customer\ncustomer: again"},
            "context: allowed value 'customer' is listed twice",
        ),
        (
            lambda v, k, t, n: {f"key-{k('account')}-required_when": "contxt"},
            "account: required when names 'contxt', which is not a key",
        ),
        (
            lambda v, k, t, n: {f"key-{k('reviewed')}-default": "maybe"},
            "reviewed: default: enter a JSON number",
        ),
        (
            lambda v, k, t, n: {f"key-{k('reviewed')}-values": "yes"},
            "reviewed: allowed values apply to enum and string keys only",
        ),
        (lambda v, k, t, n: {f"key-{n}-name": "two words"}, "two words: a new key name"),
        (lambda v, k, t, n: {f"key-{n}-name": "context"}, "context: defined more than once"),
        (
            lambda v, k, t, n: {f"tag-{t('work')}-meaning": "The day job."},
            "tag work: tick Listed to keep its meaning or aliases",
        ),
        (
            lambda v, k, t, n: {
                f"tag-{t('work')}-listed": "on",
                f"tag-{t('work')}-aliases": "work",
            },
            "tag alias 'work' names itself",
        ),
        (
            lambda v, k, t, n: {f"tag-{t('work')}-listed": "on", "tag_mode": "sometimes"},
            "tag mode must be one of open, closed",
        ),
        (lambda v, k, t, n: {"revision": ""}, "revision: a revision is required"),
    ],
)
def test_an_invalid_field_is_refused_naming_it_and_the_file_kept(page, edit, problem):
    client, store = page
    before = store.path_for("schema").read_bytes()
    values = form(client)

    def key(name: str) -> int:
        return index_of(values, "key", name)

    def tag(name: str) -> int:
        return index_of(values, "tag", name)

    response = client.post(
        "/v1/admin/fields", data=values | edit(values, key, tag, blank_index(values, "key"))
    )
    assert response.status_code == 422
    assert problem in errors(response.text)
    assert "Nothing was saved." in response.text
    assert store.path_for("schema").read_bytes() == before


def test_an_alias_of_a_tag_another_row_lists_is_refused(page):
    client, store = page
    values = form(client)
    work = index_of(values, "tag", "work")
    new_tag = blank_index(values, "tag")
    response = client.post(
        "/v1/admin/fields",
        data=values
        | {
            f"tag-{work}-listed": "on",
            f"tag-{work}-aliases": "job",
            f"tag-{new_tag}-name": "career",
            f"tag-{new_tag}-aliases": "job",
        },
    )
    assert response.status_code == 422
    assert "tag alias job: given for both work and career" in errors(response.text)
    assert store.read("schema").revision == 1


def test_unticking_a_listed_tag_unlists_it(page):
    client, store = page
    values = form(client)
    work = index_of(values, "tag", "work")
    saved = client.post(
        "/v1/admin/fields",
        data=values | {f"tag-{work}-listed": "on", f"tag-{work}-meaning": "The day job."},
    )
    assert saved.status_code == 200, errors(saved.text)
    assert schema_of(store).tags == {"work": "The day job."}

    values = Form(saved.text).values
    work = index_of(values, "tag", "work")
    cleared = unchecked(values | {f"tag-{work}-meaning": ""}, f"tag-{work}-listed")
    response = client.post("/v1/admin/fields", data=cleared)
    assert response.status_code == 200, errors(response.text)
    assert schema_of(store).tags == {}
    # Still in use, so it stays on the page with its count.
    assert "Notes: 3" in response.text


def test_markup_in_a_meaning_is_shown_as_text(page):
    client, store = page
    values = form(client)
    work = index_of(values, "tag", "work")
    response = client.post(
        "/v1/admin/fields",
        data=values | {f"tag-{work}-listed": "on", f"tag-{work}-meaning": "<script>x</script>"},
    )
    assert response.status_code == 200, errors(response.text)
    assert "<script>x</script>" not in response.text
    assert schema_of(store).tags == {"work": "<script>x</script>"}


def test_the_form_needs_a_session(tmp_path):
    from services.admin.tests.test_admin import _client

    client, _, _ = _client(tmp_path)
    with client:
        response = client.get("/admin/fields", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/claim"
