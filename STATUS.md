# STATUS

What works against `main` today. Updated 2026-09-09. Every claim here was
checked against a running compose stack on that date, not against CI alone.

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
  handled, NFC normalised, 120 character cap, case-insensitive collision gets
  a numbered suffix). Frontmatter carries `schema_version`, `id`, `date`,
  `type`, `context`, `account`, `reviewed` and `sources`, with defaults
  applied for anything the caller omitted.
- `GET /v1/notes/{id}` returns the note as a document, carrying
  `ETag: "sha256:<hash of the file bytes>"`.
- The store is the only writer of the notes filesystem, reachable only over
  the internal contract on `:8081` with a bearer token. The API holds no
  state and calls it.
- Honest readiness. With PostgreSQL stopped: `/readyz` answers 503 and names
  the failing check, note writes and reads answer 503 `metadata_unavailable`,
  a refused write leaves no file behind, and the notes filesystem is
  untouched and still fully editable. Starting PostgreSQL brings everything
  back with no intervention.
- Control state files are revisioned. A write states the revision it replaces
  and is refused if the file moved on. Readiness loads both of them, so a
  hand edit the models reject takes the store out of rotation with the file
  and the failing field named, rather than reporting ready while every note
  operation fails.
- A note whose frontmatter was broken while editing on a device reads back as
  409 `note_unparseable`, naming the note and saying Coppermind did not modify
  the file.
- CI: lint, types, unit tests, compose validity and the no-em-dash rule;
  PostgreSQL backed integration tests; dependency, secret and repository
  scans; one image build per service as an OCI tarball with provenance and an
  SBOM, scanned; and a compose smoke run of the whole storyline above against
  those exact images. Every action is pinned to a commit SHA, and a test
  enforces that.

## Not built yet

Everything below is planned and has a place in the design. None of it exists
in the tree, so do not read the absence as a decision to leave it out.

- **API authentication.** There are no API keys yet, so `/v1` is
  unauthenticated. Compose binds the API to `127.0.0.1` for that reason. Do
  not put this on a network interface until keys land in the next pull
  request.
- **Ingest.** `POST /v1/ingest`, source bundles, revisions, idempotency and
  the generated source projections.
- **Reconciliation.** Nothing yet notices a file created, edited, moved or
  deleted on a device. A note edited in Obsidian will not be reflected in the
  API, and a note deleted there leaves a row behind. Until the reconciler
  lands, treat the API as the way to create notes.
- **Conflict protection on writes.** There is no `PUT` or `PATCH` yet, so
  conditional writes are not defined at all: reads carry an `ETag`, but no
  surface reads an `If-Match` header.
- **The Git helper, Obsidian Sync, the curator and the indexer.** No history,
  no sync, no filing by rules, no search.
- **Admin.** A separate service and image in the design, not a route group in
  the API. Nothing exists yet, so settings are edited as files under
  `/data/state` for now, which is exactly the state the design says is not
  shippable. It is shippable in the sense that the defaults work; it is not
  yet the finished product.
- **Publication and release.** CI deliberately holds no token that could push
  an image anywhere. Publishing, signing and the Helm chart come later.

## Known gaps in what is here

- Bring-your-own PostgreSQL is wired in the settings (`COPPERMIND_DATABASE_URL`
  and the `COPPERMIND_DB_*` values) but the compose file only ships the bundled
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
  here yet.
- The store reads `settings.yaml` and `schema.yaml` on every call rather than
  caching them. Correct, and cheap at this size; it becomes a cache with an
  invalidation event when `settings.changed` exists.
