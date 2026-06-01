// TypeScript mirror of SPEC.md §2.2. Keep in sync.

export type Phase = "discovery" | "synthesis" | "analysis" | "drafting" | "done";

export type WorkflowState =
  | "running"
  | "awaiting_approval"
  | "approved"
  | "rejected"
  | "error";

export type ArtifactKind =
  | "matrix"
  | "summary"
  | "section"
  | "manuscript"
  | "figure"
  | "code"
  | "log";

// Canonical seven-section order — mirrors backend SectionName in
// app/models/schemas.py (BRD §5.2 FR-2.4).
export type SectionName =
  | "abstract"
  | "introduction"
  | "related_work"
  | "methodology"
  | "results"
  | "discussion"
  | "conclusion";

export type ProducedBy =
  | "librarian"
  | "critic"
  | "analyst"
  | "scribe"
  | "human";

export type PaperSource =
  | "semantic_scholar"
  | "arxiv"
  | "crossref"
  | "core"
  | "europe_pmc"
  | "upload";

export interface User {
  id: string;
  email: string;
  display_name?: string | null;
  created_at: string;
}

export interface Project {
  id: string;
  owner_id: string;
  title: string;
  seed_query: string;
  output_format: "markdown" | "latex";
  token_cap_usd: number;
  status: "draft" | "active" | "completed" | "archived";
  current_phase: Phase;
  created_at: string;
  updated_at: string;
}

export interface WorkflowRun {
  id: string;
  project_id: string;
  phase: Phase;
  state: WorkflowState;
  checkpoint_id: string;
  started_at: string;
  awaiting_since?: string | null;
  last_event_at: string;
}

export interface Paper {
  id: string;
  project_id: string;
  source: PaperSource;
  external_id: string;
  title: string;
  authors: string[];
  year?: number | null;
  abstract?: string | null;
  pdf_url?: string | null;
  citation_key: string;
  /** Wave-3/C3: optional because not all routes carry it on the wire. */
  citation_count?: number | null;
  approved: boolean;
  added_at: string;
}

export interface Artifact {
  id: string;
  project_id: string;
  kind: ArtifactKind;
  label: string;
  content: string;
  mime_type: string;
  produced_by: ProducedBy;
  parent_id?: string | null;
  created_at: string;
}

export interface ApiError {
  error: {
    code: string;
    message: string;
    trace_id?: string;
  };
}

/** Export Pack formats (BRD FR-3.5). LaTeX intentionally absent. */
export type ExportFormat = "markdown" | "bibtex" | "package" | "bundle";

/** Phase-4 telemetry block (NFR-6 / §9). All counts are project-scoped. */
export interface DraftingTelemetry {
  sections_drafted: number;
  regenerations: number;
  overrides: number;
  citation_corrections: number;
  /** Mean of `draft_ms` across `phase_4.section_ready` rows; null if none. */
  avg_section_ms: number | null;
}

/** Response shape from GET /projects/{id}/usage. */
export interface UsageRollup {
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  drafting: DraftingTelemetry;
}

/** One paper resolved against the approved pool (citation manager v1, FR-1.5). */
export interface ResolvedCitation {
  citation_key: string;
  title: string;
  authors: string[];
  year: number | null;
  source: string;
  url: string | null;
}

/** Response shape from GET /projects/{id}/drafting/citations?section=. */
export interface CitationPanel {
  section: string;
  cited_keys: string[];
  unresolved_keys: string[];
  resolved: ResolvedCitation[];
}

/** Phase-3 static-scan summary surfaced at the code-approval gate. Mirrors
 *  backend StaticScanResult (app/agents/analyst.py). */
export interface StaticScanResult {
  ok: boolean;
  denied: string[];
  unknown: string[];
  error: string | null;
}

/** The Analyst's code proposal as surfaced at the await_code_approval gate. */
export interface AnalystProposal {
  code: Artifact; // kind="code", produced_by="analyst"
  methods_narrative: string;
  scan: StaticScanResult;
}

/** Sandbox execution outcome surfaced at the await_analysis_approval gate. */
export interface AnalystResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  duration_ms: number;
  timed_out: boolean;
  oomed: boolean;
  /** Figure index + size; PNG bytes flow via the artifact endpoint. */
  figures: { index: number; bytes: number }[];
  /** Optional in-memory base64 list (when the page hydrates from a state dump). */
  figures_b64?: string[];
}

/** A user-uploaded tabular dataset (Phase 3 / FR-2.3, SPEC v0.3 §2.2). */
export interface Dataset {
  id: string;
  project_id: string;
  filename: string;
  sha256: string;
  columns: string[];
  rowcount: number;
  bytes: number;
  uploaded_at: string;
}
