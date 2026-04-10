export type ThemeMode = 'light' | 'dark'

export interface ThemeVariant {
  bg: string
  bg_input: string
  bg_footer: string
  fg: string
  fg_dim: string
  fg_subtle: string
  accent: string
  accent_warm: string
  accent_warn: string
}

export interface ThemeSpec {
  name: string
  icon: string
  description: string
  dark: ThemeVariant
  light: ThemeVariant
}

export interface ArtifactEntry {
  name: string
  href: string
  kind: string
  size_bytes: number
}

export interface ArtifactGroups {
  primary: ArtifactEntry[]
  turn_files: ArtifactEntry[]
  images: ArtifactEntry[]
  hidden_counts: Record<string, number>
}

export interface SessionFrame {
  index: number
  turn_index: number
  kind: string
  frame_index: number
  turn_elapsed_s: number
  scenario_elapsed_s: number
  agent_turn: number
  stream_open: boolean
  running_tools: number
  message_count: number
  plain: string
}

export interface TraceEvent {
  [key: string]: unknown
  t?: number
  type?: string
}

export interface TurnSummary {
  turn_index: number
  start_s: number
  end_s: number
  duration_s: number
  frame_count: number
  live_frames: number
  tool_frames: number
  max_running_tools: number
  max_agent_turn: number
  message_count_end: number
}

export interface PlaybackStats {
  frame_count: number
  trace_event_count: number
  turn_count: number
  duration_s: number
  live_frame_count: number
  stream_frame_count: number
  tool_frame_count: number
  max_agent_turn: number
}

export interface VerificationItem {
  claim: string
  evidence: string
  status: 'pending' | 'in_progress' | 'passed' | 'failed'
  observed: string
}

export interface VerificationSummary {
  status: string
  total: number
  pending: number
  in_progress: number
  passed: number
  failed: number
  active_claim: string
  items: VerificationItem[]
  updated_at_s?: number
  tool_call_id?: string
}

export interface EvaluatorStep {
  id: string
  kind: string
  spec: string
  pass_condition: string
}

export interface ExperimentAttemptSummary {
  attempt_id: number
  hypothesis: string
  summary: string
  decision: string
  files_touched: string[]
  evaluator_summary: string
  verification_summary: string
  artifact_refs: string[]
}

export interface RunbookSummary {
  configured: boolean
  objective?: string
  success_definition?: string
  scope?: string[]
  protected_surfaces?: string[]
  baseline_status?: string
  baseline_summary?: string
  active_hypothesis?: string
  status?: string
  decision_policy?: string
  evaluator?: EvaluatorStep[]
  attempt_count: number
  last_attempt?: ExperimentAttemptSummary | null
  updated_at_s?: number
  tool_call_id?: string
}

export interface ExperimentRow {
  kind: 'baseline' | 'attempt' | 'completion'
  t: number
  objective: string
  summary: string
  baseline_status?: string
  status?: string
  attempt_id?: number
  hypothesis?: string
  decision?: string
  files_touched?: string[]
  evaluator_summary?: string
  verification_summary?: string
  artifact_refs?: string[]
  tool_call_id?: string
}

export interface SessionPayload {
  kind: 'session'
  title: string
  description: string
  frames: SessionFrame[]
  trace_events: TraceEvent[]
  turns: number[]
  turn_summaries: TurnSummary[]
  event_type_counts: Record<string, number>
  stats: PlaybackStats
  artifacts: ArtifactGroups
  runbook?: RunbookSummary | null
  experiments?: ExperimentRow[]
  verification?: VerificationSummary | null
  theme_catalog: ThemeSpec[]
  default_theme: string
  default_mode: ThemeMode
  library_href?: string
}

export interface SessionLibraryEntry {
  slug: string
  title: string
  description: string
  href: string
  index_href?: string
  status: string
  status_reason: string
  updated_at: string
  duration_s: number
  frame_count: number
  trace_event_count: number
  turn_count: number
  max_agent_turn: number
  visuals_count: number
  preview_excerpt: string
  tools: string[]
  event_types: string[]
}

export interface LibraryPayload {
  kind: 'library'
  title: string
  description: string
  sessions: SessionLibraryEntry[]
  theme_catalog: ThemeSpec[]
  default_theme: string
  default_mode: ThemeMode
}

export type ReviewerPayload = SessionPayload | LibraryPayload

declare global {
  interface Window {
    __SUCCESSOR_REVIEWER_BOOTSTRAP__?: ReviewerPayload
  }
}
