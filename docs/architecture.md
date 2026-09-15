# Coppermind architecture and plan

This document exists because the approved design for Coppermind, and the
reasoning behind it, lived only in firstmate's private planning records. If
this repository ever moved on its own, the plan would not move with it. This
is that plan, carried into the repo and written for the person who runs the
system rather than the people who wrote the code.

Source material: the architecture scout (2026-09-09), the captain's interview
answers and approval of the same day, and the implementation plan built on
top of them. Where the running system has since taken a different path than
what was approved, that is called out in place, not silently smoothed over.
For what specifically works today, see [STATUS.md](../STATUS.md). For what is
merged, next, and deliberately deferred, see [roadmap.md](roadmap.md). This
file does not repeat either; it is the durable design underneath both.

This is the overview. The detail a person would need to build or operate
Coppermind without the original plan is split out by subject, the plan's own
section seams: every route and scope in [api.md](api.md), the frontmatter
schema and every setting with its default in
[data-and-settings.md](data-and-settings.md), and the slice-by-slice delivery
breakdown, the named tests each slice is proven by, and the backup and
recovery procedure in [delivery-plan.md](delivery-plan.md).

## What Coppermind is

A filesystem-first personal knowledge system. Notes are plain Markdown files
with YAML frontmatter, sitting in an ordinary directory tree, the "notes
filesystem" (never "the vault": that word names Obsidian's own remote object
and nothing here). Obsidian Sync carries that tree to a phone and a laptop,
Git keeps a history of it, and a handful of small services put things into
it, file them once reviewed, and make them searchable. If every service
disappeared, the notes would still be readable files on disk.

## Decisions the captain made

These were interview answers and plan approvals in his own words, and they
override anything else in this document if the two ever disagree.

| Question | What was decided | His words |
|---|---|---|
| Is Coppermind public? | Yes, with full CI on every change | "It will be public." / "coppermind: public, full CI" |
| Does the store stay its own service, separate from the API? | Yes, kept as a separate service and image | "seperate the store" |
| Does Admin stay its own service and image? | Yes, not folded into the API | "Admin has a different lifecycle, exposure/auth posture, and UI responsibility, and keeping that boundary now fits the broader decomposition goal." |
| What happens to API writes when PostgreSQL is down? | A clean 503, not a special database-less write mode | "Writes, ingest, listing/search, and Admin mutations can return a clean 503 until PostgreSQL recovers... don't turn database-less API operation into a feature." |
| Redis or a simpler event mechanism? | No Redis for the MVP; a PostgreSQL outbox table with an interface that could add Redis Streams later if an outside consumer ever needs it | (accepted the scout's recommendation) |
| Kubernetes storage shape? | One pod on a single ReadWriteOnce volume by default, ReadWriteMany as an explicit toggle | "I'm OK with RWO, as long as RWX is a toggle" |
| Who does the transport work (Plaud, email, etc. into the system)? | Nobody, here. Coppermind starts at the ingest API; anything upstream of that (n8n, Plaud) is a different system entirely | "you are building a data service/sync/storage engine. You don't do the transport from plaud/email/etc into the vault." |
| Wording | Never call it "the vault." It is the notes filesystem | "it's not a vault, we are decomposing vault." |
| Build priority | Prove the whole path first, then widen it | "keep the focus on proving the core source -> note -> Obsidian -> review -> filing -> search/history path before expanding into deferred capabilities." |

The full record of the interview and these decisions is
[docs/decisions/001-interview-outcome.md](decisions/001-interview-outcome.md).

## The moving parts

Six things run today or are planned to run, each with one job and a clear
blast radius if it goes down.

| Service | Job | If it stops |
|---|---|---|
| `store` | The only thing that ever writes a note file. Owns the notes filesystem, the source bundles, and the control-state files under `/data/state`. Watches the filesystem on a schedule and mirrors what it finds into PostgreSQL. | Notes stop being creatable or editable through the API; files already on disk are untouched. |
| `api` | The public HTTP contract. Holds no state of its own beyond a five-minute key cache. Talks to the store for everything that touches a file. | API calls fail; nothing on disk is affected; a restart loses nothing. |
| `admin` | The operator interface, on its own port, its own image, its own login. Never touches the notes filesystem directly, only `/data/state`. | The web control panel is unreachable; the rest of the system keeps running exactly as it was. |
| `git` | Watches the notes filesystem and commits a snapshot of it on a debounce. No network, no credential, nothing it can push anywhere. | History stops accumulating; nothing already committed is at risk; catches up automatically once restarted. |
| `obsidian-sync` | Supervises the client that will carry the notes filesystem to a phone and a laptop through Obsidian's own Sync service. | No devices see new notes; nothing on the server side is affected. |
| PostgreSQL | Holds only mirrors and derived data: note metadata, search index, job history, key hashes. Every row here can be rebuilt from `/data` by one job. | Every note operation through the API stops, including a by-ID read, because resolving a note's path goes through this mirror; listing and search stop too. A file edited directly on the notes filesystem, bypassing the API, is untouched and stays editable. Nothing is lost; everything catches up once PostgreSQL returns. |

Two more are designed but not built yet, and have no image or code in the
tree: `curator` (files a reviewed note into the right folder by rule) and
`indexer` (keeps the search index current). Their absence is expected at this
point in the build; see [roadmap.md](roadmap.md) for when they land.

## How data moves

A source (a recording, an email, whatever comes in later) is posted to the
ingest endpoint. That call writes the source bundle to disk, the note to
review, and the database mirror, and on success all three exist together.
The filesystem is written first and is the one that has to survive: if
PostgreSQL fails at the commit step after the files are already down, the
completed bundle, the Review note, and the claim that identifies the source
are kept rather than rolled back, and the request answers 503. A retry
resolves from those files, repairs the missing database row, and never
creates a duplicate. From there:

```
someone's tool  --ingest-->  api  --internal call-->  store  --writes-->  notes filesystem, source bundle
                                                        |--> PostgreSQL mirror + outbox
a phone / laptop  <--Obsidian Sync-->  the same notes files  <--commits-- git
                                                        ^
                                        store re-scans the notes filesystem every 60 seconds
                                        and picks up anything changed on a device
```

Once curator and indexer exist, a note marked reviewed on a device gets
picked up the same way and filed into the right folder, then indexed for
search. Nothing in this design depends on that pickup happening instantly:
every consumer also re-checks its own work on a schedule, so a missed
notification is a delay, never data loss.

## Where things live on disk

```
/data/notes/            the notes filesystem itself, and what Obsidian Sync carries to devices
  Review/                inbox for anything not yet reviewed
  Work/, Personal/, Reference/, Journal/     filed locations, by rule once curator exists
  _Sources/               generated, read-only pages describing what came in from where
  _Trash/                 where a delete goes, not a separate archive state
  .git/                   the Git helper's own history
/data/sources/<id>/       the original bundles ingest received, never rewritten
/data/state/              settings, the frontmatter schema, filing rules, key hashes, admin's password hash
                          every one of these is a plain versioned file, so PostgreSQL
                          can be dropped and rebuilt from this and the notes filesystem alone
```

The rule underneath all of this: the files are the truth. PostgreSQL is a
mirror that a single job can always regenerate. If you ever have to choose
which one to trust after something has gone wrong, trust the files.

## Deployment shape

Today, Docker Compose: one container per service plus a bundled PostgreSQL,
brought up with `docker compose up -d` and nothing to hand-populate first.
That is also what CI tests against.

Planned for the lab: a Helm chart with two storage shapes. The default puts
the file-writing services (store, git, obsidian-sync) in one pod on a single
ReadWriteOnce volume, because that was measured over 16 times faster for many
small file writes than spreading them across pods on shared storage, and
because the filesystem-watching pieces need real inotify events that do not
cross network storage. A `topology.mode: rwx` value exists for the case where
that tradeoff is worth taking, splitting those services into separate pods
and switching them to polling instead of watching. Neither shape is built
yet; see the roadmap for when Kubernetes work starts.

## Release process

A merge to `main` lands the work. A pushed, annotated version tag
(`vX.Y.Z`) is what actually ships it: that tag triggers the build, the
publish, the signing, and a GitHub release, every image stamped with the
same version. The plan named six images (api, store, git, obsidian-sync,
curator, indexer) plus admin as a seventh once the captain's amendment made
it its own service; curator and indexer have no code yet, so today's release
publishes five: `store`, `api`, `admin`, `git`, and `obsidian-sync`.
Bootstrap and the migration step reuse the store image rather than shipping
their own. There is no separate version-bump pull request. The full
mechanics and the one manual step the first release needs are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

Four broad milestones were planned, each ending in a tag that runs from a
plain `docker compose up`:

| Milestone | Proves | Tag |
|---|---|---|
| 1: source to phone and back | Something goes in through the API, lands as a real file, reaches a device, comes back edited, and is retrievable with conflict protection | v0.1.0 |
| 2: reviewed to filed and found | Marking a note reviewed on a device is enough to get it filed correctly and found by search | v0.2.0 |
| 3: runs in the lab | The same images run in Kubernetes under the lab's normal deployment process | v0.3.0 |
| 4: survives | A complete loss of the deployment is recoverable from the data alone; attachments, deletion, and schema changes behave | v0.4.0 (MVP complete) |

Where each of these actually stands today is in [roadmap.md](roadmap.md), not
here: this table is the plan as approved, not a live status board.

## Deliberately left out of the MVP

Each of these was considered and set aside on purpose, not overlooked. Each
has a prepared place in the design for when it is actually needed:

- **Redis**, in favor of the PostgreSQL outbox. The interface is shaped so
  Redis Streams could be added later without touching anything that produces
  events, if an outside consumer ever needs them.
- **The transport into Coppermind** (an n8n flow, Plaud's own API, anything
  upstream of a single documented ingest call). Not this repository's job by
  the captain's own words above.
- **Importing an existing Obsidian vault.** The remote vault Coppermind syncs
  to starts empty on purpose; pointing the reconciler at years of existing
  notes would rewrite every one of them and push that all at once to every
  device. Import becomes a deliberate, later Admin action.
- **Vector or hybrid search**, on top of the plain text search that ships
  first.
- **Note history and restore through the API.** Git already has the history;
  reading it through an endpoint is later work, not a missing capability
  today (`git log` inside the container reaches it now).
- **Rewriting links when a note is renamed.** Filing (a move) never breaks a
  link; renaming a note's title can, and that gap is documented rather than
  silently fixed.
- **Single sign-on for Admin.** A local claimed password is the whole story
  until Admin is ever reachable from outside the lab network.
- **Multi-arch images and per-component version numbers.** One tag stamps
  every image; the lab is a single architecture.

## Where the built system has already diverged from this plan

Two places are worth flagging explicitly, because carrying the plan
unchanged here would misstate what the code actually does.

- **Whole-document note replacement does not preserve formatting the way the
  plan promised.** The plan's frontmatter rule was minimal-diff editing that
  preserves key order, comments, and unknown keys on every write. That holds
  for the targeted frontmatter-patch endpoint, but a full `PUT` of a note
  reassembles the properties block from what was sent: a hand-arranged key
  order or a comment placed between keys does not survive it. This is a
  deliberate, accepted exception (see `AGENTS.md`'s sharp-edges notes), not
  an oversight, but it is a real narrowing of the original promise.
- **Obsidian Sync currently refuses every real connection on purpose.** The
  plan described a one-time Admin login flow (email, password, optional MFA)
  that would hand the sync client a token and start delivering notes to
  devices inside the first milestone. What is built instead is a supervisor
  that answers its control endpoint honestly but refuses to invoke the real
  client at all, connect, pause, resume, or on restart, until Admin ships the
  guided setup for the captain's own separate, encrypted remote vault. No
  note has reached a device yet through this path. This is a sequencing
  choice (the guided setup that replaces plain login has not been built),
  not a reversal of the decision to use a new vault, but it means the
  phone half of milestone 1 is not there yet even though the plan's PR
  breakdown expected it in the first slice.
- **The `journal:read` and `journal:write` scopes exist but do nothing yet.**
  The plan lists them as real, enforced scopes alongside `notes:read` and
  `notes:write`. Today they are defined in the scope vocabulary and nothing
  checks them: a key holding only `notes:write` can write a note of type
  `journal`. A key holder should not assume the finer-grained journal
  permission is actually a boundary yet.

Everything else in this document (the component boundaries, the storage
layout, the contract shapes, the deferred list) still matches what was
approved. Check [STATUS.md](../STATUS.md) before assuming any specific
behavior is live; this document describes the destination, not today's
position on the way there.
