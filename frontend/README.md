# ReplyAI web app

The ReplyAI single-page app: a React 19 + TypeScript + Vite workspace for browsing a
connected YouTube channel, generating and reviewing reply drafts, controlling automation,
working through the review queue, and confirming publication. It talks to the FastAPI
backend at `VITE_API_BASE_URL` (default `http://127.0.0.1:8001`).

For the full setup (backend, PostgreSQL, Redis, Google OAuth and the LLM providers), see
[../docs/getting-started.md](../docs/getting-started.md).

## Setup

Requires Node.js 22+ and a running ReplyAI backend.

```bash
npm ci
cp .env.example .env.local
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. `VITE_*` values are exposed to browser code, so keep them
non-sensitive (a base URL only).

## Scripts

| Command | Purpose |
| --- | --- |
| `npm run dev` | Vite dev server with hot reload |
| `npm run build` | Type-check (`tsc -b`) and production build to `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm run lint` | oxlint |

## Layout

| Path | Contents |
| --- | --- |
| `src/App.tsx` | Hash routing, OAuth result notices, unsaved-changes guard |
| `src/components/` | `AppShell` (navigation), `AutomationControls`, `SimulationPanel` |
| `src/hooks/useAppData.ts` | Session, channel, video and comment state |
| `src/lib/` | API client (`api.ts`), payload types (`types.ts`), decision labels |
| `src/pages/` | Videos, comment inbox, posted, jobs, automation, review queue, settings |
| `src/DraftPanel.tsx` | Draft generation, verification results, refinement, publish confirmation, reconciliation |
