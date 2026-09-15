# Coppermind roadmap

Current as of 2026-09-15. This is a living status board, not an archive: it
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

Thirteen increments have landed on top of the initial repository skeleton
(PR #1, 2026-09-09), all inside the first of the plan's four milestones
("source to phone and back"):

| Landed | PR | What it added |
|---|---|---|
| 2026-09-12 | #4 | The Git helper: automatic history of the notes filesystem, no credentials, no network |
| 2026-09-14 | #5 | Conditional note replacement (`PUT` with `If-Match`), so a stale edit is refused instead of silently overwritten |
| 2026-09-14 | #6 | Scoped bearer API keys required on every route |
| 2026-09-15 | #8 | `POST /v1/ingest`: a source and its note are created together or not at all |
| 2026-09-15 | #9 | Replaying an identical ingest is a no-op; changed content appends a new revision instead of a new note |
| 2026-09-15 | #10 | Targeted frontmatter edits (mark a note reviewed) without rewriting the whole file |
| 2026-09-15 | #11 | Listing and filtering notes, with paging that survives a rename between pages |
| 2026-09-15 | #12 | The reconciler's read side: edits, moves, renames, and deletes made on a device are picked up without anyone calling an API |
| 2026-09-15 | #13 | The reconciler's write side: a note created on a device gets an identity assigned automatically once it settles |
| 2026-09-15 | #7 | The Obsidian Sync helper service (supervised, but real connections are refused on purpose for now, see below) |
| 2026-09-15 | #15 | The release pipeline: a pushed version tag builds, signs, and publishes all six images and cuts a GitHub release |
| 2026-09-15 | #16 | Admin's front door: first-boot claim and password login, as its own service |
| 2026-09-15 | #14 | Ingested sources are projected into the notes filesystem as read-only pages a phone can open |

Everything above runs from a plain `docker compose up -d` with nothing
hand-configured first, and is proven by the same automated checks that run
in CI.

## Next, in order

The approved decomposition (checkpointed after the increments above) calls
for these next, still inside milestone 1 unless noted:

1. **Admin's guided Obsidian Sync connection.** Today the sync helper
   answers honestly but refuses every real connect, pause, resume, and
   reconnect-on-restart call. This is the one piece standing between where
   the build is now and the captain actually seeing a note travel to his
   phone and back, which is milestone 1's whole point.
2. **Move and rename** through the API, keeping a note's identity across
   both (milestone 2 work, `M1` in the decomposition).
3. **The curator**, which files a reviewed note into the right folder by
   rule, and the **indexer**, which keeps search current (`CU1`, `IX1`,
   milestone 2). Neither has code in the tree yet.
4. **Admin's remaining pages**: API keys, settings, frontmatter schema,
   filing rules, jobs, and real overview counters. Until these land, key
   creation and rotation stay on the store's own command line, which
   CONTRIBUTING and STATUS both call out as an interim stopgap the graphical
   pages are meant to retire (`AD3` and the rest of `AD1`/`AD2`).
5. **The Helm chart and lab handoff** (milestone 3), not started.

## Deliberately deferred

These were considered and set aside on purpose, each with a reason and a
prepared place in the design to pick them back up. This list is not
exhaustive; the full one is in [architecture.md](architecture.md)'s
"deliberately left out of the MVP" section.

| Deferred | Why | Revisit when |
|---|---|---|
| Redis for event delivery | The PostgreSQL outbox already gives correctness through reconciliation; Redis only helps an external consumer, and there is not one yet | Something outside this system needs to consume Coppermind's events |
| Importing the captain's existing Obsidian vault | The remote vault starts empty on purpose; pointing the reconciler at years of existing notes would rewrite and push every one of them at once | After the fresh vault has proven itself, as a deliberate one-time Admin action |
| Vector or hybrid search | Plain PostgreSQL text search is what milestone 2 needs; there is no case yet that plain search cannot answer | A concrete search need plain text search cannot serve |
| Rewriting links when a note is renamed | Filing (a move) never breaks a link; only a rename can, and that gap is documented rather than patched around | The first real broken-link report |
| Kubernetes and the Helm chart | Milestone 1 and 2 are still being proven on Compose; standing up the chart before the core path works would be building on an unproven foundation | Milestone 3 starts |
| Single sign-on for Admin | A local claimed password is enough while Admin is reachable only on the lab's loopback network | Admin is ever exposed outside that network |
| Multi-arch images, per-component versions | The lab runs one architecture; one tag already stamps every image consistently | The org's practice changes, or an arm64 host appears |

## What this does not cover

Bug-level detail (two open issues today: concurrent same-title creates
answering 409 instead of a numbered suffix, and a stale mirror row naming
the wrong note as unparseable) lives on the repository's issue tracker, not
here. Day-to-day behavior of what is merged, including every documented
limit and known gap, is [STATUS.md](../STATUS.md).
