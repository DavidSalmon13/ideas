// Subscribe to a run's server-sent progress (GET /runs/{id}/events). The stream emits
// `state` snapshots and a final `done` event, then closes (src/opp/api/events.py).
import { API_BASE } from './client'
import type { RunEvent } from './types'

export interface RunStreamHandlers {
  onState: (event: RunEvent) => void
  onDone?: (event: RunEvent) => void
  onError?: () => void
}

export function subscribeToRun(runId: string, handlers: RunStreamHandlers): () => void {
  const source = new EventSource(`${API_BASE}/runs/${runId}/events`)

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
