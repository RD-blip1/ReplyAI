# ReplyAI frontend

The ReplyAI single-page app: a React + TypeScript + Vite workspace for browsing a connected
YouTube channel, drafting and reviewing replies locally, and posting one reply only after an
explicit confirmation. It talks to the FastAPI backend over `VITE_API_BASE_URL` (default
`http://127.0.0.1:8001`).

For the full end-to-end setup (backend, PostgreSQL, Redis, Google OAuth, the YouTube Data
API, and the AI provider), see [../docs/FRESH_CLONE_SETUP.md](../docs/FRESH_CLONE_SETUP.md).

## Requirements

- Node.js 22 or newer and npm
- A running ReplyAI backend (see the root README)

## Setup

```bash
npm ci
cp .env.example .env.local
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. If the API runs on another port, change `VITE_API_BASE_URL` in
`.env.local` and restart Vite. `VITE_*` values are exposed to browser code, so keep them
non-sensitive (a base URL only).

## Scripts

| Command | Purpose |
| --- | --- |
| `npm run dev` | Vite dev server with HMR |
| `npm run build` | Type-check (`tsc -b`) and production build to `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm test` | Vitest + Testing Library (jsdom), single run |
| `npm run lint` | Oxlint |

## Layout

- `src/lib/` — API client (`api.ts`), shared types (`types.ts`)
- `src/hooks/useAppData.ts` — session, video, comment, and YouTube-connection state
- `src/pages/` — `InboxPage`, `PostedPage`, `SettingsPage`, `VideosPage`
- `src/components/AppShell.tsx` — navigation shell
- `src/DraftPanel.tsx` — draft generation, triage/review, refinement, and the posting confirmation
- `src/*.test.tsx`, `src/pages/*.test.tsx` — component and flow tests

## Testing notes

Tests mock the API and never contact YouTube or an AI provider. `src/lib/api.test.ts` covers
the client; `confirmFlow.test.tsx` drives the full confirm-and-post flow. Run `npm test`
before opening a pull request.
