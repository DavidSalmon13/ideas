import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { Chip, ErrorBox, Loading, Section, verdictTone } from '../components/ui'

// Home: past/in-progress runs and the opportunity Library (specs.md §2.1).
export function Library() {
  const runs = useAsync(() => api.listRuns(), [])
  const opps = useAsync(() => api.listOpportunities(), [])
  const [selected, setSelected] = useState<string[]>([])

  const toggle = (id: string) =>
    setSelected((ids) => (ids.includes(id) ? ids.filter((i) => i !== id) : [...ids, id]))

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
        {runs.data && runs.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Started</th>
                <th>Profile</th>
                <th>Status</th>
                <th>Cost</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.data.map((run) => (
                <tr key={run.id}>
                  <td>{new Date(run.created_at).toLocaleString()}</td>
                  <td>{run.profile_ref}</td>
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
                  <th>Title</th>
                  <th>Shape</th>
                  <th>Verdict</th>
                  <th>Score</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {opps.data.map((opp) => (
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
