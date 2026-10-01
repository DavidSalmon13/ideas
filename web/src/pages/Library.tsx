import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { OpportunitySummary, RunSummary } from '../api/types'
import { Chip, ErrorBox, Loading, Section, verdictTone } from '../components/ui'

type SortDir = 'asc' | 'desc'

// A clickable column header that toggles sort direction on repeat clicks and switches
// to `dir` (its sensible default) when the active column changes.
function SortHeader<K extends string>({
  label,
  sortKey,
  active,
  dir,
  defaultDir,
  onSort,
}: {
  label: string
  sortKey: K
  active: K
  dir: SortDir
  defaultDir: SortDir
  onSort: (key: K, dir: SortDir) => void
}) {
  const isActive = active === sortKey
  return (
    <th>
      <button
        className="sort-btn"
        onClick={() => onSort(sortKey, isActive && dir === defaultDir ? flip(defaultDir) : defaultDir)}
      >
        {label}
        {isActive && (dir === 'asc' ? ' ▲' : ' ▼')}
      </button>
    </th>
  )
}

function flip(dir: SortDir): SortDir {
  return dir === 'asc' ? 'desc' : 'asc'
}

type RunSortKey = 'created_at' | 'status' | 'cost_to_date_usd'
type OppSortKey = 'title' | 'composite' | 'verdict' | 'status'

function compare(a: string | number | null, b: string | number | null): number {
  if (a == null && b == null) return 0
  if (a == null) return -1
  if (b == null) return 1
  return a < b ? -1 : a > b ? 1 : 0
}

// Home: past/in-progress runs and the opportunity Library (specs.md §2.1).
export function Library() {
  const runs = useAsync(() => api.listRuns(), [])
  const opps = useAsync(() => api.listOpportunities(), [])
  const [selected, setSelected] = useState<string[]>([])
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const [runSort, setRunSort] = useState<{ key: RunSortKey; dir: SortDir }>({
    key: 'created_at',
    dir: 'desc',
  })
  const [oppSort, setOppSort] = useState<{ key: OppSortKey; dir: SortDir }>({
    key: 'composite',
    dir: 'desc',
  })

  const sortedRuns = useMemo(() => {
    const list = runs.data ?? []
    const sign = runSort.dir === 'asc' ? 1 : -1
    return [...list].sort((a: RunSummary, b: RunSummary) => sign * compare(a[runSort.key], b[runSort.key]))
  }, [runs.data, runSort])

  const sortedOpps = useMemo(() => {
    const list = opps.data ?? []
    const sign = oppSort.dir === 'asc' ? 1 : -1
    const value = (o: OpportunitySummary): string | number | null =>
      oppSort.key === 'composite' ? o.composite : (o[oppSort.key] as string | null)
    return [...list].sort((a, b) => sign * compare(value(a), value(b)))
  }, [opps.data, oppSort])

  const toggle = (id: string) =>
    setSelected((ids) => (ids.includes(id) ? ids.filter((i) => i !== id) : [...ids, id]))

  async function deleteRun(run: RunSummary) {
    if (!confirm(`Delete this run (${run.profile_ref}, started ${new Date(run.created_at).toLocaleString()})? This also deletes its leads, opportunities and reports.`)) {
      return
    }
    setDeletingId(run.id)
    setDeleteError(null)
    try {
      await api.deleteRun(run.id)
      runs.reload()
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err))
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="stack">
      <div className="row-between">
        <h1>Opportunity Discovery</h1>
        <Link className="btn primary" to="/runs/new">
          Start new run
        </Link>
      </div>

      <Section title="Runs">
        {runs.loading && <Loading what="runs" />}
        {runs.error && <ErrorBox message={runs.error} />}
        {runs.data && runs.data.length === 0 && <p className="muted">No runs yet.</p>}
        {deleteError && <ErrorBox message={deleteError} />}
        {runs.data && runs.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <SortHeader
                  label="Started"
                  sortKey="created_at"
                  active={runSort.key}
                  dir={runSort.dir}
                  defaultDir="desc"
                  onSort={(key, dir) => setRunSort({ key, dir })}
                />
                <th>Profile</th>
                <SortHeader
                  label="Status"
                  sortKey="status"
                  active={runSort.key}
                  dir={runSort.dir}
                  defaultDir="asc"
                  onSort={(key, dir) => setRunSort({ key, dir })}
                />
                <SortHeader
                  label="Cost"
                  sortKey="cost_to_date_usd"
                  active={runSort.key}
                  dir={runSort.dir}
                  defaultDir="desc"
                  onSort={(key, dir) => setRunSort({ key, dir })}
                />
                <th />
              </tr>
            </thead>
            <tbody>
              {sortedRuns.map((run) => (
                <tr key={run.id}>
                  <td>{new Date(run.created_at).toLocaleString()}</td>
                  <td>
                    {run.profile_ref}
                    {(run.hard_gates_override.max_capital_usd != null ||
                      run.hard_gates_override.max_time_to_revenue_days != null) && (
                      <>
                        {' '}
                        <Chip text="overridden" tone="warn" />
                      </>
                    )}
                  </td>
                  <td>
                    <Chip text={run.status} />
                  </td>
                  <td>
                    ${run.cost_to_date_usd.toFixed(2)} / ${run.budget_ceiling_usd.toFixed(0)}
                  </td>
                  <td>
                    <Link to={`/runs/${run.id}`}>view</Link>
                    {run.status === 'awaiting_checkpoint' && (
                      <>
                        {' · '}
                        <Link to={`/runs/${run.id}/checkpoint`}>checkpoint</Link>
                      </>
                    )}
                    {run.is_terminal ? (
                      <>
                        {' · '}
                        <button
                          className="btn danger"
                          onClick={() => deleteRun(run)}
                          disabled={deletingId === run.id}
                        >
                          {deletingId === run.id ? 'deleting…' : 'delete'}
                        </button>
                      </>
                    ) : (
                      <>
                        {' · '}
                        <span className="muted" title="Cancel the run before deleting it">
                          delete
                        </span>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Opportunities">
        {opps.loading && <Loading what="opportunities" />}
        {opps.error && <ErrorBox message={opps.error} />}
        {opps.data && opps.data.length === 0 && (
          <p className="muted">No opportunities yet — start a run.</p>
        )}
        {opps.data && opps.data.length > 0 && (
          <>
            <div className="row-between">
              <span className="muted">{selected.length} selected</span>
              {selected.length >= 2 ? (
                <Link className="btn" to={`/compare?ids=${selected.join(',')}`}>
                  Compare selected
                </Link>
              ) : (
                <button className="btn" disabled>
                  Compare selected
                </button>
              )}
            </div>
            <table>
              <thead>
                <tr>
                  <th />
                  <SortHeader
                    label="Title"
                    sortKey="title"
                    active={oppSort.key}
                    dir={oppSort.dir}
                    defaultDir="asc"
                    onSort={(key, dir) => setOppSort({ key, dir })}
                  />
                  <th>Shape</th>
                  <SortHeader
                    label="Verdict"
                    sortKey="verdict"
                    active={oppSort.key}
                    dir={oppSort.dir}
                    defaultDir="asc"
                    onSort={(key, dir) => setOppSort({ key, dir })}
                  />
                  <SortHeader
                    label="Score"
                    sortKey="composite"
                    active={oppSort.key}
                    dir={oppSort.dir}
                    defaultDir="desc"
                    onSort={(key, dir) => setOppSort({ key, dir })}
                  />
                  <SortHeader
                    label="Status"
                    sortKey="status"
                    active={oppSort.key}
                    dir={oppSort.dir}
                    defaultDir="asc"
                    onSort={(key, dir) => setOppSort({ key, dir })}
                  />
                </tr>
              </thead>
              <tbody>
                {sortedOpps.map((opp) => (
                  <tr key={opp.id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Select ${opp.title}`}
                        checked={selected.includes(opp.id)}
                        onChange={() => toggle(opp.id)}
                      />
                    </td>
                    <td>
                      <Link to={`/opportunities/${opp.id}`}>{opp.title}</Link>
                    </td>
                    <td>{opp.business_shape}</td>
                    <td>
                      {opp.verdict ? (
                        <Chip text={opp.verdict} tone={verdictTone(opp.verdict)} />
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>{opp.composite != null ? opp.composite.toFixed(2) : '—'}</td>
                    <td>{opp.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Section>
    </div>
  )
}
