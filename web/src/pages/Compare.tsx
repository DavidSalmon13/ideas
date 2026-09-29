import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { Chip, ErrorBox, Loading, verdictTone } from '../components/ui'

// Side-by-side comparison of 2+ opportunities (specs.md §2.10). Ids come from the query
// string (?ids=a,b), so the page is linkable straight from the Library selection.
export function Compare() {
  const [params] = useSearchParams()
  const ids = (params.get('ids') ?? '').split(',').filter(Boolean)
  const result = useAsync(
    () => (ids.length >= 2 ? api.compare(ids) : Promise.resolve(null)),
    [ids.join(',')],
  )

  return (
    <div className="stack">
      <div className="row-between">
        <h1>Compare opportunities</h1>
        <Link to="/">← Library</Link>
      </div>

      {ids.length < 2 && <p className="muted">Select at least two opportunities to compare.</p>}
      {ids.length >= 2 && result.loading && <Loading what="comparison" />}
      {result.error && <ErrorBox message={result.error} />}

      {result.data && (
        <div className="scroll-x">
          <table className="compare">
            <tbody>
              <tr>
                <th className="rowhead">Opportunity</th>
                {result.data.columns.map((c) => (
                  <th key={c.id}>
                    <Link to={`/opportunities/${c.id}`}>{c.title}</Link>
                    <div className="muted">{c.business_shape}</div>
                  </th>
                ))}
              </tr>
              <tr>
                <th className="rowhead">Verdict</th>
                {result.data.columns.map((c) => (
                  <td key={c.id}>
                    {c.verdict ? (
                      <Chip text={c.verdict} tone={verdictTone(c.verdict)} />
                    ) : (
                      <span className="muted">not evaluated</span>
                    )}
                  </td>
                ))}
              </tr>
              <tr>
                <th className="rowhead">Composite (rank only)</th>
                {result.data.columns.map((c) => (
                  <td key={c.id}>{c.composite != null ? c.composite.toFixed(2) : '—'}</td>
                ))}
              </tr>
              <tr>
                <th className="rowhead">Min viable team</th>
                {result.data.columns.map((c) => (
                  <td key={c.id}>{c.min_viable_team ?? '—'}</td>
                ))}
              </tr>
              <tr>
                <th className="rowhead">Solo-startable</th>
                {result.data.columns.map((c) => (
                  <td key={c.id}>{c.can_one_person_start ?? '—'}</td>
                ))}
              </tr>

              <tr>
                <th className="rowhead section" colSpan={result.data.columns.length + 1}>
                  Economics
                </th>
              </tr>
              {metricRows(result.data.columns).map((row) => (
                <tr key={row.key}>
                  <th className="rowhead">{row.label}</th>
                  {result.data!.columns.map((c) => (
                    <td key={c.id}>{formatMetric(c, row.key)}</td>
                  ))}
                </tr>
              ))}

              <tr>
                <th className="rowhead section" colSpan={result.data.columns.length + 1}>
                  Dimension scores (1–5)
                </th>
              </tr>
              {result.data.dimensions.map((dim) => (
                <tr key={dim}>
                  <th className="rowhead">{dim.replace(/_/g, ' ')}</th>
                  {result.data!.columns.map((c) => (
                    <td key={c.id}>{c.dimension_scores[dim] ?? '—'}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// The union of metric keys present across the columns, in first-seen order.
function metricRows(columns: { metrics: { key: string; label: string }[] }[]) {
  const seen = new Map<string, string>()
  for (const col of columns) for (const m of col.metrics) if (!seen.has(m.key)) seen.set(m.key, m.label)
  return [...seen].map(([key, label]) => ({ key, label }))
}

function formatMetric(
  column: { metrics: { key: string; value: number | null; unit: string | null; status: string }[] },
  key: string,
): string {
  const metric = column.metrics.find((m) => m.key === key)
  if (!metric) return '—'
  const value = metric.value != null ? `${metric.value}${metric.unit ? ` ${metric.unit}` : ''}` : '—'
  return `${value} (${metric.status})`
}
