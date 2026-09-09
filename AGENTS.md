# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

## The rules that outrank convenience

- **Say "notes filesystem", never "the vault."** Code, comments, docs,
  endpoint prose, commit messages. "Vault" names Obsidian's own remote vault
  object and nothing else.
- **One writer.** Only the store opens a note file for writing. The API, and
  later the curator and indexer, ask it over the contract in
  `coppermind/store_protocol.py`. Nothing else may grow write code.
- **Filesystem first, database second.** Files are the truth; every
  PostgreSQL row is a mirror that can be rebuilt from `/data`. Reject changes
  that make the database the only copy of something.
- **Admin is its own service and image**, not a route group in the API.
- **Every setting has a control in the interface and a working default.** A
  fresh install must run with nothing pre-configured.
- **No em-dashes anywhere.** `make prose-check` is the gate; `ci/prose-check.sh`
  builds the character from its bytes so the checker does not trip itself.

The full contributor bar, including the review and release process, is in
[CONTRIBUTING.md](CONTRIBUTING.md). What exists today versus what is only
planned is in [STATUS.md](STATUS.md); read it before assuming a capability is
present.

## Layout and commands

`coppermind/` is both the uv workspace root and the shared package; each
`services/<name>/` is a workspace member and one image, built with the
repository root as the build context. `make` is the only entry point that
matters, and CI calls the same targets: see the [Makefile](Makefile) for the
list. `make check` is what the `checks` job runs; `make db-up test-integration
db-down` needs Docker.

## Sharp edges found the hard way

- **Frontmatter is edited in ruamel round trip mode, never loaded and dumped.**
  A dump reorders keys and drops comments, and Obsidian Sync would then push
  every rewritten file to every device. `coppermind/frontmatter.py` and its
  tests hold the line.
- **SQLAlchemy connects lazily**, so a transaction that has not issued any SQL
  will not notice that PostgreSQL is gone. `LocalStore.create_note` issues a
  `SELECT 1` before touching the filesystem on purpose: without it a database
  outage would surface at commit, after a file had already been created.
- **The bundled PostgreSQL runs as uid 999 and the Coppermind images as 1000.**
  The bootstrap writes the password file owner 999, group 1000, mode 0640, and
  the secrets directory 0755 so PostgreSQL can traverse it. Tightening either
  breaks the stack at startup.
- **`grep` patterns over frontmatter need `-F`.** `sources: []` is an
  unterminated bracket expression as a basic regular expression.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
