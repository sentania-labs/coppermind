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
property proposals. An unrelated calendar change does not regenerate suggestions.

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
cannot be named with it. Select the key through Obsidian SecretStorage; the plugin saves
only its name, never its value, in plugin settings. Configure it separately on devices
that do not share local secret storage.

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
person, confidence and reason. Candidates are the linked calendar
attendees, then People named in the note or transcript, then a few recently met People;
never the whole People list. The model sees a bounded sample of each speaker's turns, not
the full transcript. If that cannot fit the input limit, speaker suggestions are skipped
with a notice and the other suggestions still arrive. A person renamed after suggestions
were made is written under the current name; one retired is refused for that speaker
only. When enrichment links an event automatically, it asks the model again with that
event's attendees. If you link an event yourself after the first pass, request fresh
suggestions. Confident names start selected, and the panel previews each change.
Choose another candidate or leave a speaker unnamed. Search existing People from the
panel, even if the model is unavailable or speaker suggestions were omitted. Completing
review replaces selected speaker labels throughout the transcript, adds confirmed attendees
and links known dossiers. Unselected speakers keep their labels. Setting reviewed
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
waits 30 seconds for stable bytes. It never deletes dropped files.

All exports merge automatically into the owner's one calendar by event UID and
recurrence instance. Calendar UID and filename never create a separate calendar.
Event details show export and file provenance, including multiple witnesses.
Conflicting revisions appear in Problems. Partial exports retain missing events;
explicit cancellation is different from absence.

Originals and import history remain under the state volume. Completed and failed
receipts can be dismissed; completed receipts also clear after seven days. An identical re-drop displays
**Already imported** without a new import.
`GET /v1/dropbox` requires `sources:read`. Unsupported files remain preserved.
Markdown in the vault Dropbox follows ordinary note adoption; the binary API does
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
  Repeating the recorded request is safe.
- `POST /v1/notes/{id}/suggestions`: explicit refresh, with the same three scopes.
- `POST /v1/notes/{id}/calendar-update`: confirm a candidate or dismiss the update,
  conditional on both note version and calendar evidence version.

The companion uses the documented Obsidian
[SecretStorage](https://docs.obsidian.md/plugins/guides/secret-storage) and
[requestUrl](https://docs.obsidian.md/Reference/TypeScript%20API/requestUrl) APIs.
