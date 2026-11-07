# Calendar intake and enrichment

Calendar intake and optional enrichment operate independently of note sync.
Enrichment never writes the note body. Explicit review completion can accept
selected proposals and mark the note reviewed. See [Note workflow](note-workflow.md)
for the shared Dropbox, Obsidian companion and lifecycle setup.

## One calendar, automatic intake

Coppermind is a single-owner knowledge base with one calendar. Drop `.ics` files
or ZIP archives containing ICS files into the vault's existing Dropbox folder.
No calendar UID, binding, identification or claim is needed. Every export merges
by event UID and recurrence instance. Calendar names and export filenames are
provenance, never separate calendar identities. No per-source views are provided.

Files must remain unchanged for 30 seconds. Originals and import history are
preserved under `/data/state/calendars`; dropped files are not deleted. Intake
never calls a model. Limits are 200,000,000 bytes per upload, 100 ZIP entries and
50,000 event records per export. The expanded size of an export is bounded by
`limits.calendar_import_memory_mib` (see the sizing table below), never above a
40 MiB zip-bomb guard. Encrypted archives,
symlinks, nested archives, unsafe paths and unsupported components are refused.
Parsing and recurrence lookup have memory and CPU limits. An invalid or
conflicting export leaves the previous calendar intact and appears in Problems.

Send binary bytes to `POST /v1/dropbox?filename=export.zip` with `sources:write`.
A 202 means the original and pending receipt are durable. Read receipts through
`GET /v1/dropbox` with `sources:read`. Failed and completed receipts can be
dismissed in Admin or with `POST /v1/dropbox/{id}/dismiss` (`sources:write`).
Completed receipts clear after seven days; failed receipts stay until
dismissed.
Import history and originals remain after dismissal or expiry. Dismissing a failed
receipt also clears its Problems entry; re-dropping the same failing bytes shows
it again, and a time or resource limit failure is retried by the re-drop. Any other
failure needs a corrected export, which is a new file and a new receipt. Any failed import can be dismissed from Calendar import history in
Admin or with `POST /v1/calendars/calendar/imports/{id}/dismiss`
(`sources:write`), including failures from direct uploads and receipts carried
over by the upgrade. A dismissed failure is marked as such and cannot be
silently reopened by retrying there. Re-dropping bytes that already imported
creates an **Already imported** receipt without another import, showing the
event count and original import time. Files waiting for the 30-second stability
check appear as waiting receipts.

Conflict errors name the event, UID, archive entry and incoming and stored
revisions. Correct the event in its source calendar and export it again, or
use **Dismiss failure** in Calendar import history. Legacy calendar conflicts
can also be acknowledged on that page without removing the current event or
originals. Problems includes unsupported and oversized Dropbox files, and
Overview uses the same active problem count.

`GET /v1/calendars` retains its paginated response shape and describes the single
calendar (`id: calendar`). Event details include provenance: export source ID,
original filename, archive entry and import timestamp. Several exports can witness
one event, including older revisions. There are no source-specific calendar views.
The compatibility upload route is `POST /v1/calendars/calendar/imports`.

On first startup after upgrade, the store snapshots legacy state and merges
existing calendars automatically. Previous calendar names remain on event
provenance. Conflicting legacy versions are retained and reported in Problems.
Old per-calendar folders become ordinary vault folders; new calendar exports
belong in Dropbox. Pending identity requests resume automatically. The migration
is idempotent and needs no clicks. Preserve the complete calendars and context
trees for recovery; the pre-upgrade snapshots alone are not an ongoing backup.
Catalog schema version 2 requires this version or newer. To roll back, stop the
store and restore the matching pre-upgrade state, including context configuration,
Dropbox state, catalog and import receipts. Migration retains compatible snapshots
as `context/before-one-calendar.json`, `calendars/before-one-calendar.json` and
`calendars/before-one-calendar-imports.json`. Originals are never rewritten.

## Updates and contacts

Event UID and recurrence identity determine event identity;
export filenames do not. Sequence and last-modified time order updates. Equal
revision numbers with different content are a visible conflict, not a
last-writer-wins update. Missing events in a partial export are retained.
Explicit cancellations suppress event occurrences. Recurrence lookup uses a
three-day window around an explicit note date and refuses more than 200
occurrences in that window rather than silently truncating the candidates.
It handles recurrence exceptions, local time zones and daylight-saving changes.

Organizers and invitees supply contacts and source evidence. Exact email
identities match, preserving the local part's case and normalizing the domain.
Names alone never merge people. Accepted invitations do not prove attendance.
Cancellation messages without attendees do not erase previously learned
contacts. Admin can record corrected names and explicitly link aliases to
another contact ID, or clear that correction. These decisions survive imports
and database reconstruction. People/company facts now use the broader
[dossier ledger](dossiers.md).

## Model connections and feature assignment

Under **Connections and model**, expand **Add connection**. Coppermind generates
its stable internal ID; only the display name is entered. Existing connections
are collapsible cards with their provider, settings, discovery and test controls.
For connections created with a manual ID, **Convert to automatic ID** changes
references in one atomic configuration write. Credentials, discovered models,
capability tests, enabled assignments and pending suggestion fingerprints remain
valid. Compatibility mappings retain the original storage identity privately;
do not remove them from backups. No provider calls or note edits occur during
conversion. A stale form is rejected and must be reloaded.

Supported connections:

- Anthropic uses its fixed official endpoint and an API key.
- OpenAI uses its fixed official endpoint and an API key.
- OpenAI-compatible uses an HTTP or HTTPS base URL and an optional API key.
  Include the API prefix, usually `/v1`, in that URL.

Refresh the model list, select a model, and test it using synthetic content.
Only models returned by discovery for that connection can be selected or tested.
Servers without working model discovery cannot be used. A compatible
server must support Chat Completions, JSON-object response format, and bounded
output. Discovery alone is not a capability check. Redirects are refused,
HTTPS verification is enabled, and remote error bodies are never shown.

Enrichment selects its connection and model separately. It cannot be enabled
until that exact connection configuration and model pass the synthetic test.
Editing a connection disables its assignment until it is tested and enabled
again. Blank API key input preserves the existing key; a separate checkbox
clears the key for an unauthenticated local server.

Select allowed metadata using schema-backed checkboxes. Linkage dropdowns show
only eligible list or string fields. The model dropdown follows the selected
connection. Advanced numeric limits are collapsed with defaults and explanations. Optional field
mappings are separate: attendee and invitee lists contain email addresses,
contact references contain contact IDs, and the event reference is a string
ID. Dossier-backed attendee/contact suggestions can also use exact People/
wikilinks in these list fields, with attendance quotations still required. Mappings must name existing fields of the appropriate kind. System fields,
including note ID, source associations, and reviewed state, cannot be enriched.
No production schema keys are created automatically.

Enablement records the existing note population. Those notes require a
separate explicit backlog selection, up to 50 at a time. New valid unreviewed
notes become eligible automatically. Reviewed notes also contribute dossier facts
through this worker, without a separate acceptance action. Calls run serially, with configurable
input/output limits, timeout, and one to three attempts. Each job records its
attempt limit before processing. Interrupted requests consume an attempt, even
when their outcome is unknown; restarting the worker never resets the budget.
Unexpected provider-response errors also consume the recorded attempt. An explicit
retry is required after exhaustion and may repeat a previously billed request.
Pause stops new calls;
an already-running call can finish. Disablement does not affect calendar intake
or ordinary note work. Usage is shown only when the provider supplies numeric
usage fields; currency budgets and billing estimates are not provided.

Only the selected note, explicitly associated text sources (maximum five),
nearby calendar events, relevant contact corrections, matching active dossiers
and selected field/tag guidance are sent. Dossier selection is bounded and includes
only direct relationship targets, never the whole corpus. Oversized input fails before transmission; the job states the
actual size against the limit and links to Maximum note and transcript
characters under Processing limits. The model has
no tools, URL fetches, or write access. It returns structured suggestions;
Coppermind checks supplied identities, exact evidence quotes, and schema rules.
Ambiguous matches can remain unresolved. Calendar invitees cannot become
attendee proposals without a supporting quote in the note or transcript.

A job the checks reject fails with a plain sentence on its Admin row and on
Inspect that names the rule, the suggestion it concerned and what to do next
(for example an unknown record, a quote not in the note, an unmapped field, a model
that is no longer discovered for its connection, or a
value that does not fit the note schema). The rule's stable name is stored on the
job as `error_rule`, and the store logs each failure at warning level with that
rule. Neither the stored message nor the log holds model output beyond a short,
sanitized field name.

## Human review and recovery

In `Review/`, validated suggestions with model-reported confidence of at least
0.8 automatically fill blank configured fields. They never replace existing
values or change body bytes, filenames or reviewed notes. Lower confidence and
uncertainties stay in Admin. Review the visible fields in Obsidian, then mark the
note reviewed; that also accepts its evidenced contributions to dossiers.
The following manual Admin operations remain available for repair.

Review current values, proposed values, reasons, and source evidence in Admin.
Approve, edit, or reject a proposal. Filename approval explicitly acknowledges
that existing filename links may need manual repair; it preserves the original
heading and every body byte. Frontmatter edits also preserve the body.

Approving contact, attendee or invitee suggestions adds identities to the mapped
list, preserving human-authored entries, their order, and earlier approvals.
Repeated approved identities are not appended again.

Apply selected suggestions individually. Each completed choice remains in the
audit history. Changing the note date makes remaining suggestions stale and
requires fresh calendar matching.
An explicit note review remains separate. Reviewed notes are never eligible
for automatic enrichment, and human values are not overwritten automatically.

The job binds the note version and path, calendar/contact and dossier evidence, schema,
provider configuration, and source revisions. Changed evidence makes a proposal
stale. Rejections suppress identical inputs; new input versions can be considered
again. An approval receipt is durable before a file write. An interrupted
approval can be resumed, but only if the file matches its recorded before or
after state. Automatic fills resume through the same receipt without another
model call. It never overwrites a divergent edit.

Back up the complete `/data/state/calendars` and `/data/state/context` trees.
They contain originals, catalog, receipts, contact corrections, suggestions,
rejections, and approvals. PostgreSQL is not their only surviving copy. Model
keys are in mode-0600 `context/credentials.json`, outside the vault and Git;
include them only in protected backups and never in a support export.
Disabling enrichment is the immediate rollback control. Older versions do not
read these new files; preserve them when reverting an image. Do not erase the
new state tree or run two store writers against it.

## Acceptance boundary

Local fixtures exercise the three provider protocols and a synthetic compatible
endpoint. Real Anthropic/OpenAI account access and a real local model require
configured credentials and endpoint capability checks. A real Obsidian phone
round trip and an off-device restore drill remain deployment acceptance work.
No production notes are sent or changed by installing the feature alone.

The GUI notes assistant, body rewriting, note merging, fuzzy automatic contact
merging, live calendar subscriptions, calendar-provider writes, invitations,
generated contact note pages, general backup tooling and unrelated document
intake are outside this batch.

Provider contract references:
[OpenAI model discovery](https://developers.openai.com/api/reference/resources/models/methods/list),
[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create),
[Anthropic models](https://platform.claude.com/docs/en/api/models/list), and
[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create).

Mistaken approved associations can be removed from a job's review page. The
correction records authorization before changing the association ledger or
mapped frontmatter. It preserves unrelated human field values and body bytes.
An interrupted correction has a separate resume action. Rejecting one suggestion
preserves the remaining choices; rejecting the remaining proposal stops all
undecided choices. Semantically unchanged calendar re-exports do not invalidate
accepted or rejected decisions solely because a new import receipt exists.


## Memory and deployment sizing

PostgreSQL holds the queryable calendar mirror. The store reads 100 event rows
per snapshot page, retains at most 100 evidence records per contact preview, and
streams recurrence evidence through a temporary file in series order. A series
with more than 1,000 master/override records is refused during lookup. Follow-up
records contain a fingerprint and message; candidates are queried only when that
note's review opens. The context snapshot never embeds the calendar in follow-ups.

`/data/state/calendars/catalog.json` remains an atomic filesystem snapshot,
written incrementally from the mirror. It is streamed only when rebuilding a
missing or stale mirror, including the first start after upgrading an existing
catalog. A normal restart queries the existing rows. Preserve this directory and
its import originals in backups. A crash after filesystem promotion and before
mirror commit is repaired from the catalog before the next query. Standalone
archive consumers without a PostgreSQL URL use a local, rebuildable SQLite mirror;
the deployed store always uses PostgreSQL.

Size the store for the **1024 MiB import parser budget** (the default) with a
**2 GiB store limit**. The parser runs in a subprocess under that address-space
cap; reserve at least another 512 MiB for the store, bounded request pages, JSON
handoff buffers and allocator overhead. PostgreSQL has its own memory allocation
outside the store container. Raising `limits.calendar_import_memory_mib` requires
raising the store limit as well.

Measured parser memory peaks near 30 bytes per expanded export byte. The import
preflight estimates **40 bytes per expanded export byte, plus 5.3 KiB per
event**, a third above the measured peak, so it refuses what the parser's memory
cap would refuse, before parsing starts. The refusal names the
setting, the budget the export needs and the expanded size the current budget
fits. Event count alone is not an import sizing formula: descriptions,
attendees, timezone components and recurrence overrides change parsing cost.
Exports exceeding the memory or event budget fail visibly without replacing the
accepted calendar.

| Expanded export | Events | Required `calendar_import_memory_mib` |
| ---: | ---: | ---: |
| 5 MiB | 1,000 | 210 |
| 14 MiB (owner-scale, 4,499 events) | 4,499 | 590 |
| 24 MiB | 7,000 | 1,000 |
| 40 MiB | 15,000 | 1,680 (about 1.65 GiB) |

Round up when setting it; the maximum is 2048. Raise the store container limit
with it: a 1,680 MiB parser budget needs a store limit above 2.2 GiB.

### Measured calendar workload

Measured on Linux/Python 3.12 with Docker Compose and a **1 GiB store cap, no
swap**: a synthetic Google Calendar export with 4,500 weekly recurring events,
22 attendees and an organizer per event, and descriptions padded to exactly
14,400,000 bytes. It was uploaded as a 90,349-byte ZIP to exercise the expanded
calendar path. The original implementation wrote a 55,459,322-byte catalog.
These measurements use an empty notes filesystem; other store workloads need
additional headroom. MiB below means 1,048,576 bytes.

| Operation | Original store RSS | Queryable mirror RSS | Container peak with mirror |
| --- | ---: | ---: | ---: |
| Empty calendar (0 events) | 88 MiB | 100 MiB | 145 MiB |
| After fresh 4,500-event import | 197 MiB | 121 MiB | 407 MiB |
| Read existing catalog after upgrade | 499 MiB after sequential snapshots | 100 MiB after streamed rebuild | 185 MiB |
| Four concurrent snapshots | OOM kill, exit 137 at 1 GiB | 102 MiB | 185 MiB |
| Restart, then four concurrent snapshots | OOM kill again | 95 MiB | 139 MiB |

The one-time rebuild of this existing catalog took about 56 seconds; health
requests continued to answer while it ran. The first context request may exceed
a caller's timeout until that rebuild completes. Subsequent restarts reuse the
mirror. The normal resident cost is bounded by request pages rather than a
resident copy of every event; these are measured points, not a linear estimate
for arbitrarily large descriptions or recurrence series.

The fresh import and restart both completed without a kill at 1 GiB. With
`calendar_max_events=4500`, an additional new event failed with the named ceiling
in its receipt. With `calendar_import_memory_mib=128`, a new event with a 9 MiB
description failed the memory preflight. Both failures appeared in Problems;
the accepted 4,500 events remained queryable, and defaults were restored.
