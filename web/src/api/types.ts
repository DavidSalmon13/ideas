// TypeScript mirror of the API's response/request shapes (src/opp/api/dtos.py).
// Kept lean — only what the UI reads.

export type RunStatus =
  | 'created'
  | 'discovering'
  | 'screening'
  | 'shallow_validation'
  | 'awaiting_checkpoint'
  | 'deep_research'
  | 'synthesizing'
  | 'building_plan'
  | 'red_team'
  | 'evaluating'
  | 'completed'
  | 'completed_partial'
  | 'cancelled'
  | 'halted_budget'
  | 'halted_error'

export type Verdict = 'pursue' | 'promising_but_unproven' | 'park' | 'kill'
export type CheckpointAction = 'approve' | 'pick' | 'veto' | 'auto'

export interface RunSummary {
  id: string
  status: RunStatus
  profile_ref: string
  mode: string
  checkpoint_mode: string
  current_stage: string | null
  cost_to_date_usd: number
  budget_ceiling_usd: number
  remaining_budget_usd: number
  is_terminal: boolean
  created_at: string
  updated_at: string
}

export interface RunTransition {
  from_status: RunStatus
  to_status: RunStatus
  at: string
  reason: string
}

export interface CheckpointDecision {
  action: CheckpointAction
  opportunity_id: string | null
  reason: string | null
  decided_at: string
}

export interface RunDetail extends RunSummary {
  transitions: RunTransition[]
  checkpoint: CheckpointDecision | null
  selected_opportunity_id: string | null
  shortlist_size: number
  has_report: boolean
  running: boolean
}

export interface RunEvent {
  status: RunStatus
  current_stage: string | null
  cost_to_date_usd: number
  budget_ceiling_usd: number
  remaining_budget_usd: number
  leads: number
  kept_leads: number
  killed_leads: number
  opportunities: number
  shortlist: number
  has_report: boolean
  awaiting_checkpoint: boolean
  is_terminal: boolean
  running: boolean
  updated_at: string
}

export interface ClaimBrief {
  id: string
  statement: string
  type: string
  confidence: string
}

export interface LeadDTO {
  id: string
  title: string
  signal_source: string
  business_shape: string
  status: string
  candidate_wedge: string
  named_customer: ClaimBrief
  specific_pain: ClaimBrief
  why_now: ClaimBrief
  source_ids: string[]
  discovered_at: string
}

export interface Kill {
  id: string
  subject_kind: string
  subject_id: string
  stage: string
  reason: string
  failed_gate: string | null
  killed_at: string
}

export interface LeadsResponse {
  leads: LeadDTO[]
  kills: Kill[]
}

export interface ShallowScore {
  rank: number | null
  composite: number
  weights_ref: string
}

export interface OpportunitySummary {
  id: string
  run_id: string
  title: string
  business_shape: string
  status: string
  verdict: Verdict | null
  composite: number | null
  shallow: ShallowScore | null
}

export interface Claim {
  id: string
  statement: string
  type: string
  confidence: string
  confidence_reason: string
  value?: number | null
  unit?: string | null
  stance: string
  evidence_strength: string
  source_ids: string[]
  conflict_ids: string[]
}

export interface Source {
  id: string
  url: string
  title: string
  source_type: string
  credibility_tier: string
  publisher?: string | null
  published_date?: string | null
  accessed_date: string
  excerpt: string
}

export interface DimensionScore {
  dimension: string
  score: number
  justification: string
  supporting_claim_ids: string[]
}

export interface Evaluation {
  dimension_scores: DimensionScore[]
  composite: number
  verdict: Verdict
  overrides: string[]
  justification: string
}

export interface EvidenceResponse {
  claims: Claim[]
  sources: Source[]
  evaluation: Evaluation | null
}

export interface ReportResponse {
  run_id: string
  opportunity_id: string | null
  incomplete: boolean
  halted_at_stage: string | null
  brief: Record<string, unknown>
  dossier: Record<string, unknown>
  markdown: string
  created_at: string
}

export interface ProfileDTO {
  id: string
  name: string
  max_capital_usd: number
  max_time_to_revenue_days: number
  weights: Record<string, number>
}

export interface ConfigResponse {
  profiles: ProfileDTO[]
  default_profile: string
  default_budget_usd: number
  stage_minimums_usd: Record<string, number>
  signal_sources: string[]
  business_shapes: string[]
  checkpoint_modes: string[]
  run_modes: string[]
  dimensions: string[]
}

// POST /runs body — a partial RunConfig; everything else defaults on the server.
export interface CreateRunRequest {
  profile_ref: string
  mode: string
  checkpoint_mode: string
  budget_ceiling_usd: number
  discovery_scope: {
    industry?: string | null
    geography?: string | null
    target_customer?: string | null
    signal_sources: string[]
  }
  lead_targets: { discovery_n: number; shortlist_n: number }
  soft_preferences: string[]
}
