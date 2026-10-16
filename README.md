# ReplyAI

ReplyAI is a YouTube workspace with read-only browsing plus a single, deliberate, manually confirmed write: posting one public reply to one existing comment. Initial channel connection requests `youtube.readonly`. Reading comments and posting replies both require a separate, explicit Google permission update described below. Local AI draft generation, independent classification, reply-quality review, editing, and human decisions are available with explicit provider configuration. A reply reaches YouTube only when you read the exact text in a confirmation dialog and select **Confirm & post**. The UI never substitutes mock videos or comments for live channel data.

New here? [docs/FRESH_CLONE_SETUP.md](docs/FRESH_CLONE_SETUP.md) is the end-to-end guide from a bare clone to a running workspace, including every environment variable, Google OAuth, the YouTube Data API, and the Groq configuration.

## Requirements

Runtime and tooling:

- **Python 3.12+** — runs the **FastAPI** backend.
- **Node.js 22+ and npm** — build and run the **React 19 + TypeScript + Vite** frontend.
- **Docker Engine + Docker Compose** — run **PostgreSQL 17** and **Redis 7** locally.
- **WSL with Docker access** (Windows users) for the local infrastructure.

External services and credentials:

- A **Google Cloud** project with an OAuth client (**Web application**) and the **YouTube Data API v3** enabled, plus your account added as a consent-screen test user. The initial connection requests `youtube.readonly`; comments and posting require the explicit `youtube.force-ssl` permission update.
- Optional: an **OpenAI-compatible chat API** key for local drafting. **Groq** is the documented default (`https://api.groq.com/openai/v1`, models `openai/gpt-oss-120b` and `openai/gpt-oss-20b`). Drafting stays disabled while unset.

Every variable and command is listed in [docs/FRESH_CLONE_SETUP.md](docs/FRESH_CLONE_SETUP.md).

If your account was just added to the `docker` group, start a new WSL session before running Compose. Do not change Docker socket permissions.

## Start Locally

From the repository root (`~/Projects/ReplyAI`):

```bash
cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
docker compose up -d --wait postgres redis
```

The environment templates use development-only placeholders. If you change `POSTGRES_*` in `.env`, update the matching credentials in `backend/.env`. Never commit actual `.env` files or use the example password outside local development.

## Google OAuth Setup

Keep the downloaded client JSON outside `~/Projects/ReplyAI`, for example under a private directory in your WSL home. In `backend/.env`, set `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` to its absolute WSL path. Do not paste the JSON contents into this repository, a chat, or the frontend environment.

For a typical Google download in `~/Downloads`, move it into a private directory without opening or printing it:

```bash
install -d -m 700 ~/.config/replyai
mv ~/Downloads/client_secret_*.json ~/.config/replyai/google-oauth-client.json
chmod 600 ~/.config/replyai/google-oauth-client.json
realpath ~/.config/replyai/google-oauth-client.json
```

Copy only the path printed by `realpath` into `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` in `backend/.env`. Replace the `TOKEN_ENCRYPTION_KEY` placeholder with a locally generated Fernet key; keep both values only in that ignored file.

Generate an independent Fernet key locally after installing backend dependencies:

```bash
cd ~/Projects/ReplyAI/backend
source .venv/bin/activate
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Put that generated value in `TOKEN_ENCRYPTION_KEY` in `backend/.env`. Keep it separate from Google's client secret and back it up securely; losing it makes stored tokens unreadable. Do not send or commit either value. In Google Cloud, the authorized redirect URI must be exactly `http://127.0.0.1:8001/auth/google/callback`. Initial Connect requests `https://www.googleapis.com/auth/youtube.readonly`.

**Comment permission update:** Google's current YouTube API discovery lists `youtube.force-ssl` as the OAuth scope for `commentThreads.list`; unlike `youtube.readonly`, Google describes this grant as allowing apps to see, edit, and permanently delete YouTube videos, ratings, comments, and captions. If you choose to proceed, add `https://www.googleapis.com/auth/youtube.force-ssl` to the OAuth consent screen's data access/scopes. In ReplyAI, the connected-channel panel explains this access and provides **Update YouTube permissions**. That action requests fresh consent for exactly `youtube.readonly` plus `youtube.force-ssl`; unrelated project scopes are not auto-included. The app makes list/read calls plus one `comments.insert` used to post a single confirmed reply; it does not edit, delete, or moderate content. Declined or partial consent leaves the existing readonly connection intact. Do not authorize the broader grant unless you accept Google's described access.

If OAuth configuration is missing, the workspace explains what needs configuration while `/health` and `/ready` continue to work. Local session cookies are HttpOnly and SameSite=Lax; set `APP_ENVIRONMENT=production` behind HTTPS to enable Secure cookies.

To test, confirm the Google account is listed as a test user on the External consent screen, restart the backend after editing `backend/.env`, and open <http://127.0.0.1:5173>. Select **Connect YouTube** and approve the initial readonly grant. For comments, review the broader access disclosure and select **Update YouTube permissions**, then approve or decline Google consent yourself. After successful approval, select a video to load its top-level comments and use pagination. On decline or partial grant, the prior channel connection remains. **Disconnect YouTube** asks Google to revoke the grant and deletes local encrypted tokens; no comment is posted.

In a WSL terminal, start the API directly (not in Docker):

```bash
cd ~/Projects/ReplyAI/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

After editing `backend/.env`, stop the API with Ctrl+C and start it again to load the OAuth path and encryption key.

In a second WSL terminal, start the frontend directly:

```bash
cd ~/Projects/ReplyAI/frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. The page checks the API at `http://127.0.0.1:8001`; API documentation is at <http://127.0.0.1:8001/docs>. Change the frontend URL in `frontend/.env.local` if you run the API on a different port, then restart Vite.

## Verify

```bash
# From backend/ with .venv active
pytest
ruff check .

# From frontend/
npm run build
npm run lint

# From the repository root
docker compose config --quiet
cd backend && alembic current
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8001/ready
```

`/health` reports application liveness independently of dependencies. `/ready` checks PostgreSQL and Redis and returns HTTP 503 if either is unavailable. It reports only availability labels, never connection details.

### Phase 2 Verification

- Backend: `pytest` passed (29 tests); the final focused OAuth rerun passed 20 tests after adding one parameter case (30 total cases across the verified suite); `ruff check .` passed. Pytest emitted one upstream Starlette deprecation warning about its `httpx` test client.
- Frontend: `npm run build` and `npm run lint` passed.
- Migration: upgrade/downgrade/re-upgrade passed on a disposable SQLite database. The existing Compose PostgreSQL database is at `20260926_01`; the migration only adds session/connection tables and an expiry index.
- Compose config validated. PostgreSQL and Redis are healthy; live `/health` and `/ready` both returned HTTP 200 with dependencies available.
- Browser checks at 1280px and 320px found no horizontal overflow. With OAuth intentionally unconfigured, setup guidance is shown and Connect remains disabled; no fake channel or comment data appears.
- Scope diagnosis: Google discovery lists `youtube.force-ssl` for `commentThreads.list` but not `youtube.readonly`. A mocked HTTP 403 `insufficientPermissions` maps to `insufficient_scope`, not reconnect-required. The explicit consent action requests only the readonly+force-ssl pair, validates returned scopes, and preserves the existing grant on denial or partial consent.
- The shared browser was disconnected during diagnosis, so no live Google status/reason was captured. Sanitized backend logging records only Google resource/method, HTTP status, allowlisted reason, mapped code, and safe message. Live consent and comment retrieval require your browser approval of the broader grant.
- Mocked tests cover OAuth CSRF/state replay and expiry, session isolation, PKCE/scope, encrypted token refresh and preservation, revocation cleanup, upload/comment pagination, ownership, production cookies, and Google error mapping.

## Implemented

- Responsive React + TypeScript workspace with actual authorized-channel uploads and paginated top-level comments.
- CSRF-protected server-side OAuth with single-use session-bound state, PKCE, opaque HttpOnly sessions, encrypted per-session Google tokens, disconnect/revocation, and refresh-token preservation.
- FastAPI `/health` and `/ready` endpoints, per-session channel/video/comment APIs, SQLAlchemy models, and an additive Alembic migration.
- Environment-based backend configuration and example environment files.
- Docker Compose PostgreSQL and Redis services, localhost-bound ports, health checks, and persistent named volumes.
- Focused backend health/readiness tests.

## Posting One Reply

Publishing is a single deliberate action, not a pipeline stage. A reply can be posted only when all of the following already hold: the draft has non-blank text, the review for the current revision is complete, all three quality checks are `pass`, the human decision is `approved`, the connected channel still owns the video and comment, and the grant includes `youtube.force-ssl`.

Select **Post reply to YouTube…** to open a preview. It shows the connected channel, the video and comment, the saved revision, and the exact text that will be posted, with a warning that the reply is public and immediate. Opening the preview and selecting **Cancel** send nothing. Only **Confirm & post** sends the request, and the server re-reads the saved text, approval, review, and ownership rather than trusting the browser.

YouTube offers no idempotency key, so ReplyAI records every attempt in its own storage before sending:

| Result | Meaning | What ReplyAI does |
| --- | --- | --- |
| Posted | YouTube returned the new reply's comment ID | Records the ID, offers no second post |
| Not posted | YouTube gave a documented refusal, so nothing was created | You may deliberately try again |
| Outcome unknown | The request was sent but no usable answer arrived | Offers no retry, because a repeat could post twice |

The unknown case is the important one. Timeouts, dropped connections, HTTP 5xx, and Google's `processingFailure` are all treated as unknown rather than as failures, because a reply may already be public. Check the video's replies manually. Nothing is retried automatically, ever, and no background job, schedule, or batch can post.

Because the attempt is committed before the request is sent, a crash mid-request leaves an unknown record that blocks any further publish for that draft until you resolve it. Editing the draft clears the local approval but does not unlock a post that may already be public. See [docs/AI_DRAFTS.md](docs/AI_DRAFTS.md) for the full workflow and [docs/RETENTION.md](docs/RETENTION.md) for storage and deletion.

## Not Implemented Yet

An OpenAI-compatible adapter, local reply drafts, independent classifications, quality review, local human decisions, and the three channel-scoped publishing modes are implemented. Automatic modes consider only newly generated eligible replies; there is no backlog processing or background worker. See [AI draft setup and workflow](docs/AI_DRAFTS.md).

The only mutating YouTube call in the application is `comments.insert`, used to post a single reply, and only on an explicit confirmation. The grant is broader than the app: Google says `youtube.force-ssl` permits viewing, editing, and permanently deleting videos, ratings, comments, and captions. Without it, comments and posting remain unavailable and show an insufficient-scope message; ReplyAI does not prompt the user to reconnect repeatedly or add the scope silently. **Update YouTube permissions** requests fresh consent for the exact readonly+force-ssl scope pair. ReplyAI never posts without a stored, reviewed, approved, unreplied draft and a matching confirmation. There is no scheduling, no background posting, no batch posting, and no automatic retry.

Duplicate prevention has three layers. Within one attempt, a claim token and a partial unique index stop two requests publishing the same draft. After an ambiguous result, an `unknown` attempt blocks retry until someone checks YouTube. After deletion, `published_reply_guards` keeps one row per channel and parent comment with no foreign key, so deleting the draft cannot erase the fact that a reply was already published there; a unique index on that pair refuses a second reservation. That last layer exists because `comments.insert` accepts no idempotency key, so with no local record nothing would stop a duplicate. The guard stores no reply text. This is not a claim that duplicates are impossible: replies created outside ReplyAI, a guard row deleted directly in the database, and an ambiguous send that YouTube honoured are all outside it.

## Data Safety

PostgreSQL and Redis data are stored in named Compose volumes and survive container restarts. `docker compose down` stops/removes containers but keeps those volumes. Do not use `docker compose down -v` unless you intentionally want to delete local database/cache data.

## Next Steps

1. Configure the external Google client JSON and encryption key, connect as the configured test user, and review the initial readonly grant.
2. Review Google's force-ssl disclosure, use **Update YouTube permissions** if acceptable, then verify uploads/comments.
3. Configure both LLM roles in the existing `backend/.env` as described in [AI_DRAFTS.md](docs/AI_DRAFTS.md).
4. Test one live comment through draft generation, review, editing, and local approval.
5. To post, open the reviewed, approved draft, read the confirmation dialog, and select **Confirm & post**. Watch the reply appear publicly on the video. Posting is never automatic.

See [docs/PROJECT_DOCUMENTATION.md](docs/PROJECT_DOCUMENTATION.md) for architecture and [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) for phase implementation and verification results.

## AI Draft Milestone

The user has confirmed live YouTube connection, permission upgrade, uploads, comment fetching, and one manually confirmed public reply posted from an approved draft. Earlier notes about pending live YouTube verification are historical. The failure, ambiguous-outcome, and concurrency branches are covered by automated tests with mocked providers only; neither has occurred against the real YouTube API. [AI_DRAFTS.md](docs/AI_DRAFTS.md) documents configuration, labels, persistence, revision rules, and browser testing. [RETENTION.md](docs/RETENTION.md) documents which events keep or destroy a draft: disconnecting, logging out, and session expiry all retain drafts and usage history, and only the explicit permanent-deletion action removes them. AI provider verification is mocked; no live AI success is claimed.
