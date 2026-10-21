# Finish the intake workflow and dossiers

Status: proposed implementation plan, October 21, 2026. Baseline: main
`f2295a7`, the target for v0.5.1. This document plans work; it does not authorize
feature implementation or claim the remaining features have shipped.

Scott's instruction, preserved verbatim before release publication:

> okay tag 0.5.1 and document an implementation plan w/ mockups as needed for the remaining features needed for issue 28 and 31

Release authorization applies to an annotated `v0.5.1` tag on that merged
main commit. The release contains the Sync settings/live controls and work-note
review fixes from #52 and #53. This plan is a separate documentation change.

## Outcome and order

A dropped document becomes a source and a reviewable note. A reviewed note can
contribute evidenced facts to people and companies. Those facts improve later
metadata suggestions without changing authored text or inventing identities.
Everything remains recoverable from the notes filesystem, source bundles and
control state when PostgreSQL is lost.

Deliver six batches, each independently releasable: **attachments and intake,
document digestion, dossiers, dossier-aware enrichment, complete rebuild and
recovery, then optional hybrid search**. Put the durable job/receipt foundation
in the first batch; do not postpone crash safety until the recovery batch.
Do not assign release numbers before Scott authorizes each release.

[Interactive mockups](../../.lavish/issues-28-31/index.html) show the proposed
Dropbox, entity detail, enrichment review, and recovery/search controls. They
use Coppermind's current Admin visual design and fictional example data.

## Coverage and decisions already made

Read issue comments as well as their original bodies:
[#28](https://github.com/sentania-labs/coppermind/issues/28),
[#31](https://github.com/sentania-labs/coppermind/issues/31).

| Requirement | Merged baseline | Remaining work / disposition |
|---|---|---|
| A0: small fixes | Source wikilinks, folder on create, attachment-setting guidance and #27 are present | Retain regression coverage; no new feature |
| A1: review to filing and move | Monthly filing and move/rename exist | Preserve the current monthly model, not the older configurable filing-rule proposal |
| A2: full-text search | PostgreSQL search and rebuild exist | Extend eligibility/indexing to derived document text and entity pages |
| A3: Dropbox | Durable receipts, duplicate-byte detection, calendar ICS/ZIP routing | Markdown intake, summary/transcript grouping, documents, failed-file disposition and explicit cleanup policy |
| A4: visibility and evidence | Sources, note sources, status and problems endpoints exist | Include new intake/digestion/entity job counters and failures |
| A5: digesters | Calendar archive, recurrence, contacts and queries exist | PDF/PPTX extraction; shared digester lifecycle and calendar adapter |
| A6: enrichment | Optional model connections, schema guidance, source text, calendar/contact context, human-approved proposals | Dossier context, per-field confidence and immutable provider/model provenance |
| A7: attachments | No public upload endpoint | Scoped uploads, sync-size cap and viewing copies |
| A8: hybrid search | No pgvector or embeddings | Final optional batch; deliberately deferred until explicitly commissioned |
| A9: import/rebuild | Folder-aware create, external frontmatter, adoption controls, metadata/source/search rebuild | Complete observable orchestration and isolated full restore evidence |
| #31: dossiers | Reviewed attendee emails produce a People view with supporting notes | Entity model, facts/relations/interests, companies, query/correction API, projections, enrichment integration |

Evidence: `dropbox.py` routes only calendars and skips Markdown;
`note_lifecycle.py::rebuild_dossiers` builds email-to-note contributions;
`context_service.py::evidence` uses selected schema fields, source text and
calendar contacts, not the dossier map. `notes.py::rebuild_metadata` already
recovers sources, reconciles notes, files reviewed notes and rebuilds search.
All paths above are under `services/store/coppermind_store/`. API routes live
under `services/api/coppermind_api/v1/`.

Keep the subsequent decisions: no doubts or review notes written into authored
notes; review remains human; tags grow organically; Sync Plus defaults to a
200 MB per-file attachment cap (define this setting in bytes); no vocabulary
migration gate. Existing partial calendar exports retain absent events and
honor explicit cancellation. Do not replace this with whole-export deletion.

## Batch 1: attachments and reliable intake (A3, A4, A7)

**User outcome:** an agent or a person can deliver a file, see exactly what
happened, and retry without creating another source or note.

1. Add a durable job contract shared by intake and later digestion: id, input
   checksum, handler/version, states, attempt count, timestamps, safe error,
   outputs and per-step completion receipts. Persist originals before queueing.
   Resume unfinished steps after restart; publish counters to status and Admin.
2. Add `POST /v1/attachments` with a new `attachments:write` scope and bounded
   streaming uploads. Return attachment identity, checksum, size and relative
   viewing path. Writes run through the store contract; Admin never writes files.
   Use server-controlled paths, content sniffing, exclusive creation and
   checksum replay. Refuse traversal, symlinks, empty/invalid names and exceeded
   limits. Existing keys gain no new scope automatically.
3. Add Admin controls for the per-file cap (default 200,000,000 bytes), enabled
   formats and intake pause. Retain the separate calendar parser limits.
   Files larger than the sync cap can be preserved as sources within a separate
   source-intake ceiling, with a visible "viewing copy omitted" outcome.
   Default the source ceiling to the same cap; changing it is an Admin action.
4. Extend the shared Dropbox router, excluding eligible pending Markdown from
   ordinary adoption. Move plain Markdown into Review using the store, preserve
   its body and supplied external keys, assign identity only if absent, and
   set unreviewed according to the intake contract. Known note IDs go through
   normal collision/recovery checks, never silently acquire another identity.
5. Recognize explicit `<stem>.summary.md` and `<stem>.transcript.txt` pairs in
   one folder. Wait for both stable members; create one source and one opening
   note, preserving both originals. A missing counterpart remains "Waiting for
   pair" with a manual "Import singly" choice. A retry or late counterpart must
   attach to the recorded intake identity, not make a second opening note.
   Arbitrary similarly named files are not paired automatically.
6. Preserve unsupported input and expose a safe reason, original path and
   retry action. Implement the original failed-folder/sidecar behavior only
   under the confirmed disposition policy below. Index neither failed originals
   nor pending sources. Show states: Waiting, Processing, Needs identification,
   Ready for review, Unsupported, Failed and Done.

**Disposition decision D1:** propose keeping the current preserve-originals
behavior as the default. Offer a separate "consume completed drops" setting:
only remove a dropped file after original, source, note and any viewing-copy
receipts are durable, and only if its current checksum still matches. Failed
files can move to `Dropbox/Failed` with a sidecar reason under that policy.
This intentionally differs from A3's original automatic removal request and
requires Scott's decision before A3 can close. Enabling removal must retain
his explicit authorization before execution. A changed device file is never
removed. Exclude Failed and receipt files from adoption and watcher recursion.

**Acceptance:** duplicate bytes through upload and filesystem create one result;
restart between each durable step resumes; changed input is preserved; a pair
arriving across two scans yields one opening note; unsupported input is visible;
the size cap has boundary tests; a PDF viewing copy opens on a real device.

## Batch 2: document digestion and calendar compatibility (A3, A5)

**User outcome:** a dropped PDF or slide deck can be read on a device and found
by its extracted text, while the original remains the evidence.

Add bounded parser workers for PDF and PPTX, isolated like the calendar worker
with CPU, memory, elapsed-time, expansion and page/slide limits. Select and pin
libraries after license/security review. No network access, macros or active
content. Encrypted, corrupt or image-only documents remain preserved with an
explicit failure or "No extractable text" outcome; OCR is outside this batch.

Write derived text and a provenance manifest (original digest, source revision,
handler version, output digests, page/slide references) in a derived namespace
inside the source revision. Never change original artifact bytes or invalidate
the original manifest's recorded hashes. Add a compatible manifest version if
required, with old bundles still readable. Repeat digestion writes a new derived
generation; an atomic pointer selects the complete generation, preventing mixed
results. Recovery re-derives these files without the model.

Create a deterministic opening note with source and attachment links, no invented
summary. Reuse source approval and publication eligibility: text becomes searchable
only when the linked reviewed note and source are eligible. Retraction, unreviewing,
missing notes and source revision changes invalidate derived search rows.

Wrap existing calendar intake in the same job/status interface; do not rewrite
calendar identity, UID binding, recurrence or partial-export behavior. Existing
`/data/state/calendars` originals/catalogs remain readable. New calendar imports
can expose source identities through the ordinary source API using a stable
calendar-to-source mapping. Backfill is explicit, resumable and deduplicated.
The structured query gains date range, calendar and attendee/organizer filters,
pagination and local timezone rendering. Calendar events remain data, not notes.

**Acceptance:** text from a PDF page and PPTX slide is searchable after review
and traceable to its source; no match before approval; duplicate ingestion and
parser crash preserve identity; calendar cancellations, DST and partial exports
keep existing behavior; rebuild restores the same active derived generation.

## Batch 3: people and company dossiers (#31)

**User outcome:** browse a person or company, see each fact's supporting reviewed
notes, correct a mistake, and reach the same record from Obsidian.

### Durable model and identity

Introduce entities (stable ID, person/company kind, canonical display name),
explicit aliases, relationships (typed directed links), interests and facts.
Each fact carries subject, predicate, value or entity target, supporting note ID,
note hash, date, quoted evidence, extraction version and confirmation state.
Keep conflicting facts visible with their evidence; do not choose the most recent
as truth without showing the conflict. Do not infer employer or identity from
email domain or name resemblance. Exact emails and explicit operator alias links
are identity evidence. A name-only mention stays unresolved until selected.

**Decision D4:** #31 originally called PostgreSQL the source of truth for curated
facts. The later roadmap and repository recovery contract require derived rows
and file-first corrections. Recommend the following file-first ledger model,
but record Scott's acceptance of that reconciliation before P5 implementation.
The alternative is a required database backup for curated facts, which changes
the existing empty-database recovery promise and needs an architecture decision.

Under the recommended model, PostgreSQL provides queryable
entity/fact/alias/relationship mirrors. Canonical
entity IDs, accepted fact records, corrections, identity links and withdrawals
are file-first under `/data/state/entities/`; no authored correction exists only
in PostgreSQL. Structured contributions from final reviewed metadata are
rebuildable without a model. Fact extraction from prose is proposed during review
and accepted explicitly. Persist accepted claims and exact evidence so a rebuild
does not require a nondeterministic model to recreate past decisions.

**Decision D2:** recommend explicit acceptance of new prose-derived facts during
note review; structured confirmed metadata can contribute automatically. The
older "reviewed means confirm everything" wording must not silently authorize
unseen model claims. Scott chooses this before implementing automatic growth.
A reviewed note edited later withdraws or marks obsolete its old-version claims;
re-extraction of new prose requires review. Unreviewing or losing a supporting
note removes its contribution from active views and search, retaining audit data.

Import the current email dossiers and contact corrections with stable mapping
receipts. Keep old files during migration. Ambiguous aliases become pending
identity decisions, never silent merges. Splits and merges need previews of
affected facts/notes and durable authorization; old IDs redirect rather than
being reused. Changes must be reversible through new correction records.

### API, Admin and projections

Add `GET /v1/entities` (kind, text, alias, relationship filters and paging),
`GET /v1/entities/{id}` and supporting-note links with `entities:read`.
Fact corrections and identity decisions require `entities:write`, version checks,
Admin CSRF where applicable and a durable receipt. Reading supporting source
content still requires `sources:read`; entity access must not bypass it.

Admin offers People and Companies lists, an entity detail with Facts,
Relationships, Interests and Evidence, and a correction preview. Proposed and
confirmed facts remain visibly different. The mockup shows a conflicting role
rather than a falsely certain biography.

Project `People/` and `Companies/` pages with a protected user section above a
stable delimiter and a generated block below it. Reserve these folders from
ordinary note adoption, review and filing. Use stable entity IDs in projection
metadata; refuse an occupied unrelated path. Update only the generated block
using a hash check; preserve the user section byte-for-byte. A missing/edited
delimiter is a visible problem, not permission to replace the page. User edits
inside the generated section trigger a conflict preview, not silent loss.

Index entity pages and active fact text in full-text search as an entity result
kind, linking to supporting notes. Gate every result kind before retrieval,
ranking, counts, facets and snippets: `notes:read` admits notes, `entities:read`
additionally admits entity facts/pages, and source-content results require
`sources:read`. Apply the same restrictions to hybrid and semantic search,
including embedding candidate selection. A notes-only key must not receive
entity snippets, counts or inferred entity results. Existing note text can still
contain a human-authored entity link; it does not grant access to the dossier.
Enrichment proposes entity wikilinks in
schema-declared list fields and the person confirms them. Renaming an entity
keeps its projection path stable unless a separate, explicit move is accepted,
so existing backlinks continue to work. Personal projection text is part of the
notes filesystem backup and Git; generated text is reproducible from state.

**Acceptance:** two people sharing a name remain separate; a company has
independent identity; each fact resolves to evidence; conflict corrections survive
empty-PostgreSQL recovery; note edits/retraction withdraw old facts; projected
personal text and backlinks survive regeneration; API scopes and pagination work;
entity pages and their links open on a phone; notes-only keys see no dossier
results, snippets or counts in any search mode.

## Batch 4: dossier-aware enrichment (A6 and #31)

**User outcome:** metadata proposals use what has been confirmed about the
people/companies involved, with evidence and confidence visible in review.

Extend the current evidence builder, not a second agent platform. Supply only
relevant confirmed entity facts and selected field/tag guidance, bounded by
input limits. Never send the whole dossier corpus. Treat source text, entity
content and field guidance as untrusted data, with no tool or URL execution.
Use exact identity references and quotations; unresolved names stay unresolved.

Allow every eligible declared field to be selected in Admin, excluding identity,
review state, sources and other system-owned keys. Propose values for blanks;
never replace a human-set value automatically. Preserve the existing human
approval flow and body invariance. Any proposed replacement is an explicit
before/after decision, never an automatic fill. Reviewed notes are frozen.

Persist a provider/config fingerprint, model identifier, provider-reported model
version when available (otherwise "not supplied"), time, input versions,
per-field confidence, evidence and decision. Configure a confidence threshold
in Admin: proposals below it remain visible as held items/problems but do not
write metadata. Confidence is model-reported, not a calibrated probability.
Dossier, schema or source changes invalidate affected proposals. Partial approval
and restart resume from receipts with the current note hash.

**Decision D3:** retain enrichment inside the store's optional worker, disabled
by default, rather than add the separate image originally requested by A6.
The store remains the only writer; parsing/model calls stay bounded. If process
isolation becomes necessary, a separate worker calls the store contract and
receives no notes-volume write access. Record Scott's packaging decision before
claiming literal A6 completion.

**Acceptance:** confirmed company/person facts improve an otherwise blank field;
unrelated dossiers are not transmitted; low-confidence fields write nothing;
human values and reviewed notes remain unchanged; changed facts invalidate a
pending proposal; model outage leaves intake, review, filing and full-text intact.

## Batch 5: complete rebuild and restore (A9, #31 recovery)

**User outcome:** Admin can show what is recoverable, rebuild derived state,
and explain exactly which step failed without restarting everything blindly.

Extend `rebuild_metadata` into a persisted, single-active-job workflow with
preview, progress, phase receipts and retry. Phases: validate durable originals
and manifests; restore control/identity ledgers; recover source and attachment
mirrors; reconcile notes without triggering a bulk import; rebuild calendar and
entity mirrors; regenerate safe projections; rebuild full-text; optionally queue
embeddings. Reuse completed phases only when input fingerprints still match.
New projections/indexes use generations and switch only after validation.

Separate a read-only verification preview from actions that rewrite projections
or trigger filing. Require explicit recorded confirmation for overwrites and
show affected paths first. Do not discard user sections, unknown files or
existing corrections. Pause intake and filing for a consistent rebuild; display
reads as unavailable where they cannot be guaranteed current, rather than serve
mixed generations. Resume using a checkpoint after PostgreSQL or worker failure.

Document the complete backup set: notes and Git, sources/derived artifacts,
attachments, all control state, calendar originals/catalogs, entity decisions,
model credentials, API keys, Admin identity, recovery receipts and Sync state.
Keep secret-bearing backups protected and outside CI artifacts. No public backup
export or automatic source purging. An optional DB dump accelerates restore but
must not be required. Default restore is isolated with Sync disabled to avoid
reconnecting a clone to real devices.

**Acceptance:** restore a synthetic corpus into an isolated empty database and
separate volumes; compare IDs, source hashes, personal entity sections, corrections,
calendar events, counters and full-text results. Kill/restart between phases.
Walk the documented operator procedure and retain a redacted result. A separate
Scott-supervised real-device reconnect proves phone delivery. A green job alone
cannot close this acceptance item.

## Batch 6: optional hybrid search (A8)

This remains deferred until explicitly commissioned, but is included so #28 has
no unplanned remainder. Add pgvector to the existing PostgreSQL, not another
database. Confirm extension availability for both bundled and external DBs before
migration. Existing full-text deployments must start without a model or extension;
only vector enablement requires pgvector.

Use the existing model-connection pattern with a separate embeddings assignment,
capability test, Admin enable/pause controls, batch limits and progress. Default
off. Store chunk identity, source/note/entity version, model ID/version/dimension
and content digest on each vector. Chunk notes, eligible source text and entity
facts with evidence locations; never index unreviewed or withdrawn content.

Add search modes `text` (default), `hybrid` and `semantic`, with identical scopes,
field filters and publication rules. Return match type and source location. A
model outage makes hybrid fall back to text with an explicit response indicator;
semantic-only requests report unavailability rather than pretending text is
semantic. Re-embedding builds a separate generation and switches atomically;
never compare incompatible model/dimension vectors. Pause/resume and progress
are visible in Admin. A model change does not discard the working generation.

**Acceptance:** cross-phrasing matches a synthetic transcript; exact field
filters hold in every mode; unreview/retraction removes vector eligibility;
model failure preserves text search; interrupted re-embedding resumes; rollback
can disable vectors without dropping extension data or damaging full-text.

## Delivery slices and completion gates

| PR slice | Depends on | Scope and proof |
|---|---|---|
| P1 | none | Durable job contract, attachment API and cap; crash/replay tests |
| P2 | P1 | Markdown/pair intake, disposition policy and Admin Dropbox; device round trip |
| P3 | P1 | PDF/PPTX workers and derived generations; extraction and publication tests |
| P4 | P1 | Calendar job/source adapter and structured query; migration/replay/recurrence tests |
| P5 | none | Entity identity/fact/correction ledger and SQL mirrors; evidence/withdrawal tests |
| P6 | P5 | Entity API, Admin and protected projections; scoped query and phone proof |
| P7 | P5, P6 | Dossier-aware enrichment and review facts; stale/low-confidence/approval tests |
| P8 | P2, P3, P4, P6, P7 | Complete rebuild orchestration and isolated restore drill |
| P9 | P3, P6, P8; explicit go | Optional pgvector, embeddings and hybrid search |

P1-P4 and P5-P6 can be scheduled independently, but this document does not
commission parallel agents or implementation. Every slice carries its Admin
controls, API documentation, unit/integration checks, rendered desktop/mobile
review and failure/restart evidence. Use the existing SDLC gates and the same
local/CI definitions. Packaging changes use the github-ci skill when applicable.

Before each state-format migration, snapshot compatible state and record the
minimum reader version. Keep older readers supported where possible; otherwise
rollback means restoring the matching pre-upgrade state, never pointing old
binaries at newer ledgers. Disable new workers first to contain an incident.
No deployment, public DNS changes or production corpus migration belongs in this
product plan.

Close #31 only after entities, projections, evidence, corrections, enrichment
consumption and recovery are demonstrated end to end. Close #28 only when every
A-row is accepted or explicitly moved/deferred with Scott's decision. A8's old
deferral does not mean it is implemented. Resolve D1-D4 in the issue record and
update stale roadmap/STATUS claims as the corresponding slices land. The Recall
boundary stays notes-native: external knowledge ingestion is outside this plan.
