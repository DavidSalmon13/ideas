// Typed fetch wrapper over the engine's REST API. Errors come back as RFC-7807
// problem+json (src/opp/api/errors.py); ApiError carries the status and detail.
import type {
  ComparisonResponse,
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

// Optional bearer-token auth (src/opp/api/auth.py): unset by default (local/dev has none
// configured server-side either); when the operator sets one for a remote deployment, it's
// kept in localStorage and sent as `Authorization: Bearer <token>` on every request. The
// SSE stream can't set headers, so `authQueryParam` below carries it as `?token=` instead.
const TOKEN_KEY = 'opp_api_token'

export function getApiToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setApiToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// `?token=<token>` for a URL the browser opens itself (EventSource), or '' when unset.
export function authQueryParam(): string {
  const token = getApiToken()
  return token ? `token=${encodeURIComponent(token)}` : ''
}

export class ApiError extends Error {
  status: number
  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = getApiToken()
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
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
  deleteRun: (id: string) => request<void>('DELETE', `/runs/${id}`),
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
  compare: (ids: string[]) =>
    request<ComparisonResponse>('POST', '/compare', { opportunity_ids: ids }),
}
