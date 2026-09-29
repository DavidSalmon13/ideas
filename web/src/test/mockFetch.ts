import { vi } from 'vitest'

type Handler = (method: string, path: string, body: unknown) => unknown

// Route fetch to an in-memory handler so tests never touch the network. The handler
// returns the JSON body; the client only reads ok/status/json.
export function mockFetch(handler: Handler): void {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : undefined
    const data = handler(method, url.pathname + url.search, body)
    return { ok: true, status: 200, json: async () => data } as Response
  }) as unknown as typeof fetch
}

// A minimal EventSource stand-in whose instances tests can drive with `emit`.
export class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {}
  onerror: (() => void) | null = null
  closed = false

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, cb: (e: MessageEvent) => void): void {
    ;(this.listeners[type] ||= []).push(cb)
  }

  emit(type: string, data: unknown): void {
    const event = { data: JSON.stringify(data) } as MessageEvent
    ;(this.listeners[type] ?? []).forEach((cb) => cb(event))
  }

  close(): void {
    this.closed = true
  }

  static reset(): void {
    FakeEventSource.instances = []
  }
}
