# Phase-4 Feature Pack — Contract Proposal (for sign-off)

**Status:** APPROVED (decisions locked below). Folding the relevant parts into
`SPEC.md`, then implementing backend contracts + tests first, frontend second.

### Locked decisions
1. **Export formats:** `markdown`, `bibtex`, `package` (ZIP of separate files),
   `bundle` (single combined markdown file). Student picks.
2. **LaTeX:** not offered at all. `?format=` only accepts the four above; a
   `latex` value gets the ordinary "not a valid format" validation error (422).
   No "coming in v0.2" messaging anywhere. (Remove the stale `latex` mention
   from the current export route too.)
3. **Unresolved citations:** block approval by default; allow an explicit,
   audited override with a written reason.

**Scope (locked with product owner):** the BRD-v0.1-mandated core only —
1. Server-side Export Pack — closes **FR-3.5** (currently a 501 stub).
2. Citation Manager v1 — closes **FR-1.5**.
3. Phase-4 Telemetry — supports **NFR-6** + **BRD §9** success metrics.
4. Diff view at the section gate — the **FR-1.4** "diff/edit view for Phase 4" slice.

**Explicitly deferred** (reasonable enhancements, NOT BRD-mandated; revisit in a
later sprint): Section Briefs, structured Regeneration Modes, the History
Timeline, the readiness checklist. Reason: keep the 1-sprint envelope honest and
ship the documented gaps well. (Free-text reject feedback — BRD §4.2 — already
exists; structured "modes" are an enhancement on top of it.)

All additions are **additive + non-breaking** and preserve strict HITL semantics.

---

## 1. Server-side Export Pack (FR-3.5)

Complete the existing `GET /projects/{id}/export` (today: 501).

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/projects/{id}/export?format=markdown` | Returns the assembled manuscript markdown. |
| GET | `/projects/{id}/export?format=bibtex` | Returns a BibTeX file built from the **approved pool only**. |
| GET | `/projects/{id}/export?format=package` | Returns a single download bundling manuscript + bib + AI-disclosure/audit appendix. |
| GET | `/projects/{id}/export?format=latex` | **Not v0.1.** Returns the standardized "unavailable" envelope below (NOT 501-as-error). |

**Preconditions:** a `kind="manuscript"` artifact must exist (Phase 4 done). If
not → `409 { code: "manuscript_not_ready" }`.

**`package` contents** (deterministic file naming — `<project-slug>/`):
- `manuscript.md` — the assembled manuscript (reuses `node_assemble` output).
- `references.bib` — BibTeX of every approved-pool paper.
- `ai-disclosure.md` — the disclosure block (reuses `_build_disclosure_block`).
- `audit-appendix.md` — human-readable audit trail (agent invocations, approvals,
  overrides, citation corrections, model + token/cost) — the BRD §10 "exportable
  AI-disclosure appendix."

**Open decision (need your call):** package container format.
- (a) **ZIP** (`application/zip`) — true multi-file bundle, standard. Adds no new
  dep (Python stdlib `zipfile`). **Recommended.**
- (b) Single concatenated markdown with `---` section dividers — simplest, no zip,
  but not separate files.

**Response — unavailable format envelope** (consistent, non-breaking):
```json
{ "code": "format_unavailable", "message": "latex export is a v0.2 feature", "format": "latex", "available": ["markdown", "bibtex", "package"] }
```
Returned with HTTP **200** (it's a known, planned limitation, not an error) — or
**501** if you prefer it stays an error code. **Need your call: 200 vs 501.**

**BibTeX entry shape** (from `Paper`): `@article{<citation_key>, title={…},
author={… and …}, year={…}, ...}`. Only approved-pool papers (FR-2.4 invariant).

**Audit:** every export writes `action="export.generated"` with `{format, by}`.

---

## 2. Citation Manager v1 (FR-1.5)

The Scribe already validates `cited_keys ⊆ approved_pool` and surfaces offenders
with an `INVALID:` prefix; the section-review UI already shows INVALID chips.
This adds the **correction + block** flow.

### 2a. New read endpoint — resolve citation context
```
GET /projects/{id}/drafting/citations?section={section}
```
Returns, for the current section draft:
```json
{
  "section": "introduction",
  "cited_keys": ["lecun2015"],
  "unresolved_keys": ["smith2020"],          // cited but NOT in approved pool
  "resolved": [
    { "citation_key": "lecun2015", "title": "...", "authors": [...], "year": 2015,
      "source": "arxiv", "url": "https://..." }
  ]
}
```
Powers the review panel: detected keys, offending keys, one-click jump to the
approved-paper metadata (the FR-1.5 "inline preview").

### 2b. Citation correction at the gate (override extension)
Extend the **existing** override payload with an optional `citation_corrections`
map — replace a malformed key with a valid approved-pool key before approving:
```json
{
  "artifact_kind": "section", "label": "introduction",
  "content": "## Introduction ... [@lecun2015] ...",
  "citation_corrections": { "smith2020": "lecun2015" },   // optional, additive
  "override_reason": "fixed hallucinated key"               // optional, additive
}
```
The correction is applied to the section content and recorded as a **human edit**:
`action="user.citation_correction"` with `{section, corrections, reason}`.

### 2c. Approve-block rule for unresolved citations
At the section approve gate: if the current draft has **unresolved citation keys**,
the approve is **rejected with `409 { code: "unresolved_citations", keys: [...] }`**
UNLESS the request carries an explicit, audited override:
```json
POST /workflow/approve  { "force_unresolved": true, "override_reason": "intentional placeholder" }
```
The forced approve writes `action="user.approve"` with `{forced_unresolved: true,
reason, keys}` so the bypass is fully auditable. (Default = block; matches BRD
risk #1 "post-generation validator rejects unknown citation keys.")

**Open decision (need your call):** default strictness — **block-by-default**
(safest, matches risk #1) vs **warn-only** (never blocks; just flags). I recommend
block-by-default with the audited override escape hatch.

---

## 3. Phase-4 Telemetry (NFR-6 / BRD §9)

Structured audit rows + a usage-view rollup for the §9 success metrics. No new
table — reuse `audit_log` (which already carries model/tokens/cost) + new action
types:

| Metric | How captured |
| --- | --- |
| draft latency per section | `phase_4.section_ready` payload gains `draft_ms` (already times the Scribe call) |
| regenerate count per section | counted from existing `user.reject` rows where `phase=drafting` |
| override rate | counted from existing `user.override` rows |
| citation correction count | new `user.citation_correction` rows (§2b) |

Extend `GET /projects/{id}/usage` (today: tokens + cost) with an additive
`drafting` block:
```json
{ "tokens_in": …, "tokens_out": …, "cost_usd": …,
  "drafting": { "sections_drafted": 7, "regenerations": 3, "overrides": 1,
                "citation_corrections": 2, "avg_section_ms": 8421 } }
```
All additive — existing `usage` consumers keep working.

---

## 4. Diff view at the section gate (FR-1.4)

Frontend-only (no new API). The section-review panel gains a **side-by-side diff**
between the current draft and the **last approved/previous version** of that
section. The drafts list already retains prior section artifacts; the existing
`diffLines` util (used in the override editor) is reused. No new contract.

> The full "history timeline" (all versions + actors + timestamps) is **deferred**
> — only the current-vs-previous diff (the FR-1.4 ask) is in scope.

---

## 5. New/changed API surface (summary — all additive)

| Change | Type |
| --- | --- |
| `GET /export?format=markdown\|bibtex\|package\|latex` | complete existing 501 |
| `GET /projects/{id}/drafting/citations?section=` | new read endpoint |
| override payload: `+ citation_corrections?`, `+ override_reason?` | additive optional fields |
| approve payload: `+ force_unresolved?`, `+ override_reason?` | additive optional fields |
| `GET /usage` `+ drafting{}` block | additive field |
| new audit actions: `user.citation_correction`, `export.generated` | additive |

**No breaking changes. No new DB table. No new third-party dep** (zip = stdlib).

---

## 6. Test plan (backend-first, then frontend)

- **Contract:** new endpoints + optional fields are backward compatible; unknown
  `format` → standardized envelope; export of non-ready project → 409.
- **Export:** markdown/bibtex contain only approved-pool citations; package always
  includes disclosure + audit appendix + deterministic file names.
- **Citation:** unresolved-citation approve blocked (409) unless audited
  `force_unresolved`; correction recorded as `user.citation_correction`.
- **Telemetry:** `usage.drafting` counters reflect the audit rows; emitted once per
  action.
- **E2E:** draft → reject → regenerate → citation-correction → approve; full
  7-section run → manuscript + package with complete audit trace.

---

## 7. Decisions I need from you before coding

1. **Package format:** ZIP (recommended) vs concatenated-markdown.
2. **Unavailable-format HTTP status:** 200-with-envelope (recommended) vs 501.
3. **Unresolved-citation default:** block-by-default + audited override
   (recommended) vs warn-only.
4. **Anything to pull back into scope** from the deferred set (Briefs/Modes,
   History Timeline), or is the mandated-core scope right?
