# ReplyAI — AI-Powered YouTube Comment Management and Automated Reply Assistant

**ReplyAI drafts replies to YouTube comments with one LLM, has a second, independent LLM
check them, and lets a reply go out automatically only when deterministic safety rules
also agree. Everything else goes to the creator for review.**

It is a full-stack AI-agent project: a FastAPI backend with a durable PostgreSQL job
queue and background workers, LangGraph-orchestrated model stages, a React workspace, and
a built-in simulation mode that lets you run the whole system without a Google account
or paid model calls.

![ReplyAI video library and comments, connected to a real YouTube test channel](docs/assets/screenshots/videos.png)

<sub>Real mode: ReplyAI connected to the author's YouTube test channel. Commenter names
and avatars are blurred. Model calls were deliberately switched off while capturing, which
is why the workspace shows "AI setup required". Each screenshot below says whether it
shows real or simulated data.</sub>

---

## Contents

- [Why ReplyAI exists](#why-replyai-exists)
- [Features](#features)
- [Architecture](#architecture)
- [Screenshots](#screenshots)
- [Technology stack](#technology-stack)
- [Quick start](#quick-start)
- [Simulation mode](#simulation-mode)
- [Safety and reliability](#safety-and-reliability)
- [Validation status](#validation-status)
- [Documentation](#documentation)
- [Acknowledgments](#acknowledgments)
- [Contributing](#contributing)
- [License](#license)

## Why ReplyAI exists

Creators get more comments than they can answer. Many deserve a reply: questions about
the video, thanks, corrections. Many do not: spam, abuse, off-topic requests, and
attempts to make the channel say something it shouldn't.

Handing that inbox to a single LLM with "reply helpfully" fails in predictable ways:

- it answers questions the video never covered, and **invents facts** to do it;
- it **follows instructions hidden in the comment** ("ignore previous instructions…");
- it **echoes a viewer's phone number**, posts an unvetted link, or promises a sequel;
- it takes **sarcasm literally** and thanks someone for a complaint;
- a crash or a lost network response leads to **duplicate replies**;
- a rate-limit error gets mistaken for a content judgement.

A public reply goes out under the creator's name and can't be quietly taken back.
ReplyAI therefore treats automatic replying as a **decision problem**, not just text
generation:

1. A **primary model** writes the reply.
2. A **secondary model** independently triages the comment, classifies it, and reviews
   the saved reply.
3. **Deterministic code** checks the comment for concealed instructions and the reply for
   personal data, unapproved links, promises and hidden characters.
4. One **decision policy** combines all of it into a single recorded outcome. Only
   `AUTO_PUBLISH` can reach YouTube, through a publication path built so a comment can
   never be answered twice.

Automation is **off by default**. The default publishing mode is **manual review**, every
decision is explained, and anything uncertain goes to a human review queue.

## Features

| Area | What ReplyAI does |
| --- | --- |
| **Google OAuth connection** | OAuth 2.0 with PKCE; read-only scope first, reply permission requested only by an explicit step; tokens encrypted at rest |
| **Video synchronization** | Lists the connected channel's uploads, with a Redis-cached video context (title, description, chapters) |
| **Comment inbox** | All comments across videos, filterable by *needs review* / *drafts*, with a per-comment status and a reply workspace |
| **Context-aware AI generation** | Replies grounded in a "scope card" of the video and the channel's tone, length, language and custom instructions |
| **Independent second-model verification** | A different, separately configured model triages, classifies and reviews each reply; the two models must agree |
| **Automatic reply publication** | Opt-in discovery polls for new comments; eligible replies post automatically under hourly/daily caps |
| **Human review** | A review queue lists every held comment with a plain-language reason; approve, edit, reject or dismiss |
| **Prompt-injection safeguards** | Detection of override phrases, fake system/creator messages, dictated replies and hidden Unicode; such comments never auto-publish |
| **Privacy and hallucination controls** | Reply rules block echoed personal data; creator-only questions ("which GPU did you use?") go to the creator instead of being guessed |
| **Sarcasm handling** | Model labels plus deterministic cues route possible sarcasm to review; a guard replaces a literal-reading reply with a neutral one |
| **English / Hindi / Hinglish** | Language-aware triage, Devanagari and romanized-Hindi rules, and a language contract that flags mismatches |
| **Regeneration and refinement** | Regenerate a draft, or refine it with chat instructions ("make it shorter"); every change is a new, re-verified revision |
| **Simulation mode** | Fake channel and scripted models exercise every workflow locally; the safety code in between is the real code |
| **Durable background jobs** | PostgreSQL-backed queue with lease-based claims, heartbeats and per-channel concurrency limits |
| **Retry and recovery** | Typed provider failures, backoff, separate retry and crash-recovery caps, and resumption without repeating paid work |
| **Duplicate-publication prevention** | A unique database guard per comment, revalidation before posting, and lost responses recorded as `unknown`, never retried blindly |
| **Token monitoring and capacity** | A 24-hour token/request gauge per model role and a local estimate of how many replies still fit |
| **Explainable decisions** | Each automatic decision is stored with its class, reason code and inputs, shown as a decision trace |
| **Green Glow interface** | A responsive dark React workspace: Videos, Inbox, Posted, Jobs, Automation, Review queue, Settings |

Every feature is described in detail, with its UI location and its limits, in
[docs/features.md](docs/features.md).

## Architecture

```mermaid
flowchart TD
    subgraph Clients
        UI["React web app<br/>(Vite, TypeScript)"]
    end

    subgraph Backend["FastAPI backend (one codebase)"]
        API["API process<br/>REST + OAuth + session/CSRF"]
        W["Worker process<br/>discovery loop + job runner"]
    end

    YT[("YouTube Data API v3")]
    PG[("PostgreSQL<br/>jobs, leases, drafts, decisions,<br/>budgets, publication guards")]
    RD[("Redis<br/>video-context cache")]

    UI -- "REST, session cookie" --> API
    API --> PG
    W --> PG
    API -. cache .-> RD
    W -. cache .-> RD

    YT -- "new comments" --> DISC["Discovery<br/>(opt-in, lease-fenced)"]
    W --- DISC
    DISC --> Q[("Durable job queue<br/>reply_jobs")]
    Q -- "claim with lease" --> PIPE

    subgraph PIPE["Shared stage pipeline (UI, worker and LangGraph paths)"]
        IN["Input triage<br/>secondary LLM + deterministic input signals"]
        GEN["Generation<br/>primary LLM"]
        CLS["Independent classification<br/>secondary LLM"]
        QR["Quality review of saved reply<br/>secondary LLM"]
        IN --> GEN & CLS
        GEN --> QR
        CLS --> QR
    end

    PIPE --> POL{"Deterministic decision policy<br/>reply rules R1–R12, caps, mode"}
    POL -- "AUTO_PUBLISH" --> PUB["Guarded publication<br/>revalidate → claim → insert → settle"]
    POL -- "HUMAN_REVIEW" --> RQ["Review queue"]
    POL -- "POLICY_BLOCK" --> RQ
    POL -- "SKIP" --> DONE["Finished, nothing posted"]
    POL -- "RETRY / TECHNICAL_FAILURE" --> Q
    PUB --> YT
    RQ -- "creator approves" --> PUB

    LLM[("Model providers<br/>OpenAI-compatible APIs")]
    PIPE -. "chat completions" .-> LLM
```

- **FastAPI API** serves the web app, Google OAuth, interactive drafting and the single
  guarded publication path.
- **Worker** (`python -m app.worker`) runs discovery and processes queued jobs. The API and
  worker share one codebase and keep no safety-relevant state in memory, so several of
  each can run side by side.
- **PostgreSQL** holds all durable state, including the job queue, leases, shared
  rate-limit counters and the one-reply-per-comment guard.
- **Redis** is a best-effort 24-hour cache for video context; the app keeps working
  without it.
- **Model providers** are any OpenAI-compatible chat-completions endpoints (Groq is the
  documented default). The primary and secondary roles must be different models.
- **LangGraph** can run the same stages as a `StateGraph`
  (`DRAFT_WORKFLOW_ENGINE=langgraph`); both engines share one stage contract.

Details, sequence diagrams and the decision order are in
[docs/architecture.md](docs/architecture.md).

## Screenshots

**Real mode**, the author's YouTube test channel (commenter identities blurred):

| | |
| :---: | :---: |
| ![Comment inbox, real mode](docs/assets/screenshots/inbox.png) | ![Settings with token meter, real mode](docs/assets/screenshots/settings.png) |
| **Comment inbox**: real comments on a test video, filtered by video | **Settings**: token meter and replies-remaining estimate from real usage records, publishing modes |
| ![Posted history, real mode](docs/assets/screenshots/posted.png) | |
| **Posted**: replies that were confirmed by hand and posted to the test channel | |

**Simulation mode**: the automation pages need automatic runs, which have only been
exercised with the fake channel and scripted models (note the *Simulation mode* banner):

| | |
| :---: | :---: |
| ![Review queue, simulation](docs/assets/screenshots/review-queue.png) | ![Automation page, simulation](docs/assets/screenshots/automation.png) |
| **Review queue**: every held comment with the reason it was held | **Automation**: discovery status and recent decisions |
| ![Jobs page, simulation](docs/assets/screenshots/jobs.png) | |
| **Jobs**: the durable queue with status, attempts and decision codes | |

## Technology stack

| Layer | Technology | Role in ReplyAI |
| --- | --- | --- |
| API | **Python 3.12, FastAPI, Pydantic v2** | Typed REST API, strict schemas for every model output |
| Orchestration | **LangGraph** | Optional `StateGraph` engine for the draft workflow (parallel generation and classification) |
| Models | **OpenAI-compatible chat completions** via `httpx` | Primary (generator) and secondary (verifier) roles; Groq by default |
| Database | **PostgreSQL 17, SQLAlchemy 2 (async), asyncpg, Alembic** | Durable state, job queue, leases, budgets, guards; additive migrations |
| Cache | **Redis 7** | Video-context cache |
| YouTube | **Google API client, google-auth-oauthlib** | OAuth with PKCE, YouTube Data API v3 |
| Security | **cryptography (Fernet)** | Encryption of stored OAuth tokens |
| Frontend | **React 19, TypeScript, Vite, Tailwind CSS v4** | Single-page workspace with hash routing |
| Tooling | **Docker Compose, uv, Ruff, oxlint** | Local services, locked Python dependencies, linting |

## Quick start

You need **Git, Python 3.12+, Node.js 22+ and Docker** (Compose v2).

```bash
git clone https://github.com/RD-blip1/ReplyAI.git
cd ReplyAI

cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local

docker compose up -d --wait postgres redis

cd backend && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' \
  && .venv/bin/alembic upgrade head && cd ..
cd frontend && npm ci && cd ..

scripts/dev.sh start sim      # simulation mode: no Google account or API keys needed
```

Open <http://127.0.0.1:5173> and click **Connect simulated channel**. Check the stack
with `scripts/dev.sh status` and stop it with `scripts/dev.sh stop`.

- Full installation, including running each process by hand: [docs/installation.md](docs/installation.md)
- Connecting a real channel and real models: [docs/real-mode-setup.md](docs/real-mode-setup.md)
- When something doesn't start: [docs/troubleshooting.md](docs/troubleshooting.md)

## Simulation mode

`scripts/dev.sh start sim` runs ReplyAI against a **fake YouTube channel** (two videos,
26 seeded comments) and **scripted models**. No Google credentials or provider API keys
are needed, and nothing leaves your machine. It uses its own `replyai_sim` database and
refuses to run against a database whose name doesn't end in `_sim`.

Only the network edges are replaced. Triage routing, the input signals, the reply
policy, the decision policy, the job queue, leases, caps and the publication guard are
the real code. Each seeded comment carries a scenario (safe question, spam, PII, prompt
injection, hidden characters, sarcasm, Hindi, Hinglish, creator-only question, rate
limit, …) that exercises a specific branch.

> **Scripted is not intelligent.** In simulation the "models" return pre-written answers
> chosen by scenario. Simulation shows that the system *routes, stores, guards and
> displays* decisions correctly. It says **nothing** about how well a real LLM writes or
> judges replies.

Step-by-step walkthrough: [docs/simulation-guide.md](docs/simulation-guide.md).

## Safety and reliability

- **Two independent models, and agreement is not enough.** Generator and verifier must
  be different recorded models. Agreement is one of many conditions for automatic
  publication; a passing quality review with ≥ 0.8 confidence and no findings is also
  required.
- **Deterministic rules around a probabilistic core.** Input signals (hidden Unicode,
  bidi controls, confusable URLs, injection and dictation phrases) and reply rules
  R1–R12 (personal data, links, handles, promises, promotion, authority claims, hidden
  characters, dictated text) are plain code, applied the same way on every path.
- **Hold when unsure.** Creator-only questions, possible sarcasm, sensitive topics
  (hacked accounts, money, legal, self-harm), injection attempts, edited comments and
  model disagreements all go to a person.
- **Infrastructure failures are never content judgements.** Rate limits, timeouts and
  invalid outputs become `RETRY` or `TECHNICAL_FAILURE`, never `HUMAN_REVIEW` or `SKIP`.
- **At most one reply per comment.** A unique database guard, a publication attempt
  committed *before* the request is sent, re-reading the comment right before posting,
  and `unknown` outcomes that are reconciled by a person instead of retried.
- **Crash-safe workers.** Leases with heartbeats, conditional-update claims, separate
  retry and recovery caps, and shared request budgets across processes.

The 24 failure categories ReplyAI was designed against, with each defense and how far it
has been validated: [docs/ai-safety.md](docs/ai-safety.md).

## Validation status

| Check | Latest result |
| --- | --- |
| Backend test suite (pytest, including PostgreSQL concurrency tests) | **829 passed, 1 skipped** (the skip is an opt-in live-model evaluation) |
| Frontend test suite (Vitest + Testing Library) | **108 passed** (15 files) |
| Ruff (backend lint) | clean |
| TypeScript (`tsc -b`) | clean |
| Frontend production build (`vite build`) | passes |
| Browser walkthroughs of every page in simulation mode | done |

The test suites are maintained in the development repository and are not part of this
public release. The public release was itself rebuilt from a clean checkout: frontend
`npm ci` + type-check + lint + production build, and a runtime-only backend install that
imports the API, worker and simulation modules and resolves one Alembic head.

> **Not yet validated:** systematic reply quality and decision accuracy with **real
> LLMs**, and **automatic** publication to a real YouTube channel. Every result above
> uses scripted models, a mocked or simulated YouTube API, or deterministic offline
> fixtures. On a real test channel, ReplyAI has so far been used to connect via OAuth,
> read videos and comments, and post replies **manually, with a human confirming each
> one**. Treat
> ReplyAI as a well-instrumented prototype, not a production service. If you connect a
> real channel, start in **Dry run** or **Manual review**. See
> [docs/limitations.md](docs/limitations.md).

## Documentation

| Document | Contents |
| --- | --- |
| [Documentation index](docs/README.md) | Overview of all documents |
| [Installation](docs/installation.md) | Prerequisites, environment, dependencies, migrations, startup, health checks |
| [Simulation guide](docs/simulation-guide.md) | Running and exploring the app with the fake channel |
| [Real-mode setup](docs/real-mode-setup.md) | Google Cloud, OAuth, model providers, dry run, safe publication |
| [Configuration](docs/configuration.md) | Every environment variable and per-channel setting |
| [Features](docs/features.md) | Feature reference: behavior, UI location, implementation, limits |
| [Architecture](docs/architecture.md) | Components, pipeline, LangGraph, job lifecycle, decision policy, publication safety |
| [AI safety](docs/ai-safety.md) | Failure categories P01–P20 and M1–M4, defenses and evidence |
| [API](docs/api.md) | API groups, authentication, OpenAPI docs |
| [Development](docs/development.md) | Project structure, conventions, migrations, debugging |
| [Troubleshooting](docs/troubleshooting.md) | Common setup and runtime problems |
| [Limitations](docs/limitations.md) | What is and isn't validated |
| [Data retention](docs/data-retention.md) | What is stored, for how long, and deletion |

## Acknowledgments

- **[Hrishik1033](https://github.com/Hrishik1033)** designed the original LLM token-usage
  gauge and its polling logic
  ([PR #3](https://github.com/RD-blip1/ReplyAI/pull/3)). That design was re-implemented
  by hand in this codebase and extended with request counts, per-role ceilings and the
  replies-remaining estimate; the source files credit it where it is used.
- ReplyAI is built on open-source software, including FastAPI, Pydantic, SQLAlchemy,
  Alembic, LangGraph, React, Vite and Tailwind CSS; each remains under its own license.
  Typefaces (Plus Jakarta Sans, JetBrains Mono) are loaded from Google Fonts under the
  SIL Open Font License.

## Contributing

Bug reports and suggestions are welcome as GitHub issues. Please open an issue before
starting a pull request: because ReplyAI has no open-source license, a contribution can
only be included with your agreement that it may be used in this repository.

To work on the code, set up the project with [docs/installation.md](docs/installation.md),
work in simulation mode, and follow [docs/development.md](docs/development.md). Changes to
the publication path, the decision policy or the guardrails should explain how they keep
the safety properties in [docs/architecture.md](docs/architecture.md). Never commit `.env`
files, OAuth client files, keys or real channel data.

## License

ReplyAI is **not** released under an open-source license, and this repository has no
license file.

Copyright © 2026 RD-blip1 and the contributors credited in
[Acknowledgments](#acknowledgments). All rights reserved.

The repository is public so that the code and documentation can be read and evaluated.
Apart from what GitHub's Terms of Service allow (such as viewing and forking on GitHub),
no permission is granted to use, copy, modify or distribute this code. Third-party
dependencies and fonts remain under their own licenses.
