# Coppermind roadmap

Current as of 2026-10-01. This is a living status board, not an archive: it
says what is merged into `main`, what is next in the order the approved plan
laid out, and what is deliberately not being built yet and why. The design
behind all of it, including what has already diverged from the original
plan, is [architecture.md](architecture.md). What specifically works against
`main` right now, verified against a running stack, is
[STATUS.md](../STATUS.md); this document does not repeat that detail.

The merged list below was read directly from `git log` on `main`, not copied
from a plan, so it reflects what actually landed rather than what was
scheduled.

## Merged

The initial repository skeleton (PR #1, 2026-09-09) is the first row below;
it is the skeleton itself, not one of the increments built on it.
Twenty-one further increments have landed on top of that skeleton, the last
two (#26, #27) on 2026-10-01, after v0.1.0 was tagged at #24 on 2026-09-30:

| Landed | PR | What it added |
|---|---|---|
| 2026-09-09 | #1 | The notes filesystem store and API with note create and get (the repository skeleton) |
| 2026-09-12 | #4 | The Git helper: automatic history of the notes filesystem, no credentials, no network |
| 2026-09-14 | #5 | Conditional note replacement (`PUT` with `If-Match`), so a stale edit is refused instead of silently overwritten |
| 2026-09-14 | #6 | Scoped bearer API keys required on every route |
| 2026-09-15 | #8 | `POST /v1/ingest`: a source and its note are created together, with a recorded claim and file-first writes so a database failure after the files land can be retried without duplicating anything |
| 2026-09-15 | #9 | Replaying an identical ingest is a no-op; changed content appends a new revision instead of a new note |
| 2026-09-15 | #10 | Targeted frontmatter edits (mark a note reviewed) without rewriting the whole file |
| 2026-09-15 | #11 | Listing and filtering notes, with paging that survives a rename between pages |
| 2026-09-15 | #12 | The reconciler's read side: edits, moves, renames, and deletes made on a device are picked up without anyone calling an API |
| 2026-09-15 | #13 | The reconciler's write side: a note created on a device gets an identity assigned automatically once it settles |
| 2026-09-15 | #7 | The Obsidian Sync helper service (supervised, but real connections were refused on purpose at this point) |
| 2026-09-15 | #14 | Ingested sources are projected into the notes filesystem as read-only pages a phone can open |
| 2026-09-15 | #15 | The release pipeline: a pushed version tag builds, signs, and publishes the five images that exist today (`store`, `api`, `admin`, `git`, `obsidian-sync`) and cuts a GitHub release |
| 2026-09-15 | #16 | Admin's front door: first-boot claim and password login, as its own service |
| 2026-09-30 | #17 | The approved architecture, roadmap, and acceptance plan written into the repository |
| 2026-09-30 | #18 | Bump urllib3 to 2.8.0 for CVE-2026-97687/97688/97689; note `make setup` first |
| 2026-09-30 | #19 | Fix concurrent creates producing incorrect suffixes |
| 2026-09-30 | #21 | Guard id recovery behind a `FrontmatterError` check in the reconciler |
| 2026-09-30 | #22 | Guided Obsidian Sync connection from Admin with the official headless client |
| 2026-09-30 | #24 | Admin API Keys and Settings pages, graphical key management and setting edits with revision checks (v0.1.0 tagged on this commit) |
| 2026-10-01 | #26 | CONTRIBUTING: package visibility follows the repository, documenting that the first tag made the five GHCR packages public and that nothing in CI changes that afterward |
| 2026-10-01 | #27 | Fix Admin's CSRF session binding and Git setting bounds (PR #20 follow-up), with regression tests pinning the session-binding fix |

Everything above runs from a plain `docker compose up -d` with nothing
hand-configured first, and is proven by the same automated checks that run
in CI.

## Next, in order

Milestone 2 is split into three waves as set out in issue 28. Each wave is
self-contained and delivers a usable step in the core workflow.

### Wave 1: docs, small fixes, move and rename

1. **Documentation pass.** README, STATUS, and the addendum are current and
   accurate against a running stack.
2. **A0 small fixes.** Remaining paper-cuts surfaced by the first live review:
   a wikilink from the opening note to its `_Sources` page, and the `folder`
   parameter on `POST /v1/notes`.
3. **Move and rename.** `POST /v1/notes/{id}/move` and `POST
   /v1/notes/{id}/rename`, keeping a note's identity across both, plus the
   `GET /v1/folders` folder tree endpoint. Filed notes stay mirrored and
   projections are not clobbered.

### Wave 2: Fields, tags, indexer, search, status dashboard

4. **Fields and tags page** (#30). Admin page for managing frontmatter field
   kinds and vocabularies, renaming keys with the migration job, and the
   tags vocabulary.
5. **Indexer and search.** Built (A2). Full-text search over note bodies,
   titles and generated source pages through PostgreSQL's own text search,
   in the same database: `GET /v1/search` with listing's filters and paging,
   an index the store keeps current from every API write and reconciliation
   pass, a rebuild job that recreates it from the notes filesystem, and an
   Admin Search index page with a Rebuild index button. No new service or
   image; vectors stay deferred (A8). What it does and does not do is in
   [STATUS.md](../STATUS.md).
6. **Sources, status, and problems dashboard** (A4). `GET /v1/notes/{id}/sources`
   to list the sources a note cites; overview counters in Admin; a problems
   view at `GET /v1/admin/notes/problems` for notes the reconciler could not
   parse or file.

### Wave 3: curator and filing rules, failure matrix

7. **Curator and filing rules.** The curator files a reviewed note into the
   right folder by rule, with the Admin pages for rules (create, preview,
   commit), frontmatter schema, and the jobs page.
8. **Failure matrix.** A structured view of all reconciliation failures in
   Admin, never written into a note (operator decisions, issue 28), so the
   person sees the problem report on screen rather than in their notes.

## Milestones 3, 3.5, 4, 5 (proposed)

The order below follows issue 28's planning.

- **Milestone 3:** Helm packaging and lab deployment. The Helm chart and the
  lab handoff run the same images from Compose under Kubernetes.
- **Milestone 3.5:** Intake (A3, A7, A5) plus attachments. A drop folder
  with a deterministic watcher that routes by file type and size through
  the ingest path; the attachments route so decks and PDFs reach devices;
  and the digester hook for per-type interpretation (ICS to calendar
  events, PDF and PPTX text extraction).
- **Milestone 4: survives** (v0.4.0). Backup and restore job with the
  runbook walked once; delete to trash; projections rebuild; schema key
  rename rewriting every note through the store as one Git snapshot; the
  complete documentation set.
- **Milestone 5: dossiers and the enricher.** Dossiers first, as recorded
  on issue 31: PostgreSQL rows for people and companies with aliases,
  relationships, interests, each fact citing the reviewed note it came from;
  queryable through `GET /v1/entities` and an Admin page; a projected entity
  page per person or company in the notes filesystem whose top section is
  the person's own notes and whose lower block, below a clear separator,
  Coppermind regenerates from the database; facts added when a note is
  marked reviewed; entity pages indexed like any note (full text now, vectors
  when A8 lands). The dossier rows in PostgreSQL are derived, not authored:
  a rebuild job re-derives every fact from reviewed notes, so the mirror
  stays regenerable from the notes filesystem like every other mirror. A
  correction an operator makes on the Admin dossier page is written
  file-first under `/data/state` (alongside settings and the schema) and
  then replayed into PostgreSQL, so PostgreSQL remains where dossiers are
  queried (the operator's decision on issue 31) without becoming the only
  copy of the correction. The enricher, as recorded on issues 28 and 31:
  proposes a best guess for every field of an unreviewed note from the
  Fields and tags guidance (#30), the note and source text and the
  dossiers; frontmatter only; never overwrites a value a person set and
  never touches a reviewed note; writes nothing about its doubts into a
  note (problems go to the Admin dashboard); provenance per field; optional
  image, off by default; plain docker compose up needs no model.

## Deliberately deferred

These were considered and set aside on purpose, each with a reason and a
prepared place in the design to pick them back up. This list is not
exhaustive; the full one is in [architecture.md](architecture.md)'s
"deliberately left out of the MVP" section.

| Deferred | Why | Revisit when |
|---|---|---|
| A8: Vectors and pgvector | Full-text search is built (wave 2 item 5); hybrid search is a follow-on to it; a second stateful system would break the one-volume restore and add a consistency problem for a few thousand notes that does not need it yet | A concrete search need plain text search cannot serve |
| A9: Import that is not the adopt reconciler, plus the on-demand rebuild job | Coppermind's remote vault (the Obsidian Sync target) starts empty on purpose; pointing the reconciler at years of existing notes would rewrite and push all of them at once | After the notes filesystem has proven itself in real use, as a deliberate one-time Admin action, and whenever a mirror needs rebuilding from disk |
| Redis for event delivery | The PostgreSQL outbox already gives correctness through reconciliation; Redis only helps an external consumer, and there is not one yet | Something outside this system needs to consume Coppermind's events |
| Rewriting links when a note is renamed | Filing (a move) never breaks a link; only a rename can, and that gap is documented rather than patched around | The first real broken-link report |
| Kubernetes and the Helm chart | Milestone 1 and 2 are still being proven on Compose; standing up the chart before the core path works would be building on an unproven foundation | Milestone 3 starts |
| Single sign-on for Admin | A local claimed password is enough while Admin is reachable only on the lab's loopback network | Admin is ever exposed outside that network |
| Multi-arch images, per-component versions | The lab runs one architecture; one tag already stamps every image consistently | The org's practice changes, or an arm64 host appears |

## Workflow addendum

The first real workflow was checked against v0.1.0 on a live instance on
2026-10-01. [roadmap-addendum-2026-10-01.md](roadmap-addendum-2026-10-01.md)
records what that review found, the operator's decisions from it, and the
reasoning behind the plan above. The asks it raised are tracked as
[#28](https://github.com/sentania-labs/coppermind/issues/28), with the
fields and tags guidance split out as [#30](https://github.com/sentania-labs/coppermind/issues/30)
and dossiers as [#31](https://github.com/sentania-labs/coppermind/issues/31).
"Next, in order" and the milestones above already fold that triage in, so
this document and the addendum should no longer disagree on order.

## What this does not cover

Bug-level detail (concurrent same-title creates answering 409 instead of a
numbered suffix, and a stale mirror row naming the wrong note as unparsed)
lives on the repository's issue tracker, not here. Day-to-day behavior of
what is merged, including every documented limit and known gap, is
[STATUS.md](../STATUS.md).
