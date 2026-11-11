# API

ReplyAI's backend is a FastAPI application. This page summarizes the API groups. The
**authoritative, always-current reference** is the OpenAPI documentation that the running
API generates:

- Swagger UI: <http://127.0.0.1:8001/docs>
- ReDoc: <http://127.0.0.1:8001/redoc>
- OpenAPI JSON: <http://127.0.0.1:8001/openapi.json>

The API is designed for ReplyAI's own web app on the same machine. It is not a public,
versioned API, and paths may change between commits.

## Authentication and request rules

- **Session.** After Google OAuth (or **Connect simulated channel** in simulation), the
  browser holds an opaque, HTTP-only session cookie. The server stores only a hash of it.
  Channel data endpoints require a valid session and check that the requested channel
  belongs to it.
- **CSRF.** Every mutating request (`POST`, `PUT`, `DELETE`) must come from an origin
  listed in `CORS_ORIGINS` and carry the session's `X-CSRF-Token` header. The web app's
  `src/lib/api.ts` does this for you.
- **Revisions.** Draft mutations include the draft `revision` they were based on; a stale
  revision is refused with `409 Conflict`, so a slow request never overwrites newer work.
- **Errors** are stable machine-readable codes with a human message (for example
  `ai_setup_required`, `insufficient_scope`). Provider and Google error details stay on
  the server.
- `/health` and `/ready` take no session; they only report process and dependency
  status.

## Endpoint groups

### Health

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness: `{"status":"ok"}` |
| GET | `/ready` | Readiness of PostgreSQL and Redis; 503 when either is unavailable |

### Session and Google OAuth

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/session` | Current session, connection state and CSRF token |
| POST | `/auth/google/start` | Begin OAuth (read-only scope) |
| POST | `/auth/google/upgrade` | Request the broader `youtube.force-ssl` scope |
| GET | `/auth/google/callback` | OAuth redirect target (called by Google) |
| POST | `/auth/google/disconnect` | Remove the stored YouTube connection |
| POST | `/auth/logout` | End the session |

### YouTube (read-only)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/youtube/channel` | Connected channel |
| GET | `/api/youtube/videos` | Channel uploads |
| GET | `/api/youtube/videos/{video_id}/comments` | Comments of one video with draft status |
| GET | `/api/youtube/channel/comments` | Comments across the channel (inbox) |
| GET | `/api/youtube/publications` | Posted-reply history |

### Drafts, review and refinement

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/drafts/setup` | Whether both model roles are configured |
| GET, POST, DELETE | `/api/drafts` | List, create (generate), and delete the channel's drafts |
| GET | `/api/drafts/{draft_id}` | One draft with stage results |
| POST | `/api/drafts/{draft_id}/regenerate` | New AI revision, verified again |
| POST | `/api/drafts/{draft_id}/edit` | Save a human edit as a new revision |
| POST | `/api/drafts/{draft_id}/review` | Run the secondary model's quality review on the saved revision |
| GET | `/api/drafts/{draft_id}/decision` | Latest *recorded* automatic decision (read-only; never re-evaluated) |
| POST | `/api/drafts/{draft_id}/decision` | Approve or reject the reviewed revision locally |
| GET | `/api/drafts/{draft_id}/conversation` | Refinement chat history |
| POST | `/api/drafts/{draft_id}/refine` | Ask the model to refine the reply |
| POST | `/api/drafts/{draft_id}/refine/{turn_id}/apply` | Apply a suggestion as a new revision |
| POST | `/api/drafts/{draft_id}/refine/{turn_id}/discard` | Discard a suggestion |
| GET | `/api/drafts/{draft_id}/usage` | Token usage of the draft's model calls |

### Publication and reconciliation

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/drafts/{draft_id}/publication` | Publication state of a draft |
| POST | `/api/drafts/{draft_id}/publication` | Publish a confirmed draft through the guarded path |
| POST | `/api/drafts/{draft_id}/publication/check` | Read-only search of the thread for an `unknown` attempt |
| POST | `/api/drafts/{draft_id}/publication/reconcile` | Record the human-confirmed outcome (`posted` / `not_posted`) |

### Settings

| Method | Path | Purpose |
| --- | --- | --- |
| GET, PUT | `/api/preferences` | Tone, length, reply language, channel instructions |
| GET, PUT | `/api/publishing-settings` | Publishing mode (`manual_review`, `dry_run`, `auto_on_agreement`, `approve_all`) |

### Automation and review queue

| Method | Path | Purpose |
| --- | --- | --- |
| GET, PUT | `/api/automation` | Discovery on/off and pause state |
| POST | `/api/automation/poll` | Request a discovery pass now |
| GET | `/api/automation/summary` | Counts per decision class |
| GET | `/api/automation/activity` | Recent decisions |
| GET | `/api/automation/decisions/{decision_id}` | One decision with its trace |
| GET, PUT | `/api/automation/allowlist` | Allowed domains, contacts and handles for replies |
| GET | `/api/review-queue` | Items held for a person |
| POST | `/api/review-queue/{decision_id}/dismiss` | Dismiss a review item |

### Background jobs

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/batch/jobs/enqueue` | Queue comments for background processing |
| GET | `/api/batch/jobs` | List jobs (filter by status) |
| GET | `/api/batch/jobs/stats/summary` | Counts per job status |
| GET | `/api/batch/jobs/{job_id}` | One job |
| GET | `/api/batch/jobs/{job_id}/publication` | The job's publication attempt |
| POST | `/api/batch/jobs/{job_id}/cancel` | Cancel a waiting job |
| POST | `/api/batch/jobs/{job_id}/requeue` | Queue a finished job again |
| POST | `/api/batch/jobs/reconcile` | Recover the channel's jobs whose worker lease expired |

### Usage

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/usage/summary` | Tokens and requests per model role over the rolling last 24 hours, configured ceilings, and a local replies-remaining estimate (shown after at least 10 completed replies) |

The token-usage gauge that consumes this endpoint was originally designed by teammate
Hrishik1033 ([PR #3](https://github.com/RD-blip1/ReplyAI/pull/3)).

### Simulation (development only)

Mounted only when `REPLYAI_SIMULATION=1`; absent in real mode.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/simulation/state` | Fake channel state |
| POST | `/api/simulation/connect` | Connect the simulated channel |
| POST | `/api/simulation/comments` | Add a comment with a scenario |
| POST | `/api/simulation/comments/{comment_id}/edit` | Simulate a viewer edit |
| POST | `/api/simulation/comments/{comment_id}/creator-reply` | Simulate a creator reply |
| POST | `/api/simulation/comments/{comment_id}/delete` | Simulate a deletion |
| POST | `/api/simulation/reset` | Restore the seed comments (database records are kept) |
