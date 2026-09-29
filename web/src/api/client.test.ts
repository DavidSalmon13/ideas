import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './client'

afterEach(() => vi.restoreAllMocks())

describe('api client', () => {
  it('parses JSON on success', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => [{ id: 'r1' }] }) as never
    await expect(api.listRuns()).resolves.toEqual([{ id: 'r1' }])
  })

  it('throws ApiError carrying the problem+json detail', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      json: async () => ({ title: 'Not Found', status: 404, detail: 'no run x' }),
    }) as never
    await expect(api.getRun('x')).rejects.toThrow(ApiError)
    await expect(api.getRun('x')).rejects.toMatchObject({ status: 404, message: 'no run x' })
  })

  it('sends a JSON body on POST', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) })
    globalThis.fetch = fetchMock as never
    await api.checkpoint('r1', 'approve')
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      action: 'approve',
      opportunity_id: null,
      reason: null,
    })
  })
})
