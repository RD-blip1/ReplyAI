# ReplyAI Development Log

## Phase 1: Local Development Foundation

### Implemented

- Created the `feature/project-foundation` branch.
- Added a React + TypeScript Vite frontend with Tailwind CSS and a live local-service status page.
- Added a FastAPI backend with environment-based, secret-typed PostgreSQL and Redis configuration.
- Added a SQLAlchemy declarative base and Alembic migration scaffold; no domain tables or revisions are defined yet.
- Added `/health` liveness and `/ready` PostgreSQL/Redis dependency checks without returning connection details.
- Added localhost-bound Compose services with health checks and persistent PostgreSQL/Redis volumes.
- Added environment examples, ignore rules, backend health tests, and setup/project documentation.
- Explicitly marked YouTube, AI, review, and publishing features as unavailable; no sample channel or comment data is shown.

### Verification

- `frontend`: `npm run build` and `npm run lint` passed.
- `backend`: `pytest` passed (3 tests); `ruff check .` passed. Pytest reports one upstream Starlette deprecation warning about using `httpx` with its test client.
- `docker compose config --quiet` passed. PostgreSQL and Redis containers both reached `healthy`, with ports bound to `127.0.0.1`.
- Alembic's online CLI configuration loaded against the local PostgreSQL service without applying a migration.
- Live `GET /health` returned HTTP 200 with `{"status":"ok"}`. Live `GET /ready` returned HTTP 200 with both dependencies `available`.
- The frontend loaded in the browser, displayed all three live checks as available, and showed the deferred features as unavailable. At 320px viewport width, no horizontal overflow was detected.
- Port 8000 was already occupied by another local process. ReplyAI's documented API port is 8001; the existing process was not changed.
- PostgreSQL and Redis remain running in Compose. The API and Vite development servers remain available on ports 8001 and 5173.

### Deferred

OAuth, YouTube API access, LLM integrations, LangGraph, comment workflows, publishing, domain models, and migration revisions remain outside this phase.

## Phase 2: YouTube Read-Only Workspace

### Implemented

- Created `feature/youtube-readonly` from the committed Phase 1 foundation while retaining its existing untracked Vite starter files.
- Added Google-maintained `google-auth-oauthlib` Flow with an initial YouTube read-only scope plus an explicit, CSRF-protected `youtube.force-ssl` permission-upgrade flow, PKCE S256, ten-minute single-use state bound to the opaque browser session, and the registered callback URI.
- Added server-side opaque sessions, HttpOnly SameSite=Lax cookies, Origin/CSRF validation, callback query redaction before access logging, separately keyed Fernet token encryption, refresh-token preservation, disconnect/revocation, and logout cleanup.
- Added only the app-session and YouTube-connection models plus an additive Alembic revision.
- Added per-session channel, uploads-playlist video, and paginated top-level comment endpoints with membership validation and mapped YouTube errors.
- Replaced the status-first UI with a personalized workspace; it displays live API data only, and keeps AI generation/publishing explicitly unavailable.
- Added a pre-consent disclosure for the broader Google `youtube.force-ssl` grant. Application code still makes only YouTube list/read requests; no post/edit/delete/moderation calls were added.

### Failure Diagnosis

- Failing frontend request: `GET /api/youtube/videos/{video_id}/comments`.
- Backend first verifies ownership with Google `playlistItems.list` using the connected channel's uploads playlist and selected video ID. Google's public discovery schema allows `youtube.readonly` for this method.
- After ownership succeeds, the backend calls Google `commentThreads.list`. Google's public discovery schema lists only `youtube.force-ssl` for this method, not `youtube.readonly`.
- The expected/synthetic provider result for the current scope is HTTP 403, reason `insufficientPermissions`, safe meaning “Google does not allow comment access with the granted YouTube read-only scope.” This is distinct from an HTTP 401 revoked/invalid credential. The previous mapper misclassified `insufficientPermissions` as reconnect-required, causing repeat reconnect attempts.
- The shared browser available during diagnosis had no connected session, so no live Google status or error reason was observed. The 403 mapping and route behavior are covered with a synthetic `HttpError`; no Google login/consent was performed.
- The fix returns 403 `insufficient_scope`, keeps the valid channel connection intact, and presents the safe message. Initial Connect stays `youtube.readonly`; the separate permission update described below requests `youtube.force-ssl` explicitly. Ownership validation remains enabled.

### Permission Upgrade

- Added `/auth/google/upgrade`, available only to a session that owns an existing connection and passes CSRF validation. It requests both the existing readonly scope and `youtube.force-ssl` with `prompt=consent` and PKCE.
- The callback validates the token response scopes before constructing credentials or updating encrypted tokens/scopes, including OAuthlib’s raised scope-change warning. Missing/denied scopes preserve the old connection and return distinct frontend states. Refresh tokens remain preserved if Google omits a new one.
- `/api/session` reports whether the comment scope is present. The UI exposes **Update YouTube permissions** and explains Google's documented view/edit/permanent-delete capability before redirecting. The functional app remains read-only.
- Ordinary reconnect preserves the already-granted known scopes; first-time Connect remains limited to `youtube.readonly`.

### Verification

- Mocked OAuth and YouTube tests cover setup-required behavior, CSRF/state rejection, expiry and replay, PKCE/scope construction, session isolation, encrypted tokens, missing-refresh-token preservation, access-token refresh, revoked refresh, disconnect cleanup, video ownership, pagination, comments-disabled/quota/unavailable/insufficient-scope/revoked errors, production cookie flags, and OAuth callback query scrubbing.
- `backend`: `pytest` passed (29 tests); the final focused OAuth rerun passed 20 tests after adding one parameter case (30 total cases across the verified suite); `ruff check .` passed. One upstream Starlette deprecation warning concerns its `httpx` test client.
- `frontend`: `npm run build` and `npm run lint` passed.
- Alembic upgrade/downgrade/re-upgrade passed on disposable SQLite. The existing Compose PostgreSQL database is now at revision `20260926_01`; no old tables or volumes were dropped.
- Compose validation passed; PostgreSQL/Redis report healthy. Live `/health` and `/ready` returned HTTP 200 and reported both dependencies available.
- Session bootstrap reports OAuth setup required without exposing configured paths or secrets. Browser layout has no horizontal overflow at 1280px or 320px; no mock channel data is shown.
- Cookie check confirmed HttpOnly and SameSite=Lax in development; credentialed CORS preflight returned HTTP 200. The production Secure flag is enabled when `APP_ENVIRONMENT=production`.
- No live Google sign-in or real YouTube API request was performed during this diagnosis; the shared browser session was disconnected. The user's client file/key contents were not accessed or requested.

### Live Verification Required

Credentials are already configured locally. In the same browser session, refresh http://127.0.0.1:5173, select **Update YouTube permissions**, review the broader grant, and complete Google consent using the existing account/channel. After returning, select an upload and verify comments and pagination. If disconnected, first use **Connect YouTube**. Google sign-in and consent remain user-operated.

## Interrupted Permission Upgrade Completion (2026-09-26)

- Inspected branch `feature/youtube-readonly` at Phase 1 checkpoint `5ee13a7`. No applicable AGENTS.md was present in the repository or ancestor directories. Preserved existing tracked edits and untracked Phase 2/starter files; no reset, commit, push, migration, or infrastructure rebuild.
- Already present: explicit upgrade route and disclosure, encrypted scope/PKCE intent, callback grant validation, session scope reporting, readonly comment gate, and mocked upgrade tests.
- Finished: handled OAuthlib's raised scope-change warning without global scope relaxation or logging its token payload. Partial/missing grants preserve the stored connection. Real HTTP-response parser tests cover partial, missing, full, and wrong-channel consent, replay, and refresh-token preservation.
- Permission updates must retain the connected channel; a different channel returns `channel_mismatch`. Refresh tokens are reused only for the same channel. Callback state consumption locks the session row on PostgreSQL and also rejects expired sessions.
- Removed stale insufficient-permission wording. Provider-side insufficient-scope errors expose the explicit update action even when stored scopes claim access; the upgrade endpoint permits this retry. No YouTube write methods were introduced.
- Repaired the ownership regression to perform the permission upgrade before checking an unrelated video. Both the local scope gate and provider-denied scope path are covered.
- Current checks: backend full pytest 29 passed, followed by a final focused OAuth run with 20 passed after adding one parameter case (30 total verified cases; one upstream Starlette/httpx deprecation warning), Ruff passed; frontend production build and lint passed. Provider calls are synthetic; live Google consent/comment retrieval remains for the user. Prior migration and browser-layout results above are historical and were not rerun for this completion.
- Runtime: confirmed the existing ReplyAI Uvicorn reload supervisor runs from this backend directory on `127.0.0.1:8001`. Auto-reload loaded edits; no manual process stop/restart was needed. Live `/health` and `/ready` passed with PostgreSQL and Redis available. No credentials, cookies, or stored tokens were printed.

## Groq-Only Cloud Draft Configuration (2026-09-27)

- Preserved branch `feature/youtube-readonly` and all existing tracked/untracked work. No commit, push, publish, or local inference was added. No applicable AGENTS.md exists in the repository or ancestors; `../KapexAI/AGENTS.md` belongs to a different project.
- Consulted only official Groq documentation (models, reasoning, structured outputs, OpenAI compatibility, errors, rate limits, spend limits). Confirmed `openai/gpt-oss-120b` and `openai/gpt-oss-20b` are currently documented production models supporting JSON-object responses.
- Confirmed the existing adapter is compatible: the base URL is the API prefix and the adapter appends `/chat/completions`. Groq returns reasoning outside `message.content`; only `content` is validated and stored, so reasoning is never persisted. Chose JSON Object Mode with strict Pydantic validation over strict JSON Schema, because Groq's JSON Schema mode is incompatible with reasoning models and requires the exact `response_format` envelope.
- Cost/abuse controls: every request now sends `max_completion_tokens=2048` (reasoning tokens count against it); HTTP 429 maps to a non-retryable `rate_limited` code; a response truncated at the cap fails as `incomplete_response` rather than being stored; provider-reported usage is recorded per stage and a missing count is stored as `null`, never zero. No local model or spend-limit feature was added.
- Migration `20260927_02` adds non-null `reply_drafts.token_usage` with a server default of `{}`. Upgrade/downgrade/re-upgrade passed on disposable SQLite, and the Compose PostgreSQL database is now at `20260927_02`; no tables or volumes were dropped.
- Usage follows stored results: create records generation/classification/quality; an edit keeps only classification because the text was replaced; review re-retains generation and classification and adds quality; regeneration resets to all three; a failed stage records no usage. Surfaced in the panel as a collapsible summary, with `null` rendered as "unknown".
- DraftPanel now maps every sanitized failure code to a human sentence, so a rate limit explains the cause and the retry action instead of showing a raw code.
- `backend/.env` is git-ignored and stayed backend-only. The six Groq variables are present with the documented base URLs and model identifiers and empty API-key fields, ready for the user's key.
- Incident: a scripted `.env` edit dropped every pre-existing line. Recovery was completed with no data loss, and no secret was ever printed to the terminal, logs, or this file.
- Recovery source: VS Code Server local history (`~/.vscode-server/data/User/History/7ba0db83/`) holds two saves of `backend/.env` from 2026-09-26 21:32 and 21:36. The 21:36 save is the last state before the damage and was restored verbatim, followed by re-appending the Groq block.
- The restored file reproduced every reconstructed non-secret value exactly (`DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, `GOOGLE_OAUTH_CLIENT_SECRETS_FILE`, `FRONTEND_BASE_URL`, `APP_ENVIRONMENT`), which independently confirms the earlier reconstruction. Only `TOKEN_ENCRYPTION_KEY` differed, and the original 44-character value came back.
- The recovered key was verified functionally, not by configuration parsing: `app.security.decrypt_tokens` decrypts the stored `youtube_connections.encrypted_tokens` row and yields the expected `access_token`/`refresh_token`/`expiry` fields. The existing YouTube connection therefore needs no re-authorization. This mattered because a wrong 32-byte key raises `InvalidToken`, so successful decryption is proof of the correct key.
- `.env` is mode 600 and still git-ignored. Temporary copies and the memory-scan helper were deleted. Data is intact: 1 YouTube connection, 0 drafts, 9 sessions, Alembic `20260927_02`.
- One follow-up requirement: the live Uvicorn worker (PID 69416, started 14:09) predates the repaired `.env` and holds no encryption key in memory, because it loaded the file while it was broken. The backend must be restarted before the running app can use the recovered credentials. This restart was deliberately not performed.
- Verification: `pytest` passed 85 tests (74 prior, 11 added for token bounds, usage shapes, per-stage retention, and rate limiting); `ruff check .` passed. `frontend`: `npm run build` and `npm run lint` passed. Migration head `20260927_02` confirmed. Live `/health` and `/ready` returned HTTP 200 with PostgreSQL and Redis available, and Google OAuth configuration loads. No live model request was performed; a real key is not yet present.

### Live Verification Required

The Groq API-key fields are intentionally empty, and the backend still needs the restart noted above. The user must create a key at `https://console.groq.com/keys` and save it locally in `backend/.env` in both `PRIMARY_LLM_API_KEY` and `SECONDARY_LLM_API_KEY`; the key must never be pasted into chat. Restart only the backend (Ctrl+C, then `alembic upgrade head` and `uvicorn app.main:app --reload --host 127.0.0.1 --port 8001`). Then verify exactly one user-triggered draft on one real comment, confirm both classifications, agreement, quality findings, and reported token counts, and confirm the YouTube connection still reports read-only behavior. Draft approval remains local and never publishes.

## Comment Lookup Diagnosis After First Live Draft Attempt (2026-09-27)

- Symptom: the first browser attempt on video `KynQ5qkE7Ik` returned HTTP 404 with "Comment is unavailable for this video." No model call occurred. `snapshot()` performs the YouTube lookup at `POST /api/drafts` line 223, while `run_models` is only reached at line 235, so the generation/classification/quality stages were never entered. The Groq key, models, and rate limit were not implicated.
- Ownership validation was not weakened. All existing checks remain: the video must be in the connected channel's uploads playlist, the returned comment id must equal the requested id, `snippet.videoId` must match, `snippet.channelId` must equal the connected channel, and the comment must be top-level.
- Hypothesis "the UI sends `commentThread.id` where the backend wants `topLevelComment.id`" was tested and **rejected**. `list_top_level_comments` already returns `snippet.topLevelComment.id`, `App.tsx` passes `comment.comment_id` through unchanged, and the failing id in the log was a real top-level `Ugx…` comment id. Regression tests now use distinct `Ugz` thread, `Ugx` top-level, and `Ugy` reply constants so the distinction cannot silently regress.
- A second log entry used a `Ugy…` id, which is a reply. The comment list never produces one, so it did not come from the UI; replies are rejected by design. The UI now says so explicitly instead of showing a generic error.
- A second hypothesis, that `snippet.channelId` is the commenter's channel, was also tested and **rejected against the official API reference**: for a video comment, `CommentSnippet.channelId` is the video's channel, while the author is `snippet.authorChannelId.value`. The existing comparison is correct, and the test fixtures now model a viewer whose channel differs from the owner so this cannot be "fixed" into a regression.
- Real defect found: the text extraction was `snippet.get("textOriginal", snippet.get("textDisplay"))`. When `textOriginal` is present but null, empty, or whitespace, the fallback never runs, so a perfectly readable comment was rejected with the same generic 404. `comment_text()` now returns the first non-blank of `textOriginal`/`textDisplay` and `None` only when neither is usable.
- Error handling now names each rejection separately: `video_unavailable`, `comment_not_returned`, `comment_id_mismatch`, `comment_video_mismatch`, `comment_channel_mismatch`, `comment_is_reply`, and `comment_text_unavailable`. The panel shows the safe message and appends the code, so one user-triggered attempt identifies the exact cause instead of a single ambiguous message. No comment text, token, key, or authorization header is logged.
- Verification with mocked providers only: `pytest` passed 100 tests (85 before, 15 added covering top-level id plumbing, viewer-not-owner acceptance, null/blank `textOriginal` fallback, each named rejection, and the pre-lookup video ownership gate); `ruff check .` passed; frontend `npm run build` and `npm run lint` passed. The reloader picked up the change in worker PID 103325 at 15:56, so no manual restart was needed. `reply_drafts` still holds 0 rows and the single YouTube connection is intact, so no stale saved state needed clearing and nothing unrelated was touched.
- If the next attempt reports `comment_not_returned`, the comment was genuinely not returned to this account and the behavior is correct; testing a different top-level comment is the right move rather than changing code.

### Live Verification Required

Reload the frontend dev server page, pick a different top-level comment, and click **Generate draft**. The Groq key and models were already accepted by `/api/drafts/setup`, and this remains the first live provider call.

## False Video-Mismatch on Comment Lookup (2026-09-27)

- Symptom: the top-level comment "Poor quality" on video `KynQ5qkE7Ik` failed with `comment_video_mismatch`. No LLM call occurred; the failure was still pre-provider, and no draft row was created.
- Values traced end to end from the request log: the comments list was fetched with `GET /api/youtube/videos/KynQ5qkE7Ik/comments`, the panel and the draft request both used `video_id=KynQ5qkE7Ik` and `comment_id=Ugx-jkGM0zGHyywBZoh4AaABAg`. The selected video id, the list-fetch video id, and the id sent to the draft endpoint all agreed, so there was no stale frontend state and no playlist-item versus `contentDetails.videoId` confusion.
- Actual mismatch, from a sanitized read-only lookup through the existing integration: the `comments.list` item came back with `id` equal to the requested id, `channelId` equal to the connected channel, no `parentId`, and a populated `textOriginal`/`textDisplay` — but **no `videoId` key at all**. Returned snippet keys were `authorChannelId, authorChannelUrl, authorDisplayName, authorProfileImageUrl, canRate, channelId, likeCount, publishedAt, textDisplay, textOriginal, updatedAt, viewerRating`. The code compared that absent field to the requested id, so `None != video_id` produced a false ownership mismatch. The official `comments.list` reference documents the `id` filter and its error table but does not promise `videoId`, so the absence was treated as unverified rather than as proof of mismatch.
- The earlier `textOriginal`/`textDisplay` defect was a real but separate bug; the visible message was this false mismatch, which is why it appeared first.
- Fix: the video binding now comes from `commentThreads.list(part=snippet, id=...)`, whose `CommentThreadSnippet.videoId` is always returned, and a thread whose `topLevelComment.id` equals the requested id additionally proves the comment is top-level. `comments.list` still supplies the authoritative text, `channelId`, and `parentId`. A missing or blank thread `videoId` now raises the distinct `comment_video_unverifiable` instead of being reported as a mismatch, so a missing field is never treated as proven ownership failure.
- Reply status is decided only by `parentId` and by the thread's `topLevelComment` identity, never by an ID prefix. This corrected an earlier wrong assumption of mine: a read-only `commentThreads.list` on `UgyopqfWzHY_0yhqCU54AaABAg` returned it as a thread's top-level comment, so that `Ugy…` id is an ordinary top-level comment and is now accepted.
- Ownership validation is unchanged and not bypassed: the video must still be in the connected channel's uploads playlist, the thread's `videoId` must equal the requested video, the comment's `channelId` must equal the connected channel, and the lookup is rejected before any model call.
- Regression coverage uses distinct identifiers — playlist item `PL-item-id-not-a-video-id`, video `KynQ5qkE7Ik` vs `OtherVideo1111`, thread `Ugz-threadId0000000000000`, comment `Ugx-…` and `Ugy-…` — and reproduces the real failure: a `comments.list` payload with no `videoId` must still be accepted. Also covered: thread video mismatch, absent thread `videoId`, non-top-level thread, prefix-independence for `Ugy…`, `parentId` reply rejection, channel mismatch, blank `textOriginal` fallback, and the video-ownership gate running before any comment lookup. `pytest` passed 102 tests (100 before, plus the new cases); `ruff check .` passed; frontend `npm run build` and `npm run lint` passed.
- Read-only verification against the live channel, with no LLM calls: "Poor quality" and the second comment were both accepted with non-empty text, and a deliberately wrong video id was still rejected as `video_unavailable`. Diagnostics printed only ids, key presence, and equality results. `reply_drafts` remains empty and the single YouTube connection is untouched, so no stale state needed clearing.
- Worker PID 108434 picked up the change at 16:03; no manual restart was needed. `/health` and `/ready` both returned 200.

### Live Verification Required

Reload the page and click **Generate draft** on "Poor quality". This remains the first live Groq call; the key and both models were already accepted by `/api/drafts/setup`.

## Live Workflow Verified End to End (2026-09-27)

- The pending live verification from the previous entry is complete. The user ran the full workflow in the browser against real Groq models and it succeeded: **Generate draft** created the draft, **Regenerate** produced a second revision, **Save changes** applied an edit, **Review edited draft** re-reviewed the edited text, **Approve draft locally** recorded the decision, and **Reload saved state** after a refresh returned the same revision, text, review, and decision. Nothing was posted to YouTube.
- This also confirms the earlier pre-provider lookup fixes against the real channel: the comment was accepted, and the draft survived reload, so comment ownership, video binding, and persistence all work outside the mocked suite.
- Provider-reported usage observed on the real draft, stored in `reply_drafts.token_usage`:
  - Revision 1 (initial generate): generation `openai/gpt-oss-120b` 391 in / 140 out, classification `openai/gpt-oss-20b` 298 / 92, quality `openai/gpt-oss-20b` 404 / 103.
  - Revision 3 (after regenerate, then edit, then re-review): classification `openai/gpt-oss-20b` 296 / 66, quality `openai/gpt-oss-20b` 399 / 189, and **no generation entry**.
- Two live drafts existed locally at that point, both owned by the session and connection of the time. A later entry in this log records that they are no longer present and what is and is not known about why; the figures above are a record of what was observed then, not current state. Ownership has since moved to the channel, so the same cascade can no longer reach a draft.

## Token Usage Was Being Overwritten, Not Accumulated (2026-09-27)

- Finding: usage was not preserved across operations. `reply_drafts.token_usage` holds one mutable slot per stage, so every later call to a stage overwrote the earlier count, and saving an edit deliberately cleared the `generation` and `quality` slots because the stored text was no longer what those stages had seen. Revision 3 above shows the consequence: its generation count was destroyed by the later edit, and the pre-edit classification and quality counts were overwritten in place. Cumulative spend per comment was unrecoverable.
- The clear was semantically correct for a *current state* view — after an edit, the old generation no longer describes the saved text — so the snapshot was kept and a separate append-only ledger, `reply_draft_usage`, was added alongside it (migration `20260927_03`). Each row records draft, revision, operation (`generate`/`regenerate`/`review`), stage, role, model, nullable input/output counts, and timestamp.
- Only real provider calls are ledgered. `remember()` is reached from the three call sites in `run_models`, so a re-review that reuses an existing secondary classification because the provider configuration is unchanged records no row. This is the case that would otherwise have double-counted a classification the user never re-ran.
- Unreported counts stay `null` and the attempt is still recorded, so `rate_limited` and other failures are visible in the history without inventing spend. The endpoint reports `reported_calls` and `unreported_calls`, and the panel says the totals are a lower bound when any call went unreported.
- Ordering fix required for correctness: the usage rows are added only after the guarded draft `UPDATE` reports `rowcount == 1`, and the superseded path now rolls back before raising `stale_review`. Previously the transaction was committed before the rowcount check, so a discarded operation would have left its provider calls in the ledger.
- No backfill. Pre-existing drafts keep only their stage snapshot, and the panel states that earlier steps are not backfilled rather than implying complete totals. Revision 3's lost generation count stays lost.
- Mocked-provider verification: `pytest` passed 109 tests (102 before, 7 added covering per-operation/stage/role/revision recording, generate → edit → regenerate revision mapping, the reused-classification no-op, the configuration-change re-classification, `null` and entirely absent usage, a failed stage recorded without counts, session scoping, and a superseded operation recording nothing). `ruff check .` passed; frontend `npm run build` and `npm run lint` passed. `alembic upgrade head` moved the local database to `20260927_03` and the new table holds 0 rows, confirming no backfill; both existing drafts and the single YouTube connection are unchanged. No live model calls were made for this change.

## Usage Accounting Corrected: Consumption Is Not Draft State (2026-09-27)

Four defects in the ledger added earlier the same day, all found by re-reading the code rather than by a test.

- **A stale guard deleted accounting for requests that were already billed.** The first version committed the draft-state transaction and then rolled it back on `stale_review`, taking the usage rows with it. The provider had served those requests; the ledger recorded nothing, so real spend vanished from the record every time a concurrent edit won a race. Accounting now commits in its own transaction after the guarded update is settled, and the rows are marked `applied: false` with the attempted revision and operation token retained. The stale guard is unchanged in what it protects: a discarded operation still cannot modify text, classifications, review status, or approval. A mutation check confirmed the regression test has teeth — reinstating the coupled behaviour fails it with `assert 3 == 6`.
- **One call could make several provider requests but record one row.** `complete()` retries once on `408/500/502/503/504` and on `requests.Timeout`/`ConnectionError`, so a single stage can issue two HTTP requests, and `usage()` only ever described the last response. A first attempt that consumed tokens and then failed was simply not counted. The adapter now appends one entry per HTTP request, exposed as `attempts()`, and the ledger records each as a separate `attempt` row.
- **Reported usage was thrown away exactly when it was most certain to exist.** `call()` returned `usage=None` on any `ProviderFailure`, and the generic handler returned `None` for an unusable response. But the adapter sets `_usage` right after decoding the payload, before validation, so `incomplete_response` and `invalid_response` had real reported counts that the router discarded. The failure paths now read `provider.usage()`, and usage is captured in `_request` at decode time, so a response that arrived and was billed is counted even when it cannot be used.
- **Unknown consumption was stored in a way that invited reading it as zero.** Each row now carries `consumption: reported | unknown`. A request with no readable response is `unknown` with null counts, and the panel says it cannot be determined whether the provider processed or billed it, rather than implying a charge.
- **Idempotency is enforced by the schema, not by convention.** A unique index on `(draft_id, operation_token, stage, attempt)` plus a pre-check means re-running the same accounting write — after a database error or by mistake — cannot insert a second row. A persistence retry therefore cannot inflate totals.
- **A bug in my own test infrastructure was caught before it could damage data.** An earlier revision of `record_attempts` opened its own `SessionLocal()`, which is the real PostgreSQL engine, while the suite runs on a temporary SQLite database. The suite was writing to the live database. It now takes the request session and commits its own transaction there, which also removed a cross-dialect problem. Worth stating plainly: the isolation guarantee came from `conftest`'s `get_db` override, which this bypassed, and nothing in the suite would have caught it.
- **Coverage is now detected rather than assumed.** A draft is incomplete if it predates tracking, or if its stage snapshot reports usage that no recorded row accounts for. The second signal matters: the live draft generated at 17:39:49 was created after the ledger existed but by a worker still running pre-ledger code, so its creation time alone would have wrongly certified it as complete. Its history is now correctly reported as unavailable.
- Verified with mocked providers and no live model calls: `pytest` passed 121 tests (109 before, 12 added). New coverage: a stale result changing nothing while keeping its reported usage; a reused classification adding no row; an accounting retry not duplicating entries; an in-call retry recorded as two attempts; a timeout leaving consumption unknown; reported usage surviving a validation failure; a 429 as one unknown attempt with no retry; `attempt_trail` falling back to a single observable entry and never silently uncounting a request; and pre-ledger history staying explicitly incomplete with the exact required wording. `ruff check .` passed. Frontend `npm run build` and `npm run lint` passed. `alembic upgrade head` moved the local database to `20260927_04`.
- The panel now separates the two kinds of incompleteness: history that predates tracking, which shows "Recorded usage since tracking began; earlier usage is unavailable.", and unknown consumption in new calls, which shows that no readable response came back. Discarded requests are listed and labelled rather than hidden. No wording presents the totals as lifetime totals.

### Live Database Observation

- While this work was in progress the local database changed underneath it, and the cause was not these changes: nothing in the accounting or draft-generation code issues a row delete, and no draft-delete endpoint exists.
**Proven: the cascade risk was real.** This is established by reading the schema and the code, not by inference from the data. Drafts could only disappear by cascade, and they cascaded from *two* parents: `app_sessions.session_hash` and `youtube_connections.id`, both `ON DELETE CASCADE`. Three code paths delete one of those parents: expired-session cleanup in `_get_or_create_session`, the disconnect endpoint, and the logout endpoint. A disconnect or a logout therefore *could* silently destroy a user's drafts and usage history, with no warning and no recovery path. That defect existed regardless of what happened to these particular drafts, and it is fixed.

**Ruled out.** Session expiry: the TTL is 14 days, so it cannot explain a same-day disappearance. A plain reconnect: the OAuth callback *updates* the existing connection when the same channel reconnects (`setattr` in place) and never deletes it. A deliberate delete: there is no such route.

**Unproven: what actually triggered the deletion of these two drafts.** The schema proves a route existed; it does not prove the route was taken. No available evidence identifies the trigger:
  - The access log would settle it and does not exist. The serving worker had already exited, no log files were present, `archive_mode` is `off`, and `log_statement` is `none`.
  - `pg_stat_user_tables` shows dead tuples in `reply_drafts` and `youtube_connections`, which is *consistent with* deletion but is a stale estimate: the same report claims `app_sessions` is empty when it holds 9 rows, so it cannot be treated as proof.
  - No application log, no crash trace, and no user-visible error was found.

  A disconnect or logout cascading the drafts away is the best-supported explanation, and it is stated here as a hypothesis rather than a finding. It should not be repeated as established fact.
- **No recovery source was found for the two lost drafts.** Every avenue was checked and none can return them: WAL archiving is off (`archive_mode=off`), statement logging is off (`log_statement=none`), no PostgreSQL log directory exists, no pre-existing dump or archive was found anywhere on the system, the only installed extension is `plpgsql` so `pageinspect` cannot inspect dead tuple contents, and the process that served the requests has exited. The backups taken for this work were created after the loss and therefore do not contain them. This is a statement about the sources that were searched, not a claim that the data is unrecoverable in principle.
- **Root cause fixed.** Draft ownership now lives on a `channel_owners` row per channel instead of on the session and credential, so neither can cascade a draft away. Retention, the migration, the backup locations, and the retained limitation on the untested `downgrade` path are described in `docs/RETENTION.md`.
- That draft has **zero ledger rows**. Its creation time is after both migrations, but the request was served by a worker still running pre-ledger code, so nothing was recorded. This is exactly the gap the snapshot-backing coverage signal exists to catch, and it is reported as unavailable rather than as a complete total.
- The uvicorn reloader was working: the serving worker had restarted at 17:41:44 and was running the corrected code, so the ledger and the `usage` endpoint are live now. Earlier drafts' figures remain in this log as a record of what was observed at the time, not as current database state.

### Reply refinement by conversation

- Refinement is a persisted conversation over a saved draft, added by migration `20260927_06` as `refinement_turns`. Each turn stores the instruction, the full suggested reply, the revision it was written against, a status, and a unique operation token, and is owned by `draft_id` with `ON DELETE CASCADE` so it inherits the channel owner and disappears with the draft.
- Generating a suggestion never modifies the saved draft, its revision, approval, review, or classifications. Applying one requires the expected revision and an idle draft, then saves the text as a new revision and clears approval, review, and quality findings in the same transaction. It reuses the existing edit concurrency controls and makes no model call, so a refined draft needs an explicit re-review. An identical suggestion is recorded as `unchanged` and saves nothing.
- **One implementation bug was found and fixed while testing.** The late-response check compared the instruction's expected revision against the `ReplyDraft` object loaded *before* the model call, so it could not see a concurrent write and would have labelled a stale response as current and applicable. It now re-reads the revision from the database after the call returns, and the turn is recorded as `stale`, still shown but not applicable.
- A suggestion is a proposed rewrite of the reply only. Comment, video, and creator fields are data the user cannot edit, so the model is never asked to change them, and no path posts, edits, approves, or checks anything on YouTube. The comment and saved reply travel as JSON data values and are never spliced into an instruction or system directive.
- Bounded context uses at most the six most recent usable turns, each clipped to 600 characters, with a 500-character instruction limit and a 5000-character suggestion limit. Instructions are stored in full; only outgoing context is clipped, so nothing stored is silently altered. Discarded turns are retained and still eligible as context, because a user may change their mind. Excluded turns are never billed.
- Each submission makes one primary-model call, and the provider's internal retries remain separate ledger attempts. `operation="refine"` and `stage="refinement"` rows record every attempt, including failures, retries, unknown consumption, stale results, and discarded work. A reused classification is not billed again.
- The UI loads conversation history from the server, so it survives a refresh and a same-channel reconnection, and the client never fabricates or resends model history. Refining is disabled while the draft has unsaved edits, so an instruction always describes the reply the user can see. A stale suggestion has its apply control disabled with an explanation rather than failing only on click.
- Verified with mocked providers and no live model or YouTube calls: `ruff check .` passed, `pytest` passed 164 tests (139 before refinement, 25 added), frontend `npm run build` and `npm run lint` passed, and `alembic upgrade head` moved the local database to `20260927_06` with `refinement_turns` created empty. The development database still holds 9 sessions, 1 connection, 1 channel owner, 1 draft, and 0 usage rows; no test touched it.
- A fresh current backup `replyai-20260927T184644` was taken after the migration and verified readable and digest-consistent, superseding `replyai-20260927T183729`. `docs/RETENTION.md` records all four and states that the two oldest predate durable ownership.

### Refinement panel reported two symptoms with one cause

Two user-visible problems were reported together: the instruction field accepted typing but not paste, and after sending, the panel stayed on "Asking the model for a suggested reply" and "Loading conversation" with no suggestion shown.

**The request was never the problem.** The live worker did expose all four refinement routes, and the database held one refinement turn (`status=suggested`, `base_revision=1`, 197-character suggestion) and exactly one usage ledger row for it: `operation=refine`, `stage=refinement`, `role=primary`, `attempt=1`, `consumption=reported`, `input_tokens=467`, `output_tokens=187`, `applied=true`. So the request reached the provider, completed, produced a suggestion, and persisted both the suggestion and its cost. The turn's `operation_token` is retained deliberately as the ledger's idempotency key, and the draft itself held no operation token, so no operation was stuck. The failure was entirely in rendering a response that had already arrived.

**Root cause: a `mounted` ref that was only ever set to `false`.** The new `RefinementConversation` component initialised `mounted` to `true` and set it to `false` in an effect cleanup, but never set it back to `true` in the effect body. The application renders under `StrictMode`, which mounts, unmounts, and remounts every component in development, so the first simulated unmount left `mounted.current` permanently `false`. Every subsequent `if (mounted.current)` guard then skipped its state update: the spinner was never cleared, the returned suggestion was never appended to the list, and `loaded` was never set, so the conversation showed "Loading conversation" forever. The pre-existing `DraftPanel` in the same file does this correctly by assigning `mounted.current = true` at the top of its effect, which is why the rest of the panel kept working. The disabled textarea was the same defect from the other side: the field is disabled while `busy` is set, so a permanently stuck `busy` refused input, and a disabled field refuses paste exactly as it refuses typing. Paste was not separately blocked; there is no `onPaste`, `onKeyDown`, or `preventDefault` handler anywhere in the source, and no overlay or `pointer-events` rule over the field.

**Fixes.**
- The `mounted` flag is now assigned in the effect body as well as cleared in cleanup, matching the existing pattern in the file. The conversation effect uses the local `active` flag instead, which is per-fetch and needs no ref.
- `request` now enforces a deadline through an `AbortController`. The default is 15s, and the refinement call is given 120s, which stays above the backend's own 95s stage budget on purpose: abandoning a request early could hide a suggestion the server had already produced, so the client's backstop must not race the server. Expiry produces a message that tells the user to reload rather than to resend.
- Reload now increments a token used in the refinement panel's key, so the panel remounts, drops any stuck request state, and re-reads the persisted conversation. That is a plain `GET`, so it recovers a stored suggestion without paying for a second refinement.
- The instruction field gained an explicit `id`/`htmlFor` label, its own visible border, focus ring, placeholder and disabled styling, and a stated reason whenever it or Send is disabled. The existing `.draft-editor textarea` rule never applied to it, so it had been rendering unbordered with no visible focus. The reason text is suppressed while a request is in flight, because the in-flight message already explains itself.
- The UI lock is released on every settled path, including failure and timeout. This is the double-submit guard only; no backend operation token is cleared and no recorded usage is discarded.

**Verification.** 11 isolated frontend tests with a mocked `fetch` cover typing, native paste, paste after a settled request, disabled-reason text, success, provider failure, a hanging connection reaching the bounded deadline and aborting the real request, a failed conversation fetch leaving the panel usable, recovery of a stored turn by reload without a second `POST /refine`, apply, and discard. The deadline test uses a mock that rejects with `AbortError` on abort, as real `fetch` does. Reinstating the original `mounted` defect fails 8 of the 11, so the tests reproduce the reported behaviour rather than merely describing the fix. `npm run build`, `npm run lint`, and `npm run vitest run` pass; `ruff check .` passes and `pytest` still passes 164 tests. No new provider request, `.env` change, database reset, YouTube action, commit, or push. The saved draft, its approval, the stored turn, and the usage ledger were read only.

**Manual browser verification is still pending:** no browser or automation binary is available in this environment, so Ctrl+V and right-click Paste were exercised through Testing Library's `userEvent`, which dispatches a real paste event and applies the insertion, but not through a physical browser context menu.

### Refined panel after refresh: presentation only

After a successful Apply, Review, and Approve, the refined reply survived a refresh but the conversation panel looked broken, with text and controls appearing to overlap or clip and an unclear suggestion status.

**State restoration was already correct.** The saved draft held revision 2, `review_status=complete`, and `human_decision=approved`, and the single turn held `status=applied` at `base_revision=1` with a 197-character suggestion equal to the saved reply. Two isolated tests with a mocked saved, applied, reviewed, approved draft confirm the panel hydrates all of that: it displays the backend's `approved` decision, revision 2, `Review: complete`, the turn as `Applied` against revision 1 with its full multi-line suggestion text, and offers neither Apply nor Discard for an applied turn. Loading history issues no `POST /refine`. Mounting and history loading write nothing back: the component only reads what the two GETs return, and the panel's local status rewrite happens solely in the apply response handler. So this was presentation, not a restoration bug.

**The layout cause was a leaking shared selector.** `.comment-content p` was a descendant selector, and the draft panel is nested inside `.comment-content`, so it reached every paragraph of the refinement conversation. At specificity (0,1,1) it beat the conversation's own (0,1,0) rules, forcing 11px text and a muted colour onto the suggestion, the status, and the note, and overriding the suggestion's margins. The same leak had been forcing 11px onto the *entire* draft panel, including the main reply editor, which was never intentional: `body` sets no font size, so those elements were rendering at 11px purely by accident. It is now scoped to `.comment-content > p`, the comment's own text, and `.draft-panel` declares an explicit 13px base so the panel's type is deliberate rather than inherited by accident. This is a visible change to the pre-existing panel, in the direction of readability.

**Other presentation faults fixed.** The suggestion block was `#f4f7fb` on a `#f8fafc` panel, too close to be distinguishable from background, so a saved suggestion did not read as its own block; it now uses a white fill with a full border and a status-coloured 4px left edge. The status was a run-on sentence mixing the state with the revision; it is now a status badge plus a separate metadata span on its own line, which also stops the two from merging for assistive technology. The composer and the history are separated by a dedicated container with a divider, so a long list of turns cannot read as part of the reply being written. Turn controls sit in their own flex row with a gap instead of relying on inherited button margins, and the suggestion wraps with `overflow-wrap: anywhere` while preserving stored line breaks. The instruction textarea's `id` is now derived from the draft id, since a fixed id would break label association if more than one panel were ever mounted. No fixed heights, `overflow: hidden`, or absolutely positioned overlays were involved; there was no grid involved at all.

**Verification.** 15 isolated frontend tests, all passing, including the refresh-restoration and layout-structure cases. `npm run build`, `npm run lint`, and the frontend suite pass; `ruff check .` passes and `pytest` still passes 164 tests. Live data was read only and is unchanged at revision 2, `approved`, with one `applied` turn.

**Visual verification is still pending.** No browser or automation binary is available in this environment. The DOM structure, class names, and cascade correctness are verified by code and tests, but actual pixel rendering, text wrapping at real viewport widths, and right-click Paste remain unconfirmed in an actual browser.

## Milestone: One Manual Reply Publish

### Scope and decisions

The milestone is a single, deliberately awkward public action: post one reply to one existing top-level comment, once, on explicit human confirmation. Automatic posting, scheduling, batching, editing, and deletion of existing YouTube content are explicitly out of scope and are not built.

Google's discovery document lists `youtube.force-ssl`, not `youtube.readonly`, for `comments.insert`, and that grant is already required to read comments at all, so posting adds no new permission. The grant is still broader than the application: Google describes it as permitting viewing, editing, and permanently deleting videos, comments, and captions. The pre-existing consent disclosure was left accurate and now states that the app performs one `comments.insert` rather than denying any write.

**YouTube documents no idempotency key for `comments.insert`.** That single fact drove most of the design. There is no way to ask YouTube "did my request already succeed?", so a local durable record is the only possible duplicate protection.

**Reply creation uses `comments.insert`, not `commentThreads.insert`.** A `commentThreads.insert` can set `snippet.addedOwnerComment` with a `replyOriginalCommentId` thunk, but it is the endpoint YouTube documents as creating a new thread that may be a reply, and `comments.insert` states directly that it creates a new top-level comment, optionally replying to an existing one. For an explicitly manual, non-top-level reply, the narrower, more predictable call is the better fit. Cost is 50 quota units per reply.

**Retry behaviour is the crux.** Because a request can be sent and its answer lost, the application must distinguish "YouTube refused" from "YouTube did not answer". Only reasons Google's documentation lists as refusals become a durable `failed`, which is the only state a human may retry. Timeouts, dropped connections, HTTP 5xx, `processingFailure`, unlisted reasons, and a response carrying no reply ID all become `unknown`, which is never retried automatically and offers no retry button, because a repeat risks two identical public replies. A consequence worth stating plainly: `processingFailure` is classified as unknown rather than as a failure precisely because Google does not promise the comment was not created.

### Backend

- Added `reply_publications` and `ReplyDraft.publication_token`. The publication row snapshots the immutable request: channel, video, parent comment, the exact text, and the draft revision that was sent, plus the operation id, status, reply comment ID, sanitized failure, and timestamps. The snapshot is what makes a stored success replayable and an unknown outcome explainable after the draft itself changes.
- `publication_token` is deliberately separate from the existing review `operation_token`. An edit must still be able to supersede an in-flight review, but a draft claimed for posting must not have its publish source changed underneath an attempt that may already be public.
- Added a partial unique index on `(draft_id)` where status is `sending`, `unknown`, or `posted`. The index, not a read-then-write check, is what makes two simultaneous confirms safe. `failed` is excluded, so a settled refusal may be retried.
- Added a dedicated write client. It performs one HTTP call with `num_retries=0` and `refresh_status_codes=()`, so a transport-level replay and a token-refresh-then-replay are both impossible. Reusing the read client would have inherited retry and refresh-replay behaviour that is correct for reads and dangerous for writes.
- `POST /api/drafts/{id}/publication` validates the request and refreshes credentials *before* claiming, then commits the attempt, then sends, then settles. The pre-send commit is not optional: without it a crash between claim and response would leave nothing on disk and the draft would look unposted. Replaying the same revision and text returns the stored result. After an edit, a posted draft answers `already_published` instead of posting again.
- `GET /api/drafts/{id}/publication` returns the record and the server-computed eligibility. It writes nothing and makes no provider call, so reading the state is free and safe, and the browser never has to reimplement the rules.
- Deletion of retained work is refused while an attempt is `sending` or `unknown`. Settled attempts cascade with the draft.
- A failing database operation after the send is treated as unknown rather than as success, because claiming a failure would offer a retry for a reply that may be public.

### Migration and live database

- Migration `20260927_07` adds the table, the column, and the partial unique index. It is additive and drops no existing column, so its downgrade removes only what it created.
- Verified upgrade, downgrade to `20260927_06`, and re-upgrade on a disposable PostgreSQL database named `replyai_migcheck`, which was then dropped. A SQLite run from scratch fails, but that is a pre-existing limitation of `20260927_05`, which alters a constraint the SQLite dialect does not support; PostgreSQL is the authoritative database for this project.
- Took and verified a protected backup at `~/.local/share/replyai/backups/replyai-20260927T193529` (migration `20260927_06`, SHA-256 `658a92648ffd6103d2633fd5a39cb874e1b07295c49547d1c9217ff2fa9cb896`, directory `0700`, files `0600`, nine files, all per-table row counts matching the manifest) before applying anything.
- Applied `20260927_07` to the live database. The live head is `20260927_07`. The single retained draft is unchanged at revision 2, review complete, decision `approved`, 197 characters, with both `operation_token` and `publication_token` null, and `reply_publications` is empty. No reply has been posted.

### Frontend

- **Post reply to YouTube…** opens a preview naming the connected channel, the video, the comment, the saved revision, and the exact text, with an explicit public-and-immediate warning. Opening the preview and choosing **Cancel** send nothing, which is asserted by test.
- **Confirm & post** sends only the revision and the confirmed text, purely so the server can reject a confirmation that no longer matches what is saved. The server reads the text, approval, review, and ownership itself.
- A `disagree` comparison is surfaced in the dialog as a suggestion to re-read, not as a block, because classification agreement is not a safety property.
- The publish request uses a 30-second deadline, deliberately longer than the write client's own 10-second HTTP budget, so an abandoned request is not mistaken for a failed one and does not invite a second reply.
- Posted and unknown outcomes replace the button entirely and survive a refresh, because the state is read from the server rather than held in the component. A failed outcome shows YouTube's safe reason and offers a deliberate retry. Repeated clicks while a request is in flight are blocked in the component and by the database index.
- After any failed request the panel re-reads the server state rather than guessing. A `failed` or `unknown` record then drives what the user sees, and the raw message is shown only when no record exists, which is the pre-send refusal case.
- A refused load does not break the draft panel, and a publication record is tagged with its draft id so one draft's outcome can never be displayed against another.
- Removed the two now-false interface claims that publishing was unavailable.

### Verification

> Superseded by the later milestones in this log: `test_publication.py` now has 54 tests,
> the frontend has 30, and the confirmation flow is exercised through the whole application.
> The counts below are what was true at this milestone.

- `backend`: `pytest` passes 208 tests; `ruff check .` passes. Pytest reports the one pre-existing upstream Starlette deprecation warning about its `httpx` test client.
- `backend/tests/test_publication.py` adds 44 tests covering eligibility rules, the claim and settle sequence, replay of a stored success, refusal versus unknown classification, the missing-scope path, the already-posted path, resolution of an unknown attempt, the edit and decision guards, the deletion guard, and concurrent claims.
- `frontend`: 24 tests pass, including 9 new publishing tests for preview contents, the no-write guarantee on open and cancel, the exact request body, repeated-click protection, refresh restoration of a posted reply, the absence of a retry affordance for unknown, a retry after a recorded refusal, recovery of the durable unknown state after a lost response, and both blocking reasons. `npm run build`, `npm run lint`, and `tsc --noEmit` pass.
- All provider and OAuth responses are mocked. No test contacts YouTube or a live model, and no test targets the development database.

### Not verified

> Historical, as of this milestone. The live post was later made and verified; see
> *First Live Manual Publication* and *Publishing Safety Audit and Failure Coverage*.

- **No live post has been made and none should be until you decide to.** The live channel already holds `youtube.force-ssl`, so no consent step is outstanding, but the first real publish is yours to trigger deliberately.
- Visual verification is still pending. No browser or automation binary exists in this environment, so the dialog's pixel rendering, text wrapping at 320px, and stacking over the comment list are unconfirmed. The DOM structure, accessible names, states, and keyboard focus behaviour are covered by tests.
- A run against the live database with a genuine YouTube failure is not simulated; the unknown-outcome path is exercised only through mocks.

## Milestone: Publishing Safety Audit and Failure Coverage

The write path was audited against the one property that matters most: a single confirmed
intent must never produce more than one public reply. The audit read the claim, send, and
settle sequence rather than assuming it behaved as documented, then added the tests that
were missing from the branches that had never run.

### The state machine as built

| From | To | Allowed when |
| --- | --- | --- |
| no record | `sending` | the draft is eligible and the claim insert succeeds |
| `sending` | `posted` | YouTube returns a comment ID |
| `sending` | `failed` | the reason is one of the 16 enumerated refusals |
| `sending` | `unknown` | timeout, transport error, HTTP 5xx, unlisted reason, `processingFailure`, or 2xx with no ID |
| `failed` | `sending` | a person chooses to try again, creating a new record |
| `sending` / `unknown` / `posted` | any | never; the partial unique index refuses the claim |

Two consequences are load-bearing. `unknown` is terminal without a person checking YouTube
and deciding, because YouTube gives no idempotency key for `comments.insert` and so a
retry could silently duplicate a reply that had actually landed. And a reader that meets
an abandoned `sending` row treats it as `unknown` rather than `failed`, which fails safe:
the cost of being wrong is an unnecessary manual check, while the opposite mistake would be
a duplicate public reply.

### Ten tests, each against a real database

`backend/tests/test_publication.py` grew from 44 tests to 54, and the new ones assert
against rows read back from the test database rather than against return values:

- a success writes one `posted` row and releases both draft tokens;
- a definitive refusal records `failed` and permits exactly one retry;
- a timeout records `unknown` and is never resent;
- an HTTP 5xx is `unknown`, not `failed`;
- a 2xx with no comment ID is `unknown`, which is the case a naive implementation gets wrong;
- repeating a confirmation adds no second row and triggers no second insert;
- a confirmation for a previous revision is refused after an edit;
- a second attempt is blocked while a claim is held;
- two concurrent confirmations leave one row and one reply;
- an `unknown` attempt cannot be reconciled into a second post.

Two mutation checks were run against this suite to confirm it fails for the right reasons.
Mapping an ambiguous outcome to `failed` broke 6 tests, and removing the `busy` lock from
the Confirm button broke the double-submit test. A suite that passes while the protections
are removed would have been worth nothing.

One assertion was wrong on the first run and the product was right. Saving an edit clears
both the approval and the completed review, and the review gate is checked before the
approval gate, so a freshly edited draft returns `review_required` rather than
`approval_required`. That is stricter than the original assumption: the reply has to be
reviewed and approved again after any edit, which is what makes "the text that goes out is
the text you approved" hold.

### Confirmation flow tested through the whole application

`frontend/src/confirmFlow.test.tsx` mounts `App` rather than the draft panel, and walks from
a comment to a posted reply through the interface: comment to draft, open the confirmation,
read the channel and comment it names, confirm. Five cases cover the path that a person
actually touches, with the backend and the provider both mocked:

- the happy path sends exactly one request carrying the saved revision and text, and offers
  no second post afterwards;
- cancelling sends nothing;
- three clicks during one in-flight request send one request;
- a request that never answers shows the durable `unknown` state and offers no retry;
- a pre-send refusal shows the server's reason and leaves the reply editable.

### Keyboard and focus corrections

The confirmation dialog was visible but unreachable by keyboard. Nothing moved focus into
it, Escape did nothing, and focus was never returned to the button that opened it, leaving
the destructive Confirm in the tab order behind the backdrop. Now focus moves to Cancel
when the dialog opens, Escape dismisses it, and focus returns to the trigger on both the
Escape and Cancel paths. Escape is ignored while a request is in flight, so an impatient
second keypress cannot dismiss the dialog out from under a live post. A mutation check
disabling the Escape handler fails the test.

### What this audit did not do

`unknown` and `failed` have still never occurred against the real API, and no test here can
change that. The first real occurrence remains the first real proof.

### Why there is still no browser test

There is no browser binary in this environment and no cached Playwright install, so any
browser suite would begin by downloading a browser. That is a large dependency to add
without being asked, and the jsdom coverage above exercises the same flows, including the
full application path and real focus behaviour, without it. If a browser suite is wanted,
Playwright is the natural choice and can be added as a deliberate step.

Visual verification remains the user's browser, for the happy path only. Narrow-viewport
wrapping of a long reply is handled in CSS but has not been seen.

## Milestone: Durable Duplicate Protection After Deletion

The review above found one honest gap. `reply_publications` is scoped to a draft and
cascades with it, so permanently deleting a posted draft also deleted the record of having
posted to that comment. ReplyAI would then be unable to tell that it had already replied
there, and a fresh draft for the same comment would post a second public reply with
nothing to object. Nothing about the concurrency or retry protections was wrong; the
duplicate boundary simply ended at the retained draft.

### Three layers, not one mechanism

These are distinct problems and are now enforced separately:

| Layer | Scope | Mechanism | Answers |
|---|---|---|---|
| Within one attempt | one draft | claim token + `uq_publication_open_draft` | two requests publishing the same draft |
| After an ambiguous result | one attempt | `unknown` blocks retry | a lost response that may still have posted |
| After deletion | channel + comment | `published_reply_guards` + unique index | a second reply after the draft is gone |

Only the third is new. The first two were already correct and are unchanged.

### The key, and why

`uq_published_reply_guard` is unique on `(channel_id, parent_comment_id)`.

The parent comment id alone would be wrong: a guard must mean "this connected channel
already replied here", so ownership is part of the identity. Adding the channel also means
two different ReplyAI owners on two different channels cannot collide even if a comment id
were ever reused, which is what a test now proves at the database level. A test also proves
the guard is not over-broad: replying to one comment leaves every other comment on the same
channel publishable.

The table has **no foreign key** to `reply_drafts` or `channel_owners`. A cascade is
exactly what this table exists to survive, so `draft_id` and `publication_id` are stored as
opaque strings for audit only. It also stores no reply text and no video id: enforcing a
guard after the user deleted their content must not mean keeping that content.

### Reserve before sending, release on a definitive refusal

The guard is reserved in `claim()`, before the insert leaves, so two concurrent requests
for one comment race on the unique index and exactly one wins. A `SELECT`-then-`INSERT`
check cannot do that, because neither request can see the other's uncommitted work.

Resolution differs per outcome, which is what keeps the three states from collapsing:

- `posted` — the guard becomes durable evidence, with the returned `reply_comment_id`.
- `unknown` — the guard is kept and set to `unknown`. The ambiguity cannot be deleted away
  into a fresh send.
- `failed` — the reservation is **deleted**. A definitive refusal created nothing, so it
  is not evidence of a public reply and must not become a permanent block.
- `sending` — a reader meeting it reports `unknown`, as before.

### Deletion semantics, stated explicitly

`POSTED` — the draft, its text, review, approval, refinement turns, and publication row are
still deletable. The guard remains. A new draft for the same channel and comment is
created and approved normally, and publication is refused with `already_replied`, so the
person is told the reason rather than silently losing a draft.

`UNKNOWN` — deletion of retained work stays refused with `publication_unresolved`, and the
guard is kept. Nothing can erase the ambiguity into a retry.

`FAILED` — unchanged. No tombstone, retry stays free, no manual cleanup needed.

`SENDING` — unchanged. The claim and the existing retention guards still apply.

### Backfill, which was not optional

`20260927_08` copies every existing `posted` and `unknown` attempt into a guard. Starting
the table empty would have left the one reply already public in this repository unprotected
from the moment its draft was deleted, which is the exact failure being fixed. `failed`
attempts are not copied, since copying one would convert a safe retry into a permanent
block. Applied to the development database, the live reply is now guarded:

```
guards:
   UCyK6__BdgBD3ETsxuHBMRrQ Ugx-jkGM0zGHyywBZoh4AaABAg posted Ugx-jkGM0zGHyywBZoh4AaABAg.AbEYbOVVfqTAbIFAf6l2PK
reply_publications rows: 1
```

The draft was not touched: revision 2, review complete, approved, both tokens null.

### Two bugs the tests caught during implementation

`claim()` originally built the guard before flushing the attempt, so `publication_id` was
still `None` and `settle()` could not match the guard to its own attempt. A definitive
refusal therefore never released its reservation and the documented retry returned 409.
Two existing tests failed immediately, which is the argument for keeping them.

### Verification

Nine new database-backed tests, bringing the publication file to 63 and the backend suite
to 227. Each drives the real ownership, claim, guard, and deletion endpoints and reads the
rows back; only `insert_comment_reply` is replaced.

- a successful publish writes durable evidence naming the attempt that owns it
- the guard table has no text column
- deleting a posted draft removes the draft and its publication row, keeps the guard, and
  refuses a second reply with no further provider insert
- an unrelated comment on the same channel is unaffected
- a definitive refusal leaves no tombstone and the retry still succeeds
- an unknown outcome is not erasable by deletion and is never retried
- the key is the pair, not the comment alone, proven by a real `IntegrityError`
- four concurrent publishes against a retained guard all return `already_replied` with a
  single insert for the comment's whole lifetime

Three mutations were checked rather than assumed:

- making the guard cascade like a draft fails 30 tests
- never releasing the reservation on a refusal fails 3, including the pre-existing retry test
- removing the pre-send guard check fails 2

A frontend test states the retained-publication reason and asserts the post control stays
disabled with no request attempted.

### What this still does not claim

ReplyAI prevents a knowingly repeated publication for a parent comment when its durable
local evidence says it already posted there, and ambiguous sends stay blocked rather than
being blindly retried. It does not claim duplicates are impossible. A reply created outside
ReplyAI, a guard row deleted directly in the database, and an ambiguous send that YouTube
honoured are all outside the guarantee. `comments.insert` provides no idempotency key, so
none of this is delegated to the provider, and `published_reply_guards` exists only because
that is impossible locally.

## Milestone: First Live Manual Publication (user-verified)

The user performed the first real publish, confirmed the reply on YouTube in the browser, and
confirmed ReplyAI reported **Posted to YouTube** with the returned comment ID. This is the
first time the write path has touched the real API, and it is the end of the deferred item
recorded above.

### What the user verified

- The reply is publicly visible on the video, as the connected channel.
- ReplyAI showed the Posted state and the comment ID that YouTube returned.

### The stored record, read back from the live database

Read-only inspection of `reply_publications`, with no further publish and no model call:

| Field | Value |
| --- | --- |
| `status` | `posted` |
| `draft_revision` | `2` |
| `channel_id` | `UCyK6__BdgBD3ETsxuHBMRrQ` |
| `video_id` | `KynQ5qkE7Ik` |
| `parent_comment_id` | `Ugx-jkGM0zGHyywBZoh4AaABAg` |
| `reply_comment_id` | `Ugx-jkGM0zGHyywBZoh4AaABAg.AbEYbOVVfqTAbIFAf6l2PK` |
| `created_at` / `resolved_at` | `2026-09-28T08:19:00.318634Z` / `2026-09-28T08:19:01.177971Z` |
| `failure` | `{}` |

The checks that matter, all confirmed by direct comparison rather than by eye:

- `p.draft_revision = d.revision` is true, so the posted text belongs to **revision 2**.
- `p.text = d.text` is true, byte for byte, at 197 characters. The stored snapshot is the
  saved draft, not something the browser supplied.
- `p.parent_comment_id = d.comment_id` and `p.channel_id = d.channel_id` are true, so the
  reply targeted the reviewed comment on the owned channel.
- The returned ID has the documented reply shape, the parent comment ID as its prefix
  followed by `.`, which is what Google returns for a reply created inside a thread.
- `created_at` and `sent_at` were both persisted before the request and are 0.86 seconds
  before `resolved_at`, so the attempt was recorded ahead of the send, as designed.
- `d.publication_token` and `d.operation_token` are both null again, so the claim was
  released on settle and the draft is not left locked.
- `failure` is empty, consistent with a confirmed result rather than a tolerated one.

The draft itself is unchanged: revision 2, review complete, decision `approved`, 197
characters, with the same text. Nothing was regenerated and no model was called.

### Interface corrections

Three user-facing statements still claimed the app made only read requests, which had
become false once the write path existed. Two of them were in the `youtube.force-ssl`
consent disclosure, so the user was being told something untrue at the moment of granting a
broad Google permission; that is the more serious of the three, and the footer was the least.
All three now state that ReplyAI reads the channel and performs a single reply post, and
that posting happens only on explicit confirmation. No edit, delete, or moderation call
exists, and the new wording still says so.

### What remains unverified

- The `unknown` and `failed` branches have still never occurred in real life. They are
  covered only by mocked tests, and the first real occurrence will be the first proof.
- Visual verification of the dialog and of the Posted state was performed by the user in
  their browser for the happy path only. Narrow-viewport wrapping has not been seen.
- No browser-driven test exists, because this environment has no browser and installing one
  was not requested. The confirmation flow is covered through the whole application in
  jsdom instead, including focus movement, Escape, and focus restoration.
