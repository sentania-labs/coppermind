# People and company dossiers

Dossiers are undated reference pages in `People/` and `Companies/`. Ordinary
reviewed notes continue to file under `Notes/{yyyy}/{yyyy-mm}/`. Names and
aliases are searchable through the entity API. Two people can have the same
name; exact email addresses and explicit identity decisions distinguish them.
A company has its own identity. A name alone or an email domain never creates
an inferred identity or employer.

Read and correct the pages in Obsidian. Writing above
`<!-- coppermind:dossier -->` is preserved byte for byte, except that the
generated title and frontmatter `aliases` follow the dossier's name and aliases.
Below the delimiter, edit a fact
line to correct it or delete the line to withdraw it. The edit becomes a durable
correction with that page and its hash as evidence. There is no conflict alarm
for an ordinary edit, and no separate acceptance ceremony. Keep the delimiter
and `entity_id` intact; malformed or unrelated files are preserved and reported
in Admin Problems. Generated pages never overwrite an unrelated occupied path.
Names can be changed through the API without changing page paths or backlinks;
the page title and `aliases` are updated to match.

Reviewed metadata contributes exact attendee emails and explicit dossier
wikilinks in list fields. The API also accepts evidenced prose claims from a
reviewed note. All such claims count immediately; there is no proposed state.
Each records the note ID, current hash, date, quotation and extraction version.
Editing, unreviewing or deleting the supporting note withdraws its old-version
contribution. Independent corrections survive.
If that same reviewed note version returns, evidence-driven withdrawals are
restored; explicit withdrawals, corrections and splits remain in effect.
Contradictory recorded facts remain visible together with evidence. This first
slice provides the model and claim API; automatic model extraction and
dossier-aware enrichment are the follow-up slice of issue 31.

## Agent API

Grant `entities:read` and, for corrections, `entities:write` in Admin's key
settings. Existing and default keys gain neither scope automatically.

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
returns 422. Entity access does not grant `sources:read`. Public `/v1/search`
requires `notes:read`; entity hits additionally require `entities:read`, and
source hits require `sources:read`. Filtering happens before ranking and paging.
Entity snippets include only active facts, not withdrawn evidence.

Admin's Dossiers view shows People, Companies, facts, relationships, interests
and evidence for setup and repair. Daily reading, corrections and identity
operations work in Obsidian or the agent API.

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
PostgreSQL `entities` and entity search rows are mirrors, rebuilt during metadata
rebuild or entity reconciliation without a model. Notes alone cannot restore
independent corrections. Keep the previous complete-data snapshot for rollback
to a version without dossier support; disable new workers before rolling back.
The migration leaves the previous email dossier and contact-correction files
in place and records stable mapping receipts in the entity catalog.

This is notes-native knowledge only. There is no Recall synchronization or
cross-source ingestion. A real Obsidian phone round trip remains a device check;
local projection and API tests do not claim that evidence.
