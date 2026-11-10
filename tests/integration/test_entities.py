"""Dossier behavior against real files and PostgreSQL, including recovery and purge."""

import pytest
import sqlalchemy as sa
from coppermind_store.entities import DELIMITER, EntityService, dossier_marker
from coppermind_store.reconciler import reconcile_once

from coppermind import frontmatter as fm
from coppermind.db.models import EntityRecord, SearchDocument
from coppermind.entities import EntityChange, EntityInput, EntityQuery
from coppermind.store_protocol import CreateNote, PatchFrontmatter, SearchQuery, VersionConflict
from tests.integration.test_purge import confirmation, retention


async def person(store, name="Casey", address="casey@example.test"):
    return await store.entities.create(EntityInput(kind="person", name=name, emails=[address]))


async def reviewed_note(store, body="Casey is an engineer."):
    note = await store.create_note(
        CreateNote(
            title="Meeting",
            body=body,
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "attendees": ["casey@example.test"],
            },
        )
    )
    await store.entities.refresh()
    return note


async def test_reviewed_accounts_create_companies_and_attendee_associations(store):
    note = await store.create_note(
        CreateNote(
            title="Customer meeting",
            body="A meeting.",
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "account": ["Xcel Energy"],
                "attendees": ["arthur.smith@xcelenergy.com"],
            },
        )
    )
    await store.entities.refresh()
    ledger = store.entities.load()
    people = [e for e in ledger.entities.values() if e.kind == "person"]
    companies = [e for e in ledger.entities.values() if e.kind == "company"]
    assert len(people) == len(companies) == 1
    person, company = people[0], companies[0]
    assert person.name == "Arthur Smith"
    assert person.path == "People/Arthur Smith.md"
    assert person.emails[0] in person.aliases
    assert any(
        f.target == company.id and f.predicate == "attended a meeting for"
        for f in person.facts
        if f.evidence.note_id == note.id
    )
    assert any(
        f.target == person.id and f.predicate == "met with"
        for f in company.facts
        if f.evidence.note_id == note.id
    )
    link = f"[[{company.path.removesuffix('.md')}]]"
    person_page = (store.notes_root / person.path).read_text()
    assert link in person_page
    assert fm.parse(person_page)[0]["company"] == [link]
    company_page = (store.notes_root / company.path).read_text()
    assert fm.parse(company_page)[0]["domains"] == ["xcelenergy.com"]
    domain = next(f for f in company.facts if f.predicate == "domain")
    assert (domain.value, domain.evidence.note_id) == ("xcelenergy.com", note.id)
    assert f"[[{person.path.removesuffix('.md')}]]" in (store.notes_root / company.path).read_text()
    await store.entities.refresh()
    assert len(store.entities.load().entities) == 2


async def test_moved_dossier_follows_identity_and_respects_owner_name(store):
    entity = await person(store, "Casey Smith")
    old = store.notes_root / entity["path"]
    moved = store.notes_root / "People" / "Casey Jones.md"
    old.rename(moved)
    await store.entities.refresh()
    current = await store.entities.get(entity["id"])
    assert current["path"] == "People/Casey Jones.md"
    assert current["name"] == "Casey Jones"
    assert not current["retired_at"]
    await store.entities.refresh()
    assert (await store.entities.get(entity["id"]))["name"] == "Casey Jones"


@pytest.mark.parametrize("change", ["legacy", "no_id", "empty_id", "no_marker", "moved_no_marker"])
async def test_dossier_identity_migrates_and_recovers_without_visible_ids(store, change):
    entity = await person(store, "Casey Smith")
    path = store.notes_root / entity["path"]
    marker = dossier_marker(entity["id"])
    text = path.read_text().replace(marker, "Personal text stays.\n\n" + marker)
    if change == "legacy":
        text = text.replace("---\n", f"---\nentity_id: {entity['id']}\ntype: person\n", 1)
        text = text.replace(marker, DELIMITER)
    elif change == "no_id":
        text = text.replace(marker, DELIMITER)
    elif change == "empty_id":
        text = text.replace(marker, "<!-- coppermind:dossier id= -->\n")
    else:
        text = text.replace(marker, "")
    path.write_text(text)
    if change == "moved_no_marker":
        moved = path.with_name("Casey Renamed.md")
        path.rename(moved)
        path = moved
    await store.entities.refresh()
    current = await store.entities.get(entity["id"])
    assert not current["retired_at"] and not current["problem"]
    assert current["path"] == path.relative_to(store.notes_root).as_posix()
    written = path.read_text()
    assert "Personal text stays.\n\n" in written
    assert marker in written
    assert "entity_id:" not in written.split("---", 2)[1]
    assert "\ntype:" not in written.split("---", 2)[1]
    assert "## Scott's Notes" in written and "## Coppermind's Notes" in written
    assert "## Facts\n" not in written and "### Facts\n" not in written
    await store.entities.refresh()
    assert path.read_text() == written


async def test_edited_page_without_marker_is_left_alone_and_reported(store):
    p = await person(store)
    note = await reviewed_note(store)
    await claim(store, p, note)
    path = store.notes_root / p["path"]
    marker = dossier_marker(p["id"])
    text = path.read_text()
    assert marker in text
    edited = text.replace(marker, "").rstrip("\n") + "\nOwner edit under Coppermind's Notes\n"
    path.write_text(edited)
    await store.entities.refresh()
    assert path.read_text() == edited
    assert (await store.entities.get(p["id"]))["problem"]


async def test_generated_sections_are_nested_and_empty_sections_omitted(store):
    p = await person(store)
    note = await reviewed_note(store)
    await claim(store, p, note)
    text = (store.notes_root / p["path"]).read_text()
    assert "## Scott's Notes\n" in text
    assert "## Coppermind's Notes\n" in text
    assert "\n### Facts\n" in text and "\n### Supporting notes\n" in text
    assert "\n### Interests\n" not in text and "\n### Relationships\n" not in text


async def test_company_moved_with_missing_marker_recovers_by_name_and_aliases(store):
    company = await store.entities.create(EntityInput(kind="company", name="Acme"))
    old = store.notes_root / company["path"]
    old.write_text(old.read_text().replace(dossier_marker(company["id"]), ""))
    moved = old.with_name("Acme New.md")
    old.rename(moved)
    await store.entities.refresh()
    current = await store.entities.get(company["id"])
    assert current["path"] == "Companies/Acme New.md" and not current["retired_at"]
    assert dossier_marker(company["id"]) in moved.read_text()


async def test_moved_company_page_with_non_text_name_is_not_recovered_and_does_not_abort(store):
    company = await store.entities.create(EntityInput(kind="company", name="Acme"))
    old = store.notes_root / company["path"]
    text = old.read_text().replace(dossier_marker(company["id"]), "")
    assert "name: Acme" in text
    moved = old.with_name("Acme New.md")
    moved.write_text(text.replace("name: Acme", "name: [Acme]", 1))
    old.unlink()
    await store.entities.refresh()
    current = await store.entities.get(company["id"])
    assert current["path"] == company["path"]
    assert dossier_marker(company["id"]) not in moved.read_text()


async def test_legacy_marker_migration_preserves_owner_frontmatter_and_body_bytes(store):
    p = await person(store, "Casey Smith")
    path = store.notes_root / p["path"]
    marker = dossier_marker(p["id"])
    text = (
        path.read_text()
        .replace("---\n", f"---\nentity_id: {p['id']}\ntype: person\ncustom: 'mine' # keep\n", 1)
        .replace(marker, "My own text.\n\n" + DELIMITER)
    )
    path.write_text(text)
    old_head = text.split(DELIMITER)[0]
    expected = old_head.replace(f"entity_id: {p['id']}\n", "").replace("type: person\n", "")
    await store.entities.refresh()
    assert path.read_text().split(marker)[0] == expected


async def test_markerless_dossier_moved_outside_reserved_folders_is_not_claimed(store):
    p = await person(store, "Casey Smith")
    old = store.notes_root / p["path"]
    old.write_text(old.read_text().replace(dossier_marker(p["id"]), ""))
    moved = store.notes_root / "Casey Moved.md"
    old.rename(moved)
    await reconcile_once(store, full=True)
    await store.entities.refresh()
    current = await store.entities.get(p["id"])
    assert current["path"] == p["path"] and current["retired_at"]
    assert dossier_marker(p["id"]) not in moved.read_text()


@pytest.mark.parametrize(
    "folder,front",
    [
        ("Contacts", "email: [casey@example.test]\nname: Casey Smith\naliases: []\n"),
        ("Contacts", "name: Casey Smith\n"),
        ("People", "name: Casey Smith\n"),
        ("People", "email: [casey@example.test]\nname: Casey Smith\n"),
    ],
)
async def test_owner_note_sharing_email_or_name_is_never_claimed_or_written(store, folder, front):
    p = await person(store, "Casey Smith")
    (store.notes_root / p["path"]).unlink()
    (store.notes_root / folder).mkdir(exist_ok=True)
    owner = store.notes_root / folder / "Casey Contact.md"
    text = f"---\n{front}---\nMy own contact note.\n"
    owner.write_text(text)
    await store.entities.refresh()
    await reconcile_once(store, full=True)
    await store.entities.refresh()
    current = await store.entities.get(p["id"])
    assert current["path"] == p["path"]
    assert "My own contact note." in owner.read_text()
    assert "coppermind:dossier" not in owner.read_text()
    assert "## Coppermind's Notes" not in owner.read_text()


async def test_owner_note_in_another_folder_is_adopted_not_withheld(store):
    p = await person(store, "Casey Smith")
    (store.notes_root / p["path"]).unlink()
    owner = store.notes_root / "Casey Contact.md"
    owner.write_text("---\nemail: casey@example.test\n---\nMy own contact note.\n")
    await store.entities.refresh()
    await reconcile_once(store, full=True)
    assert "\nid:" in owner.read_text().split("---", 2)[1]


def customer_note(**extra):
    return CreateNote(
        title=extra.pop("title", "Customer meeting"),
        body="A meeting.",
        frontmatter={"reviewed": True, "date": "2026-11-01", **extra},
    )


def page_company(store, entity):
    return fm.parse((store.notes_root / entity.path).read_text())[0]["company"]


def named(store, name):
    return next(e for e in store.entities.load().entities.values() if e.name == name)


async def test_company_comes_from_the_attendees_email_domain_not_from_attendance(store):
    await store.create_note(
        customer_note(
            account=["Xcel Energy"],
            attendees=[
                "arthur.smith@xcelenergy.com",
                "vendor.rep@othervendor.test",
                "scott@gmail.com",
            ],
        )
    )
    await store.entities.refresh()
    company = named(store, "Xcel Energy")
    assert page_company(store, named(store, "Arthur Smith")) == [
        f"[[{company.path.removesuffix('.md')}]]"
    ]
    assert page_company(store, named(store, "Vendor Rep")) == []
    assert page_company(store, named(store, "Scott")) == []
    relationships = [f.predicate for f in named(store, "Vendor Rep").facts]
    assert "attended a meeting for" in relationships


async def test_public_mail_domains_never_belong_to_a_company(store):
    await store.create_note(customer_note(account=["Gmail"], attendees=["casey@gmail.com"]))
    await store.entities.refresh()
    company = named(store, "Gmail")
    assert not [f for f in company.facts if f.predicate == "domain"]
    assert fm.parse((store.notes_root / company.path).read_text())[0]["domains"] == []
    assert page_company(store, named(store, "Casey")) == []


async def test_a_known_domain_gives_a_company_to_attendees_of_later_notes(store):
    await store.create_note(
        customer_note(account=["Xcel Energy"], attendees=["arthur.smith@xcelenergy.com"])
    )
    await store.entities.refresh()
    await store.create_note(
        customer_note(title="Internal sync", attendees=["jo.lee@xcelenergy.com"])
    )
    await store.entities.refresh()
    company = named(store, "Xcel Energy")
    assert page_company(store, named(store, "Jo Lee")) == [
        f"[[{company.path.removesuffix('.md')}]]"
    ]


@pytest.mark.parametrize(
    "company,address",
    [("Ford", "x@afford.com"), ("Ford", "y@crawford.com"), ("Dell", "z@modellers.test")],
)
async def test_domain_never_attaches_on_a_substring_of_a_longer_word(store, company, address):
    await store.create_note(customer_note(account=[company], attendees=[address]))
    await store.entities.refresh()
    assert not [f for f in named(store, company).facts if f.predicate == "domain"]
    person = next(e for e in store.entities.load().entities.values() if address in e.emails)
    assert page_company(store, person) == []


@pytest.mark.parametrize(
    "company,address,domain",
    [
        ("Acme Power", "x@acme-power.test", "acme-power.test"),
        ("Xcel Energy", "y@mail.xcelenergy.com", "mail.xcelenergy.com"),
        ("Acme", "z@acme-power.test", "acme-power.test"),
    ],
)
async def test_domain_attaches_on_a_whole_label_or_component(store, company, address, domain):
    await store.create_note(customer_note(account=[company], attendees=[address]))
    await store.entities.refresh()
    held = [f.value for f in named(store, company).facts if f.predicate == "domain"]
    assert held == [domain]


async def test_editing_a_note_keeps_its_domain_without_a_gap(store):
    note = await store.create_note(
        customer_note(account=["Xcel Energy"], attendees=["arthur.smith@xcelenergy.com"])
    )
    await store.entities.refresh()
    path = store.notes_root / note.path
    path.write_text(path.read_text() + "More discussion.\n")
    await reconcile_once(store, full=True)
    company = named(store, "Xcel Energy")
    live = [f.value for f in company.facts if f.predicate == "domain" and f.state == "confirmed"]
    assert live == ["xcelenergy.com"]
    assert fm.parse((store.notes_root / company.path).read_text())[0]["domains"] == [
        "xcelenergy.com"
    ]


async def test_removed_domain_stays_removed_after_its_note_is_edited(store):
    note = await store.create_note(
        customer_note(account=["Xcel Energy"], attendees=["arthur.smith@xcelenergy.com"])
    )
    await store.entities.refresh()
    company = named(store, "Xcel Energy")
    page = store.notes_root / company.path
    page.write_text(page.read_text().replace("domains:\n  - xcelenergy.com\n", "domains: []\n"))
    await store.entities.refresh()
    assert fm.parse(page.read_text())[0]["domains"] == []
    path = store.notes_root / note.path
    path.write_text(path.read_text() + "More discussion.\n")
    await reconcile_once(store, full=True)
    await store.entities.refresh()
    company = named(store, "Xcel Energy")
    assert not [f for f in company.facts if f.predicate == "domain" and f.state == "confirmed"]
    assert fm.parse(page.read_text())[0]["domains"] == []


async def test_owner_set_company_and_domains_always_win(store):
    await store.create_note(
        customer_note(account=["Xcel Energy"], attendees=["arthur.smith@xcelenergy.com"])
    )
    await store.entities.refresh()
    arthur, company = named(store, "Arthur Smith"), named(store, "Xcel Energy")
    page = store.notes_root / arthur.path
    page.write_text(
        page.read_text().replace(f"[[{company.path.removesuffix('.md')}]]", "Consultant")
    )
    await store.entities.refresh()
    assert page_company(store, arthur) == ["Consultant"]

    await store.create_note(
        customer_note(title="Other", attendees=["pat@xcel-partner.test", "kim@acme.test"])
    )
    await store.entities.refresh()
    assert page_company(store, named(store, "Pat")) == []
    page = store.notes_root / company.path
    page.write_text(
        page.read_text().replace(
            "domains:\n  - xcelenergy.com", "domains:\n  - xcelenergy.com\n  - xcel-partner.test"
        )
    )
    await store.entities.refresh()
    assert page_company(store, named(store, "Pat")) == [f"[[{company.path.removesuffix('.md')}]]"]
    assert page_company(store, named(store, "Kim")) == []

    page.write_text(page.read_text().replace("  - xcelenergy.com\n", ""))
    await store.entities.refresh()
    await store.entities.refresh()
    assert "xcelenergy.com" not in fm.parse(page.read_text())[0]["domains"]
    domains = [
        f.value
        for f in named(store, "Xcel Energy").facts
        if f.state == "confirmed" and f.predicate == "domain"
    ]
    assert domains == ["xcel-partner.test"]


async def test_unsorted_mixed_case_domains_settle_without_new_receipts(store):
    await store.create_note(customer_note(account=["Xcel Energy"]))
    await store.entities.refresh()
    company = named(store, "Xcel Energy")
    page = store.notes_root / company.path
    page.write_text(
        page.read_text().replace(
            "domains: []",
            "domains:\n  - Zeta.test\n  - alpha.test\n  - ALPHA.test\n  - not a domain",
        )
    )
    await store.entities.refresh()
    settled = store.entities.load()
    held = sorted(f.value for f in settled.entities[company.id].facts if f.predicate == "domain")
    assert held == ["alpha.test", "zeta.test"]
    for _ in range(2):
        await store.entities.refresh()
    again = store.entities.load()
    assert len(again.receipts) == len(settled.receipts)
    assert again.entities[company.id].version == settled.entities[company.id].version


async def test_deleted_company_page_is_not_recreated(store):
    await store.create_note(
        CreateNote(
            title="Customer meeting",
            body="A meeting.",
            frontmatter={"reviewed": True, "date": "2026-11-01", "account": ["Xcel Energy"]},
        )
    )
    await store.entities.refresh()
    company = next(e for e in store.entities.load().entities.values() if e.kind == "company")
    (store.notes_root / company.path).unlink()
    await store.entities.refresh()
    await store.entities.refresh()
    ledger = store.entities.load()
    companies = [e for e in ledger.entities.values() if e.kind == "company"]
    assert [e.id for e in companies] == [company.id]
    assert companies[0].retired_at
    assert not (store.notes_root / company.path).exists()


async def test_account_matches_the_live_company_when_a_retired_one_shares_its_name(store):
    old = await store.entities.create(EntityInput(kind="company", name="Xcel Energy"))
    await store.entities.change(old["id"], EntityChange(version=old["version"], action="retire"))
    live = await store.entities.create(EntityInput(kind="company", name="Xcel Energy"))
    note = await store.create_note(
        CreateNote(
            title="Customer meeting",
            body="A meeting.",
            frontmatter={"reviewed": True, "date": "2026-11-01", "account": ["Xcel Energy"]},
        )
    )
    await store.entities.refresh()
    ledger = store.entities.load()
    assert any(f.evidence.note_id == note.id for f in ledger.entities[live["id"]].facts)
    assert len([e for e in ledger.entities.values() if e.kind == "company"]) == 2


async def test_renamed_company_page_still_receives_notes_for_its_old_account_name(store):
    await store.create_note(
        CreateNote(
            title="First meeting",
            body="A meeting.",
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "account": ["Xcel Energy"],
                "attendees": ["arthur.smith@xcelenergy.com", "jo.lee@xcelenergy.com"],
            },
        )
    )
    await store.entities.refresh()
    company = next(e for e in store.entities.load().entities.values() if e.kind == "company")
    page = store.notes_root / company.path
    page.rename(page.with_name("Xcel Energy Corp.md"))
    await store.entities.refresh()
    await store.create_note(
        CreateNote(
            title="Second meeting",
            body="Another meeting.",
            frontmatter={"reviewed": True, "date": "2026-11-02", "account": ["Xcel Energy"]},
        )
    )
    await store.entities.refresh()
    ledger = store.entities.load()
    companies = [e for e in ledger.entities.values() if e.kind == "company"]
    assert [e.id for e in companies] == [company.id]
    assert companies[0].name == "Xcel Energy Corp"
    assert "Xcel Energy" in companies[0].aliases
    text = (store.notes_root / companies[0].path).read_text()
    assert text.count("met with") == 2
    assert "Other recorded values" not in text


async def test_unusable_account_value_does_not_stop_dossier_refresh(store):
    for account in ("x" * 201, "Line one\nLine two"):
        await store.create_note(
            CreateNote(
                title="Odd account",
                body="A meeting.",
                frontmatter={
                    "reviewed": True,
                    "date": "2026-11-01",
                    "account": [account],
                    "attendees": ["casey@example.test"],
                },
            )
        )
    await store.entities.refresh()
    ledger = store.entities.load()
    assert not [e for e in ledger.entities.values() if e.kind == "company"]
    assert any(e.emails == ["casey@example.test"] for e in ledger.entities.values())


async def test_trashed_dossier_page_is_not_treated_as_a_move(store):
    entity = await person(store, "Casey Smith")
    old = store.notes_root / entity["path"]
    trash = store.notes_root / ".trash"
    trash.mkdir()
    old.rename(trash / old.name)
    await store.entities.refresh()
    current = await store.entities.get(entity["id"])
    assert current["path"] == entity["path"]
    assert current["retired_at"]


async def test_account_wikilink_uses_only_existing_company(store):
    company = await store.entities.create(EntityInput(kind="company", name="Acme"))
    p = await person(store)
    await store.create_note(
        CreateNote(
            title="Meeting",
            body="Notes.",
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "account": [
                    f"[[{company['path'].removesuffix('.md')}]]",
                    f"[[{p['path'].removesuffix('.md')}]]",
                ],
                "attendees": ["casey@example.test"],
            },
        )
    )
    await store.entities.refresh()
    ledger = store.entities.load()
    assert [e.name for e in ledger.entities.values() if e.kind == "company"] == ["Acme"]
    assert any(f.target == company["id"] for f in ledger.entities[p["id"]].facts)


@pytest.mark.parametrize("step", ["intent", "copy", "links", "remove", "complete"])
async def test_legacy_page_move_resumes_after_interruption(store, monkeypatch, step):
    address = "arthur.smith@example.test"
    entity = await person(store, address, address)
    ledger = store.entities.load()
    subject = ledger.entities[entity["id"]]
    old = f"People/{address} {subject.id}.md"
    (store.notes_root / subject.path).rename(store.notes_root / old)
    subject.path, subject.automatic = old, True
    store.entities.save(ledger)
    note = await store.create_note(
        CreateNote(
            title="Meeting",
            body=f"See [[{old.removesuffix('.md')}|Arthur]].",
            frontmatter={"reviewed": True, "attendees": [address], "date": "2026-11-01"},
        )
    )
    hand = await store.create_note(
        CreateNote(
            title="Hand written",
            body=f"Keep [[Elsewhere]] and this text. See [[{old.removesuffix('.md')}|Hand link]].",
        )
    )
    # Metadata already proves this note references the entity before the move starts.
    ledger = store.entities.load()
    store.entities.contribute(ledger, await store.entities.notes())
    store.entities.save(ledger)

    def interrupt(point):
        if point == step:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(store.entities, "move_checkpoint", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await store.entities.refresh()
    monkeypatch.setattr(store.entities, "move_checkpoint", lambda _: None)
    await store.entities.refresh()
    current = await store.entities.get(subject.id)
    assert current["path"] == "People/Arthur Smith.md"
    assert not (store.notes_root / old).exists()
    assert (store.notes_root / current["path"]).exists()
    assert "[[People/Arthur Smith|Arthur]]" in (store.notes_root / note.path).read_text()
    assert "[[People/Arthur Smith|Hand link]]" in (store.notes_root / hand.path).read_text()
    assert "Keep [[Elsewhere]] and this text. See" in (store.notes_root / hand.path).read_text()


async def test_unscannable_page_keeps_the_old_dossier_until_it_can_be_checked(store, monkeypatch):
    address = "arthur.smith@example.test"
    entity = await person(store, address, address)
    ledger = store.entities.load()
    subject = ledger.entities[entity["id"]]
    old = f"People/{address} {subject.id}.md"
    (store.notes_root / subject.path).rename(store.notes_root / old)
    subject.path, subject.automatic = old, True
    store.entities.save(ledger)
    hand = await store.create_note(
        CreateNote(title="Hand written", body=f"See [[{old.removesuffix('.md')}|Hand link]].")
    )
    hand_file = store.notes_root / hand.path
    good = hand_file.read_bytes()
    hand_file.write_bytes(b"\xff\xfe not utf-8 \xff")
    await store.entities.refresh()
    current = await store.entities.get(subject.id)
    assert current["path"] == old
    assert (store.notes_root / old).exists()
    assert current["problem"]
    hand_file.write_bytes(good)
    await store.entities.refresh()
    current = await store.entities.get(subject.id)
    assert current["path"] == "People/Arthur Smith.md"
    assert not (store.notes_root / old).exists()
    assert "[[People/Arthur Smith|Hand link]]" in hand_file.read_text()


async def legacy_person(store):
    address = "arthur.smith@example.test"
    entity = await person(store, address, address)
    ledger = store.entities.load()
    subject = ledger.entities[entity["id"]]
    old = f"People/{address} {subject.id}.md"
    (store.notes_root / subject.path).rename(store.notes_root / old)
    subject.path, subject.automatic = old, True
    store.entities.save(ledger)
    return subject.id, old


async def test_a_page_move_does_not_duplicate_facts_for_the_rewritten_note(store):
    identity, old = await legacy_person(store)
    note = await store.create_note(
        CreateNote(
            title="Meeting",
            body=f"See [[{old.removesuffix('.md')}|Arthur]].",
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "account": ["Example"],
                "attendees": ["arthur.smith@example.test"],
            },
        )
    )
    ledger = store.entities.load()
    store.entities.contribute(ledger, await store.entities.notes())
    store.entities.save(ledger)
    for _ in range(3):
        await store.entities.refresh()
    ledger = store.entities.load()
    assert ledger.entities[identity].path == "People/Arthur Smith.md"
    assert (store.notes_root / note.path).read_text().count("[[People/Arthur Smith|Arthur]]") == 1
    for entity in ledger.entities.values():
        facts = [(f.predicate, f.value) for f in entity.facts if f.state == "confirmed"]
        assert len(facts) == len(set(facts)), (entity.name, facts)
    assert len([f for f in ledger.entities[identity].facts if f.predicate == "mentioned"]) == 1


async def test_table_escaped_alias_links_follow_the_move(store):
    identity, old = await legacy_person(store)
    target = old.removesuffix(".md")
    hand = await store.create_note(
        CreateNote(title="Table", body=f"| who |\n| --- |\n| [[{target}\\|Arthur]] |\n")
    )
    await store.entities.refresh()
    assert (await store.entities.get(identity))["path"] == "People/Arthur Smith.md"
    assert "[[People/Arthur Smith\\|Arthur]]" in (store.notes_root / hand.path).read_text()


async def test_unparsed_note_linking_the_old_page_blocks_the_move_and_is_named(store):
    from coppermind.db.models import Note

    identity, old = await legacy_person(store)
    hand = await store.create_note(
        CreateNote(title="Broken", body=f"See [[{old.removesuffix('.md')}|Arthur]].")
    )
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.update(Note).where(Note.id == hand.id).values(state="unparsed"))
    await store.entities.refresh()
    current = await store.entities.get(identity)
    assert current["path"] == old and (store.notes_root / old).exists()
    assert hand.path in current["problem"]
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.update(Note).where(Note.id == hand.id).values(state="ok"))
    await store.entities.refresh()
    assert (await store.entities.get(identity))["path"] == "People/Arthur Smith.md"
    assert "[[People/Arthur Smith|Arthur]]" in (store.notes_root / hand.path).read_text()


async def test_move_retries_a_concurrent_note_edit(store, monkeypatch):
    import coppermind_store.entities as module

    address = "arthur.smith@example.test"
    entity = await person(store, address, address)
    ledger = store.entities.load()
    subject = ledger.entities[entity["id"]]
    old = f"People/{address} {subject.id}.md"
    (store.notes_root / subject.path).rename(store.notes_root / old)
    subject.path, subject.automatic = old, True
    store.entities.save(ledger)
    note = await store.create_note(
        CreateNote(
            title="Meeting",
            body=f"See [[{old.removesuffix('.md')}]].",
            frontmatter={"reviewed": True, "attendees": [address], "date": "2026-11-01"},
        )
    )
    note_path = store.notes_root / note.path
    original_stage = module.stage_bytes
    edited = False

    def stage_with_edit(path, data):
        nonlocal edited
        staged = original_stage(path, data)
        if path == note_path and not edited:
            edited = True
            path.write_text(path.read_text() + "Owner added this.\n")
        return staged

    monkeypatch.setattr(module, "stage_bytes", stage_with_edit)
    await store.entities.refresh()
    assert edited
    assert "Owner added this." in note_path.read_text()
    assert "[[People/Arthur Smith]]" in note_path.read_text()


async def claim(store, entity, note, **kwargs):
    current = await store.entities.get(entity["id"])
    return await store.entities.change(
        entity["id"],
        EntityChange(
            version=current["version"],
            action="claim",
            predicate="role",
            value="Engineer",
            note_id=note.id,
            quote="Casey is an engineer.",
            **kwargs,
        ),
    )


async def test_identity_claims_correction_projection_and_recovery(store):
    a = await person(store)
    b = await person(store, address="other@example.test")
    company = await store.entities.create(EntityInput(kind="company", name="Example"))
    assert a["id"] != b["id"] and a["path"] != b["path"]
    note = await reviewed_note(store)
    a = await claim(store, a, note, target=company["id"])
    path = store.notes_root / a["path"]
    marker = dossier_marker(a["id"])
    original = path.read_text()
    prefix, block = original.split(marker)
    prefix += "Personal writing stays byte for byte.\n\n"
    path.write_text(prefix + marker + block.replace("role: Engineer", "role: Architect"))
    await store.entities.refresh()
    updated = await store.entities.get(a["id"])
    assert not updated["problem"]
    corrections = [f for f in updated["facts"] if f["origin"] == "correction"]
    assert corrections[-1]["value"] == "Architect"
    assert corrections[-1]["evidence"]["path"] == a["path"]
    assert path.read_text().split(marker)[0] == prefix
    assert not any(f["state"] == "confirmed" and f["value"] == "Engineer" for f in updated["facts"])
    # SQL is disposable. Rebuilding uses durable corrections without a model.
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(EntityRecord))
        await session.execute(sa.delete(SearchDocument))
    store.entities = EntityService(store)
    await store.entities.refresh()
    assert (await store.entities.get(a["id"]))["facts"] == updated["facts"]
    assert path.read_text().split(marker)[0] == prefix
    with pytest.raises(VersionConflict):
        await store.entities.change(a["id"], EntityChange(version=1, action="rename", name="New"))
    current = await store.entities.get(a["id"])
    renamed = await store.entities.change(
        a["id"], EntityChange(version=current["version"], action="rename", name="New")
    )
    assert renamed["path"] == a["path"]


async def test_evidence_withdraws_on_unreview_edit_or_delete(store):
    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": False}), note.content_hash
    )
    await store.entities.refresh()
    withdrawn = await store.entities.get(p["id"])
    assert all(f["state"] == "withdrawn" for f in withdrawn["facts"])
    assert "Engineer" not in (store.notes_root / p["path"]).read_text()
    assert not (await store.search(SearchQuery(q="Engineer"), entities=True)).items


@pytest.mark.parametrize("absence", ["deleted", "unreviewed"])
async def test_returning_reviewed_note_restores_its_existing_facts(store, absence):
    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    fact_ids = {f["id"] for f in p["facts"]}
    path = store.notes_root / note.path
    original = path.read_bytes()
    if absence == "deleted":
        path.unlink()
    else:
        assert b"reviewed: true" in original
        path.write_bytes(original.replace(b"reviewed: true", b"reviewed: false"))
    await reconcile_once(store, full=True)
    withdrawn = await store.entities.get(p["id"])
    assert all(f["state"] == "withdrawn" for f in withdrawn["facts"])
    # The reason for withdrawal must survive a restart while the note is absent.
    store.entities = EntityService(store)
    path.write_bytes(original)
    await reconcile_once(store, full=True)
    restored = await store.entities.get(p["id"])
    assert {f["id"] for f in restored["facts"]} == fact_ids
    assert all(f["state"] == "confirmed" for f in restored["facts"])
    assert restored["version"] > withdrawn["version"]
    page = (store.notes_root / p["path"]).read_text()
    assert "role: Engineer" in page and note.path.removesuffix(".md") in page
    hits = await store.search(SearchQuery(q="Engineer"), entities=True)
    assert any(h.kind == "entity" and h.id == p["id"] for h in hits.items)


@pytest.mark.parametrize("withdrawal", ["api", "page", "correction"])
async def test_returning_evidence_does_not_reverse_owner_decisions(store, withdrawal):
    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    fact = next(f for f in p["facts"] if f["predicate"] == "role")
    page = store.notes_root / p["path"]
    if withdrawal == "page":
        page.write_text(
            "\n".join(
                line
                for line in page.read_text().split("\n")
                if f"<!-- fact:{fact['id']} -->" not in line
            )
        )
        await store.entities.refresh()
    elif withdrawal == "correction":
        await store.entities.change(
            p["id"],
            EntityChange(
                version=p["version"],
                action="correct",
                fact_id=fact["id"],
                predicate="role",
                value="Architect",
            ),
        )
    path = store.notes_root / note.path
    original = path.read_bytes()
    path.unlink()
    await reconcile_once(store, full=True)
    if withdrawal == "api":
        # Explicitly withdrawing an already auto-withdrawn fact must also stick.
        current = await store.entities.get(p["id"])
        await store.entities.change(
            p["id"],
            EntityChange(
                version=current["version"],
                action="withdraw",
                fact_id=fact["id"],
            ),
        )
    store.entities = EntityService(store)
    path.write_bytes(original)
    await reconcile_once(store, full=True)
    restored = await store.entities.get(p["id"])
    assert next(f for f in restored["facts"] if f["id"] == fact["id"])["state"] == "withdrawn"
    assert "role: Engineer" not in page.read_text()
    if withdrawal == "correction":
        assert "role: Architect" in page.read_text()


async def test_retired_subject_purges_without_rewriting_git_or_regeneration(store):
    retention(store, 0)
    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    path = store.notes_root / p["path"]
    path.unlink()
    await store.entities.refresh()
    retired = await store.entities.get(p["id"])
    assert retired["retired_at"]
    assert not (await store.entities.list(EntityQuery())).get("items")
    preview = await store.preview_purge()
    assert [c.kind for c in preview.candidates] == ["entity"]
    assert preview.counts.entities == 1
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert p["id"] not in store.entities.load().entities
    await store.entities.refresh()
    assert not path.exists()
    assert not (await store.entities.list(EntityQuery()))["items"]
    assert (await store.get_note(note.id)).body == note.body


async def test_projection_race_occupied_path_and_reserved_folders(store, monkeypatch):
    p = await person(store)
    path = store.notes_root / p["path"]
    path.write_text("An unrelated file, never overwrite me.")
    await store.entities.refresh()
    item = await store.entities.get(p["id"])
    assert item["problem"] and path.read_text() == "An unrelated file, never overwrite me."
    unknown = store.notes_root / "People" / "Handwritten.md"
    unknown.write_text("# A personal document\n")
    await reconcile_once(store)
    assert unknown.read_text() == "# A personal document\n"


async def test_query_paging_relationship_search_and_identity_preview(store):
    p = await person(store)
    await person(store, name="Casey", address="second@example.test")
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    page = await store.entities.list(EntityQuery(limit=1, q="Casey"))
    second = await store.entities.list(EntityQuery(limit=1, q="Casey", cursor=page["next_cursor"]))
    assert page["items"][0]["id"] != second["items"][0]["id"]
    assert not (await store.search(SearchQuery(q="Engineer"))).items
    hits = await store.search(SearchQuery(q="Engineer"), entities=True)
    assert hits.items[0].kind == "entity" and hits.items[0].id == p["id"]
    fact = next(f for f in p["facts"] if f["predicate"] == "role")
    request = dict(version=p["version"], name="Split Casey", fact_ids=[fact["id"]])
    preview = await store.entities.change(p["id"], EntityChange(action="split-preview", **request))
    split = await store.entities.change(
        p["id"], EntityChange(action="split", preview=preview["preview"], **request)
    )
    assert split["id"] != p["id"] and split["facts"][0]["id"] == fact["id"]
    await store.entities.refresh()
    current = await store.entities.get(p["id"])
    assert next(f for f in current["facts"] if f["id"] == fact["id"])["state"] == "withdrawn"
    request = dict(version=split["version"], target=current["id"])
    preview = await store.entities.change(
        split["id"], EntityChange(action="merge-preview", **request)
    )
    merged = await store.entities.change(
        split["id"], EntityChange(action="merge", preview=preview["preview"], **request)
    )
    assert merged["id"] == current["id"]
    assert (await store.entities.get(split["id"]))["redirect"] == current["id"]
    await store.entities.refresh()
    merged = await store.entities.get(current["id"])
    copies = [f for f in merged["facts"] if f["id"] == fact["id"]]
    assert len(copies) == 1 and copies[0]["state"] == "confirmed"
    assert "role: Engineer" in (store.notes_root / merged["path"]).read_text()
    hits = await store.search(SearchQuery(q="Engineer"), entities=True)
    assert any(h.kind == "entity" and h.id == merged["id"] for h in hits.items)


async def test_note_purge_scrubs_quotes_and_orphan_dossier(store):
    from tests.integration.test_note_recovery import commit, git

    retention(store, 0)
    note = await reviewed_note(store)
    await store.entities.refresh()
    p = (await store.entities.list(EntityQuery()))["items"][0]
    assert p["automatic"]
    p = await store.entities.change(
        p["id"], EntityChange(version=p["version"], action="rename", name="Casey Renamed")
    )
    await claim(store, p, note)
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    preview = await store.preview_purge()
    assert {c.kind for c in preview.candidates} == {"note", "entity"}
    before = git(store.notes_root, "rev-parse", "HEAD")
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert "Casey is an engineer" not in (store.entities.root / "catalog.json").read_text()
    assert not (store.notes_root / p["path"]).exists()
    assert git(store.notes_root, "rev-parse", "HEAD") == before


async def test_projection_receipt_survives_interrupted_write(store, monkeypatch):
    import coppermind_store.entities as entities

    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    path = store.notes_root / p["path"]
    path.write_text(path.read_text().replace("role: Engineer", "role: Architect"))
    original = entities.commit_staged

    def interrupt(temp, destination):
        original(temp, destination)
        raise OSError("simulated crash after atomic rename")

    monkeypatch.setattr(entities, "commit_staged", interrupt)
    await store.entities.refresh()
    monkeypatch.setattr(entities, "commit_staged", original)
    store.entities = EntityService(store)
    await store.entities.refresh()
    restored = await store.entities.get(p["id"])
    corrections = [f for f in restored["facts"] if f["origin"] == "correction"]
    assert len(corrections) == 1 and corrections[0]["value"] == "Architect"
    assert not restored["problem"]


async def test_projection_racing_device_edit_is_imported_next_pass(store, monkeypatch):
    import coppermind_store.entities as entities

    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    path = store.notes_root / p["path"]
    original = entities.stage_bytes
    fired = False

    def device_write(destination, data):
        nonlocal fired
        staged = original(destination, data)
        if not fired:
            fired = True
            destination.write_text(
                destination.read_text().replace("role: Engineer", "role: Captain")
            )
        return staged

    monkeypatch.setattr(entities, "stage_bytes", device_write)
    await store.entities.change(
        p["id"], EntityChange(version=p["version"], action="rename", name="Casey New")
    )
    assert "role: Captain" in path.read_text()
    await store.entities.refresh()
    restored = await store.entities.get(p["id"])
    assert any(f["value"] == "Captain" and f["state"] == "confirmed" for f in restored["facts"])


async def test_claim_evidence_is_current_and_reserved_folder_writes_refused(store):
    from coppermind.store_protocol import MoveNote, ValidationFailed

    p = await person(store)
    note = await reviewed_note(store)
    with pytest.raises(ValidationFailed):
        await store.entities.change(
            p["id"],
            EntityChange(
                version=(await store.entities.get(p["id"]))["version"],
                action="claim",
                note_id=note.id,
                quote="Invented quotation",
                value="Invented",
            ),
        )
    for folder in ("People", "Companies/Subfolder", "people"):
        with pytest.raises(ValidationFailed):
            await store.create_note(CreateNote(title="Wrong place", folder=folder))
        with pytest.raises(ValidationFailed):
            await store.move_note(note.id, MoveNote(target_folder=folder), note.content_hash)


async def test_purge_interruption_never_recreates_a_removed_page(store, monkeypatch):
    from coppermind.store_protocol import NotesFilesystemUnavailable, ValidationFailed

    retention(store, 0)
    p = await person(store)
    p = await store.entities.change(p["id"], EntityChange(version=p["version"], action="retire"))
    preview = await store.preview_purge()
    original = store.entities.purge

    def interrupted(*args):
        raise OSError("simulated crash after page removal")

    monkeypatch.setattr(store.entities, "purge", interrupted)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert not (store.notes_root / p["path"]).exists()
    await store.entities.refresh()
    assert not (store.notes_root / p["path"]).exists()
    with pytest.raises(ValidationFailed, match="interrupted retention purge"):
        await store.entities.get(p["id"])
    monkeypatch.setattr(store.entities, "purge", original)
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert p["id"] not in store.entities.load().entities


async def test_orphan_purge_preserves_personal_writing_arriving_before_preview(store):
    from tests.integration.test_note_recovery import commit, git

    retention(store, 0)
    note = await reviewed_note(store)
    await store.entities.refresh()
    p = (await store.entities.list(EntityQuery()))["items"][0]
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    path = store.notes_root / p["path"]
    marker = dossier_marker(p["id"])
    path.write_text(path.read_text().replace(marker, "Personal reminder.\n\n" + marker))
    preview = await store.preview_purge()
    assert [c.kind for c in preview.candidates] == ["note"]
    await store.confirm_purge(confirmation(preview))
    assert "Personal reminder" in path.read_text()
    assert "Personal reminder" in (await store.entities.get(p["id"]))["personal_text"]


async def test_page_edit_keeps_relationship_target(store):
    a = await person(store)
    company = await store.entities.create(EntityInput(kind="company", name="Example"))
    note = await reviewed_note(store)
    a = await claim(store, a, note, target=company["id"])
    path = store.notes_root / a["path"]
    path.write_text(path.read_text().replace("role: Engineer", "role: Architect"))
    await store.entities.refresh()
    updated = await store.entities.get(a["id"])
    edited = next(
        f for f in updated["facts"] if f["value"] == "Architect" and f["state"] == "confirmed"
    )
    assert edited["target"] == company["id"]


async def test_note_linking_a_merged_page_contributes_to_the_target(store):
    keep = await store.entities.create(EntityInput(kind="person", name="Keep"))
    old = await store.entities.create(EntityInput(kind="person", name="Old"))
    request = dict(version=old["version"], target=keep["id"])
    preview = await store.entities.change(
        old["id"], EntityChange(action="merge-preview", **request)
    )
    await store.entities.change(
        old["id"], EntityChange(action="merge", preview=preview["preview"], **request)
    )
    await store.create_note(
        CreateNote(
            title="Linked",
            body="Met them.",
            frontmatter={
                "reviewed": True,
                "attendees": ["[[" + old["path"].removesuffix(".md") + "]]"],
            },
        )
    )
    await store.entities.refresh()
    current = await store.entities.get(keep["id"])
    assert any(f["predicate"] == "mentioned" and f["value"] == "Linked" for f in current["facts"])


async def test_rename_and_aliases_regenerate_the_header_without_marking_personal(store):
    await reviewed_note(store)
    p = (await store.entities.list(EntityQuery()))["items"][0]
    assert p["automatic"] and not p["personal"]
    path = store.notes_root / p["path"]
    marker = dossier_marker(p["id"])
    assert marker in path.read_text() and "entity_id:" not in path.read_text()
    renamed = await store.entities.change(
        p["id"], EntityChange(version=p["version"], action="rename", name="Casey Smith")
    )
    aliased = await store.entities.change(
        p["id"], EntityChange(version=renamed["version"], action="aliases", aliases=["KC"])
    )
    head = path.read_text().split(marker)[0]
    assert "# Casey Smith\n" in head
    assert "- KC" in head or "[KC]" in head
    assert not (await store.entities.get(p["id"]))["personal"]
    path.write_text(path.read_text().replace(marker, "Mine.\n\n" + marker))
    await store.entities.refresh()
    owner = await store.entities.get(p["id"])
    assert owner["personal"]
    await store.entities.change(
        p["id"], EntityChange(version=owner["version"], action="rename", name="Casey Jones")
    )
    head = path.read_text().split(marker)[0]
    assert "# Casey Jones\n" in head and "Mine.\n" in head
    assert aliased["aliases"] == ["KC"]


async def test_reads_and_search_do_not_rescan_dossiers_and_mirror_skips_unchanged(
    store, monkeypatch
):
    p = await person(store)
    await reviewed_note(store)
    await store.entities.refresh()

    async def indexed_at():
        async with store.session_factory() as session:
            return (
                await session.execute(
                    sa.select(SearchDocument.indexed_at).where(SearchDocument.kind == "entity")
                )
            ).all()

    before = await indexed_at()
    await store.entities.refresh()
    assert await indexed_at() == before

    def scanned(*args):
        raise AssertionError("reads must not scan dossier pages")

    monkeypatch.setattr(store.entities, "observe", scanned)
    assert (await store.entities.list(EntityQuery()))["items"]
    assert (await store.entities.get(p["id"]))["id"] == p["id"]
    assert (await store.search(SearchQuery(q="Casey"), entities=True)).items


async def test_search_finds_dossiers_after_an_index_rebuild(store):
    from coppermind_store.indexer import rebuild

    p = await person(store)
    note = await reviewed_note(store)
    await claim(store, p, note)
    await store.entities.refresh()
    assert (await store.search(SearchQuery(q="Engineer"), entities=True)).items
    await rebuild(store)
    hits = await store.search(SearchQuery(q="Engineer"), entities=True)
    assert [h.id for h in hits.items if h.kind == "entity"] == [p["id"]]


async def test_overlong_address_and_dossier_faults_do_not_stop_reconciliation(store, monkeypatch):
    address = "x" * 250 + "@example.test"
    await store.create_note(
        CreateNote(
            title="Odd",
            body="Odd attendee.",
            frontmatter={"reviewed": True, "date": "2026-11-01", "attendees": [address]},
        )
    )
    await reconcile_once(store)
    assert (await store.entities.list(EntityQuery()))["items"] == []

    async def broken(self):
        raise RuntimeError("dossier fault")

    monkeypatch.setattr(EntityService, "refresh", broken)
    counts = await reconcile_once(store, full=True)
    assert isinstance(counts, dict)


async def test_refresh_reads_each_dossier_once_and_never_rereads_notes(store, monkeypatch):
    from pathlib import Path

    p = await person(store)
    note = await reviewed_note(store)
    await claim(store, p, note)
    await store.entities.refresh()
    reads = []
    original_bytes, original_text = Path.read_bytes, Path.read_text

    def spy_bytes(self):
        reads.append(self.name)
        return original_bytes(self)

    def spy_text(self, *args, **kwargs):
        reads.append(self.name)
        return original_text(self, *args, **kwargs)

    async def no_note_reads(identity):
        raise AssertionError("refresh must use the note mirror rows")

    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    monkeypatch.setattr(Path, "read_text", spy_text)
    monkeypatch.setattr(store, "get_note", no_note_reads)
    await store.entities.refresh()
    page = (store.notes_root / p["path"]).name
    assert reads.count(page) == 1
    assert (await store.search(SearchQuery(q="Engineer"), entities=True)).items


async def test_failing_dossier_refresh_does_not_fail_an_index_rebuild(store, monkeypatch):
    from coppermind_store.indexer import rebuild

    await person(store)

    async def broken(self):
        raise RuntimeError("dossier fault")

    monkeypatch.setattr(EntityService, "refresh", broken)
    counts = await rebuild(store)
    assert "notes" in counts
