# n8n recording intake

Status: the JSON ingest route below exists in v0.2.1. The staged upload
contract is a proposed next increment, not an available endpoint. This branch implements monthly filing and reviewed-only indexing; those changes
are not deployed yet. Validate the released version before connecting production intake.

## Where to send material

Send to the Coppermind API, not directly to `_Sources`, `_Attachments`, or
its internal archive. Coppermind owns those files. Use an n8n credential
holding an API key with `sources:write` and `notes:write`. Keep credentials
out of workflow exports and request bodies.

One submitted package contains a transcript, a summary, and optionally the
original audio. Preserve each original independently. Use the summary as
the draft note body as well as retaining it as a source artifact. A later
Zoom summary, OCR export, or manual note is a separate note. Coppermind does
not infer a calendar match or merge their contents.

## Available now: small JSON bundles

`POST /v1/ingest`, with `Content-Type: application/json` and an
`Authorization: Bearer <API key>` header. Example with synthetic material:

```json
{
  "source": {
    "provider": "plaud",
    "external_source_id": "example-recording-42/export-1",
    "source_type": "transcript",
    "origin": "n8n",
    "captured_at": "2026-10-13T09:30:00-05:00",
    "metadata": {
      "recording_id": "example-recording-42",
      "submission_id": "export-1"
    },
    "artifacts": [
      {
        "name": "transcript.txt",
        "mime_type": "text/plain",
        "content": "Synthetic transcript for an integration test."
      },
      {
        "name": "summary.md",
        "mime_type": "text/markdown",
        "content": "Discussed the intake test."
      }
    ]
  },
  "note": {
    "title": "Intake test",
    "body": "Discussed the intake test.",
    "frontmatter": {
      "date": "2026-10-13T09:30:00",
      "reviewed": false
    }
  }
}
```

Artifacts require unique filenames without directory paths. Supply exactly
one of `content` (UTF-8 text) or `content_base64` (binary encoded as base64).
Optional audio is another artifact, for example `audio.m4a` with its actual
MIME type and `content_base64`. The default ingest request ceiling is 25 MiB,
including JSON and base64 overhead. This route buffers the request; it is
not a large-recording upload interface. Do not raise the limit casually to
accommodate hours of audio.

A first creation returns 201; a replay or source revision returns 200. Save
`source.id`, `source.revision`, `note.id`, `note.path`, and `projection_path`
from the response. A response is not evidence that a note was reviewed.

Use a stable provider and external source ID for a particular submission.
Never use the n8n execution ID as the sole identity: retries must send the
same identity and bytes. An external ID composed from recording ID and a
stable export ID gives a later export its own note. Persist that export ID
before attempting the request; retrying must not generate a new one.

Current replay behavior matters:

- Same identity and artifact bytes return the existing source and note.
- Changed artifact bytes under the same identity create a source revision;
  they do not update the existing note. Its title, body, and frontmatter
  payload are ignored when `note.created` is false.
- Metadata-only corrections can be returned in `source.unstored_fields`.
  Treat a nonempty list as requiring attention, not a fully applied update.
- After a timeout or 503, retry the same request with bounded backoff. Files
  may already exist; retry repairs the mirror without duplicating the note.
- Stop on 401/403 until credentials or scopes are corrected. Stop on 413
  until the package is within limits. Inspect 422 validation details instead
  of repeatedly resending an invalid package. Escalate other conflicts.

New-install defaults use types `work`, `personal`, `reference`, and `journal`
(default `reference`), and contexts `internal`, `external`, and `personal`
(default `internal`). Workflow exports using `context: customer` should map
that to `external`, retaining the customer name in `account`, which is required
when context is `external`. `account` is a list of customer names. A plain
string is read as a one-item list, so `account: Acme` and `account: [Acme]`
are the same note, and an empty list counts as no account. `date` is
`YYYY-MM-DD` or a local date-and-time `YYYY-MM-DDTHH:MM:SS` with no UTC offset,
in the configured timezone; ingest writes the start time into `date` and never
writes `time` or `timezone`.
Existing installations retain their saved schema.

The currently configured frontmatter schema still applies. Keep workflow
metadata on the source unless its corresponding note field is declared.
Ingest always creates an unreviewed draft, even if automation supplies
`reviewed: true`. Drafts live in `Review/`; source pages wait under
`_Sources/_Pending/YYYY-MM/`. Mark the note reviewed in Obsidian or through the
frontmatter API. The next reconciliation pass files the note and source page
under their respective month folders, copies non-text, non-audio attachments
under `_Attachments/YYYY-MM/`, and permits indexing only after successful filing.
A source keeps the month chosen when it was first filed, including when another
note cites it or a note's date is later corrected. All citing notes retain the
same source link. Revision-specific attachment filenames preserve earlier copies.
Audio stays in the immutable archive. Filing errors appear in Admin Problems
and retry automatically. Human edits of a filed note retain review and update search.

A changed payload under the same external ID creates an archive revision and
preserves the existing note. It does not silently publish the new source text.
To approve a revision, review the current source page and explicitly PATCH the
note's reviewed field to true again. In Obsidian, change reviewed to false,
allow it to sync and reconcile, then change it to true. Ordinary body, date,
and association edits never approve a newer source revision. New associations
on an already reviewed note also require that deliberate review action.
Approval records pin the exact source revisions; restore cannot advance them.
Use a distinct submission ID for new material that needs its own reviewable note.

## Proposed next contract: staged bundles

These paths and field names are a draft for implementation. A workflow must
not call them until a release explicitly supports contract version 1.

| Operation | Proposed route | Result |
|---|---|---|
| Reserve bundle | `POST /v1/intake` | Receipt ID, state, accepted limits, artifact upload URLs |
| Upload original | `PUT /v1/intake/{receipt_id}/artifacts/{artifact_id}` | Durable receipt for streamed bytes |
| Finalize | `POST /v1/intake/{receipt_id}/finalize` | Accepted complete bundle, draft pending review |
| Inspect/retry | `GET /v1/intake/{receipt_id}` | State, missing artifacts, failure detail, source/note IDs when available |

Reserve with `contract_version: 1`, `provider`, `recording_id`,
`submission_id`, `title`, optional `note_date`, optional offset-qualified
`captured_at`, optional `meeting_ref`, and an artifact manifest. Each manifest
entry declares `artifact_id`, `role` (`transcript`, `summary`, or `audio`),
`filename`, `mime_type`, `size_bytes`, and `sha256`. Version 1 requires exactly
one transcript and one summary, plus zero or one audio file. Summary content
is UTF-8 Markdown and becomes the opening draft. An explicitly supplied
meeting reference is metadata only, never an instruction to merge notes.

Identity is `(provider, recording_id, submission_id)`. n8n must persist the
submission ID and manifest before upload. An identical reservation returns
the same receipt. Reusing that identity with a different manifest returns
409, requiring an explicit new submission ID. A new submission produces a
separate note even when its recording ID is unchanged.

Uploads stream to durable staging outside the browsable notes filesystem.
Coppermind validates the declared byte count and hash before marking each
artifact complete. Retrying an identical completed artifact succeeds;
conflicting bytes fail. This version retries a whole artifact, without
partial-byte resume. Reserve returns per-artifact and aggregate limits so
n8n can reject an oversized package before transferring it.

Finalize is idempotent and refuses missing or invalid artifacts. Accepted
originals remain immutable. Its states distinguish `uploading`,
`processing`, `pending_review`, `filed`, and `failed`; a failure includes its
stage and whether retry is possible. Processing may continue after the
request ends. n8n polls with bounded backoff until `pending_review` or a
failure; reaching `pending_review` completes delivery, not human review.
Neither partial upload nor an incomplete filing operation enters search.
There is no automatic deletion of accepted pending material.

Draft notes and browsable source material stay in a durable review area
until review. The final month is selected from explicit note date, otherwise
first received date in the configured local timezone. Retries preserve that
received date. After review, notes, source pages, and browsable attachments
are filed by month. Original archive paths remain stable. Audio stays in the
archive by default rather than being copied to every Obsidian device.

## Before production activation

Prove delivery with one representative n8n transcript and summary, then one
recording with audio at the expected size. Exercise timeout after finalize,
repeated submission, conflicting content, a failed upload, and a later export
of the same recording. Verify there is one draft per submission, every
original remains readable, and unreviewed content is absent from search.
Then mark a note reviewed and verify monthly filing and indexed contents.
Those final requirements need backend work before this contract can be
advertised as implemented.
