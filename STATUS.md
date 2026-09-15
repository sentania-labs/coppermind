# STATUS

What works against `main` today. Updated 2026-09-15. Every claim here was
checked against a running compose stack on that date, not against CI alone.

This is the first slice of the build. The shape is deliberately narrow: one
vertical path proved end to end, then widened.

## Working

- `docker compose up -d` on a clean checkout reaches a healthy stack with no
  manual setup and no hand populated setting. The one-shot `bootstrap`
  container creates `/data`, generates the internal bearer token and the
  PostgreSQL password as files on volumes (never environment values), creates
  a working full-scope default API key in a separate restricted volume, and
  writes `settings.yaml`, `schema.yaml` and the key's Argon2 hash at revision
  1. Running it again keeps every existing secret, key and setting. Revoke
  that default and it stays revoked. The revocation takes effect for
  authentication as soon as the API's cache next loads; the reveal file is
  replaced by a sentence saying so at the next `docker compose up`, so until
  that restart it still holds the dead credential. Bootstrap mints the default
  once, on the install with no record of one, and decides that from the
  record's own mark in `keys.json` rather than its name. Lose the credential
  volume while the default is live and the next start reports it
  unrecoverable and leaves the record alone, rather than minting a second
  full-scope key while the first stays usable.
- Every `/v1` route requires `Bearer cm_<key_id>_<secret>`. A missing or bad
  key answers 401 and a key without the route's scope answers 403. Note reads
  need `notes:read`; creates and replacements need `notes:write`; ingest needs
  both `sources:write` and `notes:write`, so a key holding one of the two
  answers 403. Successful verification and key hashes are cached for five
  minutes, so a key created after a load is picked up at the next cache expiry
  rather than at once.
  Health, readiness and OpenAPI remain open, and Compose remains bound to
  loopback by default.
  The content-typed journal scopes, `journal:read` and `journal:write`, are
  defined in the scope vocabulary but nothing enforces them in this
  increment: only route-level scopes are enforced, so a `notes:write` key can
  write a note whose type is `journal`.
- `POST /v1/notes` creates a note. It lands as a Markdown file in the notes
  filesystem, under the review folder, named by the portable naming rules
  (date prefix for dated types, Windows-reserved characters and device names
  handled, NFC normalised, capped at both 120 characters and 252 bytes of
  UTF-8 so a multi-byte title still fits the filename limit, case-insensitive
  collision with another note gets a numbered suffix). Frontmatter carries the shipped
  schema's keys: `schema_version`, `id`, `date`, `type`, `context`,
  `reviewed`, `sources` and `tags`, with defaults applied for anything the
  caller omitted, plus `account`, which the schema requires on a customer
  note.
- `GET /v1/notes/{id}` returns the note as a document, carrying
  `ETag: "sha256:<hash of the file bytes>"`.
- `POST /v1/ingest` takes a source and the note to open for it, and creates
  both or neither. A deterministic `.external-id-<sha256>.json` file claims
  each `provider` plus `external_source_id` before the bundle is written. The
  artifacts land under `/data/sources/<source_id>/r0001/`, then
  `manifest.json` last, so a bundle without a manifest is an unfinished one;
  the Review note is written in the same database transaction with the source
  identifier in its `sources` frontmatter key. A revision is identified by its
  artifact bytes alone, so resending an identical payload answers 200 with
  `created: false`, the existing source and note identifiers, and the current
  revision. Changed artifact content appends an immutable numbered revision
  and answers 200 without rewriting the Review note or changing its reviewed
  state. The response carries real `created` values for both records and the
  source revision. A payload whose artifacts are unchanged while
  `captured_at`, `metadata`, `source_type` or `origin` differs is refused with
  409 `descriptive_correction_unsupported` naming the differing fields:
  correcting those fields alone is not built yet, and the store refuses rather
  than accepting a correction it would discard. Nothing is written and the
  stored source is untouched.
  `uq_sources_provider_external_id` remains the database mirror's second
  guard, and it now answers in its own voice: an ingest that finds no claim
  file while the mirror still holds that `provider` plus `external_source_id`
  is refused with 409 `source_claim_missing`, not a 503 blaming PostgreSQL.
  Nothing is written, including the claim the refused attempt made. That state
  comes from restoring `/data/sources` from a snapshot without restoring the
  database, or from removing a claim file by hand; the operator action is to
  restore both from the same point in time, or delete the stale `sources` row,
  then retry. Nothing heals it automatically, because the filesystem is the
  truth and it no longer claims the identifier. A failure before the note is
  complete removes the bundle and then the claim, so a later legitimate retry
  can proceed. PostgreSQL failing at commit after
  the filesystem writes answers 503 `metadata_unavailable` and rolls the rows
  back, but retains the complete bundle, Review note and external-id claim.
  A retry resolves from the claim and completed files, repairs a missing
  mirror when needed, and cannot create a duplicate. A submitted body over
  `limits.ingest_max_bytes`
  (25 MiB by default, settable like every other setting) answers 413
  `payload_too_large` before filesystem or database writes. The API preserves
  the public body length across the Store contract, but checks it only after
  the whole body has been read and parsed: it refuses the request, it does not
  spare process memory.
- `PUT /v1/notes/{id}` replaces a note's frontmatter and body on the condition
  that `If-Match` names the ETag the file has now. The body is the document
  shape a read returns, so a client reads, edits and sends it back; the
  identifier and the path are kept, and the frontmatter is validated against
  the schema. Both `frontmatter` and `body` are required, so a request
  missing either answers 422 `validation_error` rather than erasing it. A
  key sent back unchanged keeps the YAML type it has in the file, so a date
  stays a date. Without `If-Match` the answer is 428 `precondition_required`.
  With an ETag the file no longer hashes to, because another client wrote it
  or a person edited it on a device, the answer is 409 `version_conflict`
  carrying `current_version`, and the file is untouched. The compare and the
  write happen under a per-note lock in the store, so two writers holding
  the same ETag cannot both win.
- The store is the only writer of the notes filesystem, reachable only over
  the internal contract on `:8081` with a bearer token. The API holds no
  state and calls it.
- Honest readiness. With PostgreSQL stopped: `/readyz` answers 503 and names
  the failing check, note creates, replaces and reads answer 503
  `metadata_unavailable`, a refused write leaves no file behind and touches no
  existing one, and the notes filesystem is untouched and still fully
  editable. Starting PostgreSQL brings API operations back with no
  intervention; what changed in the notes filesystem during the outage waits
  for the reconciler under "Not built yet". Control state is checked the same
  way: a settings, schema or key file the models reject answers 503 and names
  the file, while key state that loads and happens to hold no usable key is an
  operator's choice and stays ready. Readiness names the source bundle
  filesystem as its own check beside the notes filesystem, so a `/data/sources`
  the store cannot create or write in holds it unready rather than failing at
  the first ingest.
- Control state files are revisioned. A write states the revision it replaces
  and is refused if the file moved on. The readiness check above is what
  catches a hand edit the models reject, naming the file and the failing
  field rather than reporting ready while every note operation fails.
- A note whose frontmatter was broken while editing on a device reads back as
  409 `note_unparseable`, naming the note and saying Coppermind did not modify
  the file.
- Git history of the notes filesystem, from the separate `git` helper image
  (added 2026-09-11). On first start it makes `/data/notes` a repository, or
  adopts one already there and keeps its history, branch and `.gitignore`.
  The only configuration of its own it writes is a marked block at the end of
  `.git/info/exclude`; rules already in that file are kept byte for byte.
  After that it records changes by itself: it scans every
  `git.poll_interval_s` (300 seconds) and commits a change once it has been
  quiet for `git.debounce_s` (60 seconds), as `Coppermind
  <coppermind@localhost>`, with a `Coppermind snapshot` subject and the
  changed files listed. A start scans at once, so edits made while it was
  stopped land about a minute later on top of the existing history.
  `.obsidian/`, `.trash/`, the trash and sources folders and the store's
  temporary files never enter history, even against a `!` rule in a person's
  own `.gitignore`; control state and credentials live under `/data/state`,
  outside the repository. It has no network, no credential and no remote, and
  keeps working with the store and PostgreSQL down. It reports through
  `/data/state/git/status.json`, whose age is its container health check.
  `ci/failure.sh` proves the catch-up, the history and the exclusions against
  the tested images.
- CI: lint, types, unit tests, compose validity and the no-em-dash rule;
  PostgreSQL backed integration tests; dependency, secret and repository
  scans; one image build per service as an OCI tarball with provenance and an
  SBOM, scanned; and a compose smoke run of the whole storyline above, plus
  the Git helper's failure storyline, against those exact images. Every action
  is pinned to a commit SHA, and a test enforces that.

## Not built yet

Everything below is planned and has a place in the design. None of it exists
in the tree, so do not read the absence as a decision to leave it out.

- **Remaining source capabilities.** Generated source projections into the
  notes filesystem and tombstoning a source are not built. Tombstones in particular have no
  columns in the mirror and no keys in `manifest.json`, so adding them costs
  a migration of its own and a manifest `schema_version` bump.
- **Reconciliation.** Nothing yet notices a file created, moved or deleted on
  a device. An edit in place is the exception and does read back: a note read
  by its identifier is parsed from the file every time, so a body or
  frontmatter change made in Obsidian is reflected on the next read. What
  needs the reconciler is anything that invalidates or lacks the mirrored
  path. A note created on a device has no row and cannot be read by
  identifier at all; a note moved, renamed or deleted there leaves a row
  pointing nowhere, and the read answers 404 `not_found`. So does a read whose
  row points at a file that now carries a different identifier, rather than
  serving another note's content. A replace of such a note answers the same
  404, whatever ETag it carries. Until the reconciler lands, treat the API as
  the way to create notes.
- **Frontmatter patching.** `PUT` takes the JSON document shape only; the
  `text/markdown` whole-file body and `PATCH /v1/notes/{id}/frontmatter` do
  not exist yet. A replace rewrites the frontmatter block from what was sent,
  so the keys land in the schema's order with any key the schema does not
  know after them, and neither a hand order nor a comment a person left
  between the keys survives it; the patch is the minimal-diff path for a
  one-key change such as marking a note reviewed.
- **Obsidian Sync, the curator and the indexer.** No sync, no filing by
  rules, no search.
- **History through the API.** Nothing reads Git history or restores a note
  from it yet; `docker compose exec git git -C /data/notes log` is the way in.
- **Admin.** A separate service and image in the design, not a route group in
  the API. Nothing exists yet, so settings are edited as files under
  `/data/state` for now, which is exactly the state the design says is not
  shippable. It is shippable in the sense that the defaults work; it is not
  yet the finished product. Its graphical API keys page also arrives later;
  until then `python3 -m coppermind_store.keys` is the interim path for adding
  and rotating keys.
- **Publication and release.** CI deliberately holds no token that could push
  an image anywhere. Publishing, signing and the Helm chart come later.

## Known gaps in what is here

- Bring-your-own PostgreSQL is wired in the settings (`COPPERMIND_DATABASE_URL`
  without a password, `COPPERMIND_DB_PASSWORD_FILE`, and the other
  `COPPERMIND_DB_*` values) but the compose file only ships the bundled
  instance. The plan puts a profile around it; a profile that has to be turned
  on in a `.env` file would break the "no manual setup" rule, so the bundled
  instance is simply the default and the external path arrives with the
  deployment work.
- Correcting only a field that describes a source (`captured_at`,
  `metadata`, `source_type` or `origin`) has no path yet: the ingest is
  refused with 409 `descriptive_correction_unsupported` rather than kept. An
  automation that stamps a fresh capture time on every retry therefore gets
  that refusal instead of the replay it expects, so a retry of an uncertain
  answer must resend the payload it originally sent, timestamps included.
- A note file removed outside the store leaves its row behind, because
  nothing reconciles the mirror yet. Creating a note with that title again
  answers 409 `path_collision` every time until the reconciler lands or the
  row is cleared by hand.
- Nothing repairs a note whose frontmatter a person broke. Reads of it answer
  409 `note_unparseable` and the file is left exactly as it is; putting it
  right means editing it on a device, because the reconciler and Admin are not
  here yet. The identifier that answer names is the one asked for, and a stale
  mirror row can point at a different note's file, so with an unreconciled
  rename the wrong note is named. The file's own bytes never reach the answer;
  issue #2 tracks the rest.
- The Git helper polls; there is no filesystem event watcher. With the
  shipped settings a change is recorded within about six minutes, and a note
  edited for a long stretch without a 60 second pause lands as one snapshot
  when the editing stops.
- An existing repository that already tracked `.obsidian/`, `.trash/` or the
  trash or sources folder stops tracking them in the helper's first snapshot.
  The files stay on disk and in the earlier commits.
- The store reads `settings.yaml` and `schema.yaml` on every call rather than
  caching them. Correct, and cheap at this size; it becomes a cache with an
  invalidation event when `settings.changed` exists.
