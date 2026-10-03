# STATUS

What works against `main` today. Updated 2026-10-01. Every claim here was
checked against a running compose stack on that date, not against CI alone.

This is the first slice of the build. The shape is deliberately narrow: one
vertical path proved end to end, then widened.

## Working

- Admin is a separate service and image on loopback port 8082. A fresh install
  opens on a server-rendered Claim page. Bootstrap creates the one-time code
  at `/data/state/internal/claim-code`, records that location in its log, and
  keeps the same code across restarts. A successful claim accepts the code,
  writes the Argon2 password hash, claim time and a random session signing
  secret to mode 0600 `admin.json`, then removes the code. Later claim
  attempts are refused, and a login submitted on a system that is not claimed
  returns to the Claim page rather than reporting a bad password. Password
  login creates an expiring HMAC-signed cookie with the shipped 12-hour
  default and no database session state. The protected overview links to API
  Keys, Settings, Obsidian Sync and Problems, and shows the store's counters
  (see `GET /v1/status` below), or a plain notice when the store cannot answer.
  Logout clears the cookie from that browser and the protected
  page redirects to Login again; because the session is the signed cookie and
  not a row, an issued token stays valid until its expiry no matter where Log
  out is clicked, and re-claiming is the only thing that ends every session at
  once. Claim, login and logout accept the rendered forms only, and every
  refusal returns to the page that names its own cause, including a control
  state file Admin cannot read, a claim code on the volume it cannot read, and
  a state directory that will not take the record, each named on screen along
  with what was rejected. The session cookie is always Secure, which browsers
  honour on the loopback address Admin is fixed to; there is no setting that
  publishes it anywhere else. Admin mounts only `/data/state`, so the notes
  filesystem is not reachable from it at all. Re-claiming after password
  recovery replaces the signing secret, which immediately refuses every cookie
  issued under the old password. This path was driven through its rendered
  pages in Chrome against a fresh compose stack, and `ci/smoke.sh` now drives
  it unattended across the bootstrap and Admin containers: it reads the claim
  code the documented way, claims, refuses a second claim, signs in, renders
  the overview, signs out and loses it again.
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
  patches need both `notes:read` and `notes:write`; source and artifact reads
  need `sources:read`; ingest needs both `sources:write` and `notes:write`, so
  a key holding one of the two answers 403. Successful verification and key
  hashes are cached for five minutes, so a key created after a load is picked
  up at the next cache expiry rather than at once.
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
- `GET /v1/sources` (`sources:read`) lists mirrored sources newest first,
  filtered by `provider` and by `from` and `to` on the date a source was first
  ingested, paged by an opaque cursor with the same limits as note listing.
  `GET /v1/notes/{id}/sources` (`notes:read` and `sources:read`) lists the
  sources a note cites with the projection path each manifest records, and
  answers 404 for an unknown note. `GET /v1/status` (any key) answers the
  counters: notes awaiting review (directly in the configured
  `notes.review_folder` with `reviewed: false`), notes by state, sources,
  rejected ingests, name collisions and unparseable files. Only the counters
  are live; version, capabilities and helper ages are not yet in it.
- Admin's Problems page lists every refused ingest (over the size limit or
  failing the schema), every note identity two files carry, and every
  unparseable file, each linked to a read-only page for the note or source.
  Collisions are recomputed on every reconciliation pass; refused ingests are
  persisted under `/data/state/rejections/` and mirrored as rows, restored
  by reconciliation after database loss. Nothing is ever written into a note
  for any of them.
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
  observed, `ok`, `unparsed` or `missing`, with `state_reason` naming what the
  scan saw when that state is not `ok`. With PostgreSQL unavailable,
  listing answers 503 `metadata_unavailable`, never an empty page.
- The store scans the notes filesystem on its own schedule, every 60 seconds
  by default. The filesystem walk runs outside the request loop, so requests
  continue while a scan is in progress. A known note edited, moved or renamed
  on a device is mirrored by the identity in its frontmatter. Only a file the
  scan did not find becomes `missing`: one it can see at a known note's path
  but cannot parse, open or identify is `unparsed` there, and two live copies
  of one identity leave the row as it was rather than guessing. An interval
  scan stats every note file and reads only the ones a stat says may have
  changed. A device-created file carrying no identity waits until its mtime
  has been quiet for the configured period, then the store assigns an identity
  and writes that identity into the file even if an edited schema marks its key
  optional, because the filesystem must remain the durable copy. It fills other
  absent frontmatter the schema requires of that note from the shipped defaults.
  Any other key the schema marks optional is left out even when it ships a
  default, because the minimum necessary bytes go into a file a person owns.
  Pointing the
  store at a notes filesystem that already holds notes rewrites every one of
  them once. That happens gradually: a fixed 50 adoptions per pass, counted in
  files actually taken on rather than files tried, so an existing tree arrives
  over successive scans rather than as one burst through Obsidian Sync, and the
  remainder is simply picked up next pass. Adoption checks the observed hash
  again immediately before the atomic replacement, so another device write wins
  without losing bytes. The tested line endings, inline comments, key order,
  list style and body stay in place. One shape is the exception: a required
  property a person added and left blank, which is what Obsidian writes for an
  empty property, cannot be appended without writing the key twice, so those
  files take the ordinary targeted property change and have their properties
  block reassembled. Key order, comments and quoting survive that; the block's
  own line endings do not. Every other adoption only appends. A file the store
  refuses, because its frontmatter does not validate, because it carries a
  malformed identifier, because a value it carries is one the mirror cannot
  store at all, or because its frontmatter delimiter lines end in a bare
  carriage return that the shared parser reads as having no body, is left byte
  exact and counted rejected. The reason names the parser category, the schema
  keys at fault or the kind of fault, and never the person's own values,
  because logs are collected and shipped. A carriage return in the body is the
  person's own byte and never blocks adoption, and neither does a NUL inside a
  value: the file keeps that byte and the mirror, which is the rebuildable
  copy, drops it.
  Everything a file is judged on is decided before a database connection is
  asked for, so a note the schema refuses costs a read
  and a parse however often the scan rediscovers it. A file the store cannot
  write is counted unwritable with the same detail. Both keep their stat like
  any other
  rejected file, so cheap passes stop sweeping them and the daily thorough
  rehash is what tries them again; because neither names an identity the mirror
  knows, neither holds back a deletion report or the rehash itself. A file that
  already carries an identity this store knows is left alone, even when another
  file holds the same identity: nothing observable tells a copy apart from a
  move whose delete has not arrived yet, and the service will not guess. The
  pass counts that collision and names every path claiming the identity, beside
  the files it refused, so an operator sees both sides rather than an
  identifier alone. The process remembers up
  to 10,000 rejected paths by stat, so an unchanged rejected tree costs one
  stat per file per pass rather than repeated reads and parses. Only a durable
  rejection is remembered; a file still inside the quiet period is read again
  on the next pass instead, so a whole notes filesystem arriving at once
  cannot fill that memory with paths that are about to settle. A file changed
  inside the quiet period waits for the next pass. Trusting a stat
  is safe because it is not the only
  pass: once a day, at the configured local time, the store rereads and
  rehashes every note file, which is what catches an edit that left the file's
  size and timestamp where they were. That pass stays due until one completes
  having deferred nothing, so a rehash that could not run, or that had to
  leave a file for later, is retried on the next interval rather than skipped
  for the day. Several scans in a row that cannot complete make `/readyz`
  report not ready rather than serving state nothing is refreshing, as does a
  long silence with no scan landing; the first scan after a start gets a grace
  of its own first, because it reads everything and there is no measured
  runtime yet to judge it by. The interval, the quiet period and the rehash
  time are product settings with working defaults; their graphical controls
  arrive with Admin's later settings page.
- **What adoption will and will not write into.** Adoption skips `.git`,
  `.obsidian` and `.trash`, everything below the configured trash, sources and
  attachments folders (`_Trash`, `_Sources` and `_Attachments` by default), and
  any file whose frontmatter already carries source associations, because
  ingest wrote that and generated output is not a note a person made. Those
  files are still read, so a known note moved into one of those folders is
  followed there rather than reported gone; they are only never given an
  identity. Everything else under the notes root that parses is adopted, and
  that is wider than it sounds: a template, an Excalidraw drawing, a Kanban
  board and anything else a plugin keeps as an ordinary Markdown file are all
  rewritten and mirrored as notes. Narrowing that, with a control for it, is
  follow-up work and is not built. Until it is, do not run a migration against
  the captain's real notes filesystem.
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
- Ingesting a source writes one generated Markdown projection under the
  configured sources folder, by default
  `_Sources/<Provider>/<YYYY-MM-DD Title>.md`. Its frontmatter marks it
  managed and records the source identity, the revision and that revision's
  ingest time in `revision_ingested_at`, so rebuilding a projection from the
  same revision produces the same document rather than a new timestamp. Text
  artifacts are readable in the document; an artifact that is not text, or
  whose bytes do not decode as UTF-8 whatever its declared type, is listed with
  its MIME type, size and SHA-256, in the order the manifest records them. A
  changed source revision regenerates the same projection path from the
  immutable bundle, so a person's edit to a projection is not merged or
  preserved. The manifest's `projection_path` is the only record of where a
  projection lives: it is written in the same manifest write that lands the
  revision, and a source whose manifest does not name one has no projection.
  The generated page is written only after checking that the path holds this
  source's own page or nothing, and that check is the last thing before the
  write: the new bytes are staged and made durable first, so nothing slower
  than the check itself stands in front of it. So if a device delivers one of
  the captain's own notes onto the recorded path while a revision is landing,
  the ingest answers 409 `projection_not_placed`: the revision is stored, its
  readable page is not, and ingesting the same source again places the page at
  a free name and rebuilds the mirror rows the refused attempt rolled back.
  Until that happens the path the manifest records may not name the refused
  page: `projection_path` can name a file the store did not generate, or a
  page other than the one the refusal names. Every reader of it checks the
  file before trusting it, and a re-ingest is what corrects the record. A note
  delivered in the instant between that check and the write can still be
  overwritten. That window is accepted for now: closing it needs a filesystem
  primitive the platform does not currently provide. On a source's first ingest
  there is no revision yet, so a taken projection path answers 409
  `path_collision` and nothing is written: the claim, the bundle and the Review
  note are all removed and the ingest can simply be retried.
  Generation happens on ingest or on a new revision only: nothing backfills, so
  a source ingested before this landed has no projection until it is ingested
  again. An ingest answers with the path its projection occupies, and that is
  how a client learns where the file landed; nothing serves the projection back
  over the API, because being readable without the service is its whole point.
  A projection is never adopted as a note because it sits under the store-owned
  sources folder, and that folder is the whole mechanism: nothing about the
  file itself declines it, so a generated page anywhere else is an ordinary
  Markdown file to the reconciler and is adopted like one. The folder is still
  read, so a note the captain files into it himself is followed there like a
  note in any other folder.
  Obsidian Sync does not exclude the folder. Git excludes it, and the two agree
  on its name by construction: both the Store and the Git helper, which carries
  none of the shared package, use the configured name exactly as given, so
  settings validation refuses any `notes.sources_folder` the Store would have to
  rewrite (a leading dot, a trailing space, a character it strips) and refuses
  an empty one, which would scatter projections through the notes filesystem
  root, rather than let the projections enter Git history. That agreement
  covers the folder in force, not a folder that used to be in force, so do not
  rename `notes.sources_folder` once sources have been ingested: nothing moves
  the pages already generated, they stay under the old name, the Git helper
  stops excluding that name, and the next snapshot commits them. Deleting them
  afterwards takes them out of the working tree but not out of Git history.
  Adoption stops excluding the old name at the same moment, so the next
  reconciliation pass writes an id into every stranded page and mirrors it as a
  note that then appears in listing and search, while a later revision of that
  source still replaces the file where it stands.
- `GET /v1/sources/{id}` reads the filesystem manifest. Its artifact route
  returns an artifact that decodes as UTF-8 text as the response body, always
  as `text/plain` and never as the ingested type, and describes every other
  artifact as JSON with its size and SHA-256, after verifying the stored bytes.
  A source with no manifest, and an artifact the manifest does not record,
  answer 404; bytes the manifest records that the volume cannot deliver, whole
  and matching their digest, answer 503 rather than reporting the bundle gone.
  Both need `sources:read`. `PUT`, `PATCH` and `DELETE` on a source or an
  artifact return 405 `method_not_allowed`, because only reads are routed:
  neither the public nor the internal surface offers a way to change source
  data. With the Store down and the key cache cold the boundary answers 503
  first, since the keys live in the Store; a mutation reaches nothing that
  could change source data either way.
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
  intervention; what changed in the notes filesystem during the outage
  converges on the next scan that completes. Control state is checked the same
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
  the Git helper's failure storyline and the simulated Obsidian Sync
  lifecycle, against those exact images. Every action is pinned to a commit
  SHA, and a test enforces that.
- A version tag adds the publication gates on top of the same run: the tag has
  to be annotated and reachable from `main`, and only then are the tested
  archives pushed to `ghcr.io/sentania-labs/coppermind`, signed with cosign,
  read back and smoke tested with no credentials at all, recorded as a GitHub
  release listing their digests, and named by `latest`, which only ever moves
  forward. Publication authority is held by tag-triggered jobs alone and
  `tests/ci/test_workflow_pins.py` enforces that. Proven live by v0.1.0 on
  2026-09-30: the tag run published all five images to
  `ghcr.io/sentania-labs/coppermind`, and they were created public because the
  repository is public, with no manual package-settings step (see
  [CONTRIBUTING.md](CONTRIBUTING.md)).
- **Obsidian Sync update, 2026-09-30 (unit-tested Admin, runtime proof pending).**
  `/admin/sync` provides guided create-or-join setup, plan and device controls,
  status, pause, resume and disconnect. Python tests drove these forms against
  an authenticated fake HTTP helper and checked that submitted credentials do
  not return in pages or state files. The image pins official
  `obsidian-headless` 0.0.14 on the existing Node 22 base. Its published
  `cli.js` was inspected: login and create have no JSON flag; listing, setup
  and status do. Login and creation use exit codes, then listing resolves the
  unique remote vault name. Setup uses `/data/notes` and the device setting.
  The dependency-free settings reader handles the block scalar emitted by
  Admin, plus JSON settings; complex hand-written YAML device scalars are
  refused rather than interpreted as a different device name.
  The helper supervises continuous sync, resumes a saved unpaused connection
  on boot, and unlinks and logs out on disconnect. Its connection file contains
  only remote vault name and ID, device name and paused state. Submitted email,
  password, MFA and encryption password are not persisted; the client's token,
  derived encryption key and operational state live on its separate credential
  volume under `/var/lib/obsidian-sync`.
  `real_sync_supported` is true. Status exposes remote vault and device names,
  client-reported configuration, and `last_sync_at: null`, because 0.0.14
  supplies no delivery timestamp. `liveness: child_process_only` remains
  deliberate. Client output is not forwarded to logs.
  Stubbed-client Node tests cover command order, failure secrecy, boot and
  disconnect. `ci/sync-smoke.sh` now drives the simulated connection and
  lifecycle through Admin. Neither Node tests nor Compose ran in this worker,
  which has no Node, npm or Docker; those runtime checks belong to branch CI.
  No real account was used, and phone delivery remains an operator proof.

## Not built yet

Everything below is planned and has a place in the design. None of it exists
in the tree, so do not read the absence as a decision to leave it out.

- **The curator, the indexer, and search.** Neither the curator (files a
  reviewed note into the right folder by rule) nor the indexer (keeps the
  search index current) has any code or image in the tree yet
  (`docs/architecture.md`'s "moving parts" table says the same). A note
  marked `reviewed: true` is not filed anywhere by the system; it stays
  wherever it already is. `GET /v1/search` does not exist, so there is no
  full-text search over note bodies; listing still filters on frontmatter
  only.
- **Remaining source capabilities.** Storing a correction to a field that
  describes a source (`captured_at`, `metadata`, `source_type`, `origin` or an
  artifact `mime_type`) when the artifacts are unchanged is not built; today
  those are reported back as unstored. A correction carried in alongside
  changed artifacts does land, because it rides the new revision. Keeping the
  rest means recording them per revision, which costs keys in `manifest.json`,
  a manifest `schema_version` bump and columns on `source_revisions`.
  Tombstoning a source is not built. It has no columns in the mirror
  and no keys in `manifest.json`, so adding them costs a migration of its own
  and a manifest `schema_version` bump.
- **A whole-file note body.** `PUT` takes the JSON document shape only; the
  `text/markdown` whole-file body does not exist yet. A replace also rewrites
  the frontmatter block from what was sent, so the keys land in the schema's
  order with any key the schema does not know after them, and neither a hand
  order, a comment a person left between the keys nor the block's own
  indentation survives it.
  `PATCH /v1/notes/{id}/frontmatter` is the minimal-difference path for a
  one-key change such as marking a note reviewed.
- **Line endings in the frontmatter block.** Both API write paths reassemble the
  block from the YAML dump, which emits line feeds, so a block written with
  carriage returns is rewritten whole and Obsidian Sync pushes every line of
  it. The body keeps its own line endings. The repair belongs in the shared
  compose, which is why it is deferred rather than done inside the patch.
  Separately, `split` ends the block on a line feed, so a file whose lines end
  in a bare carriage return is read as having no body at all: `GET` serves it
  empty and a note takes its filename as its title. Adoption refuses those
  files rather than mirroring one wrongly. The correction belongs in `split`
  and changes every reader of it, `parse`, `patch` and the whole document
  replace, which is why it is its own piece of work.
- **History through the API.** Nothing reads Git history or restores a note
  from it yet; `docker compose exec git git -C /data/notes log` is the way in.
- **Remaining Admin pages.** Schema, filing rules, jobs, source problems and
  real overview counters are not built. API Keys (`/admin/keys`), Settings
  (`/admin/settings`) and Obsidian Sync (`/admin/sync`) now exist:
  signed-in operators can create and revoke keys, edit every product setting
  with revision checks, and guide the sync client through email, password,
  optional MFA and a vault name.
  `python3 -m coppermind_store.keys` remains a recovery path when Admin is
  unavailable. Rendered-page tests and a local uvicorn/curl run exercise the
  pages; compose smoke covers them but requires Docker to execute.
- **Helm packaging and lab deployment.** The Helm chart and the lab handoff
  are not built. Publishing the existing service images and a release from a
  version tag is in place and is under Working above; no automation pushes a
  tag or changes GHCR package visibility.

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
  right means editing it on a device, because Admin has no page for it yet.
  When the broken file still carries one known identity, reconciliation
  associates the failure with that identity rather than with a stale path.
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
