"""A note arrives filled in: what goes in, where, and what is never touched."""

from types import SimpleNamespace
from typing import Any

from coppermind_store import filled_in

from coppermind.schema import KeyDefinition, default_schema


def person(identity, name, emails=(), path=None):
    return SimpleNamespace(
        id=identity,
        kind="person",
        name=name,
        emails=list(emails),
        path=path or f"People/{name}.md",
        retired_at=None,
        redirect=None,
    )


def context(people=(), owned=True, tags="open"):
    schema = default_schema()
    schema.tag_mode = tags
    if owned:
        schema.keys.append(KeyDefinition(name="attendees", kind="list"))
        schema.keys.append(KeyDefinition(name="meeting", kind="string"))
    enrichment = SimpleNamespace(attendees_field="", event_field="")
    return SimpleNamespace(
        store=SimpleNamespace(
            control=SimpleNamespace(schema=lambda: schema),
            entities=SimpleNamespace(
                load=lambda: SimpleNamespace(entities={p.id: p for p in people})
            ),
        ),
        config=lambda: SimpleNamespace(assignments={"enrichment": enrichment}),
    )


def saved(**more: Any) -> dict[str, Any]:
    return {
        "suggestions": [
            {"field": "account", "value": ["Acme"]},
            {"field": "tags", "value": ["vsphere", "A New Tag"]},
        ],
        **more,
    }


def test_the_write_up_answers_type_account_and_tags():
    found = filled_in.answers(context(), saved())
    # An account means a customer meeting, which is work.
    assert found == {"type": "work", "account": ["Acme"], "tags": ["vsphere", "A New Tag"]}


def test_a_work_type_the_model_gave_is_kept_and_personal_too():
    for kind in ("work", "personal"):
        found = filled_in.answers(context(), {"suggestions": [{"field": "type", "value": kind}]})
        assert found == {"type": kind}


def test_closed_tags_keep_only_the_listed_ones():
    ctx = context(tags="closed")
    ctx.store.control.schema().tags["vsphere"] = "VMware vSphere"
    assert filled_in.answers(ctx, saved())["tags"] == ["vsphere"]


def test_attendees_are_the_recognised_voices_and_the_matched_invite():
    dana = person("p1", "Dana Whitfield", ["dana@example.com"])
    priya = person("p2", "Priya Raman", ["priya@example.com"])
    lee = person("p3", "Lee Chen")
    twin_a, twin_b = person("p4", "Sam"), person("p5", "Sam")
    write_up = saved(
        voice_map={"A": "Dana Whitfield", "B": "Unknown voice 2", "C": "Sam", "D": "Lee Chen"},
        calendar={
            "matched": True,
            "event": {
                "event_id": "evt-1",
                "people": [
                    {"address": "mailto:dana@example.com"},
                    {"address": "mailto:priya@example.com"},
                    {"address": "mailto:outsider@acme.test", "name": "Olive Outsider"},
                    {"address": "mailto:nameless@acme.test"},
                    {"address": "mailto:echo@acme.test", "name": "echo@acme.test"},
                ],
            },
        },
    )
    found = filled_in.answers(context([dana, priya, lee, twin_a, twin_b]), write_up)
    # A name two people share names nobody; an invitee nobody knows is kept by the name on the
    # invitation, and left out when it gives none. An address is never written.
    assert found["attendees"] == [
        "[[People/Dana Whitfield]]",
        "[[People/Lee Chen]]",
        "[[People/Priya Raman]]",
        "Olive Outsider",
    ]
    assert found["meeting"] == "evt-1"


def test_nothing_is_written_for_a_property_the_note_does_not_have():
    ctx = context(owned=False)
    write_up = saved(calendar={"event": {"event_id": "e", "people": []}})
    assert set(filled_in.answers(ctx, write_up)) == {"type", "account", "tags"}


def test_blanks_and_the_default_type_are_filled():
    ctx = context()
    note = {"type": "reference", "tags": [], "reviewed": False}
    found = filled_in.answers(ctx, saved())
    assert filled_in.fill(ctx, note, found, {}) == found


def test_what_the_owner_typed_is_never_overwritten():
    ctx = context()
    note = {"type": "personal", "account": ["Contoso"], "tags": ["mine"], "reviewed": False}
    assert filled_in.fill(ctx, note, filled_in.answers(ctx, saved()), {}) == {}
    # Not even a value that happens to equal an earlier answer of Coppermind's, once changed.
    note["account"] = ["Acme", "Contoso"]
    assert filled_in.fill(ctx, note, {"account": ["Acme"]}, {"account": ["Acme"]}) == {}


def test_what_coppermind_filled_and_the_owner_left_is_refreshed():
    ctx = context()
    note = {"type": "work", "account": ["Acme"], "tags": ["a"], "reviewed": False}
    filled = {"type": "work", "account": ["Acme"], "tags": ["a"]}
    found = {"account": ["Fabrikam"], "tags": ["b"]}
    assert filled_in.fill(ctx, note, found, filled) == {"account": ["Fabrikam"], "tags": ["b"]}
    note["tags"] = ["a", "mine"]
    assert filled_in.fill(ctx, note, found, filled) == {"account": ["Fabrikam"]}


def test_an_answer_the_schema_refuses_is_left_out_on_its_own():
    ctx = context(tags="closed")
    note = {"type": "reference", "tags": [], "reviewed": False}
    found = {"type": "work", "tags": ["unlisted"]}
    assert filled_in.fill(ctx, note, found, {}) == {"type": "work"}


def test_a_property_coppermind_filled_and_the_owner_cleared_is_not_filled_again():
    ctx = context()
    filled = {"type": "work", "account": ["Acme"], "tags": ["a"], "meeting": "evt-1"}
    note: dict[str, Any] = {"reviewed": False}
    found = {"type": "work", "account": ["Acme"], "tags": ["a"], "meeting": "evt-1"}
    assert filled_in.fill(ctx, note, found, filled) == {}
    note = {"type": "work", "account": ["Acme"], "tags": [], "reviewed": False}
    assert "tags" not in filled_in.fill(ctx, note, found, filled)


def test_a_blank_property_coppermind_never_filled_is_still_filled():
    ctx = context()
    note = {"type": "work", "tags": [], "reviewed": False}
    found = {"account": ["Acme"], "tags": ["a"]}
    assert filled_in.fill(ctx, note, found, {"type": "work"}) == found
    assert filled_in.fill(ctx, note, found, {"tags": None, "account": []}) == found
