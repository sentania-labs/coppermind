# STATUS

What works against `main` today. Updated 2026-09-11. Every claim here was
checked against a running compose stack, not against CI alone.

This is the first slice of the build. The shape is deliberately narrow: one
vertical path proved end to end, then widened.

## Working

- `docker compose up -d` on a clean checkout reaches a healthy stack with no
  manual setup and no hand populated setting. The one-shot `bootstrap`
  container creates `/data`, generates the internal bearer token and the
  PostgreSQL password as files on a volume (never environment values), and
  writes `settings.yaml` and `schema.yaml` at revision 1 with shipped
  defaults. Running it again keeps every existing secret and setting.
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
- The store is the only writer of the notes filesystem, reachable only over
  the internal contract on `:8081` with a bearer token. The API holds no
  state and calls it.
- Honest readiness. With PostgreSQL stopped: `/readyz` answers 503 and names
  the failing check, note writes and reads answer 503 `metadata_unavailable`,
  a refused write leaves no file behind, and the notes filesystem is
  untouched and still fully editable. Starting PostgreSQL brings API
  operations back with no intervention; what changed in the notes filesystem
  during the outage waits for the reconciler under "Not built yet".
- Control state files are revisioned. A write states the revision it replaces
  and is refused if the file moved on. Readiness loads both of them, so a
  hand edit the models reject takes the store out of rotation with the file
  and the failing field named, rather than reporting ready while every note
  operation fails.
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

- **API authentication.** There are no API keys yet, so `/v1` is
  unauthenticated. Compose binds the API to `127.0.0.1` for that reason. Do
  not put this on a network interface until keys land in the next pull
  request.
- **Ingest.** `POST /v1/ingest`, source bundles, revisions, idempotency and
  the generated source projections.
- **Reconciliation.** Nothing yet notices a file created, moved or deleted on
  a device. An edit in place is the exception and does read back: a note read
  by its identifier is parsed from the file every time, so a body or
  frontmatter change made in Obsidian is reflected on the next read. What
  needs the reconciler is anything that invalidates or lacks the mirrored
  path. A note created on a device has no row and cannot be read by
  identifier at all; a note moved, renamed or deleted there leaves a row
  pointing nowhere, and the read answers 404 `not_found`. So does a read whose
  row points at a file that now carries a different identifier, rather than
  serving another note's content. Until the reconciler lands, treat the API as
  the way to create notes.
- **Conflict protection on writes.** There is no `PUT` or `PATCH` yet, so
  conditional writes are not defined at all: reads carry an `ETag`, but no
  surface reads an `If-Match` header.
- **Obsidian Sync, the curator and the indexer.** No sync, no filing by
  rules, no search.
- **History through the API.** Nothing reads Git history or restores a note
  from it yet; `docker compose exec git git -C /data/notes log` is the way in.
- **Admin.** A separate service and image in the design, not a route group in
  the API. Nothing exists yet, so settings are edited as files under
  `/data/state` for now, which is exactly the state the design says is not
  shippable. It is shippable in the sense that the defaults work; it is not
  yet the finished product.
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
