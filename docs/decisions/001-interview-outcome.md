# 001: interview outcome for spec v0.3

- **Status:** accepted
- **Date:** 2026-09-09
- **Assignment:** Coppermind architecture scout (task coppermind-arch-scout) and its interview (task coppermind-interview), answered by the captain in chat on 2026-09-09
- **Lane:** fast lane
- **Workers dispatched:** None (directive authority)
- **Authority:** the captain's interview answers of 2026-09-09 and his approval of the implementation plan on 2026-09-09, quoted below where they decide something

This record has no worker round because the decisions below were made by the
captain in the interview, not proposed by a team. It exists so the build
never has to reconstruct them from chat.

## Context

Spec v0.3 is the design contract for Coppermind, a filesystem-first personal
knowledge platform. The scout assessed it against sentania-labs conventions
and found five things that change what gets built: an official headless
Obsidian Sync client now exists; the lab's storage measurements favor one
ReadWriteOnce pod over ReadWriteMany; two internal services could be removed;
the filing vocabulary contradicted itself between sections 8 and 35; and no
first-party Helm chart exists in the org. Ten interview questions followed.

## Decision

| Topic | Decision | Captain's words |
| --- | --- | --- |
| Q1 visibility | public | "It will be public." |
| Q2 delivery | full validation pipeline on every PR | "coppermind: public, full CI" |
| Q3 sync client | official obsidian-headless CLI in its own image | (default accepted) |
| Q4 remote vault | new, empty; import later from Admin; Standard or Plus is an Admin setting driving file and total size limits | "Assume a fresh vault"; "Sync plan - we shoudl support both" |
| Q5 store | the store is its own single-writer service holding the notes volume, with the Git helper and the Sync client as sibling containers in the same pod; the API is stateless and may run more than one replica | "seperate the store" |
| Q6 events | no Redis in the MVP; PostgreSQL outbox plus LISTEN/NOTIFY behind an event interface so Redis Streams can be added for external consumers later | (default accepted with the interface requirement) |
| Q7 vocabulary | `type`, `context`, `account` as proposed; keys, vocabularies and filing rules are Admin settings with shipped defaults; a key rename is a deliberate migration action | "we should be able to admin interface define the front-matter standards right?" |
| Q8 storage | RWO core pod by default; RWX as a chart value that splits the writer containers into separate pods with polling instead of inotify | "I'm OK with RWO, as long as RWX is a toggle" |
| Q9 control state | versioned files under /data/state; PostgreSQL fully rebuildable; backup is /data | (default accepted) |
| Q10 transport | none in Coppermind; slice 1 ends at the ingest API with a documented sample request | "you are building a data service/sync/storage engine. YOu don't do the transport from plaud/email/etc into the vault. Obsidian sneaks in just because of the filesystem access." |
| PostgreSQL | chart and compose support bring-your-own as well as a bundled single instance | "or cloudnativepg?" |
| Sync login | one-time login from an Admin "Connect Obsidian Sync" page that passes credentials to the sync container and stores only the resulting token under /data/state, with a paste-a-token option and a reveal-once for sealing into GitOps | (captain's description) |
| Wording | "notes filesystem", never "the vault", except when naming Obsidian's own concept | "it's not a vault - we are decomposing vault." |

Two further decisions came when the captain approved the implementation plan
later the same day: where Admin lives, and what happens during a PostgreSQL
outage. They are recorded below and win over anything above them.

Binding rules restated for this repo: every setting has an Admin control with
a working default; no em-dashes anywhere; done means seen working; releases
are tag-driven with no version-bump PR; one Codex review round per PR after
a non-author review.

## Amendments at plan approval (2026-09-09)

### Admin is a separate service and image

`coppermind-admin` is its own service and its own image, not a route group in
the API. It calls the API with an admin-scoped service key, holds no state of
its own, and its exposure and authentication are configured independently of
the public API. The component list is therefore api, admin, store, git,
obsidian-sync, curator and indexer, plus PostgreSQL.

> I do want `coppermind-admin` to remain a separate service/image rather than
> being folded into the API. Admin has a different lifecycle, exposure/auth
> posture, and UI responsibility, and keeping that boundary now fits the
> broader decomposition goal.

### PostgreSQL outage behaviour is deliberately simple

Notes on disk, Obsidian Sync, the Git helper and the reconciler keep working
with PostgreSQL down. A by-id read may still serve when the path resolves
without new caches or bookkeeping; when it cannot, it returns 503 as well.
Ingest, writes, listing, search and Admin mutations return a clean 503 until
PostgreSQL recovers, after which reconciliation converges whatever changed on
disk. Nothing is built whose only purpose is keeping the API mutable without
PostgreSQL, and database-less operation is not advertised as a capability.

> the filesystem and Obsidian path must remain fully usable, and safe by-ID
> reads can continue if they can be resolved without introducing additional
> state machinery. I do not think we need to deliberately support API
> mutations while PostgreSQL is unavailable. Writes, ingest, listing/search,
> and Admin mutations can return a clean `503` until PostgreSQL recovers,
> after which reconciliation should converge anything changed through the
> filesystem. Preserve the filesystem-first crash/recovery semantics, but
> don't turn database-less API operation into a feature.

## Consequences

Seven first-party images (api, admin, store, git, obsidian-sync, curator,
indexer) plus PostgreSQL. The store's contract is a typed Protocol with an HTTP
mapping so the service boundary is real from the first commit. The event
bus is an interface with one implementation. The Helm chart renders two
topologies from one template set. The acceptance matrix's Redis row becomes
a workers-stopped row and a PostgreSQL-down row.

## Dissent

The scout's recommendation on Q5 lost and is recorded in its own words:

> Recommend Store as a Python package inside the API process with a narrow,
> typed interface, so there is one write gateway and one HTTP contract;
> Curator and Admin call the API with scoped service keys. Extraction to a
> service later is a refactor of that one interface.

The captain chose a separate store service. The narrow typed interface is
kept, which is what makes the two shapes interchangeable later.

## Protected paths touched

None

## Sign-offs

None (directive authority; see the Authority line).
