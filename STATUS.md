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
  need `notes:read`; creates and replacements need `notes:write`; frontmatter
  patches need both `notes:read` and `notes:write`; ingest needs both
  `sources:write` and `notes:write`, so a key
  holding one of the two answers 403. Successful verification and key hashes
  are cached for five minutes, so a key created after a load is picked up at
  the next cache expiry rather than at once.
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
- `GET /v1/notes` requires `notes:read` and lists summaries for notes whose
  identifiers and paths are known to the metadata mirror. Filters cover folder,
  reviewed state, type, context, account, inclusive date bounds, tag and file
  state. `folder` is the exact path prefix, so `folder=Review` selects
  `Review/...` and a leading or trailing slash is not normalised away. A text
  filter (`folder`, `type`, `context`, `account` or `tag`) sent empty answers
  422 `validation_error`, because an unset form field arriving as `folder=`
  must not come back as an ordinary empty page. The opaque cursor pages in
  immutable identifier order, with a default limit of 50 and an allowed range
  of 1 through 200, so a rename between pages cannot move the cursor boundary.
  Summaries come from the latest reconciliation scan and carry the state it
  observed, `ok`, `unparsed` or `missing`. With PostgreSQL unavailable,
  listing answers 503 `metadata_unavailable`, never an empty page.
- The store scans the notes filesystem on its own schedule, every 60 seconds
  by default. The filesystem walk runs outside the request loop, so requests
  continue while a scan is in progress. A known note edited, moved or renamed
  on a device is mirrored by the identity in its frontmatter. Only a file the
  scan did not find becomes `missing`: one it can see but cannot parse or open
  is `unparsed` at its own path, and two live copies of one identity leave the
  row as it was rather than guessing. An interval scan stats every note file
  and reads only the ones a stat says may have changed, and a file changed
  inside the quiet period waits for the next pass. Trusting a stat is safe
  because it is not the only pass: once a day, at the configured local time,
  the store rereads and rehashes every note file, which is what catches an
  edit that left the file's size and timestamp where they were. That pass is
  due until it completes, so one that could not run is retried on the next
  interval rather than skipped for the day. Several scans in a row that
  cannot complete make
  `/readyz` report not ready rather than serving state nothing is refreshing.
  Files whose identity is not already known are left byte for byte alone. The
  interval, the quiet period and the rehash time are product settings with
  working defaults; their graphical controls arrive with the separate Admin
  service.
- `POST /v1/ingest` takes a source and the note to open for it, and creates
  both or neither. A deterministic `.external-id-<sha256>.json` file claims
  each `provider` plus `external_source_id` before the bundle is written. The
  artifacts land under `/data/sources/<source_id>/r0001/`, then
  `manifest.json` last, so a bundle without a manifest is an unfinished one;
  the Review note is written in the same database transaction with the source
  identifier in its `sources` frontmatter key. Creating the pair answers 201;
  every answer that creates no note answers 200. A revision is identified by
  its artifact bytes alone, so resending an identical payload answers 200 with
  `created: false`, the existing source and note identifiers, and the current
  revision. Changed artifact content appends an immutable numbered revision
  and answers 200 without rewriting the Review note or changing its reviewed
  state. Before confirming a replay, the store reads every artifact in the
  current revision and verifies its recorded SHA-256 digest. A missing or
  damaged artifact answers 503 `sources_filesystem_unavailable` rather than
  reporting that the source is intact, and the database mirror is not rebuilt
  from the unverified manifest. The response carries real `created` values for
  both records and the source revision. A payload whose artifacts are
  unchanged while a field describing them differs (`captured_at`, `metadata`,
  `source_type`, `origin` or an artifact's `mime_type`) is still a replay:
  200, `created: false`, the same source and note identifiers, one note.
  Storing a correction to those fields is not built in this increment, so the
  answer names every one of them in `source.unstored_fields` rather than
  discarding it in silence. A caller that reads an empty `unstored_fields`
  knows the stored source matches what it sent. Keeping those corrections
  arrives with the remaining source capabilities under "Not built yet".
  `unstored_fields` describes the source and nothing else. An ingest that does
  not create the note ignores the request's whole `note` object, title, body
  and frontmatter alike, because the note belongs to the captain once it
  exists. A caller resending a changed note body with an existing external id
  gets 200, `note.created: false` and an empty `unstored_fields`, and its note
  payload was not used: the way to edit a note is `PUT /v1/notes/{id}`, or
  `PATCH /v1/notes/{id}/frontmatter` for named fields.
  An interrupted revision write can leave a numbered revision directory that
  `manifest.json` does not record. The next ingest of changed artifacts for
  that source answers 409 `incomplete_revision` naming the directory, rather
  than a 503 blaming a healthy volume. It removes nothing: the operator
  inspects `/data/sources/<source_id>/<rNNNN>/`, removes it, then retries.
  If replacing `manifest.json` begins but its durability acknowledgement
  fails, the revision directory is retained because the replacement may
  already be live. A retry verifies the revision's artifacts before it can
  report a replay.
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
  can proceed. PostgreSQL failing at commit after the filesystem writes
  answers 503 `metadata_unavailable` and rolls the rows back, but retains the
  complete bundle, Review note and external-id claim.
  A retry resolves from the claim and completed files, repairs a missing
  mirror when needed, and cannot create a duplicate. That repair runs on the
  replay path, so a retry carrying a corrected capture time still rebuilds the
  rows and then reports the correction as unstored. Rebuilding the note link
  means reading every note, so it runs in a worker thread: a retry recovering
  from an outage does not stop the store answering readiness and other
  requests while it walks. A submitted body over `limits.ingest_max_bytes`
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
- `PATCH /v1/notes/{id}/frontmatter` changes named frontmatter fields without
  replacing the note. The body is `set` (keys to write values for) and `unset`
  (keys to remove); a null value in `set` is refused, naming the field and
  directing the caller to `unset`, so there is one way to remove a key rather
  than two. The note's own body is read from the file and is never accepted
  from the caller, so an edit made in Obsidian while a phone marks the note
  reviewed survives. Untouched keys keep their position, their comments and
  their YAML types, and a date lands as a date the same way a create and a
  replace write one. The block is written back in the note's own list style,
  so a list a person wrote flush with its key stays flush and a block with no
  list keeps its mapping nesting. A targeted change preserves the note's
  content and its ordinary formatting, while some unusual formatting is
  normalised and syncs with it; what survives and what does not is recorded
  shape by shape in
  `coppermind/tests/test_frontmatter.py::test_a_patch_preserves_the_note_and_its_ordinary_formatting`.
  A patch whose result is byte identical to the file writes nothing and moves
  no mtime, so marking an already reviewed note reviewed is free. The
  identifier cannot be set or removed, a key the schema requires cannot be
  removed, the source-association field is owned by ingest and cannot be
  patched, and a key named in both `set` and `unset` is refused: each answers
  422 `validation_error` and leaves the file alone, as does frontmatter the
  schema rejects. A targeted change is validated against the whole resulting
  properties block, not only the keys it names, so a note an unrelated edit on
  a device made invalid cannot be marked reviewed until that edit is
  corrected. Without `If-Match` the answer is 428
  `precondition_required`; with an ETag the file no longer hashes to, 409
  `version_conflict` carrying `current_version`. The compare and the write
  happen under the same per-note lock a replace uses.
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

- **Remaining source capabilities.** Storing a correction to a field that
  describes a source (`captured_at`, `metadata`, `source_type`, `origin` or an
  artifact `mime_type`) when the artifacts are unchanged is not built; today
  those are reported back as unstored. A correction carried in alongside
  changed artifacts does land, because it rides the new revision. Keeping the
  rest means recording them per revision, which costs keys in `manifest.json`,
  a manifest `schema_version` bump and columns on `source_revisions`.
  Generated source projections into the notes filesystem and tombstoning a
  source are not built. Tombstones in particular have no columns in the mirror
  and no keys in `manifest.json`, so adding them costs a migration of its own
  and a manifest `schema_version` bump.
- **Write-side reconciliation.** A note created on a device has no row and no
  identifier, so the read-side scanner deliberately leaves it alone. Assigning
  its identity and filling required frontmatter is the next reconciliation
  increment. Until then, treat the API as the way to create notes.
- **A whole-file note body.** `PUT` takes the JSON document shape only; the
  `text/markdown` whole-file body does not exist yet. A replace also rewrites
  the frontmatter block from what was sent, so the keys land in the schema's
  order with any key the schema does not know after them, and neither a hand
  order, a comment a person left between the keys nor the block's own
  indentation survives it.
  `PATCH /v1/notes/{id}/frontmatter` is the minimal-difference path for a
  one-key change such as marking a note reviewed.
- **Line endings in the frontmatter block.** Both write paths reassemble the
  block from the YAML dump, which emits line feeds, so a block written with
  carriage returns is rewritten whole and Obsidian Sync pushes every line of
  it. The body keeps its own line endings. The repair belongs in the shared
  compose, which is why it is deferred rather than done inside the patch.
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
  `metadata`, `source_type`, `origin` or an artifact `mime_type`) is reported
  and not stored. The ingest succeeds as a replay and names the fields in
  `source.unstored_fields`, but the manifest and the mirror keep the values
  they already had, so an automation that means to correct one of them must
  wait for the storage chunk under "Remaining source capabilities". An
  automation that stamps a fresh capture time on every retry sees
  `unstored_fields: ["captured_at"]` on every retry and nothing else changes.
- Nothing repairs a note whose frontmatter a person broke. Reads of it answer
  409 `note_unparseable` and the file is left exactly as it is; putting it
  right means editing it on a device, because Admin is not here yet. When the
  broken file still carries one known identity, reconciliation associates the
  failure with that identity rather than with a stale path.
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
