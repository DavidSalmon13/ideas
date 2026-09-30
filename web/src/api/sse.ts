// Subscribe to a run's server-sent progress (GET /runs/{id}/events). The stream emits
// `state` snapshots and a final `done` event, then closes (src/opp/api/events.py).
import { API_BASE, authQueryParam } from './client'
import type { RunEvent } from './types'

export interface RunStreamHandlers {
  onState: (event: RunEvent) => void
  onDone?: (event: RunEvent) => void
  onError?: () => void
}

export function subscribeToRun(runId: string, handlers: RunStreamHandlers): () => void {
  // EventSource can't set an Authorization header, so a configured token rides as a query
  // param instead (src/opp/api/auth.py accepts either).
  const query = authQueryParam()
  const source = new EventSource(
    `${API_BASE}/runs/${runId}/events${query ? `?${query}` : ''}`,
  )

  source.addEventListener('state', (e) => {
    handlers.onState(JSON.parse((e as MessageEvent).data) as RunEvent)
  })
  source.addEventListener('done', (e) => {
    handlers.onDone?.(JSON.parse((e as MessageEvent).data) as RunEvent)
    source.close()
  })
  source.onerror = () => {
    handlers.onError?.()
    source.close()
  }

  return () => source.close()
}
