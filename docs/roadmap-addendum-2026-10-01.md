# Roadmap addendum: the first real workflow (2026-10-01)

The roadmap in [roadmap.md](roadmap.md) orders work by the approved plan. This
addendum orders it by the first workflow Coppermind is meant to carry, after
checking v0.1.0 against a live instance and against the operator's existing
pipeline, which turns meeting recordings and dropped files into Markdown notes,
puts them in front of a person for review, files them, and lets agents query
them. The asks that came out of it are tracked as
[#28](https://github.com/sentania-labs/coppermind/issues/28). Where this
document and roadmap.md disagree on order, roadmap.md is the plan and this is
the reason to revisit it.

Nothing here changes the design contract. The decisions in
[architecture.md](architecture.md) stand; every item below fits inside them,
and the one that strains them (the enricher) says so and states its rules.

## What was checked

- The live instance at v0.1.0: health, readiness, the generated OpenAPI
  contract (10 routes), scope probes with empty bodies (422 rather than 403),
  a listing of every note. No note or source was created during the review,
  because there is no delete route yet and a test note would have reached the
  operator's devices.
- A device round trip, seen working. A Markdown note created on a PC at the
  root of the notes filesystem and one inside a folder were both adopted by
  the store (at 1:08 PM and 1:14 PM Central), the identity and shipped defaults
  written server-side, and the frontmatter delivered back to the same PC
  through Obsidian Sync within about two minutes. That is acceptance case L4,
  first half, on a real deployment rather than a compose stack.
- Container logs for the reconciler, Admin and the sync helper.

Not verified: delivery to a phone (logs cannot show it), and whether the
settings a device needs (below) travel between devices.

## Delivered against the workflow

| Workflow step | Capability | v0.1.0 |
|---|---|---|
| A recording arrives with its raw artifacts | `POST /v1/ingest`: bundle plus opening note, one transaction, idempotent replay, revisions on changed content. The provider plus external id pair maps directly onto a recording id. | delivered |
| The raw material stays readable on a device | `_Sources/<Provider>/` projection, regenerated per revision | delivered for text; a binary artifact is listed, not shown |
| The note lands where a person sees it | Every create and ingest writes to the configured review folder (`Review/` by default) with `reviewed: false` unless the request's frontmatter says otherwise | delivered |
| Metadata is applied after arrival | `PATCH /v1/notes/{id}/frontmatter` with `If-Match`, body untouched | delivered |
| A person marks a note reviewed on any device | Edit the property in Obsidian, mirrored within about 90 seconds; or patch it | delivered |
| The reviewed note gets filed | Curator | not built |
| Move a note deliberately | `POST /v1/notes/{id}/move`; `folder` on create | not built (`folder` is in api.md, not live) |
| An agent asks "what do I know about X" | `GET /v1/search` | not built; listing filters on frontmatter only |
| A consumer asks how many notes await review | `GET /v1/status` | not built; a paged list filtered on the configured review folder (`folder=Review` by default) and `reviewed=false` is the workaround |
| The operator's frontmatter vocabulary | Admin-editable schema | the schema file is honoured; the Admin page is not built |
| Attachments on notes | `POST /v1/attachments` | not built |
| History and recovery | Git helper snapshots | delivered; no API over history |
| Per-caller access | Scoped `cm_` keys, Admin keys page | delivered |
| Something outside reacts to a change | Event outbox for an external consumer | deferred until a consumer exists |

## A person has no deterministic way in

`Review/` is an outbox toward the person: services write it, the person clears
it. The API is the only entry point, and it is for tools. A file dropped into
any folder on a device outside the store-owned folders is adopted if it is
Markdown (with the shipped defaults, which are wrong for most dropped things)
and ignored if it is not. So the
natural gesture "download the thing, put it in the inbox, it gets digested" has
no landing spot. #28 A3 is that landing spot: a store-owned, visible peer of
`Review/` with a deterministic watcher that routes by file type and size
through the ingest path and leaves the folder empty when caught up.

## Where everything lives, with the drop folder and the digester in place

| Place | Holds | Synced | In Git | Written by |
|---|---|---|---|---|
| `/data/notes/Review`, `Work`, `Personal`, `Reference`, `Journal` | The person's notes. Mutable, theirs. | yes | yes | the person; ingest (opening note); enricher (frontmatter only); curator (moves only) |
| `/data/notes/<drop folder>` | Things the person owes the system. Empty when caught up. | yes | no | the person drops; the watcher removes |
| `/data/notes/_Sources` | One rendered page per source: what arrived, from where, text inline, binaries listed. | yes | no | the store, on ingest and revision; a person's edits do not survive |
| `/data/notes/_Attachments` | Files notes embed and Obsidian opens. A viewing copy. | yes, within plan limits | yes | Obsidian; the attachments route; the watcher |
| `/data/sources/<id>/rNNNN` | The original bytes, immutable, revisioned. What a note is checked against. | no | no | ingest only |
| `/data/state` | Settings, schema, rules, keys, the Admin record. | no | no | Admin, bootstrap |
| PostgreSQL | Mirror and indexes: frontmatter columns, full-text, calendar rows, embeddings. All derived. | no | no | store, indexer, digester; rebuildable from `/data` |

The distinction that took longest to land, stated once: a note is the person's
account of what happened and it evolves; a source is what was recorded and it
does not. The `sources` key on a note is an identity, not a path, so a rename
or a new revision never breaks it. An agent asked "is this note accurate" reads
the note, fetches its source bundle, verifies the recorded hashes, and compares.
That question is the reason the two are kept apart.

## Asks, in workflow order

The table in #28 is authoritative; this is the summary and the reasoning.

| # | Ask | In roadmap.md | Why here |
|---|---|---|---|
| A0 | Small fixes: #27 (Admin CSRF session binding and Git setting bounds); a wikilink from the opening note to its `_Sources` page (a bare id is not a link a person can follow); `folder` on create; a README first-visit line that Obsidian's "default location for new attachments" is per device and defaults to the root | partly | Each is a paper cut a first user hits in the first hour |
| A1 | Curator and `notes:move` | next | Review stops being a dead end |
| A2 | Indexer, PostgreSQL full-text | next | Body search for agents |
| A3 | Drop folder and deterministic watcher | no | The person-facing intake |
| A4 | Sources list, `GET /v1/notes/{id}/sources`, status counters, Admin problems view | later | Operators see rejections; a consumer gets one number; an agent verifies a note in two calls |
| A5 | Digester hook: per-type interpretation that appends derived artifacts to the revision and rows to the mirror. First: ICS to calendar events with a structured query; PDF and PPTX text extraction | no | Calendar becomes a source type, not a second service and not notes |
| A6 | Enricher: optional image, off by default, proposes frontmatter through a model endpoint named in one setting | no, and it strains the plan | The simple non-deterministic metadata work moves next to the data; rules below |
| A7 | Attachments route | planned | Decks and PDFs reach devices |
| A8 | pgvector in the same PostgreSQL, embeddings via an endpoint setting, default off, model version per row, re-embed job | deferred | Hybrid search over transcripts; full-text keeps working when the model is down |
| A9 | Import that is not the adopt-everything reconciler; the rebuild job | deferred, planned | Bulk migration of an existing corpus; on-demand rebuild |

## Calendar and other machine-readable data

Proposed (A5), not decided: calendar imports become a source type. The
alternative weighed was a separate service for them, and it lost because
Coppermind already has the right domain, sources: immutable, revisioned, rendered to a
read-only page, never adopted as notes, and reachable from a synced folder
under the exception already made for Obsidian. A weekly ICS export dropped into
the drop folder becomes one source. The ICS digester writes event rows to the
mirror, rebuildable from the artifact. A re-dropped export is a new revision
and the query reads the latest, so cancellations and moved slots resolve
without a correction route. One export is one `_Sources` page, not thousands of
files on a phone. Nothing reads calendar rows as notes: they are the lookup
table notes are checked against. The one case that is a note's business: an
invite that is the authoritative identity for a recording can also ride inside
that recording's bundle as an `invite.ics` artifact, as provenance.

The same shape covers other machine data with one consumer. Each is a source
type with a digester if an agent needs to query it, and stays outside
Coppermind otherwise.

## The enricher, and the rules that keep it inside the design

The plan keeps Coppermind deterministic, and the curator stays that way. The
enricher is the one place a model is allowed, and only under these rules:

- It proposes, the curator disposes. It writes frontmatter; filing remains a
  deterministic rule on `reviewed: true`. No model moves a file.
- Frontmatter only, through `PATCH` with `If-Match`. The body is the person's.
  It writes nothing into a note about its own doubts: low confidence or a
  note it believes is wrong goes to the Admin problems dashboard (operator
  decision, 2026-10-01, below).
- It never overwrites a human value. A key set on a device, and anything on a
  note already reviewed, is frozen to it. Re-running fills blanks only.
- It leaves provenance: model, version, time, per-field confidence. Below a
  threshold it writes nothing and flags.
- Everything it writes is derived and regenerable from the bundle and the
  mirror, so the files-are-truth recovery story holds.
- Off by default; one setting names an OpenAI-compatible endpoint and a key
  file. A plain `docker compose up` never needs a model.

## Search: one PostgreSQL, two indexes

Full-text ships with the indexer as planned. Proposed (A8), not decided:
vectors as a follow-on increment to the same component (pgvector column, HNSW
index, a setting, a caller) rather than a separate vector database. A second stateful system would break the one-volume
restore and add a consistency problem for a corpus of a few thousand notes that
does not need it. Most real questions are entity plus topic, which frontmatter
filters and full-text answer exactly; vectors earn their place on long
transcripts and phrasing variance, which is the first thing asked once
transcripts are sources. Model name and version live on each row so a model
change re-embeds through a job rather than mixing vectors in one ranking.

## Agents are consumers

Any agent platform that uses Coppermind does so with scoped keys and never
files, enriches, imports, or holds a copy. If the platform is down the notes
workflow does not notice; if Coppermind is down the platform says so. The
flows that justify a tool on the platform side, in order: a read key plus the
sources query for "search my notes" and "is this note accurate against its
source"; one `POST /v1/notes` for "save this to my review pile"; a paged count
of unreviewed notes in the configured review folder for a daily view until
`GET /v1/status` exists.

## Decisions the operator made during this review

- No shadow period. Coppermind receives no real notes until A1, A2, A3 and A9
  exist; the existing pipeline stays authoritative and the corpus moves in one
  import. Acceptance runs on synthetic material only. This supersedes the
  opening line of #28 as first written, which said shadow-writing could start
  at once; the issue has been corrected to match.
- Calendar and machine data are source types, not notes and not a sibling
  service.
- The simple non-deterministic metadata work is Coppermind's enricher, under
  the rules above, not a function of the consuming agent platform.

Open, and gating the import rather than anything sooner: the mapping of the
existing frontmatter vocabulary onto the shipped keys (about thirty keys onto
six plus additions), the drop folder's name.

Resolved (2026-10-02): vocabulary mapping is not needed. Tags grow organically
through the Fields page.

Resolved (2026-10-09): the sync plan. The operator's account is Sync Plus
(10 GB total, 200 MB per file). The attachment cap reads from a setting with
200 MB as the default, not the 5 MiB Standard limit. The 49-oversized-files
figure no longer applies.

## Gaps noted, not asks

- `_Sources` renders binary artifacts as a listing only, so a dropped PDF is
  stored safely and unreadable on a device until A5 or A7.
- Whether Obsidian Sync carries `.ics` and `.txt` depends on the account's "all
  other types" setting, outside this project. This is a constraint to confirm
  in A3's design.
- Whether a device inherits the attachment-folder setting depends on
  `sync.sync_configs`, shipped empty. A new device drops images at the root
  until it is told otherwise.

## Decided after this review (2026-10-01)

The operator settled these the same evening; [#28](https://github.com/sentania-labs/coppermind/issues/28),
[#30](https://github.com/sentania-labs/coppermind/issues/30) and
[#31](https://github.com/sentania-labs/coppermind/issues/31) carry the words, and
[roadmap.md](roadmap.md) carries the resulting plan.

- Problems are shown on an Admin dashboard and never written into a note. No
  `review_notes`, and the curator does not write `unresolved`: a note the rules
  cannot file stays in the review folder untouched and appears on the dashboard.
- Review is the person's. Setting `reviewed: true` is the confirmation and the
  only trigger to file.
- The enricher proposes a best guess for every field of an unreviewed note,
  from the field and tag guidance (#30), the note and source text, and the
  dossiers.
- Dossiers (people, companies, relationships, interests) live in PostgreSQL
  and are queryable; each gets a projected page in the notes filesystem with
  the person's own notes at the top and a Coppermind-maintained block below a
  clear separator. The indexer covers those pages like any note.
- An Admin page sets guidance and rules for fields and tags (#30), built before
  the curator.

