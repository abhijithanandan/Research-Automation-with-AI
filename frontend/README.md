# ResearchFlow AI — Frontend

Next.js 14 (App Router) + TypeScript + Tailwind. Read the root `SPEC.md` before editing routes or types.

## Quick start

```bash
npm install
cp .env.example .env.local  # point NEXT_PUBLIC_API_BASE_URL at backend
npm run dev
```

The dev server runs on http://localhost:3000.

## Layout

```
app/                      # App Router pages
├── layout.tsx
├── page.tsx              # landing
├── projects/[id]/page.tsx (TODO)
└── globals.css
components/
├── ui/                   # shared primitives (shadcn-style)
├── workflow/             # PhaseTracker, ApprovalPanel
└── agents/               # Per-persona views (PaperList, MatrixViewer, ...)
lib/
├── api.ts                # typed REST client. Mirrors SPEC.md §3.
├── ws.ts                 # WS client. Mirrors SPEC.md §4.
└── types.ts              # TS types matching SPEC.md §2.2.
```

## Rules

1. Every API call goes through `lib/api.ts`. Don't `fetch` from a component.
2. Every WS event is parsed against the discriminated union in `lib/ws.ts`.
3. Types in `lib/types.ts` are the single source of truth on the TS side and must match `SPEC.md §2.2`. If they drift, update both in the same PR.
4. Approval gate transitions are *server-authoritative*. Wait for `state.changed` before updating the local view.
