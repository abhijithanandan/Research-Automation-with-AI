# Contributing to ResearchFlow AI

Welcome. This repo is in the onboarding phase — the specs are settled, the implementation is just beginning. Please read this file end-to-end before opening your first PR.

---

## TL; DR

1. Read `BRD.md`, `SPEC.md`, and the agent contract under `docs/agents/` for the area you're touching.
2. If your change alters a contract, update `SPEC.md` and `docs/api/openapi.yaml` in the *same* PR.
3. Branch off `main`, keep PRs small, write tests, run linters, fill the PR template.
4. Get one approving review (plus a second from the relevant lead if you touched a contract).

---

## Spec-driven development

This project follows a strict spec-first discipline. Concretely:

- **Source of truth = `SPEC.md` + `docs/api/openapi.yaml` + `docs/agents/*.md`.** Code matches these, not the other way around.
- **Spec changes precede code.** If you discover the spec is wrong, open a spec PR first (or include the spec change in the same PR clearly labeled). Reviewers will block code-only changes that diverge from the spec.
- **Breaking contract changes require a `/v2` namespace.** Don't silently break wire formats.
- **Every new endpoint, event, or agent input field requires:** (1) update to `SPEC.md`, (2) update to `openapi.yaml`, (3) update to the relevant agent doc if applicable, (4) implementation + tests.

---

## Branching

- Base branch: `main`. Always.
- Feature branches: `feature/<scope>-<short-name>` (e.g. `feature/librarian-arxiv-source`).
- Fix branches: `fix/<scope>-<short-name>`.
- Spec-only branches: `spec/<scope>-<short-name>`.
- Chore branches: `chore/<short-name>`.

Keep branches short-lived (≤ 5 working days) and rebase onto `main` before opening a PR.

---

## Commit messages

Conventional Commits. Subject in imperative mood, ≤ 72 chars.

```
feat(librarian): add Crossref source
fix(graph): persist checkpoint before emitting approval.required
docs(spec): clarify citation key collision rule
refactor(scribe): extract citation validator
test(critic): cover empty pool case
chore(ci): pin Python to 3.11
```

Do not squash unrelated changes into one commit. Each commit should be a coherent unit of work.

---

## Code style

### Backend (Python)
- Python ≥ 3.11.
- `ruff` for lint + format. Run `ruff check . --fix` and `ruff format .` before pushing.
- `mypy --strict` on `app/`. Type-hint every public function.
- Pydantic v2 models for all wire types.
- Async everywhere on the request path. Sync only for CPU-bound work, and only inside `asyncio.to_thread`.
- No `print`. Use `structlog` with the standard fields `trace_id`, `project_id`, `workflow_run_id`.

### Frontend (TypeScript)
- Strict mode on (`tsconfig.json` already configured).
- `eslint` + `prettier` enforced in CI.
- Functional components + hooks only. No class components.
- Server state via TanStack Query; client state via Zustand. Don't reach for Redux.
- Tailwind for styling; reach for `shadcn/ui` primitives before writing new components.
- All API calls go through `lib/api.ts`. Don't `fetch` inline in components.

### General
- File names: `snake_case.py` for Python, `kebab-case.tsx` for components, `camelCase.ts` for utilities.
- No commented-out code in commits. Use git history.
- Comments explain *why*, not *what*. If the code needs a comment to be readable, prefer renaming or extraction.

---

## Code hygiene & git hooks

The repo enforces formatting, lint, and commit-message rules through **Husky** hooks. They run automatically — your first contribution should mostly involve installing them.

### One-time setup (after cloning)

```bash
# From the repo root:
npm install        # installs husky, lint-staged, commitlint, prettier (root devDeps)
```

The `prepare` script wires `.husky/` into `.git/hooks/`. You do **not** need to copy hooks manually.

### What runs when

| Hook | Trigger | What it does |
| --- | --- | --- |
| `pre-commit` | Every `git commit` | `lint-staged`: runs Prettier + ESLint --fix on staged `.ts/tsx/js/jsx/json/css/md`, and `ruff check --fix` + `ruff format` on staged `.py`. |
| `commit-msg` | After you write the message | `commitlint` validates against Conventional Commits (types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`, `spec`). |
| `pre-push` | Every `git push` | `tsc --noEmit` on the frontend + `ruff check` on the backend. CI runs the same checks; this is just a fast local short-circuit. |

If a hook rejects your commit:

- **Lint/format failure** — the auto-fixer has already patched what it can; re-stage the modified files (`git add -u`) and re-commit.
- **commitlint failure** — your subject is malformed. Use one of the allowed types and keep the subject ≤ 100 chars (e.g. `feat(scribe): enforce citation-key allowlist`).
- **Type/lint failure on pre-push** — fix the reported issues. Do **not** push with `--no-verify` to bypass; the same checks gate CI.

### Skipping hooks (don't, but if you must)

`--no-verify` exists; using it is a code smell. The hooks exist because someone, somewhere, made a mess. If you genuinely need to bypass (e.g. WIP commit on a private branch), call it out in the PR description.

### Backend hook requirements

The Python hooks call `ruff` directly. If your shell can't find it, activate your backend virtualenv first (`source backend/.venv/bin/activate`) so the hook inherits it, or install ruff globally (`pipx install ruff`).

---

## Testing

- Backend: `pytest`. Required coverage for any new code path that touches a gate, an agent, or the audit log. Mock LLM calls; use the in-memory checkpointer for graph tests.
- Frontend: Vitest for unit, Playwright for E2E (post-MVP). Mock the API client; do not hit a running backend in unit tests.
- CI must be green before merge. Don't merge with `--admin`.

---

## Pull request process

1. Open the PR against `main`.
2. Fill in the template (it's prefilled — don't delete sections).
3. Link the issue / spec section you're implementing.
4. Add a short test plan (what you ran, what you observed).
5. Attach screenshots for any UI change.
6. Request review from at least one teammate. If you touched a contract, also request review from the backend lead *and* the frontend lead.
7. CI must pass. Lint, type, and tests are all blocking.
8. Squash-merge unless the PR contains intentionally-separated commits worth preserving.

---

## Reviewing

- Look at the spec changes first. If the spec is wrong, nothing downstream matters.
- Check that tests exercise the new behavior, not just the existing behavior.
- Push back on scope creep — say so in a review comment and ask for a follow-up issue rather than expanding the PR.
- Approve when you'd be willing to be on-call for the change.

---

## Issue tracking

Open an issue *before* starting work on anything non-trivial. Use labels:
- `area/backend`, `area/frontend`, `area/spec`, `area/infra`, `area/docs`
- `kind/feat`, `kind/fix`, `kind/refactor`, `kind/test`, `kind/chore`
- `priority/p0`, `priority/p1`, `priority/p2`

Reference issues in commits and PRs (`Closes #123`).

---

## Onboarding checklist (for new contributors)

- [ ] Read `BRD.md` and `SPEC.md`.
- [ ] Skim `ARCHITECTURE.md`.
- [ ] Get the local stack running via `docker compose up`.
- [ ] Run `pytest` (backend) and `npm run test` (frontend) — both should be green.
- [ ] Pick a `good-first-issue` from the tracker.
- [ ] Open your first PR with a small change to the docs or a test, just to walk the workflow.

Welcome aboard.
