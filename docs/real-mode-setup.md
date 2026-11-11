# Real-mode setup

Real mode connects ReplyAI to **your YouTube channel** through Google OAuth and to **real
LLM providers**. Complete the [installation](installation.md) first, and try the
[simulation guide](simulation-guide.md) so you know what each page does.

> **Validation status.** Connecting a test channel, reading videos and comments, and
> posting replies **manually with a human confirmation** have been exercised. Systematic
> reply-quality evaluation with real models and **automatic** publication on a real
> channel **have not been validated yet**. The automatic path (discovery, worker,
> automatic posting) is covered by tests with mocked responses, not by live runs. Start in **Manual review** or
> **Dry run**, and read [limitations.md](limitations.md) before enabling an automatic mode.

## 1. Google Cloud project and YouTube Data API

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project
   (or pick one).
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**: choose **External** (fine for personal
   testing), fill in the app name and support email, and add the Google account that
   owns your channel under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URI: `http://127.0.0.1:8001/auth/google/callback`

   The URI must equal `GOOGLE_REDIRECT_URI` in `backend/.env` character for character
   (`127.0.0.1` and `localhost` are different hosts to Google).
5. Download the client JSON and store it **outside the repository**:

   ```bash
   install -d -m 700 ~/.config/replyai
   mv ~/Downloads/client_secret_*.json ~/.config/replyai/google-oauth-client.json
   chmod 600 ~/.config/replyai/google-oauth-client.json
   realpath ~/.config/replyai/google-oauth-client.json
   ```

While the consent screen is in **Testing** status, only listed test users can connect,
and Google may expire refresh tokens after a short period, so you'll have to reconnect.
Check Google's current OAuth policies if you need longer-lived access.

## 2. Backend OAuth settings

In `backend/.env`:

```env
GOOGLE_OAUTH_CLIENT_SECRETS_FILE=/home/you/.config/replyai/google-oauth-client.json
GOOGLE_REDIRECT_URI=http://127.0.0.1:8001/auth/google/callback
TOKEN_ENCRYPTION_KEY=<generated below>
FRONTEND_BASE_URL=http://127.0.0.1:5173
```

Generate the Fernet key that encrypts stored Google tokens, and back it up securely.
Losing it makes stored connections unreadable, and you'll have to reconnect.

```bash
backend/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

## 3. Permissions (OAuth scopes)

ReplyAI asks for the least access first:

| Step in the UI | Scope | Allows |
| --- | --- | --- |
| **Connect YouTube** | `youtube.readonly` | Channel and video listing |
| **Update YouTube permissions** | `youtube.force-ssl` | Reading comment threads and posting replies |

The broader scope is requested only when you choose **Update YouTube permissions**.
Tokens are stored encrypted in PostgreSQL, and the browser only receives an opaque,
HTTP-only session cookie. **Disconnect channel** in Settings removes the stored
connection.

## 4. Model providers

ReplyAI uses two roles through any **OpenAI-compatible chat-completions** API. The base
URL is the API prefix; the adapter appends `/chat/completions`.

| Role | Does | Suggested Groq model |
| --- | --- | --- |
| Primary | Writes the reply and classifies the comment | `openai/gpt-oss-120b` |
| Secondary | Triage, independent classification, quality review | `openai/gpt-oss-20b` |

```env
PRIMARY_LLM_BASE_URL=https://api.groq.com/openai/v1
PRIMARY_LLM_API_KEY=<your key>
PRIMARY_LLM_MODEL=openai/gpt-oss-120b
SECONDARY_LLM_BASE_URL=https://api.groq.com/openai/v1
SECONDARY_LLM_API_KEY=<your key>
SECONDARY_LLM_MODEL=openai/gpt-oss-20b
```

- Drafting stays disabled until **all six** values are set.
- The two roles must be **different models**. Automatic publication refuses a draft
  whose generator and verifier are the same model (`two_llm_invariant_violated`).
- Models must support JSON-mode chat completions; outputs are validated against strict
  schemas, and malformed output is a technical failure, not a decision.
- Set `LLM_PRIMARY_TOKENS_PER_DAY` and `LLM_SECONDARY_TOKENS_PER_DAY` to your plan's
  daily token limits so the Settings token meter shows meaningful percentages.

Keys are loaded as secrets and never logged or returned by the API.

## 5. Start and connect your channel

```bash
scripts/dev.sh start real
```

1. Open <http://127.0.0.1:5173> and click **Connect YouTube**. Sign in with the test user
   that owns the channel.
2. Check that the header shows the **intended channel**. ReplyAI ties all drafts,
   settings and jobs to the verified channel id, so connecting a different Google
   account shows a different, separate workspace.
3. **Settings → Update YouTube permissions** to grant `youtube.force-ssl`.
4. **Videos** → pick a video → comments should load.

## 6. Recommended rollout

1. **Manual review** (the default). Generate drafts for a few comments, read the AI
   quality review and the final decision, edit and approve, then post one reply yourself
   through the confirmation dialog. Verify it on YouTube.
2. **Dry run.** Turn on discovery on the **Automation** page. ReplyAI makes and records
   every automatic decision, but sends nothing. Review the **Jobs** and **Automation**
   pages: would you have posted every reply marked *Dry run: would publish*?
3. **Allow-lists.** Under **Settings → Automatic operation**, add the domains, contacts
   and handles your replies may legitimately contain. Anything else is held.
4. **Auto-publish on agreement**, only once dry-run decisions look right, with the
   default caps (10 per hour, 50 per day) or lower.

You can **pause** or turn off automation at any time from the Automation page. Queued
jobs wait while it is paused and go to review if it's turned off.

## 7. Publication safety in real mode

Before any reply is sent, ReplyAI:

1. checks eligibility, permission scope and the hourly/daily caps;
2. **re-reads the comment** and stops if it was edited, deleted or already answered by
   you;
3. reserves the comment in a unique database guard, so it can be answered at most once;
4. records the attempt **before** calling `comments.insert`;
5. settles it as `posted`, `failed` or `unknown`.

If YouTube's response is lost, the attempt becomes **unknown** and is **never retried
automatically**. Use **Check YouTube** on the draft to search the thread read-only, then
record the outcome.

## 8. Rate limits and quota

| Limit | Setting | Default | Why |
| --- | --- | --- | --- |
| LLM requests per minute / day | `LLM_REQUESTS_PER_MINUTE`, `LLM_REQUESTS_PER_DAY` | 28 / 900 | Below Groq's free-plan 30 RPM / 1000 RPD |
| YouTube worker reads per minute | `YOUTUBE_READS_PER_MINUTE` | 30 | Spreads comment lookups |
| YouTube quota units per day | `YOUTUBE_UNITS_PER_DAY_BUDGET` | 8000 | Below the default 10,000-unit project quota |
| Automatic replies per hour / day | `AUTO_PUBLISH_MAX_PER_HOUR`, `AUTO_PUBLISH_MAX_PER_DAY` | 10 / 50 | Limits blast radius |

These counters are **ReplyAI's own** and are shared by every API and worker process.
They are not the provider's live quota: other applications using the same API key or
Google project, and the provider's own reset schedule, are invisible to ReplyAI. Check
your provider's limits page and the Google Cloud quota page, and set the values to
match. Posting a reply costs considerably more YouTube quota than a read, so the daily
unit budget limits how many replies a day are possible.

When a budget runs out, jobs are rescheduled (`RETRY`), not failed and not sent to
review.

## 9. Disconnecting and data

- **Settings → Disconnect channel** removes the stored Google tokens.
- Drafts and history belong to the channel and survive logout and reconnection. What is
  kept and how to delete it: [data-retention.md](data-retention.md).
- You can also revoke ReplyAI's access in your Google Account under
  **Security → Third-party apps & services**.
