# Local AI drafts and review

The Phase 2 workflow adds one-comment reply drafting, model review, editing, local human decisions, and one explicitly confirmed manual publish. Phase 3 adds three channel-scoped publishing modes described below. AI and publication verification in automated tests uses mocked providers and mocked YouTube operations; no live reply is posted during development or testing.

## Backend configuration

Set all six variables in the existing `backend/.env` without replacing its Google or database settings:

```dotenv
PRIMARY_LLM_BASE_URL=
PRIMARY_LLM_API_KEY=
PRIMARY_LLM_MODEL=
SECONDARY_LLM_BASE_URL=
SECONDARY_LLM_API_KEY=
SECONDARY_LLM_MODEL=
```

Supply your provider's actual API prefix, secret key, and exact model identifier for each role. The adapter appends `/chat/completions` to each base URL. No URL, key, or model is defaulted. URLs cannot contain credentials, query strings, or fragments. Keys remain backend-only and must never be configured as `VITE_*` variables. Both models must support chat completions and JSON-object responses. Separate role configuration permits different models/providers; independence here means separate requests and isolated inputs, not proof that models have independent training or failure modes.

Missing or invalid configuration leaves YouTube operational. Opening a comment's reply panel shows **AI setup required**, lists variable names only, and disables generation. Saved drafts remain accessible.

## Groq configuration

Groq's OpenAI-compatible endpoint works with the existing adapter. Use the API prefix below; the adapter appends `/chat/completions`. Create a key at `https://console.groq.com/keys` and put the same key in both API-key variables. If the models are not available to the account, the provider rejects them and the panel shows a safe error; do not substitute a model or provider silently.

```dotenv
PRIMARY_LLM_BASE_URL=https://api.groq.com/openai/v1
PRIMARY_LLM_API_KEY=
PRIMARY_LLM_MODEL=openai/gpt-oss-120b
SECONDARY_LLM_BASE_URL=https://api.groq.com/openai/v1
SECONDARY_LLM_API_KEY=
SECONDARY_LLM_MODEL=openai/gpt-oss-20b
```

Cost and abuse controls:

- Every request sends `max_completion_tokens=2048`. Reasoning tokens count against that limit, so the cap bounds spend per request. A response cut off at the limit fails as `incomplete_response` instead of being stored.
- Groq's Free Plan is metered by rate, not by local budget. Documented Free Plan limits are 30 requests/minute, 1,000 requests/day, 8,000 tokens/minute, and 200,000 tokens/day. Requests over the limit fail as `rate_limited` and are not retried, because retrying immediately cannot help.
- Spend limits are a paid-plan Groq feature. Keep them disabled.
- Reported token counts are stored per stage in `reply_drafts.token_usage` and shown in the panel. A count the provider did not report is stored as `null` and shown as "unknown"; it is never recorded as zero.
- `reply_drafts.token_usage` is a **per-stage snapshot of the most recent call, not a total**. Each stage has one slot: a later call to the same stage overwrites the earlier count, and saving an edit clears the stages it invalidated. The panel labels it "Latest step token usage" and says plainly that it is not a running total.
- Cumulative usage is a separate append-only ledger, `reply_draft_usage`, with one row per **HTTP request** actually issued, recording draft, revision, operation (`generate`/`regenerate`/`review`), stage, role, attempt number, model, nullable input/output counts, whether consumption was `reported` or `unknown`, and whether the result was `applied`. Migrations `20260927_03` and `20260927_04` add it. `GET /api/drafts/{draft_id}/usage` returns the rows, totals, and a coverage block; the panel shows them as a per-request list.
- The ledger tracks **provider consumption, not draft state**. Accounting commits in its own transaction, after the guarded draft update has been settled, so a request whose result a stale-revision guard rejects is still recorded, marked `applied: false` and shown as "discarded, not saved", with the attempted revision and operation token retained. The stale guard still fully protects the draft: a discarded operation never changes text, classifications, review status, or approval.
- A retry is a second real request, so it is a second ledger row. The adapter appends one attempt entry per HTTP request and exposes it via `attempts()`; a request that fails mid-flight can consume tokens even though its result is never used. The stage snapshot in `reply_drafts.token_usage` remains last-response-wins, but the ledger is per request.
- A step that reuses a stored result without calling the provider — a re-review that keeps an existing secondary classification because the provider configuration is unchanged — records no row, so reuse is never counted as a new request.
- `consumption` is `reported` when the provider returned counts and `unknown` when it returned nothing readable. An unknown request is stored with null counts, never zero, and the panel states that it cannot be said whether the provider processed or billed it. Absent counts are never estimated.
- Reported counts are captured when the response is decoded, before validation, so a response that arrived and was billed is still counted when it fails validation as `invalid_response` or `incomplete_response`.
- Accounting writes are idempotent. Only requests not already recorded are inserted, and a unique index on `(draft_id, operation_token, stage, attempt)` is the authority: re-running the same write, after a database error or by mistake, cannot insert a second row, so a persistence retry never becomes a second charge.
- **Coverage is reported, not assumed.** A draft is marked incomplete when it was created before tracking began, or when its stage snapshot reports usage that no recorded row accounts for. Incomplete drafts display exactly "Recorded usage since tracking began; earlier usage is unavailable." Their totals are never labelled lifetime totals, and no earlier count is invented.
- History is not backfilled. Drafts created before `20260927_03` keep only their stage snapshot, and the panel says so instead of implying the totals are complete. Per-operation counts for those earlier steps cannot be reconstructed and are not estimated.

### Known accounting limitations

These are real gaps, stated so the totals are not read as a billing statement:

- **Process failure before the usage commit loses that accounting.** Consumption is durable only once the accounting transaction commits. If the process is killed, the container is restarted, or the worker is replaced between the provider responding and the accounting commit, the request happened and is billed but no row is written, and nothing in the database can recover it. The window is the gap between the draft-state commit and the accounting commit. This is the one case where real spend can be missing from the ledger with no trace.
- **A discarded result is counted, but its content is not kept.** Only consumption is recorded for a stale operation, never the reply text or classifications it produced, so nothing discarded can leak into the draft.
- **Unknown is not zero and not a confirmed charge.** A request with no readable response may never have reached the provider, may have been served and billed, or may have been rejected before processing. The ledger records `unknown` and the panel says so; it never asserts which.
- **Coverage is detected, not proven.** A draft is marked incomplete when it predates tracking or its snapshot reports unbacked usage. A draft whose every stage call went unrecorded *and* whose snapshot is empty cannot be distinguished from a draft that made no calls, so a gap can still go unnoticed. A ledger truncated by database restore or migration rollback is likewise not detectable from application data.
- **Deleted drafts take their accounting with them.** The ledger cascades on draft deletion, so disconnecting the channel or deleting a draft removes its usage history along with the draft.
- **Only one provider's accounting is visible.** The ledger records what this app's adapter observed. Provider-side retries, internal routing, or billing adjustments made after the response are outside its view.
- No local model runs and no local GPU/CPU inference is added.

Restart the backend after changing `.env` (Uvicorn file reload does not reliably detect environment-file edits): stop its current terminal with Ctrl+C, then run:

```bash
cd ~/Projects/ReplyAI/backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

The frontend uses the existing API configuration. If it is not running:

```bash
cd ~/Projects/ReplyAI/frontend
npm run dev -- --host 127.0.0.1
```

## Shared classification schema

Sentiment and intent are distinct fields. Both classifiers use the same validated enum schema:

| Field | Labels and meanings |
| --- | --- |
| Sentiment | `positive`: favorable; `neutral`: no clear emotional polarity; `negative`: unfavorable; `mixed`: both favorable and unfavorable; `uncertain`: insufficient or ambiguous evidence |
| Intent | `praise`: appreciation; `question`: seeks information; `feedback`: observations or suggestions; `complaint`: dissatisfaction; `request`: asks the creator to act; `spam`: unsolicited promotion or irrelevant repetitive material; `other`: identifiable intent outside these labels; `uncertain`: unclear intent |

The primary request classifies the original comment and generates a reply. A separate secondary request classifies only the original comment: it sees neither primary labels nor the reply. A third request asks the secondary model to assess the reply against the original comment. No model gets tools, OAuth tokens, channel credentials, or posting capabilities. All comment/reply strings are untrusted data, placed in the user JSON separately from system instructions.

Comparison is deterministic backend code: missing classifications produce `pending`; either classifier using `uncertain` or `other` produces `uncertain`; identical sentiment and intent produce `agree`; otherwise `disagree`. A failed model stage leaves the overall comparison `pending`, retaining available classifications for inspection. Model agreement does not establish factual correctness or safety.

Reply-quality findings are separate from classification agreement. Relevance, unsupported claims, and inappropriate content each return `pass`, `concern`, or `uncertain`, plus findings. `pass` means the model identified no issue, not guaranteed safety. A completed review may contain concerns or uncertainty. Human decisions are always separate and never automatically approved. Approval requires a completed review for the current saved revision; the human can inspect concerns and decide. Rejection does not require a successful model review.

## Authorization, storage, and revision rules

Draft reads require the verified channel that owns the draft, through the same verified YouTube connection used for the original lookup. Mutations also require the existing Origin/CSRF validation. Draft creation and regeneration verify video ownership with YouTube read calls, then fetch authoritative comment text and verify its video, channel, and top-level status. The comment endpoints require the explicitly granted `youtube.force-ssl` scope; `youtube.readonly` alone is refused before the comment request. Client-supplied text/channel/ownership fields are rejected.

Migration `20260927_01` adds only `reply_drafts`; migration `20260927_02` adds `reply_drafts.token_usage` with a non-null empty-object default; migrations `20260927_03` and `20260927_04` add the `reply_draft_usage` ledger, cascading on draft deletion with no backfill, keyed uniquely per request attempt. A draft stores session/connection ownership, channel/video/comment identifiers, comment snapshot, reply text, revision, provider base URLs and model identifiers (no keys), classifications, comparison, quality findings, generation/review status, sanitized failure codes, per-stage reported token usage, human decision, and timestamps. One draft exists per channel/comment.

Migration `20260927_05` replaces session/connection ownership with durable channel ownership in `channel_owners`, keyed uniquely per channel and populated by verified OAuth only. Session expiry, logout, and connection deletion retain drafts. Reconnecting the same verified channel restores access; a different or unverified channel cannot see them. `DELETE /api/drafts` is the only destructive path and removes every draft for the channel, its usage ledger, and its refinement conversations. This is a channel-level archive, not a cross-channel account archive.

Saving an edit increases the revision, clears reply-quality findings and human decision, and sets review pending. Original-comment classifications are retained. Regeneration increases the revision and resets text, both classifications, comparison, quality findings, and human decision before calling models. Review of an edited draft uses its stored original-comment snapshot. If secondary configuration changes, its classification is rerun independently.

Revision checks and operation tokens prevent stale calls from updating newer drafts. Duplicate creation is blocked by a database uniqueness constraint. A second concurrent generation/review is rejected; the UI also guards repeated clicks. Edits may invalidate an in-flight operation. Provider calls happen after committing the operation claim, without holding database write locks. Interrupted operations remain explicitly pending; after six minutes review/regeneration can reclaim them. Use **Reload saved state** after a network interruption, conflict, or revisiting an in-progress draft.

## Channel reply preferences

Tone (`Professional`, `Friendly`, `Casual`), reply length (`Short`, `Medium`, `Detailed`), language (`English`, `Hindi`, `Hinglish`), and optional channel instructions (up to 2,000 characters) are stored in `reply_preferences`, keyed by the durable verified `channel_owners` row. Migration `20261008_01` adds the table. `GET /api/preferences` returns saved values or defaults without creating a row; CSRF-protected `PUT /api/preferences` uses an optimistic revision check and rejects stale updates. Preferences survive disconnect/reconnect to the same verified channel and are not available to another channel owner.

New drafts and regenerated drafts send the selected comment, video ID, verified channel metadata, and the preferences to the primary generation model. Refinement uses the same saved preferences together with the existing draft and bounded conversation context. Channel instructions are lower-priority style guidance, not application rules; they cannot bypass safety, human review, or publishing safeguards. A one-off refinement instruction takes priority over saved style preferences when they conflict. Secondary classification and quality review remain independent of preferences, and changing preferences never rewrites an existing saved draft.

The adapter uses existing `requests`, a worker thread, 5-second connection/35-second read timeouts, two attempts total for transient errors, and a 95-second async deadline per model stage. It rejects redirects, oversized responses, incomplete completions, tool calls, refusals, malformed JSON, extra fields, and invalid labels. Reasoning content, which Groq returns outside `message.content`, is never validated or stored. HTTP 429 maps to `rate_limited` and is not retried. Token usage is recorded from the provider's own `usage` field, and a stage that fails records no usage. Provider bodies and raw exceptions are not shown or logged. Partial completion preserves available results with failed review and safe stage/error codes.

## Endpoints

All paths start with `/api/drafts`:

- `GET /setup`: names of missing/invalid settings, never keys.
- `GET ?video_id=...&comment_id=...`: saved draft for that comment, or null.
- `GET /{id}`: owning session's saved draft.
- `POST /`: authoritative lookup, generation, independent classification, quality review.
- `POST /{id}/edit`: save text with expected revision.
- `POST /{id}/regenerate`: generate a new revision with expected revision.
- `POST /{id}/review`: review saved text with expected revision.
- `POST /{id}/decision`: save `approved`/`rejected` locally with expected revision.
- `DELETE /`: permanently delete every draft for the channel, including conversations and usage.
- `GET /{id}/conversation`: persisted refinement turns, newest first, no model call.
- `POST /{id}/refine`: produce one suggestion from one instruction, saved separately.
- `POST /{id}/refine/{turn}/apply`: save a suggestion as a new revision.
- `POST /{id}/refine/{turn}/discard`: discard a suggestion, leaving the draft untouched.
- `GET /{id}/publication`: current posting state and eligibility, never a write and never a provider call.
- `POST /{id}/publication`: post one reply, with `revision` and `text` as confirmation only.

## Reply refinement by conversation

Refinement is a separate, persisted conversation over the saved draft. Each turn stores the instruction, the full suggested reply, the revision it was written against, and a status: `suggested`, `applied`, `unchanged`, `discarded`, or `stale`. The UI loads history from the server on open, so the conversation survives a refresh and a same-channel reconnection, and the client never fabricates or resends model history.

Migration `20260927_06` adds `refinement_turns`, owned by `draft_id` with `ON DELETE CASCADE` and unique per operation token. Authorization is the draft's channel owner, so turns inherit the same verified-connection checks and Origin/CSRF validation as the draft itself. Comment, video, and creator fields are data the user cannot edit, so the model is never asked to change them; nothing here can post, edit, approve, or check anything on YouTube.

Each submission makes one primary-model call. The provider's own internal retries remain separate ledger attempts. Refinement text is untrusted: the comment and saved reply travel as JSON data values and are never spliced into an instruction or a system directive. The instruction is limited to 500 characters, the suggestion to 5000. Bounded context uses at most the six most recent usable turns, each clipped to 600 characters, and excluded turns are never charged. Instructions are preserved in full; only outgoing context is clipped. Discarded turns are retained and sent as context, because a user may change their mind.

Generating a suggestion never modifies the saved draft, its revision, approval, review, or classifications. **Apply suggestion** requires the expected revision and an idle draft, then saves the text as a new revision and clears approval, review, and quality findings in one transaction, reusing the existing edit concurrency controls. It makes no model call, so use **Review edited draft** afterwards. An identical suggestion records `unchanged` and saves nothing. A suggestion is recorded as `stale` when the draft moved during the model call; it is still shown, but cannot be applied. Supported provider failures surface sanitized `rate_limited`, `timeout`, `transient_error`, and `invalid_output` codes with a 502 fallback, and leave the saved draft untouched.

Ledger rows use `operation="refine"` and `stage="refinement"`. The UI disables refining while the draft is dirty, so instructions always describe the reply the user can see. A separate **Refine the reply by conversation** panel appears next to each saved draft.

## Posting one reply to YouTube

Publishing is manual, singular, and deliberately awkward to trigger by accident. A draft becomes postable only when the saved text is non-blank, the reply-quality review for the current revision is complete, all three quality checks are `pass`, the human decision is `approved`, the connected channel still owns the video and comment, and the grant includes `youtube.force-ssl`. A `disagree` comparison between the classifiers remains visible, but it does not replace the separate human approval required by manual publishing.

**Post reply to YouTube…** opens a preview naming the connected channel, the video, the comment, the saved revision, and the exact text. It also states that the reply is public and immediate. Opening the preview and choosing **Cancel** send nothing at all. **Confirm & post** sends `revision` and `text`, but only as proof that the dialog matched what is saved; the server reads the text, approval, review, and ownership itself and rejects a mismatch, so a draft edited in another tab cannot be published by an old confirmation.

The write uses `comments.insert` with `part="snippet"` and a `parentId` equal to the top-level comment, which creates one reply inside the existing thread. It is a dedicated authenticated client that never retries the request and never retries it after refreshing an access token, because a replayed insert is indistinguishable from a user wanting two identical replies. YouTube documents no idempotency key, so local storage is the only duplicate protection.

Migration `20260927_07` adds `reply_publications` and `reply_drafts.publication_token`. The `publication_token` is separate from the review `operation_token` so an edit can still supersede an in-flight review, while a draft claimed for posting cannot have its publish source changed underneath an attempt that may already be public. A partial unique index on `sending`, `unknown`, and `posted` allows at most one such attempt per draft, enforced by the database rather than by a read-then-write check, so two simultaneous confirms cannot both pass. A `failed` attempt is not covered by the index and therefore may be retried deliberately.

Every attempt is committed before the request is sent, so an interrupted process leaves evidence. Outcomes:

- `posted` stores YouTube's new reply comment ID. The same revision and text replays to the stored success, and editing the draft does not unlock a second post.
- `failed` is used only for reasons Google's documentation lists as definitive refusals, for example replies closed, parent comment unavailable, or reply too long. Nothing was created, so a deliberate retry is allowed.
- `unknown` is used for everything else that might have succeeded: timeouts, dropped connections, HTTP 5xx, `processingFailure`, unlisted reasons, and a response with no reply ID. No retry is offered, because the reply may already be public; the UI tells the user to check the video's replies.

There is no automatic retry, no background job, no scheduler, and no batch posting. A posted or unknown outcome blocks further publishing for that draft, and deleting retained work is refused while an attempt is unresolved.

Migration `20260927_08` adds `published_reply_guards`, because the protection above ends at the draft: deleting a posted draft cascaded away the record that a reply existed. One row per `(channel_id, parent_comment_id)`, with no foreign key and no reply text, keeps that fact after deletion. A second draft for the same comment can still be created and approved, but publishing it is refused with `already_replied` and nothing is sent to YouTube. The reservation is made before the request leaves, so concurrent requests for one comment race on the unique index instead of on a read-then-write check, and a `failed` attempt releases it so a refusal never becomes a permanent block. See [RETENTION.md](RETENTION.md).

## Test one real comment

1. Configure both providers and restart the backend. Open http://127.0.0.1:5173 in the browser session connected to YouTube.
2. Select a video, load its comments, and select **Open reply draft** for one comment.
3. Select **Generate draft**. This sends the selected comment to the configured providers. Inspect both sentiment/intent results, their comparison, and separate reply-quality findings.
4. Edit the reply and select **Save changes**. Confirm quality review and human decision reset. Select **Review edited draft**.
5. Select **Approve draft locally** or **Reject draft locally**. Neither posts anything. Revisit the comment to confirm the draft and decision reload.
6. Select **Regenerate** to confirm a new revision resets the previous approval. Provider errors must show failed/pending states, never automatic approval.
7. Open **Refine the reply by conversation** and send an instruction such as "make it warmer". Confirm the saved revision, approval, and review did not change. Refresh the page and confirm the turn reloads.
8. Edit the reply manually and confirm refining is disabled until the edit is saved or discarded. **Apply suggestion** and confirm the revision increases and the approval is cleared; select **Review edited draft**. Discard a second suggestion and confirm the draft is untouched.
9. Do not select **Confirm & post** during manual verification. No live YouTube reply should be sent.

## Phase 3 publishing modes

`publishing_settings` stores a channel owner's mode and optimistic revision. Migration `20261009_01` creates it; a missing row reads as `manual_review`. The table also stores the automatic-send rate window and count. A mode update is CSRF-protected, resolves the channel only from the authenticated, OAuth-verified connection, validates the mode on the server, and rejects stale revisions. Enabling or changing to either automatic mode requires an explicit confirmation value from the UI.

- **Manual review** is the default. Generation never publishes. The existing path still requires a locally approved saved revision and an explicit publish confirmation that includes the channel, target, revision, and exact reply text.
- **Dry Run** applies the automatic eligibility checks to a newly generated revision and writes a `draft_evaluations` decision only. It creates no `reply_publications` row, publication reservation, or duplicate guard, and never calls `comments.insert`. It may validate or refresh credentials while evaluating a draft that otherwise qualifies; this still cannot create a public reply.
- **Auto-publish on agreement** considers only a just-generated or explicitly regenerated revision. Triage must classify the comment as `on_topic`; `generic_engagement` and every other category stay for human review. The primary generation classification and independent secondary classification must have identical sentiment and intent, with neither returning `uncertain` or `other`. The secondary review must complete and return `pass` for relevance, unsupported claims, and inappropriate content; confidence must be at least `0.80`; findings must be empty. Disagreement, missing/error/uncertain review, low confidence, or findings leaves the reply for human review.
- **Approve all** also considers only a just-generated or explicitly regenerated revision and requires the same on-topic triage, complete all-pass review, at-least-`0.80` confidence, and no findings. It does not waive classifier agreement: disagreement always leaves the reply for human review. It does not mean every draft is eligible.

The review model now returns a structured `confidence` score. Automatic eligibility uses a fixed threshold of `0.80`; this is a model-reported score, not a calibrated probability or a guarantee. Classification agreement is semantic at the sentiment/intent field level, not text matching between the reply and comment.

Both automatic modes are further limited to ten send attempts per channel in a one-hour window. Before sending, the backend rechecks the current mode, verified channel, `youtube.force-ssl` scope, refreshed credentials, draft revision and decision, quality/review criteria, and durable duplicate guards. An automatic mode change does not invoke a job, scan drafts, or publish saved work. Edits, re-reviews, refinement, settings updates, and existing drafts do not trigger automation. Regeneration is an explicit new generation and can be considered under the current mode.

The publishing mode and its revision are snapshotted before generation starts. If settings change while the models are working, that generation is not eligible for automation, even if automation is enabled before its response returns. The durable publication claim is committed before the YouTube request. A concurrent mode switch detected before the insert cancels that attempt. A switch racing after the final server-side check cannot recall a request already in flight; the setting change applies to subsequent decisions. Explicit provider refusals remain `failed`; a timeout, server error, missing reply ID, or failed persistence after a possible YouTube insert remains `unknown` and is never retried automatically. Every actual insert uses the existing no-retry YouTube call and per-channel/per-comment guard; automatic publishing does not bypass ownership, permission, rate-limit, publication-history, or duplicate checks.

For a safe manual verification without posting anything: keep **Manual review** selected except while exercising **Dry Run**; generate, edit, regenerate, refine, review, and inspect saved drafts; verify that generation and settings changes create no publication row; and confirm Dry Run records only its evaluation. Do not enable automatic publishing on a real channel or confirm a real YouTube post as part of verification.

There are no scheduled jobs, background workers, bulk operations, or automatic processing of a backlog.
