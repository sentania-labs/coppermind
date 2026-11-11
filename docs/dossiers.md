# People and company dossiers

Dossiers are undated reference pages in `People/` and `Companies/`. Ordinary
reviewed notes continue to file under `{type}/{yyyy}/{yyyy-mm}/`. Names and
aliases are searchable through the entity API. Two people can have the same
name; exact email addresses and explicit identity decisions distinguish them.
A company has its own identity. A reviewed note's explicit `account` creates or
matches a company; an email domain never creates a company, and only attaches to a company the account names.
Email-only people get readable provisional names and retain their addresses as
aliases. A fuller evidenced name can replace the provisional name.

Read and correct the pages in Obsidian. The first section is Scott's Notes;
the section after the hidden dossier marker is Coppermind's Notes. Properties
include the name, emails, linked companies and aliases. Personal writing is
retained when generated properties are refreshed.
Below the marker, edit a fact
line to correct it or delete the line to withdraw it. The edit becomes a durable
correction with that page and its hash as evidence. There is no conflict alarm
for an ordinary edit, and no separate acceptance ceremony. Keep the marker when
possible; a page that lost its marker ID, or its whole marker while the generated
section is intact, is recovered at its own path. A page moved without its marker is
found only inside `People/` or `Companies/`, by an exact email (person) or domains
or name with aliases (company), and only when one page matches. Notes elsewhere are
never claimed, even when they share an email or name. Malformed or unrelated files are preserved and reported
in Admin Problems. Generated pages never overwrite an unrelated occupied path.
Names can be changed through the API. Moving or renaming a page in Obsidian keeps
its identity via the hidden `<!-- coppermind:dossier id=… -->` marker and adopts
the chosen filename as its name. Legacy frontmatter IDs migrate into that marker. Legacy
email-named pages migrate to readable filenames with journaled link updates for
known references; collisions retain an identifier suffix. Git and Obsidian Sync
may observe an intermediate state during this resumable per-file migration.

Reviewed metadata contributes exact attendee emails, explicit account companies,
attendee-to-company associations and explicit dossier wikilinks in list fields.
Each attendee gets an "attended a meeting for" relationship to the company and the company a
"met with" relationship to the attendee, both under Relationships. Attending never sets a
person's `company`.

A company page lists the email domains known to belong to it in its `domains` property. When a
reviewed note's attendee has an email domain with a whole DNS label equal to the normalized
account name, for example `xcelenergy.com` or
`xcel-energy.com` for Xcel Energy, the domain is recorded on the company
as a `domain` fact with the note as evidence. Public mail domains such as `gmail.com` never
qualify. A person's `company` property is filled from their email domain when exactly one
company holds it, or from an explicit evidenced `organization` fact. Editing `domains` on a
company page, or `company` on a person page, always wins and is not overwritten; removing a
domain withdraws it. A generated property (`name`, `email`, `aliases`, `company`, `domains`) is
filled in only on a page that never had it. Once Coppermind has written a property, deleting it
from the page keeps it deleted.
Existing reviewed notes contribute on refresh. The API also accepts evidenced prose claims from a
reviewed note. All such claims count immediately; there is no proposed state.
Each records the note ID, current hash, date, quotation and extraction version.
Editing, unreviewing or deleting the supporting note withdraws its old-version
contribution. Independent corrections survive.
If that same reviewed note version returns, evidence-driven withdrawals are
restored; explicit withdrawals, corrections and splits remain in effect.
Contradictory recorded facts remain visible together with evidence.

The optional enrichment worker extracts explicit people, companies, relationships,
roles and interests from reviewed note frontmatter and prose. The existing
Enrichment connection, model, enable/pause and attempt limits also govern this
work. Marking the note reviewed is acceptance; there is no separate claim step.
When enrichment is enabled, previously reviewed notes are eligible for extraction
without another review; pausing or disabling enrichment stops these model calls.
Each bounded extraction validates exact quotations and identity references before
committing to the same claim ledger. Names alone never merge identities. New
people need a full name or exact email, and companies need an explicit name.
Failures stay in Admin and do not block review, filing or search. Extraction
receipts prevent replay from duplicating facts or restoring corrected claims.
The worker sends the note and bounded passages retrieved around known attendees,
accounts and matching dossier names, rather than the whole transcript. The job
records omitted transcript characters and validates quotations against stored source
text; changes to source revisions withdraw claims supported by those sources.

For an unreviewed note in the configured review folder (`Review/` by default),
enrichment considers every configured blank
field using selected field and tag guidance, note/source text, and relevant
active dossier facts. It sends at most 20 matching dossiers, including direct
relationship targets, with at most 40 active facts each. The existing total input
limit still applies before transmission. All supplied content is untrusted data;
there are no tools or external retrieval. Unique full-name matches select context,
not identity merges; ambiguous names remain unresolved.

Schema-valid suggestions with model-reported confidence of at least 0.8 fill
blank frontmatter fields, including exact dossier wikilinks in configured list
fields. Confidence is not a calibrated probability. Other suggestions and doubts
stay in Admin. No automatic fill replaces a person-set value or changes reviewed
notes, body bytes or filenames. Read the visible values in Obsidian and mark the
note reviewed when satisfied. Admin's existing controls remain available for
repair. The worker persists input versions, provider/config fingerprint, model
and reported model version (or `not supplied`), confidence, evidence and decisions.
Changes to the note, sources, guidance or relevant dossiers invalidate a pending
write; interrupted automatic writes resume only against their recorded bytes.

## Agent API

Grant `entities:read`, `notes:read` and, for corrections, `entities:write` in
Admin's key settings. Existing and default keys gain neither scope automatically.

| Request | Purpose |
| --- | --- |
| `GET /v1/entities` | Find by `kind=person/company`, `q`, exact `alias`, `relationship` and `target`; `limit` and `cursor` page the results |
| `GET /v1/entities/{id}` | Facts, evidence, aliases, version and page path; withdrawn history is explicitly marked |
| `POST /v1/entities` | Create with `kind`, `name`, optional `aliases` and exact `emails` |
| `POST /v1/entities/{id}` | Apply a versioned correction or identity decision |

Every change supplies the current integer `version` and an `action`:

- `rename`: `name`. `aliases`: the supplied `aliases` and/or `emails` replace
  those lists. Omitted lists are preserved.
- `claim`: `predicate`, `value`, optional entity `target`, `note_id`, and an
  exact `quote` from the current reviewed note body. Source artifacts are not
  read by this endpoint. Use predicate `interest` for interests, `organization`
  with a company target for employment, and `role` for a role.
- `correct`: `fact_id`, replacement `predicate`, `value` and optional `target`.
  The old fact stays withdrawn and the new fact records what it superseded.
- `withdraw`: `fact_id`. `retire` hides the subject from active queries.
  `restore` restores a retired subject, including a deleted page.
- `merge-preview`: `target`. `split-preview`: a new `name`, selected `fact_ids`,
  optionally the original subject's `emails` to transfer for future mentions.
  The response lists affected facts and notes and a `preview` token. Submit the
  same arguments with `merge` or `split` and that token to apply. A changed
  catalog requires another preview. Merged IDs remain redirects; a later split
  separates mistakenly combined facts under a new identity.

Version conflicts return 409. Invalid evidence or an expired identity preview
returns 422. Entity access does not grant `sources:read`. Dossiers quote reviewed
notes, so every entity route also requires `notes:read`. A fact that cites a
transcript or other source returns its value and `source_versions`, with an empty
`quote` and `quote_withheld: true`, unless the key also holds `sources:read`. The
dossier page in the owner's vault keeps the quote. Public `/v1/search`
requires `notes:read`; entity hits additionally require `entities:read`.
Filtering happens before ranking and paging.
Entity snippets include only active facts, not withdrawn evidence.

Admin's Dossiers view shows People, Companies, facts, relationships, interests
and evidence for setup and repair. Daily reading, corrections and identity
operations work in Obsidian or the agent API.

## Voice recognition

Recognising recurring speakers by voice is biometric data about other people, so it is off
by default and enabled per person. Coppermind keeps voice signatures only, never audio clips.
The transcription service returns a signature (`model`, `version`, vector) for each speaker;
until review these are held privately with the recording, never in notes, the vault or API
responses. They are removed when review completes (after the opted-in speakers' signatures
are saved), whether or not the audio removal succeeded, and also if the note is deleted or
purged.

- **Saving:** completing review with a speaker name the owner accepted or corrected saves
  that speaker's signature against the person, only if the person is opted in. There is no
  separate confirmation step.
- **Suggesting:** on a later recording, each speaker's signature is compared with saved ones
  of the same `model` and `version`. A close match appears in the review panel as
  `Voice: <name>` (cosine at least the Voice threshold, 0.60 by default) or
  `Maybe: <name>` (at least the Maybe threshold, 0.45 by default). A single `Voice` match
  that does not contradict a confident model name starts selected; `Maybe` matches and
  ambiguous or conflicting matches are left for the owner to choose. Nothing is applied
  without review. A candidate the owner passed over is not offered again
  for that speaker on that note.
- **Deleting:** a person's signatures can be deleted at any time, which also clears their
  turned-down suggestions. Turning a person's recognition off does the same in one step,
  after a warning ("Turning this off deletes N saved voice signatures for <name>. Turning it
  on again starts from nothing."), so off means nothing is stored.
  Retiring, merging or purging the person removes them too.

The People view in Obsidian (command "Manage people and voice recognition") shows a
person's opt-in, saved signatures with their source notes, and Delete signatures. Admin's
Dossiers tab offers the same per-person controls for setup and repair, and is where the
two thresholds are set; a change affects future suggestions only.

| Request | Scopes | Purpose |
| --- | --- | --- |
| `GET /v1/entities/{id}/voice` | `entities:read`, `notes:read` | Opt-in state and signature summaries (note, date, speech seconds; never vectors) |
| `PUT /v1/entities/{id}/voice` | adds `entities:write` | `{"enabled": true/false}`; `false` also deletes the person's signatures |
| `DELETE /v1/entities/{id}/voice` | adds `entities:write` | Delete the person's signatures |
| `GET`/`PUT /v1/entities/voice/settings` | read / adds `entities:write` | The `strong` and `maybe` thresholds, each -1 to 1 with `maybe` not above `strong` |

## Deletion and recovery

Deleting a page in Obsidian retires the subject at the next reconciliation, index rebuild,
retention preview or dossier change. It stays out of active queries and enrichment and is not recreated
without an explicit restore. Retired dossiers join the existing retention
preview and confirmed purge. The preview includes their pages, facts and
relationships; other subjects' notes and shared sources stay. Purging a deleted
supporting note removes its quoted claims and context records. An automatic
subject with no remaining support, independent correction or personal writing
can be purged with its last supporting notes. Minimal identity hashes suppress
unwanted re-creation. Git history is never rewritten.

Back up the complete notes filesystem and `/data/state/entities/`, alongside
the existing sources and control state. `entities/catalog.json` schema version
1 is authoritative for identities, corrections, evidence and operation receipts.
Back up `/data/state/context/` too: it contains extraction and automatic-fill
receipts. Versions preceding this slice cannot read the new dossier job purpose;
rollback requires the complete pre-upgrade snapshot, including context state.
PostgreSQL `entities` and entity search rows are mirrors, rebuilt during metadata
rebuild or entity reconciliation without a model. Notes alone cannot restore
independent corrections. Keep the previous complete-data snapshot for rollback
to a version without dossier support; disable new workers before rolling back.
The migration leaves the previous email dossier and contact-correction files
in place and records stable mapping receipts in the entity catalog.

This is notes-native knowledge only. There is no Recall synchronization or
cross-source ingestion. A real Obsidian phone round trip remains a device check;
local projection and API tests do not claim that evidence.
