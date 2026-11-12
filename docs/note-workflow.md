# Note workflow and shared Dropbox

Enable **Admin > Note workflow** to add optional attendees,
invitees, calendar reference and time-unknown properties. Existing
notes, type choices and schema roles are preserved. Conflicting property types stop setup.
The common Dropbox defaults to `Dropbox` in the vault. Old per-calendar folders are ordinary vault folders after automatic migration.

## Review once

New valid unreviewed notes receive one initial enrichment pass when enrichment
is enabled. The existing backlog is opt-in. Ordinary edits do not run the model
again. **Refresh suggestions** explicitly requests another pass. Related calendar
changes can generate a calendar-only pass, without repeating filename or general
property proposals. An unrelated calendar change does not regenerate suggestions. A recording's meeting
summary is read from the transcript once per transcript revision: automatic refreshes
carry it forward, and only **Refresh suggestions** or a changed transcript reads it again.

The **Review notes** screen shows the note and proposals together. Select changes
and complete review once, or leave everything unchecked to retain only your edits.
A filename change requires acknowledgement that existing filename links can need
repair. The authored heading and body stay unchanged. Stale suggestions cannot
be applied; reload or explicitly request fresh suggestions.

Install the companion ZIP from **Note workflow** into
`.obsidian/plugins/coppermind-review/`. Enable it in Community plugins. It requires
Obsidian 1.11 or later and a Coppermind API key with `notes:read`, `notes:write` and
`sources:read`. Naming speakers also needs `sources:write` and `entities:read`; a key
without `entities:read` still reviews everything else, and the panel says that speakers
cannot be named with it. Naming a calendar contact who needs a new People entry also
requires `entities:write`; choosing an existing person does not. The People view
(voice recognition) also needs `entities:write`
to change recognition or delete signatures. Combining or separating voices also needs `sources:write`. Select the key through
Obsidian SecretStorage; the plugin saves only its name, never its value, in plugin settings. Configure it separately on devices
that do not share local secret storage.

Run **Coppermind Review: Meetings to review** (or use the list icon in the ribbon) to see
how many meetings are waiting for review and which recordings failed. Each waiting meeting
opens its note for review. Each failed recording says why in plain words, and offers
Retry when a retry can help and Dismiss when the app is not going to retry it by itself;
Dismiss only takes the entry off the list and deletes nothing. The view needs `notes:read` and
`sources:read`; Retry also needs `sources:write`, `notes:write` and `artifacts:read`, and
Dismiss needs `sources:write`. The calls are in [the API reference](api.md#recordings-audio-on-its-own).

Open a note and run **Coppermind Review: Review current note**. The panel checks
that the exact local bytes match the server before permitting completion. Wait
for Obsidian Sync after editing. `date` is the one field for a note's date and
start time: `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM:SS` as a local date-and-time with no
UTC offset, matching Obsidian's Date and Date & time properties. Times are in the
configured timezone (`America/Chicago` by default), a setting rather than a note
property. New notes, ingest, enrichment and the review form write the start time
into `date`, and never write `time` or `timezone`. A `date` carrying a UTC offset
is rejected at review with a Problems message. Work notes (`type: work`) require a
start time in `date`, or `time_unknown: true` with a date-only `date`. Other note
types need only a day. Midnight is an actual time, never a sentinel for unknown.
Legacy notes with a `date` and an `HH:MM` `time` keep validating and filing, a
legacy `timezone` is ignored, and when a legacy `time` carries its own date and
disagrees with `date`, `date` wins and Problems reports a `date_mismatch` entry
naming both dates. Saving `date` from the review form or the Obsidian plugin, or
approving an enrichment date, removes the legacy `time` and `timezone`. An
enrichment `account` suggestion is added to the note's list and never replaces it.

`account` is a list of customer names. A plain string is read as a one-item list.
`account` is required when `context: external` and needs at least one entry; an
empty list fails review the same way a missing account does. A reference note
that applies to several customers lists them all, and a note found by any one of
them.

For a recording, the review panel also shows each diarized speaker with a proposed
person, confidence and reason. Candidates are the linked calendar attendees, or people
from possible meetings when several timed events overlap (up to 40 across them; a
single linked meeting lists all its invitees), then People named in the note
or transcript, then a few recently met People;
never the whole People list. The model sees a bounded sample of each speaker's turns, not
the full transcript. If that cannot fit the input limit, speaker suggestions are skipped
with a notice and the other suggestions still arrive. A person renamed after suggestions
were made is written under the current name; one retired is refused for that speaker
only. When enrichment links an event automatically, it asks the model again with that
event's attendees. If you link an event yourself after the first pass, request fresh
suggestions. Confident names start selected, and the panel previews each change.
For people with saved signatures, a close voice match also appears as a `Voice` or `Maybe`
candidate for that speaker, returned as `voice_matches` in the review response
(see [voice recognition](dossiers.md#voice-recognition)). A single `Voice` match that the
model does not contradict starts selected; `Maybe` matches, several `Voice` matches, or a
disagreement with a confident model name are left for you to choose.
Choose another candidate or leave a speaker unnamed. Search existing People from the
panel, even if the model is unavailable or speaker suggestions were omitted. Completing
review replaces selected speaker labels throughout the transcript, adds confirmed attendees
and links known dossiers. Without an attendees property in Model
connections, speakers are still renamed, the panel says attendees will not be updated, and
attendees and contacts are left alone. Unselected speakers keep their labels. Setting reviewed
directly without the panel does not apply speaker suggestions. The review API accepts
`speaker_choices`, an object mapping selected suggestion indexes to candidate IDs;
an empty value leaves that speaker unnamed. For manual names, pass `manual_speakers`
(label to candidate ID), `manual_source_id` and `manual_source_revision` from the review
response. The review response provides the current speakers and bounded candidates.
The transcript sample is sent only to the configured enrichment connection, under the same
input limit and retry rules as other enrichment.

Review completion is recorded durably before writes and can be resumed. Each
selected change uses the existing version checks and approval receipts. The
reviewed flag is written last. Unselected proposals are dismissed. If someone
marks reviewed directly in Obsidian, finalization waits for an unchanged file
observation for at least 30 seconds and validates meeting metadata before filing.

Confirmed people records derive only from final reviewed attendee email addresses,
with note identity, version, date and properties as evidence. Names alone do not
create or merge identities. Edits replace a note's contribution; unreviewing or
losing access to a note withdraws it from the derived people view. These records
are a limited, evidence-backed dossier foundation, not automatic biographical or
company-profile synthesis.

## Calendar intake

Drop ICS files or ZIPs containing ICS files into Dropbox, or send binary bytes to
`POST /v1/dropbox?filename=export.ics` with `sources:write`. A 202 means the original
and intake receipt are durable, not that parsing has completed. Inspect Dropbox
in Admin for processing and failure status. The vault scanner
waits 30 seconds for stable bytes. A completed drop leaves Dropbox only after its
original is durable and the stored bytes are checked again; a file that cannot be
handled moves to `Dropbox/Failed` with a reason sidecar, and moving it back retries it.

All exports merge automatically into the owner's one calendar by event UID and
recurrence instance. Calendar UID and filename never create a separate calendar.
Event details show export and file provenance, including multiple witnesses.
Conflicting revisions appear in Problems. Partial exports retain missing events;
explicit cancellation is different from absence.

Originals and import history remain under the state volume. Completed and failed
receipts can be dismissed; completed receipts also clear after seven days. An identical re-drop displays
**Already imported** without a new import.
`GET /v1/dropbox` requires `sources:read`. Unsupported files are preserved in
`Dropbox/Failed`. Supported documents, images, audio and video are stored as
artifacts (see [the API](api.md#artifacts)). Markdown in the vault Dropbox follows ordinary note adoption; the binary API does
not adopt Markdown. Use the notes API for authored notes.

Limits: 200,000,000 bytes input, expanded size bounded by
`limits.calendar_import_memory_mib` (about 40x, never above 40 MiB), 100 archive
entries, safe paths only, and isolated parsing with CPU/memory limits. Oversized vault files
stay in place with a dismissible failed receipt. See
[Calendar intake](calendar-enrichment.md) for upgrade and recovery details.

## Late calendar changes

Relevant event evidence is fingerprinted per note's date window. For reviewed
notes, changed evidence creates a follow-up with meeting candidates. Confirm an
association or dismiss the update in Admin or the companion. These decisions live
in sidecar state and never rewrite a reviewed note or infer attendance from an
invitation. Unchanged re-exports do not reopen the decision.

## Recording summaries

After transcription, the existing enrichment job reads the whole transcript and
proposes a summary, decisions and action items with owners. Enrichment must be
enabled with a tested model connection. The companion displays the three sections
beside speaker suggestions. Edit the text or use **Drop** to clear a section;
**Apply kept suggestions & mark reviewed** appends the nonempty sections to the
note. There is no separate accept step for each section. Existing note prose is
preserved. Action items are recorded only, never carried out.

**Mark reviewed without changes** discards the summary. Directly setting
`reviewed: true` also adds no proposed prose. Failed jobs expose their reason and
a retry action in review, with durable failure details in Problems. Stale or
unsynced proposals cannot be applied. Completing review is recorded before the
append, so retrying an interrupted completion does not append a second copy.

When several timed events overlap a recording's start (a recording that starts up to
ten minutes early still matches), Review offers the matching meetings and may suggest
one from names and the topic in the conversation, nearest start first. When exactly one
event overlaps, it alone is offered, and its invitees become speaker candidates once it is linked. When none
overlaps, every event on the note's day stays a candidate, as before. The
recording and summary do not wait for that choice. Choose the meeting to link it;
the generated heading takes its title when that heading has not been edited. Until
then, possible meeting attendees are marked in the speaker chooser. Invited
people are not automatically treated as attendees.

Calendar event descriptions can hold private details, so they stay out of every API
response and out of the model prompt by default. Turn on **Send calendar event
descriptions to the model** in the enrichment settings to include them, and only
while the model works out which of several meetings a recording belongs to; with
it off the model compares titles and invitees.

If an existing hand-written note is linked to the same event on the same day,
Review offers to add the recording to it. Check the destination and summary, then
confirm the merge. The original text stays in place, the recording summary and
transcript link are appended, and the separate recording note is retired. Keeping
them separate dismisses the offer for that recording. A merge receipt and snapshots
are stored in the state volume before changes. An interrupted merge resumes on the
worker's next pass; if a note is unavailable or the recording note has been edited,
it is kept and Review reports what needs attention. Once the note is available again,
use **Retry merge** in Review. Merging requires `sources:write`
in addition to the review scopes. After you review and file the merged note, the
audio is removed as usual. Its removal receipt is written only inside the recording
section the merge added, and the recording's citation is dropped from the
frontmatter. Your own text and frontmatter comments are left untouched.

## State, recovery and rollback

Lifecycle receipts, confirmed contributions, follow-ups and Dropbox originals
are in the state volume and must be included in backups. PostgreSQL remains a
mirror. A complete review and completed monthly filing are separate recovery
steps. A filing failure keeps the note out of published search until corrected.

This version adds context configuration fields. Before upgrade, back up the
state volume. Older binaries do not understand those fields; a rollback needs
the compatible pre-upgrade context configuration as well as the earlier images.
Do not restore old note files over newer reviewed content.

## API for companions

- `GET /v1/notes/{id}/review`: note, current proposals and follow-up status.
  Requires `notes:read` and `sources:read` because evidence can reference sources.
  Speaker proposals and the manual speaker chooser list People (names, emails, pages), so
  they are returned only to a key that also holds `entities:read`; otherwise
  `speakers_unavailable` explains that and the rest of the review is unchanged.
- `POST /v1/notes/{id}/review`: explicit completion with `if_match`, selected
  proposal indexes and optional filename-risk acknowledgement. Also requires
  `notes:write`. Confirming a speaker also requires `sources:write` and `entities:read`.
  For recording summaries, GET includes `meeting_summary` with string fields
  `summary`, `decisions` and `action_items`. POST includes that object, with the
  owner's edits and the matching `job_id`, to keep it; empty strings drop sections.
  Omit it or send `null` to discard it. Each field is limited to 12,000 characters.
  Repeating the recorded request is safe.
- `POST /v1/notes/{id}/suggestions`: explicit refresh, with the same three scopes.
- `POST /v1/notes/{id}/calendar-update`: confirm a candidate or dismiss the update,
  conditional on both note version and calendar evidence version.
- `POST /v1/notes/{id}/recording-meeting`: select a current overlapping event using
  `if_match`, `calendar_hash` and `event_id`. Requires the review scopes and `notes:write`.
- `POST /v1/notes/{id}/recording-merge`: confirm a proposed merge with `if_match`,
  `target_id`, `target_hash`, `job_id`, `meeting_summary` and confirmation
  `Merge recording`. Also requires `sources:write`.
- `POST /v1/notes/{id}/recording-merge/dismiss`: remember dismissal for the source,
  with `if_match` and the review write scopes.
- `POST /v1/notes/{id}/recording-merge/retry`: retry a recorded merge with `if_match`
  and the same scopes as confirmation.

The companion uses the documented Obsidian
[SecretStorage](https://docs.obsidian.md/plugins/guides/secret-storage) and
[requestUrl](https://docs.obsidian.md/Reference/TypeScript%20API/requestUrl) APIs.
