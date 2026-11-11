# Features

Each feature lists what it does, how it works, where it appears in the web app, the main
implementation files, and its limits. Paths are relative to `backend/app/` or
`frontend/src/`.

**Pages:** Videos · Comment inbox · Posted · Jobs · Automation · Review queue · Settings

---

## Channel connection and content

### Google OAuth connection

- **What.** Connects one YouTube channel to a ReplyAI session.
- **How.** OAuth 2.0 authorization-code flow with PKCE and a single-use, expiring
  `state`. Read-only access first; reply permission (`youtube.force-ssl`) only through an
  explicit upgrade. Tokens are Fernet-encrypted in PostgreSQL. The browser holds an
  opaque HTTP-only cookie, and mutations require a CSRF header and an allowed origin.
  OAuth codes are redacted from URLs and logs.
- **UI.** **Connect YouTube** in the header; **Settings → Connected channel**
  (disconnect, update permissions).
- **Code.** `routers/auth.py`, `services/google_oauth.py`, `middleware/sensitive_query.py`.
- **Limits.** One channel per session. While the Google consent screen is in Testing,
  refresh tokens may expire and require reconnecting.

### Video synchronization

- **What.** Lists the channel's uploads and loads their comments.
- **How.** YouTube Data API reads with typed error mapping. Video context (title,
  description, chapters) is cached in Redis for 24 hours.
- **UI.** **Videos**: a carousel of uploads, the comment list and the reply workspace;
  **Sync videos** refreshes.
- **Code.** `routers/youtube.py`, `services/youtube_api.py`, `services/video_context.py`.
- **Limits.** Reads count against the YouTube quota; very large channels are paged.

### Comment inbox

- **What.** All comments across videos in one list.
- **How.** Each comment shows its draft or decision status (*Draft ready*, *Approved*,
  *Needs review*, *Blocked by policy*, *Posted*) and the reason. Statuses refresh in
  place after every saved change.
- **UI.** **Comment inbox**: search, a video filter, tabs for *All / Needs review /
  Drafts*, and the reply workspace beside the list.
- **Code.** `pages/InboxPage.tsx`, `hooks/useAppData.ts`, `components/SafeText.tsx`.
- **Limits.** Top-level comments only; threaded follow-ups are not drafted.

## Drafting and verification

### Context-aware AI generation

- **What.** Writes a reply grounded in the video and the channel's style.
- **How.** The primary model receives the comment, an evidence pack from the video
  (grounding variant `v1`) and the channel's **reply preferences**: tone, length, reply
  language and up to 2000 characters of channel instructions. It is told not to guess
  facts only the creator knows.
- **UI.** Reply workspace → **Generate**; **Settings → Reply preferences**.
- **Code.** `services/stage_pipeline.py`, `services/prompts.py`, `services/llm.py`,
  `services/reply_preferences.py`.
- **Limits.** Reply quality with real models is not yet validated.

### Independent second-model verification

- **What.** A different model checks every reply.
- **How.** The secondary model triages the comment (should it be answered at all?),
  classifies it independently (sentiment, intent, sarcasm, risky segments) and reviews
  the **saved** reply for relevance, unsupported claims and inappropriate content, with
  a confidence score. The two classifications must agree on sentiment and intent, and
  the review must pass with ≥ 0.8 confidence and no findings. Generator and verifier
  must be different, recorded models.
- **UI.** Reply workspace → **AI quality review** card; **Settings → AI review**
  explains the checks.
- **Code.** `services/llm.py` (`agreement`), `services/stage_pipeline.py`,
  `services/automation_policy.py` (two-model invariant).
- **Limits.** The verifier is itself a model and can be wrong; it is one input, never
  the only gate.

### LangGraph workflow engine

- **What.** An alternative orchestration of the same stages as a LangGraph `StateGraph`.
- **How.** `DRAFT_WORKFLOW_ENGINE=langgraph` runs context loading, triage, parallel
  generation and classification, a quality gate, quality review, persistence, policy
  evaluation and a protected publish hand-off as graph nodes. It uses the same
  provider-input contract as the default engine.
- **UI.** Transparent; drafts look the same.
- **Code.** `services/draft_graph.py`, `services/stage_inputs.py`.
- **Limits.** Default is `legacy` (sequential); both are covered by parity tests.

### Regeneration and AI refinement

- **What.** Get another draft, or adjust one with instructions.
- **How.** **Regenerate** creates a new revision and re-runs verification. **Refine with
  AI** is a chat: each instruction ("make it shorter") returns a suggestion you can apply
  or discard. Applying creates a new revision, which is verified again. Every mutation
  carries the revision it was based on, so a slow response never overwrites newer work.
- **UI.** Reply workspace → **Regenerate**, **Refine with AI**, **Save changes**.
- **Code.** `routers/drafts.py`, `services/refinement.py`, `DraftPanel.tsx`.
- **Limits.** In simulation, refinement only understands *shorter / more professional /
  friendlier / simpler*.

### Explainable decisions

- **What.** Every automatic outcome comes with a reason a person can read.
- **How.** `decide()` returns one class and a reason code. It is stored in
  `automation_decisions` with an input snapshot. The UI shows a plain-language reason
  ("Only you can answer this question") and the technical code.
- **UI.** Reply workspace → **Final decision** and **View technical details**; **Jobs**
  (latest note and code); **Automation → Recent decisions**; **Review queue** ("Why: …").
- **Code.** `services/automation_policy.py`, `services/automation_decisions.py`,
  `lib/decisions.ts`, `lib/jobText.ts`.
- **Limits.** Decisions are recorded, not re-evaluated; a historical draft keeps its
  original verdict.

## Safety controls

### Prompt-injection safeguards

- **What.** Comments that try to instruct the AI never auto-publish.
- **How.** Deterministic detection of override phrases, fake system/creator/verifier
  messages, dictated replies and credential requests (English and Hindi/Hinglish) →
  review. Hidden Unicode, bidi controls and confusable URLs → policy block. The models
  have no tools. Rule R12 blocks dictated text, and the reply guard replaces a dictated
  reply with a holding reply.
- **UI.** Review queue reasons "The comment tries to give instructions to the AI" and
  "The comment contains hidden characters"; hidden characters are shown as visible
  markers.
- **Code.** `services/input_analysis.py`, `services/reply_policy.py`,
  `services/reply_guard.py`, `components/SafeText.tsx`.
- **Limits.** Pattern-based detection can miss novel phrasing. See
  [ai-safety.md](ai-safety.md).

### Privacy and hallucination controls

- **What.** Keep personal data and invented facts out of public replies.
- **How.** R1 blocks personal data echoed from the comment (email, phone, UPI, PAN,
  Aadhaar); R2 holds contact details not on the allow-list. Creator-only questions go to
  the creator with a holding reply. The quality review flags unsupported claims.
- **UI.** "The reply repeats private contact details from the comment"; "Only you can
  answer this question".
- **Code.** `services/reply_policy.py`, `services/reply_guard.py`,
  `services/automation_policy.py`.
- **Limits.** Hallucination control depends on model judgement and is the least
  validated area.

### Sarcasm handling

- **What.** Don't thank people for complaints.
- **How.** Model sarcasm labels plus deterministic cues → review. If the generated reply
  read the comment literally, the reply guard replaces it with a neutral one that asks
  what went wrong, and keeps the original in technical details.
- **UI.** "The comment may be sarcastic" notice in the workspace and queue.
- **Code.** `services/automation_policy.py`, `services/reply_guard.py`.
- **Limits.** Cues are heuristic.

### English, Hindi and Hinglish

- **What.** Handles Devanagari and romanized Hindi as well as English.
- **How.** Language-aware triage; the language contract sends script/label mismatches to
  review; promise, injection and holding-reply texts exist in all three forms; reply
  language is a channel preference.
- **UI.** **Settings → Reply preferences → Language**.
- **Code.** `services/input_analysis.py`, `services/reply_policy.py`,
  `services/reply_guard.py`.
- **Limits.** Other languages are skipped as unsupported.

### Human review

- **What.** One place for everything a person should decide.
- **How.** Drafts classed `HUMAN_REVIEW` or `POLICY_BLOCK` appear with their reason and
  the comment text. Open the draft to edit, approve and post, or dismiss the item.
- **UI.** **Review queue** (filters *All / Needs review / Blocked by policy*).
- **Code.** `routers/automation.py` (review router), `pages/ReviewQueuePage.tsx`.
- **Limits.** The person who approves a reply is responsible for its final text; the
  queue explains the concern but does not rewrite the reply.

## Automation and reliability

### Automatic reply publication

- **What.** Hands-off replying for comments that pass every check.
- **How.** Opt-in discovery polls the channel, queues new comments and processes them in
  the background. Publishing modes: **Manual review** (default; nothing posts without
  confirmation), **Dry run** (decide and record only), **Auto-publish on agreement** and
  **Approve all** (same eligibility rules, labelled separately). Hourly and daily caps
  apply (10 / 50 by default). Per-channel allow-lists define legitimate links, contacts
  and handles.
- **UI.** **Automation** (on/off, pause, **Poll now**, recent decisions);
  **Settings → Publishing mode**, **Settings → Automatic operation**.
- **Code.** `services/discovery.py`, `worker.py`, `services/job_pipeline.py`,
  `routers/publishing_settings.py`.
- **Limits.** Automatic publication to a real channel has not been validated; only
  manual, human-confirmed posting has been exercised on a test channel.

### Durable background jobs

- **What.** Work survives restarts and runs across several workers.
- **How.** `reply_jobs` is a PostgreSQL queue. Claims are conditional `UPDATE`s with a
  120 s lease renewed by heartbeats between stages. Per-channel concurrency is derived
  from live leases.
- **UI.** **Jobs**: counts by status, filter, latest note, decision code, attempts,
  **Requeue**, **Reconcile**, **Enqueue jobs**.
- **Code.** `services/job_queue.py`, `routers/batch.py`, `pages/JobsPage.tsx`.
- **Limits.** Polling-based (no push notifications from YouTube).

### Retry and recovery

- **What.** Temporary failures retry; crashes recover without repeating paid work.
- **How.** Typed provider failures → `RETRY` with backoff or `TECHNICAL_FAILURE`.
  Retries and crash recoveries have separate caps (3 each). A job that crashed after
  verification resumes at publication. Budget deferrals keep the job's progress.
- **UI.** **Jobs** status *Will retry* / *Failed*; **Reconcile** recovers expired
  leases on demand.
- **Code.** `services/job_queue.py`, `services/stage_outcomes.py`,
  `services/job_pipeline.py`.

### Duplicate-publication prevention

- **What.** At most one ReplyAI reply per comment, ever.
- **How.** Revalidate the comment, then reserve it in `published_reply_guards` (unique
  per channel and comment) together with an attempt row, commit, and only then send. A
  lost response is `unknown` and is never retried automatically; **Check YouTube**
  resolves it.
- **UI.** Reply workspace confirmation dialog; **Posted** (*Posted / Failed /
  Unconfirmed*).
- **Code.** `routers/publication.py`, `services/publishing.py`,
  `services/revalidation.py`, `services/publication_reconcile.py`.
- **Limits.** Cannot see replies made by other tools.

### Token monitoring and estimated capacity

- **What.** How much model budget has been used, and roughly how many replies still fit.
- **How.** Sums ReplyAI's own per-draft usage ledger over a rolling 24 hours, per role,
  against locally configured ceilings (`LLM_*_TOKENS_PER_DAY`, `LLM_REQUESTS_PER_DAY`).
  After at least 10 completed replies it averages the most recent ones and estimates the
  remaining replies, limited by the busier role. Polls every 10 s.
- **UI.** **Settings → LLM usage**: gauge, per-role bars, the replies-remaining estimate.
  Simulated numbers are labelled *Simulated counts*.
- **Code.** `routers/usage.py`, `components/TokenUsageGauge.tsx`.
- **Credit.** The gauge's original design and polling logic are by teammate
  [Hrishik1033](https://github.com/Hrishik1033)
  ([PR #3](https://github.com/RD-blip1/ReplyAI/pull/3)).
- **Limits.** A local estimate, **not the provider's quota**: other apps sharing the key
  and the provider's reset time are invisible.

## Environment and interface

### Simulation mode

- **What.** The whole app without Google or model providers.
- **How.** Replaces only the YouTube API and the models with a fake channel and scripted
  outputs; everything in between is real code. Guarded to development and `_sim`
  databases.
- **UI.** Green **Simulation mode** banner, **Connect simulated channel**, **Simulation
  controls** panel.
- **Code.** `simulation.py`, `components/SimulationPanel.tsx`, `scripts/dev.sh`.
- **Limits.** Scripted outputs prove wiring, not intelligence. See the
  [simulation guide](simulation-guide.md).

### Green Glow interface

- **What.** The responsive React workspace.
- **How.** Dark theme with green accents, built with React 19, TypeScript and Tailwind
  CSS v4. Hash routing (`#videos`, `#inbox`, …) so it can be served as static files. An
  unsaved-changes guard protects edits. Layouts adapt from desktop to phone width.
- **Code.** `index.css`, `components/AppShell.tsx`, `components/ui.tsx`, `pages/`.
- **Limits.** English UI only. Typefaces load from Google Fonts, so they fall back to
  system fonts offline.
