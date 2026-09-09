# Contributing

Thanks for helping. This page is the whole process; there is no separate wiki.

## Run it locally

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker with Compose, and
Python 3.12 (uv will fetch it if you do not have it).

```bash
make setup                             # sync the uv workspace
make check                             # what CI's `checks` job runs
make db-up test-integration db-down    # the PostgreSQL backed tests
make scan                              # dependency, secret and repository scans
make up                                # the stack, on 127.0.0.1:8080
make smoke                             # the compose storyline end to end
make down                              # stop; `make clean` also drops volumes
```

`make check` and `make scan` predict CI exactly, because CI calls the same
targets. There is no command in the workflow that you cannot run here.

## The bar for a pull request

1. **Tests for what you changed.** A behaviour that can be proved without a
   database belongs in a unit test; the write protocol, reconciliation and
   anything about ordering belong in `tests/integration`. Say in the body what
   you observed.
2. **Reviewed before it opens.** Someone other than the author (a peer, a
   reviewer agent, or a genuinely separate self-review pass) reads the diff
   and tries to break it before the pull request exists. The author's own
   "looks good" does not count.
3. **One round of external review.** Address that one round, fix what is
   valid, reply to what is not, then stop. Do not loop.
4. **CI green, and seen working.** Green is necessary and not sufficient. Bring
   the stack up, do the thing the change claims to do, and put what you saw in
   the body. A healthy `/healthz` next to a broken page is the failure this
   rule exists for.
5. **Tags release.** From a merged `main` commit, `git tag -a vX.Y.Z -m vX.Y.Z`
   and push the tag. No version-bump pull request. The quickstart tracks
   `latest`; anything deploying Coppermind for real pins a version or a digest
   in its own repository.

Write the body in operational terms: what changes for someone running it,
what the blast radius is, how to recover if it is wrong. Name which regime the
change falls under (software, live infrastructure, or knowledge).

## House style

- **Say "notes filesystem", never "the vault".** In code, comments, docs,
  endpoint prose and commit messages. "Vault" names one thing only: Obsidian's
  own remote vault object on Obsidian's servers. We are decomposing a vault,
  not building one.
- **Every setting has a control in the interface and a working default.** If
  it is configurable, it is configurable from Admin, and a fresh install runs
  with nothing pre-configured. "Set this up yourself first" is not a shippable
  state. Wiring (ports, hostnames, the paths of credential files) is the
  deployer's and comes from `COPPERMIND_` environment variables; product
  settings are the operator's and live in `/data/state/settings.yaml`.
- **One writer.** Exactly one process writes the notes filesystem: the store.
  The API, the curator and the indexer ask it. If you find yourself opening a
  note file for writing anywhere else, the design has drifted.
- **Filesystem first, database second.** The files are the truth. Every
  PostgreSQL row is a mirror that one job can rebuild from `/data`. A change
  that makes the database the only copy of something is a change to reject.
- **Admin is its own service and image.** Different lifecycle, different
  exposure and authentication posture, different responsibility. Nothing may
  assume Admin lives inside the API process.
- **No em-dashes.** Anywhere: code, comments, docs, commit messages. Use a
  comma, a colon, parentheses or a period. `make prose-check` is the gate.
- **Never commit a secret.** Not in code, fixtures, pull request bodies,
  issues or screenshots. Credentials are files on volumes, never environment
  values, so `docker inspect` and a pod spec show nothing. The secret scan
  reads history, so a leaked secret must be rotated, not just removed.
- **Scope discipline.** Fix the thing the pull request is for. Anything else
  you notice becomes an issue, not extra commits here.

## Where things live

`coppermind/` is the shared package every image installs: identifiers, the
note file format, portable naming, the frontmatter schema, product settings,
control state files, the store contract and its HTTP client, and the database
models and migrations. `services/<name>/` is one image each, built with the
repository root as the build context. `ci/` holds the scripts CI and you both
run. `tests/` holds what crosses a service boundary; a service's own tests
live beside it.

The shared package deliberately carries declarations named by the approved plan ahead of
their consuming slices; check that plan before raising an unused-declaration finding.

## Decisions

Numbered records under `docs/decisions/`, never renumbered and never reused.
Add one when a choice would otherwise have to be reconstructed from a chat
log later.
