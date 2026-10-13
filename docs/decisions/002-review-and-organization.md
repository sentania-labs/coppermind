# 002: Review, monthly organization, and workflow intake

Recorded 2026-10-13. These requirements supersede provider-based filing and
indexing before review in earlier plans. This record does not claim the
implementation has caught up.

Scott's decisions:

- "Everything, including reviewed notes, by month"
- "Don't index things until they have been marked reviewed"
- "Human edits stay reviewed; update the index"
- "Explicit note date, then received date"
- "for now they become seperate notes - the merging of them I think becomes part of a workflow later and not something coppermind explicity deals with (at least until I get to that point)"
- "the mockup looks good. specifically for n8n - we need to make sure we publish a contract of how and where we want it to drop: transcript and summary, potentially source audio."
- "I agree with your storage recommendation about browseable vs. immutable sources"

## Required behavior

Use `YYYY-MM` for reviewed notes, browsable `_Sources` pages, and browsable
attachments. Provider, account, and topic remain metadata. Use explicit note
date first, otherwise the first received date in the configured local
timezone. Do not infer a meeting date from text or regenerate received date
on retries.

Keep immutable originals in the internal source archive under stable source
identities. Moving a browsable projection must not relocate or rewrite the
original. Retain pending material durably until review. Exact review-area
folder names remain an implementation choice, not an approved literal path.

Review commits the association and final filing through a recoverable
operation. Partial failures remain visible and retryable; incomplete filing
must not publish an index entry. The guarantee concerns Coppermind's state,
not simultaneous arrival of all files on every Obsidian device.

Only reviewed content is eligible for search indexing, including any source
text indexed alongside a note. Human edits of an already reviewed note keep
it reviewed and refresh the index. Incoming automation creates unreviewed
notes. Separate arrivals remain separate notes. No automatic merging,
calendar matching, or replacement of a person's note is in MVP scope.

n8n submits transcript, summary, and optional audio through a published API
contract. It does not write Coppermind-managed folders. Large audio needs
streamed staged upload. Keep audio archived by default; copying it into the
browsable notes filesystem is a separate explicit choice. See
[n8n intake](../n8n-intake.md) for current support and proposed delivery rules.

## Implementation boundary

The approved Admin mockup guides the usability update. Its intake/review and
monthly filing screens depend on backend work and must not be presented as
working controls ahead of that work. Current Admin settings, keys, fields,
problems, sync, and search operations can be improved independently.

This branch implements the review/index gate, durable JSON intake and monthly
filing, and metadata recovery for ingested notes and source associations.
Release and deployment, streamed large-audio intake, and the end-to-end
Obsidian device and restore exercise remain. Source manifest and note files must be
sufficient for recovery, without treating PostgreSQL as the only surviving
record of an association.

On upgrade, migration 0007 discards only the derived search rows. The next
reconciliation pass files existing reviewed notes and rebuilds eligible search
entries. Search can temporarily be empty during that pass. Existing saved
field vocabularies are retained; monthly filing applies to reviewed notes
regardless of their previous folder. Restore must retain the complete `/data`
tree, including source archives, filing receipts under `state/filing`, and pinned
review approvals under `state/reviews`.

Shared sources retain the month of their first filing so a later citation never
breaks an earlier note's link. Notes continue to follow their own dates. Source
revision approval is explicit and durable; date corrections and ordinary edits
do not approve new source material. Revised browsable attachments get separate
revision-specific filenames, preserving earlier copies.
