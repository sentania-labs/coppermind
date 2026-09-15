# Coppermind delivery plan (approved)

The four milestones as approved, each broken into the pull requests planned
to build it, the specific named tests each one is proven by, and the backup
and recovery procedure that the last milestone has to prove end to end. This
is the plan as it was written and approved; what has actually landed against
it is [roadmap.md](roadmap.md), which is the document to read for current
status. This one is the fixed yardstick roadmap.md is measured against.

Firstmate's own dispatch mechanics, who reviewed what and when, are left out
on purpose: that is process, not something this project needs to carry.

## The four milestones

Each is a complete vertical slice through every layer, and each ends in a
tagged release that runs from a plain `docker compose up`.

| Milestone | Tag | What it proves | Acceptance gate |
|---|---|---|---|
| 1: source to phone and back | v0.1.0 | A source and its note enter through the API, land as a real file, reach a device through Obsidian Sync, come back edited, and are retrievable with conflict protection; Git history accumulates on its own | `T-ING-1`, `T-ING-2`, `T-CE-1`, `T-FR-1`, `T-GIT-1`, `T-SRC-1`, and `T-OP-1` green in CI; `T-OP-2` and the phone half of the story seen working by the captain with real Obsidian; the sample ingest request in `examples/ingest/` works exactly as documented |
| 2: reviewed to filed, and found | v0.2.0 | Marking a note reviewed on a device is enough to get it filed by rule and found by search; the system converges on its own after a worker or PostgreSQL disappears | `T-ID-1`, `T-ID-2`, `T-ID-3`, `T-CU-1`, `T-EV-1`, `T-PG-1`, `T-IX-1` green; the whole story green end to end; every setting in `settings.yaml` has a matching Admin control |
| 3: runs in the lab | v0.3.0 | The same images run in Kubernetes under the lab's normal deployment process, on the default storage shape and on the alternate one | Kind smoke green in both storage modes; the chart installs in the lab from its published digests; Obsidian Sync connects from the in-cluster Admin page and a note round-trips to a device; the lab's own inventory and diagram are updated in the same change |
| 4: survives | v0.4.0 (MVP complete) | A complete loss of the deployment is recoverable from the data alone; attachments, deletion, and a schema key rename all behave; the documentation set is complete | `T-BR-1` green; an attachment over the plan's limit is refused; renaming a schema key rewrites every note through the store as one Git snapshot; the lab restore runbook is completed once, end to end, with evidence kept |

## Milestone 1: source to phone and back

| Planned PR | What it was scoped to contain | Seen working as |
|---|---|---|
| 1 | Repository skeleton: the shared package, the store with create and get, the API's health checks and first note routes, Compose with bootstrap and PostgreSQL, and the first CI pipeline | `docker compose up`, create a note through the API, open the file on the volume |
| 2 | Ingest: source bundles, idempotency, revisions, projections, the deterministic no-body note, API keys with a claim-less bootstrap key for CI | The sample request works verbatim; a second identical call answers 200 |
| 3 | The reconciler and `ETag`: the stat scan, hashing on change, moves and renames tracked by identity, deletions, a new device file getting an identity after its quiet period, the outbox table | Edit the file on the volume, see the change in the API; a stale `PUT` gets 409 |
| 4 | The Git helper image: init, watch and poll, debounce, commit, garbage collection, a managed `.gitignore` block, a status file | `git log` inside the volume shows the edit; stop the container, edit, restart, see the catch-up commit |
| 5 | The Obsidian Sync image: supervisor, control endpoint, a fake mode for CI; Admin's claim, login, overview, keys, settings, and the guided Connect Obsidian Sync flow | The captain connects from Admin on a workstation stack; the note appears on his phone; an edit comes back |
| 6 | Release: the tag check, the publish gates, signing, the release assets, promoting `latest`, the deployment contract table | An anonymous pull of the tagged image boots |

## Milestone 2: reviewed to filed, and found

| Planned PR | What it was scoped to contain | Seen working as |
|---|---|---|
| 1 | Move, rename, folders; the `rebuild_metadata` job | Move through the API, identity unchanged, Git shows the rename |
| 2 | The curator image: consumer, inbox sweep, the rules evaluator, ambiguity marking; Admin's filing rules editor with a dry-run preview | Set reviewed on the phone, the note lands in the right customer folder |
| 3 | The indexer image and search: the index table, its consumer, reconciliation, a rebuild job; the search endpoint; Admin's jobs page | Search from the command line; delete the index, rebuild it from Admin |
| 4 | The frontmatter schema editor: keys, vocabularies, roles, with validation on both ingest and a targeted patch | Add a vocabulary value in Admin, ingest a note using it |
| 5 | The failure matrix extended to workers-stopped and PostgreSQL-down scenarios; the store's in-memory path map; the API's key cache; complete overview counters | Stop PostgreSQL, edit through the API by ID, restart it, watch everything converge |

## Milestone 3: runs in the lab

| Planned PR | What it was scoped to contain | Seen working as |
|---|---|---|
| 1 | The Helm chart: both storage modes, bundled or external PostgreSQL, the migration job, services, ingress, monitoring, resource limits, security contexts | `helm template` for both modes passes validation |
| 2 | Kind cluster smoke testing, including restart evidence and permission assertions | The kind job is green |
| 3 | Publishing the chart to the container registry, generating the digest-pinned values file for each release, an example deployment application, and the Kubernetes section of the deployment docs | The chart can be pulled and installed by version |
| 4 (a separate repository's PR, live-infrastructure regime) | Namespace, sealed secrets, the deployment application pinned to the release's values, the internal ingress route, the inventory and diagram update | The Admin page is served through the internal route; Obsidian Sync connects from there; a note round-trips to a device |

## Milestone 4: survives

| Planned PR | What it was scoped to contain | Seen working as |
|---|---|---|
| 1 | The backup-restore job and its script; the backup and recovery documentation | The job is green; the restore section of the runbook is walked once on Compose |
| 2 | Attachments with plan-based size limits; delete-to-trash; a projections rebuild job | An oversized upload on Standard is refused; a deleted note is found in the trash folder |
| 3 | The schema key rename migration job: sync paused, every note rewritten through the store with a minimal diff, one Git snapshot for the whole change | Rename `account` to `customer` in Admin; every file changes exactly once |
| 4 | The complete documentation set: architecture, storage, events, admin, operations, the acceptance matrix with test identifiers, `STATUS.md` | The docs match the running system when a reviewer walks them against the live stack |

## Named tests and what they prove

These identifiers are the concrete evidence each gate above depends on. They
are worth keeping visible because they are what "proven" means in this plan,
not a vague claim of quality.

| Test id | Proves |
|---|---|
| `T-ID-1` | A rename keeps a note's identity |
| `T-ID-2` | A move keeps a note's identity |
| `T-ID-3` | Rebuilding metadata from disk alone recovers the same identifiers |
| `T-ING-1` | Ingesting the same thing twice creates exactly one source and one note |
| `T-ING-2` | Changed content under the same external identity creates a new revision, not a new note |
| `T-OP-1` | An edit made in Obsidian is visible through the API |
| `T-OP-2` | An edit made through the API reaches Obsidian (needs a real subscription; proven in the lab, not CI) |
| `T-CE-1` | A stale `If-Match` is refused with 409 |
| `T-CU-1` | A reviewed note is filed by the curator, and its identity survives |
| `T-GIT-1` | Stopping the Git helper, editing, and restarting produces a catch-up commit |
| `T-EV-1` | Stopping the curator and indexer, editing and reviewing notes, then restarting converges everything |
| `T-PG-1` | With PostgreSQL down, the filesystem stays editable and nothing is lost; things converge once it returns |
| `T-IX-1` | Deleting the search index and rebuilding it restores search |
| `T-SRC-1` | A source cannot be mutated through any route |
| `T-FR-1` | An edit made directly on disk is detected by the scheduled scan, without relying on an event |
| `T-BR-1` | A full backup and restore recovers notes, Git history, sources, keys, settings, and search |

## Backup and recovery

Infrastructure owns the mechanics of backing up; the product's job is to
keep the backup set small and the restore procedure short.

| Item | What it is |
|---|---|
| What is backed up | All of `/data`: the notes filesystem with its Git history, source bundles, attachments, and every control-state file (settings, schema, rules, key hashes, the admin password hash, and the sync client's own token file) |
| Optional | A PostgreSQL dump, which only saves the time a reindex would otherwise take |
| Nothing is excluded | Including the sync token: restoring it reconnects the same device identity. Treat the whole backup set as secret-bearing |
| Consistency | Pause or stop the writers before copying: on Compose, stop the store, Git, and Obsidian Sync containers; in the lab, a storage snapshot is acceptable because every write is an atomic rename and every Git commit is atomic on its own |

Restore procedure, in order:

1. Bring up the same or a newer version of the workloads against empty
   storage.
2. Restore `/data` from the backup.
3. Restore the PostgreSQL dump if one exists; otherwise start with an empty
   database and let migrations create the schema fresh.
4. Start the store and run the `rebuild_metadata` job (from Admin, or the
   equivalent API call): it walks the notes filesystem for identifiers and
   frontmatter, reads every source manifest, mirrors every control file
   (which restores API keys, settings, filing rules, and the admin
   password), and publishes an event for every note it finds.
5. Start the indexer; it rebuilds the search index from those events, or run
   the reindex job directly.
6. Start Git; it finds the existing repository and resumes recording
   history.
7. Start Obsidian Sync; it finds its token file and resumes as the same
   device. If the token is missing, Admin will show "not connected"; either
   reconnect through the guided setup or paste a sealed token back in.
8. Verify: the status counters match what they were before the backup, `git
   log` shows the same depth, a search returns a result, and an edit made on
   a device arrives.

Recovery is measured in minutes plus however long reindexing takes (roughly
1,000 notes per minute on the lab's hardware was the working estimate, to be
confirmed once milestone 4 is actually built). Every step except the sync
reconnect is provable entirely in CI; the sync reconnect is the one step
that needs the lab runbook and the captain's own subscription.

## Test strategy, briefly

Four layers, cheapest first: unit tests with no external dependency; tests
backed by a real PostgreSQL for anything about ordering, crash recovery, or
the reconciler; a conformance suite that runs the same tests against both
implementations of the internal store contract, so they cannot silently
drift apart; and full compose and Kubernetes smoke tests that exercise the
real images, with a small "tools" container standing in for a human editing
files in Obsidian. There is no coverage-percentage requirement anywhere in
this plan, matching the rest of the org's practice; every test that proves a
specific failure behavior is named for the row above it covers.
