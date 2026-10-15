# Calendar intake and enrichment

This branch adds calendar intake and optional enrichment. It is not deployed.
Existing note sync and review work without a model connection. Enrichment never
writes the note body or changes its reviewed flag.

## Start in Admin

Open **Calendar and enrichment** and add a calendar source. Give it a stable
lowercase ID, a name, a vault drop folder such as `_Calendar/Work`, and its
local timezone. Use a separate, non-overlapping folder for each calendar.
Changing an existing calendar's drop folder is refused rather than silently
reassigning its history. Disable its watcher to stop further intake.

Drop `.ics` files, or ZIP archives containing only ICS files and directories,
into that folder through Obsidian. The files must remain unchanged across scans
for 30 seconds. Importing preserves originals under `/data/state/calendars`;
it does not delete the dropped files and does not call a model. Markdown in a
configured drop folder is excluded from adoption. Files already carrying a
known note identity continue to follow the ordinary identity reconciliation rule.

Limits are 10 MiB per upload, 40 MiB expanded ZIP, 100 ZIP entries, 50,000 event
records per export, and 1,000 input files per drop folder. Encrypted archives,
symlinks, nested archives, unsafe paths, and unsupported calendar component
types are refused. Parsing and recurrence lookup have process memory and CPU
limits. A failed import leaves the previous calendar intact and retains its
original and an error receipt. Retry it from Admin after resolving the cause.

An alternate intake is `POST /v1/calendars/{calendar_id}/imports?filename=export.ics`.
Send the binary file as the request body with a `sources:write` API key.
A 202 response means the original and pending receipt are durable, not that
parsing has succeeded. `GET /v1/calendars` with `sources:read` returns import
status, event records, contacts, and pagination. Follow `next_offset` as the
next request's `offset`. Admin uses the same queue and displays 100 records
per section per page. The API does not expose model credentials or note jobs.

## Updates and contacts

Calendar ID, event UID, and recurrence identity determine event identity;
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
and database reconstruction. Broader people/company dossiers are not included.

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
ID. Mappings must name existing fields of the appropriate kind. System fields,
including note ID, source associations, and reviewed state, cannot be enriched.
No production schema keys are created automatically.

Enablement records the existing note population. Those notes require a
separate explicit backlog selection, up to 50 at a time. New valid unreviewed
notes become eligible automatically. Calls run serially, with configurable
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
nearby calendar events, relevant contact corrections and selected schema
guidance are sent. Oversized input fails before transmission. The model has
no tools, URL fetches, or write access. It returns structured suggestions;
Coppermind checks supplied identities, exact evidence quotes, and schema rules.
Ambiguous matches can remain unresolved. Calendar invitees cannot become
attendee proposals without a supporting quote in the note or transcript.

## Human review and recovery

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

The job binds the note version and path, calendar/contact evidence, schema,
provider configuration, and source revisions. Changed evidence makes a proposal
stale. Rejections suppress identical inputs; new input versions can be considered
again. An approval receipt is durable before a file write. An interrupted
approval can be resumed, but only if the file matches its recorded before or
after state. It never overwrites a divergent edit.

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
