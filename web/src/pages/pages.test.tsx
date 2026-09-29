import { afterEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'
import { FakeEventSource, mockFetch } from '../test/mockFetch'
import { Library } from './Library'
import { NewRun } from './NewRun'
import { Checkpoint } from './Checkpoint'
import { RunProgress } from './RunProgress'
import { EvidenceExplorer } from './EvidenceExplorer'

afterEach(() => {
  vi.restoreAllMocks()
  FakeEventSource.reset()
})

const now = new Date().toISOString()

function runDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: 'r1',
    status: 'discovering',
    profile_ref: 'bootstrap',
    mode: 'open_ended',
    checkpoint_mode: 'mandatory',
    current_stage: 'discovery',
    cost_to_date_usd: 1,
    budget_ceiling_usd: 20,
    remaining_budget_usd: 19,
    is_terminal: false,
    created_at: now,
    updated_at: now,
    transitions: [],
    checkpoint: null,
    selected_opportunity_id: null,
    shortlist_size: 0,
    has_report: false,
    running: true,
    ...overrides,
  }
}

function Probe() {
  const { id } = useParams()
  return <div>run page {id}</div>
}

describe('Library', () => {
  it('lists runs and opportunities', async () => {
    mockFetch((_m, path) => {
      if (path === '/runs') return [runDetail({ status: 'completed' })]
      if (path === '/opportunities')
        return [
          {
            id: 'o1',
            run_id: 'r1',
            title: 'HVAC dispatch board',
            business_shape: 'saas',
            status: 'new',
            verdict: 'pursue',
            composite: 3.9,
            shallow: null,
          },
        ]
      return []
    })
    render(
      <MemoryRouter>
        <Library />
      </MemoryRouter>,
    )
    expect(await screen.findByText('HVAC dispatch board')).toBeInTheDocument()
    expect(screen.getByText('pursue')).toBeInTheDocument()
  })
})

describe('NewRun', () => {
  it('submits a run config and navigates to the run', async () => {
    let posted: Record<string, unknown> | undefined
    mockFetch((method, path, body) => {
      if (path === '/config')
        return {
          profiles: [
            {
              id: 'bootstrap',
              name: 'Bootstrap',
              max_capital_usd: 2000,
              max_time_to_revenue_days: 120,
              weights: {},
            },
          ],
          default_profile: 'bootstrap',
          default_budget_usd: 20,
          stage_minimums_usd: {},
          signal_sources: ['unmet_need', 'incumbent_weakness'],
          business_shapes: ['saas'],
          checkpoint_modes: ['mandatory', 'autonomous'],
          run_modes: ['open_ended', 'constrained'],
          dimensions: [],
        }
      if (method === 'POST' && path === '/runs') {
        posted = body as Record<string, unknown>
        return runDetail({ id: 'r2' })
      }
      return {}
    })
    render(
      <MemoryRouter initialEntries={['/runs/new']}>
        <Routes>
          <Route path="/runs/new" element={<NewRun />} />
          <Route path="/runs/:id" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByLabelText('Profile')
    await userEvent.click(screen.getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('run page r2')).toBeInTheDocument()
    expect(posted?.checkpoint_mode).toBe('mandatory')
  })
})

describe('Checkpoint', () => {
  it('shows the shortlist and approves the top candidate', async () => {
    let posted: Record<string, unknown> | undefined
    mockFetch((method, path, body) => {
      if (path === '/runs/r1/shortlist')
        return [
          {
            id: 'o1',
            run_id: 'r1',
            title: 'HVAC dispatch board',
            business_shape: 'saas',
            status: 'new',
            verdict: null,
            composite: 3.5,
            shallow: { rank: 1, composite: 3.5, weights_ref: 'bootstrap' },
          },
        ]
      if (method === 'POST' && path === '/runs/r1/checkpoint') {
        posted = body as Record<string, unknown>
        return runDetail({ status: 'deep_research' })
      }
      return {}
    })
    render(
      <MemoryRouter initialEntries={['/runs/r1/checkpoint']}>
        <Routes>
          <Route path="/runs/:id/checkpoint" element={<Checkpoint />} />
          <Route path="/runs/:id" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByText('HVAC dispatch board')
    await userEvent.click(screen.getByRole('button', { name: 'Approve top candidate' }))
    expect(await screen.findByText('run page r1')).toBeInTheDocument()
    expect(posted?.action).toBe('approve')
  })
})

describe('RunProgress', () => {
  it('renders live SSE snapshots', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    mockFetch((_m, path) => (path === '/runs/r1' ? runDetail() : {}))
    render(
      <MemoryRouter initialEntries={['/runs/r1']}>
        <Routes>
          <Route path="/runs/:id" element={<RunProgress />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1))
    act(() =>
      FakeEventSource.instances[0].emit('state', {
        status: 'discovering',
        current_stage: 'discovery',
        cost_to_date_usd: 2,
        budget_ceiling_usd: 20,
        remaining_budget_usd: 18,
        leads: 5,
        kept_leads: 3,
        killed_leads: 2,
        opportunities: 0,
        shortlist: 0,
        has_report: false,
        awaiting_checkpoint: false,
        is_terminal: false,
        running: true,
        updated_at: now,
      }),
    )
    expect(await screen.findByText('5')).toBeInTheDocument()
    expect(screen.getByText('Inspect leads & kills')).toBeInTheDocument()
  })
})

describe('EvidenceExplorer', () => {
  it('filters claims by type', async () => {
    mockFetch((_m, path) => {
      if (path === '/opportunities/o1/evidence')
        return {
          claims: [
            {
              id: 'c1',
              statement: 'Customers pay $99/mo',
              type: 'fact',
              confidence: 'high',
              confidence_reason: 'two sources',
              source_ids: ['s1'],
              conflict_ids: [],
              stance: 'supporting',
              evidence_strength: 'strong',
            },
            {
              id: 'c2',
              statement: 'They would switch quickly',
              type: 'assumption',
              confidence: 'low',
              confidence_reason: 'inference',
              source_ids: [],
              conflict_ids: [],
              stance: 'neutral',
              evidence_strength: 'weak',
            },
          ],
          sources: [
            {
              id: 's1',
              url: 'https://example.com/pricing',
              title: 'Pricing',
              source_type: 'pricing_page',
              credibility_tier: 'high',
              accessed_date: now,
              excerpt: '',
            },
          ],
          evaluation: null,
        }
      return {}
    })
    render(
      <MemoryRouter initialEntries={['/opportunities/o1/evidence']}>
        <Routes>
          <Route path="/opportunities/:id/evidence" element={<EvidenceExplorer />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByText('Customers pay $99/mo')).toBeInTheDocument()
    expect(screen.getByText('2 of 2 claims')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('type'), 'fact')
    expect(screen.getByText('1 of 2 claims')).toBeInTheDocument()
    expect(screen.queryByText('They would switch quickly')).not.toBeInTheDocument()
  })
})
