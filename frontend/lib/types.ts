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
  | "figure"
  | "code"
  | "log";

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
  | "upload";

export type SectionName =
  | "abstract"
  | "introduction"
  | "related_work"
  | "methodology"
  | "results"
  | "discussion"
  | "conclusion";

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
