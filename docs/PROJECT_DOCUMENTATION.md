# ReplyAI Project Documentation

## Purpose

ReplyAI connects one browser session to an authorized channel, displays uploads, and lets the owner publish one public reply per comment. The initial OAuth grant is `youtube.readonly`; comment retrieval and reply posting require an optional explicit `youtube.force-ssl` permission update. Local AI drafts, model review, human decisions, and manual publishing are implemented. Phase 3 adds channel-scoped Manual review, Auto-publish on agreement, and Approve all modes; automated sends are limited to eligible newly generated replies and retain the same ownership, permission, review, duplicate, rate-limit, and publication-history guards. See [AI_DRAFTS.md](AI_DRAFTS.md).

## Architecture

- `frontend/`: React, TypeScript, Vite, and Tailwind CSS. It holds CSRF in component memory and never receives OAuth credentials or tokens.
- `backend/`: FastAPI, Pydantic settings, SQLAlchemy async PostgreSQL, Alembic, Redis health, Google OAuth, YouTube read APIs, and the single confirmed `comments.insert` write path.
- `compose.yaml`: PostgreSQL and Redis only. The frontend and API run directly in WSL.
- PostgreSQL and Redis ports bind to `127.0.0.1`; named volumes retain their data across container restarts.
- Backend `/health` reports application liveness. `/ready` performs `SELECT 1` and a Redis `PING`, reports only availability labels, and returns HTTP 503 if either check fails.
- OAuth uses the Google-maintained `google-auth-oauthlib` Flow with PKCE S256, a random session-bound single-use state, offline access, and `prompt=consent`. Initial connection requests only `youtube.readonly`; the CSRF-protected upgrade route requests exactly `youtube.readonly` plus `youtube.force-ssl` only after explicit user action. Unrelated prior project scopes are not automatically included.
- Sessions use random HttpOnly SameSite=Lax cookies. State-changing auth endpoints validate the allowed Origin and CSRF header. The callback query is cleared from the ASGI scope after route handling so Uvicorn access/error logs cannot record code or state.
- Google tokens are Fernet-encrypted at rest with a separately configured key. Connections are keyed to their owning session; channel/video/comment routes never query a global credential.
- Videos are read from the channel uploads playlist with pagination. Each comments request checks video membership in that playlist, then paginates top-level comment threads. Replies are not fetched or shown, and media collections are not persisted.
- Google's current discovery schema lists `youtube.readonly` and `youtube.force-ssl` for `channels.list` and `playlistItems.list`, but only `youtube.force-ssl` for `commentThreads.list` and `comments.insert`.
- Posting is durable rather than fire-and-forget: each attempt is committed to `reply_publications` before the request, a partial unique index allows one unresolved attempt per draft, and the write client never retries a request or retries it after a token refresh.
- Duplicate prevention has three separate layers, and they are not interchangeable:
  1. **Within one attempt** — the claim token and the partial unique index on `reply_publications` stop two requests publishing the same draft.
  2. **After an ambiguous result** — an `unknown` attempt blocks retry until a person checks YouTube, because a lost response cannot be distinguished from a reply that landed.
  3. **After deletion** — `published_reply_guards` keeps one row per `(channel_id, parent_comment_id)` with no foreign key, so deleting the draft cannot erase the fact that ReplyAI already replied there. A unique index on that pair, not an application-level check, is what refuses a second reservation.

  Layers 1 and 2 live in `reply_publications`, which is draft-scoped and cascades. Layer 3 exists because `comments.insert` accepts no idempotency key: with no local record of a prior post, a fresh draft would post a duplicate and nothing would object.

  The guard holds no reply text and no video id. See [RETENTION.md](RETENTION.md).

## Local Setup

Requirements: Python 3.12+, Node.js 22+, npm, Docker Engine, and the Docker Compose plugin. The current WSL account must have Docker access; after adding it to the `docker` group, open a new WSL session before using Docker directly.

From the repository root:

```bash
cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
docker compose up -d postgres redis
```

Create the Python environment and install the backend plus development dependencies:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

In another WSL terminal, install and run the frontend:

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. The browser API base is configured by `frontend/.env.local`; its development default is `http://127.0.0.1:8001`. Restart Vite after changing that value.

### Google OAuth Configuration

Keep the downloaded Web client JSON outside this repository. For a typical download in `~/Downloads`, use `install -d -m 700 ~/.config/replyai`, move `client_secret_*.json` to `~/.config/replyai/google-oauth-client.json`, and run `chmod 600` on it. Set `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` in `backend/.env` to the absolute WSL path from `realpath`; never print or share the file contents. Generate `TOKEN_ENCRYPTION_KEY` with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` and place it in `backend/.env`; do not share the key. The Google Console redirect URI must be exactly `http://127.0.0.1:8001/auth/google/callback`. Initial Connect requests `https://www.googleapis.com/auth/youtube.readonly`; the explicit comment update requests that plus `https://www.googleapis.com/auth/youtube.force-ssl`.

OAuth configuration is optional for service startup. Without it, `/api/session` provides setup instructions and `/auth/google/start` responds with `oauth_setup_required`; `/health` and `/ready` remain usable. Live Google consent occurs only after the user selects Connect YouTube, Reconnect YouTube, or Update YouTube permissions.

After changing `backend/.env`, stop the API with Ctrl+C and start Uvicorn again so the new path/key are loaded. Confirm the Google account is listed as an OAuth consent-screen test user and both scopes are configured in OAuth data access.

For live verification, open the Vite URL and select Connect YouTube. To retrieve comments, read the disclosure: Google describes `youtube.force-ssl` as allowing apps to see, edit, and permanently delete YouTube videos, ratings, comments, and captions. ReplyAI makes list/read calls plus one `comments.insert` per confirmed reply. Select **Update YouTube permissions** and approve or decline consent yourself. Denied or partial permission leaves the existing connection intact. Check the channel, uploads, watch links, and paginated top-level comments after a successful update. Disconnect removes the local token row and requests Google's revocation. Never paste an authorization code or token into the frontend.

## Verification

Run backend checks from `backend/` with its virtual environment active:

```bash
pytest
ruff check .
```

Run frontend checks from `frontend/`:

```bash
npm run build
npm run lint
```

Check Compose configuration from the repository root with `docker compose config --quiet`. Once the containers are running, `docker compose ps` should show PostgreSQL and Redis as healthy, and `curl -i http://127.0.0.1:8001/ready` should return HTTP 200 with both dependencies available. `/health` should return HTTP 200 independently of database or Redis state.

### Phase 2 Verification

- `backend/.venv/bin/python -m pytest`: 29 passed; final focused OAuth rerun: 20 passed after adding one parameter case (30 total verified cases). One upstream Starlette deprecation warning notes its `httpx` test client.
- `backend/.venv/bin/ruff check .`, `frontend/npm run build`, and `frontend/npm run lint`: passed.
- Alembic upgrade/downgrade/re-upgrade passed using disposable SQLite. The existing Compose PostgreSQL database is migrated to `20260926_01`; the revision adds only Phase 2 tables/indexes.
- Compose config validated; PostgreSQL and Redis are healthy. Live `/health` and `/ready` return HTTP 200.
- Browser checks at 1280px and 320px show no horizontal overflow. The permission-upgrade notice describes Google's grant before redirecting.
- OAuth and YouTube responses in tests are mocked. No live Google sign-in, consent, channel, video, or comment request has been verified.
- The shared browser available during the failure investigation had no connected session. Permission-upgrade behavior and denial handling were verified synthetically.

### Comment Permission Upgrade

The failing call is `GET /api/youtube/videos/{video_id}/comments`. Before the permission upgrade, ReplyAI called Google `playlistItems.list` with the channel uploads playlist and selected video ID to verify ownership. That method accepts `youtube.readonly`. After ownership succeeded, it called Google `commentThreads.list`; its discovery schema does not accept `youtube.readonly` and lists `youtube.force-ssl` instead. The expected/mocked Google response is HTTP 403 with reason `insufficientPermissions`. The previous error mapper treated that as revoked credentials and returned `reconnect_required`, which encouraged reconnect loops that could never fix the grant.

The UI offers a separate, explicitly confirmed **Update YouTube permissions** action. It requests exactly `youtube.readonly` and `youtube.force-ssl` with fresh consent, while ordinary initial Connect remains readonly. The backend binds requested scopes and upgrade intent to encrypted, session-bound state, validates the actual callback scope set, and changes the stored token/scopes only after the required grant and channel fetch succeed. Denied/partial upgrades leave the old readonly connection intact. The local comments route rejects missing force-ssl before making Google calls; granted connections still undergo video ownership checks. Scope-change exceptions from OAuthlib are validated locally without relaxing checks globally. A different channel during upgrade is rejected, and stored refresh tokens are reused only for the same channel. Explicit updates can be retried when Google rejects a scope despite the stored scope list. Reauth preserves previously granted scopes. The app's only mutating YouTube call is the `comments.insert` used to post one confirmed reply, which is reached only through `POST /api/drafts/{id}/publication` after a durable claim. However, Google describes `youtube.force-ssl` as allowing apps to edit and permanently delete YouTube content, so users must explicitly accept that broader grant.

The mapper returns HTTP 403 `insufficient_scope` with a safe message, and the UI leaves the valid channel connected rather than showing reconnect. Sanitized logs record only Google resource/method, status, allowlisted reason, mapped code, and safe message. The shared browser was disconnected, so no live Google status/reason was captured; upgrade/403/denial paths are verified with synthetic provider responses. Ownership checks remain enabled.

## Environment and Data Safety

The committed `.env.example` files contain local-only development placeholders, not production credentials. Actual `.env` files are ignored. Change the backend PostgreSQL URL when changing the Compose `POSTGRES_*` values. Keep the OAuth client JSON outside the repository and `TOKEN_ENCRYPTION_KEY` only in the ignored `backend/.env`; never send either value in chat or logs. Do not commit secrets or reuse the development database password. Compose volumes are named `replyai_postgres_data` and `replyai_redis_data`; stopping services does not delete them. Do not use `docker compose down -v` unless intentionally deleting local data.

## Not Yet Implemented

- LangGraph workflows, bulk approval, or background automation
- Scheduled, automatic, or batch posting; a reply is posted only by explicit confirmation
- Editing or deleting existing YouTube comments, ratings, or videos
- Reply-thread loading; only top-level comments are shown

## Next Steps

1. Configure the external OAuth client path and encryption key, then connect as the test user with readonly access.
2. Review Google's force-ssl disclosure and explicitly approve **Update YouTube permissions** if its broader access is acceptable.
3. Configure the six backend LLM variables in [AI_DRAFTS.md](AI_DRAFTS.md).
4. Verify a live single-comment draft and review, then post one reply through the confirmation dialog and confirm it appears publicly on the video.

## Official References

- [Google OAuth 2.0 web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server)
- [google-auth-oauthlib Flow reference](https://google-auth-oauthlib.readthedocs.io/en/latest/reference/google_auth_oauthlib.flow.html)
- [YouTube playlistItems.list](https://developers.google.com/youtube/v3/docs/playlistItems/list)
- [YouTube commentThreads.list](https://developers.google.com/youtube/v3/docs/commentThreads/list)
- [YouTube channels.list](https://developers.google.com/youtube/v3/docs/channels/list)

Current completion checks are recorded in [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md#interrupted-permission-upgrade-completion-2026-09-26); earlier migration/layout observations are historical. Google scope descriptions: [YouTube OAuth scopes](https://developers.google.com/identity/protocols/oauth2/scopes#youtube).

## Current live verification

The user confirmed live channel connection, permission upgrade, video loading, and comment fetching, and one manually confirmed public reply posted from an approved draft. Prior Phase 2 statements that live verification was pending describe earlier testing, not the current user-confirmed state. The local AI draft milestone is documented in [AI_DRAFTS.md](AI_DRAFTS.md); its provider tests are mocked.

## What duplicate protection does and does not claim

ReplyAI prevents a knowingly repeated publication for a parent comment when its durable
local evidence says it already posted there, and ambiguous sends stay blocked rather than
being blindly retried.

It does **not** claim it can never produce duplicate YouTube replies. Three things stay
outside it: a reply created outside ReplyAI, a person deleting a `published_reply_guards`
row directly in the database, and the irreducible case where YouTube created a comment and
the confirmation was lost. That last one is why an ambiguous result blocks rather than
retries. YouTube provides no idempotency key for `comments.insert`, so none of this is
delegated to the provider.
