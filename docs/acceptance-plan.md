# Coppermind acceptance plan

This is the plan for how the captain personally checks that Coppermind
actually does what it is supposed to, in his own words and his own path
through the system: get something in, keep his edits safe, see it on his
phone, have it filed and findable, and survive things going wrong. It is a
plan to run, not a record of testing already completed, and it is separate
from the automated test suite, which proves narrower things continuously in
CI.

Carried in from the acceptance plan written 2026-09-14 against `main` at
commit `33d2b23`. Several increments have merged since that commit (see
[roadmap.md](roadmap.md) for the current merged list), so some cases marked
"not runnable" below may now be runnable; recheck against the current build
before relying on a status label here. The cases and evidence themselves are
carried as written.

## Before running any of this

Use a separate acceptance installation from anything live, an empty Obsidian
remote vault, synthetic material, and the captain's actual phone. Fault
exercises stay on that separate installation; changes to the real lab still
go through the normal deployment process, never a manual fix on the spot.

Carry one synthetic item through the whole path: **UAT Lantern**, body
"cobalt lantern 731", type `note`, context `personal`; for ingest, provider
and external ID `uat` / `lantern-731`. Keep its note and source identifiers.
Use a second item as the unreviewed control, to prove the system does not
touch things it should leave alone.

For every case: record the case ID, the package or commit under test, local
date and time, pass/fail/not runnable, any delay observed, and redacted
evidence. Open the actual note and compare its content and identifier. A
green badge, a status code, or a matching title alone is never enough to
pass a case. Exclude credentials from anything recorded.

**Timing to expect** (current defaults; use whatever is actually configured
if it has been changed): roughly six minutes for a Git snapshot to appear
after an edit settles; up to five minutes for a key change to take effect;
a 60-second filesystem scan with 30 seconds of quiet before a device-created
note gets an identity; a five-minute filing sweep once the curator exists;
a ten-minute search catch-up backstop once the indexer exists. Real phone
delivery has no promised deadline; measure it rather than assuming one.

## Runnable against the system today

These prove the foundation only. There is no Admin page or phone connection
proven by these cases alone.

| Case | What the captain does | What he should see | What counts as failure, including a false success |
|---|---|---|---|
| N1. Get a note in | Start a clean checkout from the README quickstart, use its generated default key, and submit UAT Lantern through the documented note request. Read the returned ID and open the returned path on the notes filesystem. | A complete Markdown note in `Review`, the supplied words, the same ID in the file and the response, and `reviewed: false`; nothing hand-configured first. | Healthy containers next to a failing note request; a 201 with no readable file; wrong content or ID; any configuration edit needed to boot. This proves direct note creation, not ingest. |
| N2. Keep my newer edit | Read the note and keep its ETag. Add "newer correction retained" to its body using that version, keeping the original marker. Then submit the older document with the old version. | The first save works; the stale save answers 409 `version_conflict`. Reopening the file by ID still shows the correction. | A conflict message with older bytes actually on disk; a stale save that succeeds; a client that claims a rejected edit was saved. Repeat this with a real phone edit once real Obsidian Sync is running. |
| N3. Limit and revoke access | Using the interim keys command, create a read-only test key and a separate read-write test key. Read with the first, attempt a write with it, then revoke the second and retry its previously successful request after five minutes. Restart normally and retry again. | The read-only key reads but cannot write (403). The revoked key is refused (401) after the cache expires, and stays refused after a restart. The existing note is unchanged by any refusal. | A successful mutation on a read-only key; a revoked key that still works after the cache should have expired, or that comes back to life at boot. A newly created key can be briefly rejected by today's cache; that delay should be recorded, not treated as a failure. This case moves to Admin's graphical keys page once it exists. |
| N4. Keep a usable history | Let the note settle into a Git snapshot. Change a sentence, wait for another snapshot, and inspect the earlier content with `git show <commit>:<note-path>` inside the Git helper. | Both versions are readable in local Git history without anyone manually running a commit. | Git is healthy but the edit never entered history; only commit counts were checked; history restarts after a normal restart. A history or restore screen in Admin is not built yet and is not required for this case to pass. |

## As the next increments land

Not runnable yet; recheck what has actually merged before changing any label
here, since these depend on specific pieces of work by name.

| Case / needs | What the captain does | What he should see | What counts as failure, including a false success |
|---|---|---|---|
| P1. Get something in | Submit the ingest example, adapted to UAT Lantern, with a short transcript artifact and the note body. Open the resulting note and inspect the saved source bundle. | One Review note, still unreviewed, linked to the same source ID ingest returned; the stored transcript matches what was sent. | Ingest reports success with no usable note; an empty or altered original; a note pointing at the wrong source. Repeating the same external identity should be refused and create nothing extra. |
| P2. Retry without making a mess | Resend the identical request. Then correct one source sentence and resend the same external identity after personally editing the note. | An identical replay answers 200 with `created: false`, the same source and note identifiers, one note. The changed source becomes a new revision; the earlier one stays intact on disk, and the captain's own note edit survives untouched. | Duplicate Review notes; an overwritten original; a replay that resets `reviewed` or replaces his prose. |
| P3. See it on my phone | Follow the delivered one-time connection instructions, open the matching remote vault on the phone, and open UAT Lantern. Add "edited on my phone"; read the same ID through Coppermind. Then save another sentence through Coppermind and reopen it on the phone. | Exact content travels both directions, with the same note ID, and no repeated login on restart. | "Connected" shown next to an absent or stale note; the wrong remote vault; only a server-side stand-in was ever exercised; a login that did not actually start continuous sync. |
| P4. Read where it came from | Follow the note's source link on the phone. Compare its transcript with the original, then repeat after P2's source correction. Retrieve the earlier revision through the documented source interface. | A readable source page, the correct source identity and latest revision, with the original artifact unchanged and still available. | A broken link or credential wall on the phone; a stale generated page; the original bytes changed; the generated page mistaken for a second, separate note. |
| P5. Start without setup chores | Start the delivered package with no settings prepared in advance. Open Admin, claim access through the documented flow, create a scoped key, connect Obsidian graphically, and repeat P1 and P3. Change a benign setting, such as Git's debounce interval, through its control; restart and check it persisted. | Default operation works immediately. Keys and the sync connection are usable entirely from the interface. A changed setting persists and actually changes behavior. | The Admin login page renders but Keys, Connect, or Settings do not work; a save that shows success without the value actually taking effect; needing hidden YAML edits, a shell login, or a direct database edit to get there. Every advertised configurable setting needs a graphical control, not just the one exercised here. |

## Later: review, filing, finding

| Case / needs | What the captain does | What he should see | What counts as failure, including a false success |
|---|---|---|---|
| L1. Mark it reviewed | On the phone, set the note's `reviewed` property to true and add a review sentence. Reopen it and read its ID through Coppermind. | `reviewed: true` and the sentence both survive; source linkage and other properties stay intact. | The interface shows reviewed while the saved file still says false; an unrelated property or comment disappears during what should be a one-field change. |
| L2. Have it filed | Leave the control note unreviewed. Review UAT Lantern with its valid personal context. Then try a separate item whose filing information is incomplete or ambiguous. | The reviewed personal note moves from Review to its filed folder, including on the phone, keeping its ID, prose, and source link. The unreviewed control stays put. The ambiguous item stays visible in Review, marked unresolved with an explanation. | The note disappears instead of moving; a duplicate appears; the wrong customer is inferred; the unreviewed control files itself; an unresolved item vanishes or produces repeated notices. |
| L3. Find it later | Search for "cobalt lantern 731", open the result, and apply reviewed-only and unreviewed filters using the control note. Change the body to a new unique phrase and search again after catch-up. | The correct current note, path, snippet, and review state; the filters correctly distinguish the two items; the updated body becomes findable. | Claiming search passed because Obsidian's own in-app search found it; a stale result or a dead link; only titles are actually searched; generated source pages crowding out the real note. |
| L4. Keep working as files | Create a note on the phone, pause editing, and inspect its assigned ID. Move or rename it on the phone, then read that same ID. | The new note gets an ID after the quiet period; a move or rename keeps its identity and stays retrievable. | A new file that never becomes addressable; the same ID resolving to a different note; a move that permanently breaks lookup. |

## Interruptions the captain will actually encounter

| Case / readiness | What the captain does | Acceptable outcome | What counts as failure, including a false success |
|---|---|---|---|
| F1. Restart mid-work | Restart the acceptance installation while a submission or edit is in flight. After recovery, inspect the item before retrying; for ingest, retry the same external identity. | Acknowledged work survives. An uncertain result can be resolved without guessing whether to resubmit; a complete write converges to the same item on retry. Sync reconnects and history continues. | A truncated note; missing acknowledged work; a duplicate on retry; a perpetual "healthy" status next to an inaccessible file. |
| F2. Database unavailable | With the database stopped, try a submission, a read, and an edit to an existing phone note, then restore the database. | A clean 503 for anything needing the metadata mirror; notes on disk stay usable. Phone sync keeps working independently. No manual database repair is needed; changed, new, or moved files converge once the reconciler catches up. | A green readiness status next to a broken operation; an acknowledged write silently lost; a device edit overwritten by stale metadata; a database recovery that needs manual row repair. A 503 on a by-ID read is explicitly acceptable, not a failure. |
| F3. Storage unavailable | Make the acceptance notes storage unavailable or read-only. Try a new submission and an edit, then restore the storage. | A clear storage failure with no claim that an unsaved edit succeeded; existing content survives; once storage is back, previously acknowledged items open and new work succeeds. | An empty writable replacement directory mistaken for restored storage; files that vanish from devices; a retry that creates duplicates; a green status that only checked the process was running. |
| F4. Phone edit during downtime | Stop Coppermind, edit an existing note on the phone, create another note there, and restart. Separately, edit the same note differently on phone and laptop while disconnected, then reconnect. | The existing edit returns; the new note joins once reconciliation runs. Conflicting edits stay recoverable, and any conflict is visible enough to resolve without guessing which text was lost. | Startup replacing the device's newer content; duplicates; a silently lost sentence; a "synced" badge next to genuinely divergent text. |
| F5. Background work stops | Let a note change while Git is stopped, then restart it. Later, stop filing and search, mark another note reviewed on the phone, and restart them. | Git records the missed edit. Review stays available during the delay; filing and search catch up automatically once restarted. Status shows stale or error information where available. | A fresh heartbeat that hides a failed commit; a worker restart that leaves a permanent backlog; a note that gets filed repeatedly or lost. |
| F6. A source fails to become a note | Submit one invalid source request, then a valid source with no note body supplied. Provide an example that fails during body or projection generation, and follow its recovery path. | Invalid input is clearly refused and correctable. A missing body gets the deterministic generated note with no AI involved. For an actual generation failure: the original stays recoverable, the failure names the item, a retry does not duplicate or overwrite it, and success is never shown before usable output exists. | An empty note reported as a success; a source that disappears after being accepted; silent infinite retry; an unrelated later submission that stops working. A malformed-JSON rejection does not prove conversion-failure recovery. |

## Known limits to keep in mind while running this

- Graphical control coverage is incomplete by design at this stage: Admin's
  own pages for keys, connection, and settings are still landing, so the
  universal "every setting has a working GUI control" rule is not yet fully
  met. P5 should specifically look for a graphical control for every
  advertised setting, not just the one it exercises.
- Restart safety cannot be assumed from "writes are atomic." Creates write
  directly to the final filename rather than through the same stage-then-
  rename path a replace uses, so an abrupt failure during a create, or a
  database commit failure right after one, are still real risks worth
  exercising under F1, not settled by the atomic-write description alone.
- Conflict protection (`If-Match`) does not by itself rule out a
  simultaneous write arriving from Obsidian at the exact same moment; N2
  proves refusal after a phone edit has already landed, not protection
  against a truly concurrent one. F4 and its documented conflict behavior
  are where that limit should be checked.
- A source that fails to convert into readable content has no complete
  failure contract yet: deterministic note generation and the readable
  source page are proven, but a conversion failure's retry behavior and
  presentation are not. F6 exercises what preservation and honest failure
  look like; it does not assume they are already perfect.
- A working note is not automatically a navigable one: source links and
  search both depend on later pieces (P4, L2, L3) actually landing, and a
  renamed note's links are a documented, permanent gap rather than a bug to
  find.
- Backup instructions need to reflect wherever the live default credential
  actually lives at the time of the restore test, since losing that specific
  file separately from the rest of the backup can leave a live default
  unrecoverable.

Known open gaps that are tracked as issues, not acceptance failures:
concurrent creates of the same title can answer 409 instead of a numbered
suffix, and a stale mirror row can name the wrong note as unparseable.

## Extend only once these later milestones exist

| Milestone | What the captain checks | Pass / fail |
|---|---|---|
| Milestone 3, lab deployment | Open the actual Admin URL, use Keys and Connect, and repeat the earlier cases with the phone against the deployed release. | Real pages and content work through the deployed route; healthy pods next to a broken Admin page is a failure. |
| Milestone 4, restore | Follow the delivered restore runbook into a separate installation, then open a known note, its original source, and its earlier history; use a restored key, search, and round-trip a phone edit. | Identifiers, content, settings, usable access, and sync all survive. Matching counts alone, empty source artifacts, missing key access, or a requirement to restore a database backup all contradict the promised recovery path. |
| Milestone 4, attachments, trash, schema rename | Add an allowed attachment and open it on the phone; try one over the displayed plan limit. Trash a synthetic note. Rename a property through Admin's migration action. | The allowed attachment opens; the oversized one is clearly refused; the trashed note is recoverable through its documented location; the renamed property and its content stay usable on devices. |

Keep early sign-off scoped to the core path proven so far. Importing an
existing vault, any transport into Coppermind, semantic search, and a
history-and-restore interface all remain deliberately deferred (see
[roadmap.md](roadmap.md)); route anything found while running this plan
through the normal build process, not around it.
