// Typed fetch wrapper over the engine's REST API. Errors come back as RFC-7807
// problem+json (src/opp/api/errors.py); ApiError carries the status and detail.
import type {
  ConfigResponse,
  CreateRunRequest,
  EvidenceResponse,
  LeadsResponse,
  OpportunitySummary,
  ProfileDTO,
  ReportResponse,
  RunDetail,
  RunSummary,
} from './types'

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const problem = await res.json()
      if (problem && typeof problem.detail === 'string') detail = problem.detail
    } catch {
      // non-JSON error body; keep the status text
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  config: () => request<ConfigResponse>('GET', '/config'),
  profiles: () => request<ProfileDTO[]>('GET', '/profiles'),

  listRuns: () => request<RunSummary[]>('GET', '/runs'),
  getRun: (id: string) => request<RunDetail>('GET', `/runs/${id}`),
  createRun: (body: CreateRunRequest) => request<RunDetail>('POST', '/runs', body),
  cancelRun: (id: string) => request<RunDetail>('POST', `/runs/${id}/cancel`),
  resumeRun: (id: string, budget?: number) =>
    request<RunDetail>('POST', `/runs/${id}/resume`, { budget_ceiling_usd: budget ?? null }),
  checkpoint: (id: string, action: string, opportunityId?: string, reason?: string) =>
    request<RunDetail>('POST', `/runs/${id}/checkpoint`, {
      action,
      opportunity_id: opportunityId ?? null,
      reason: reason ?? null,
    }),
  leads: (id: string) => request<LeadsResponse>('GET', `/runs/${id}/leads`),
  shortlist: (id: string) => request<OpportunitySummary[]>('GET', `/runs/${id}/shortlist`),
  runReport: (id: string) => request<ReportResponse>('GET', `/runs/${id}/report`),

  listOpportunities: (params?: { verdict?: string; opp_status?: string }) => {
    const q = new URLSearchParams()
    if (params?.verdict) q.set('verdict', params.verdict)
    if (params?.opp_status) q.set('opp_status', params.opp_status)
    const query = q.toString()
    return request<OpportunitySummary[]>('GET', `/opportunities${query ? `?${query}` : ''}`)
  },
  getOpportunity: (id: string) => request<Record<string, unknown>>('GET', `/opportunities/${id}`),
  evidence: (id: string) => request<EvidenceResponse>('GET', `/opportunities/${id}/evidence`),
  opportunityReport: (id: string) =>
    request<ReportResponse>('GET', `/opportunities/${id}/report`),
  setOpportunityStatus: (id: string, status: string) =>
    request<Record<string, unknown>>('PATCH', `/opportunities/${id}`, { status }),
}
