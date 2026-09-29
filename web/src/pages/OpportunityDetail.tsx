import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { Chip, ErrorBox, Loading, Section, verdictTone } from '../components/ui'

const STATUSES = ['new', 'promoted', 'pursued', 'parked', 'discarded']

// The decision brief + dossier (specs.md §18). The report's Markdown is the human-readable
// deliverable; the operator can set the opportunity's status and drill into the evidence.
export function OpportunityDetail() {
  const { id = '' } = useParams()
  const opp = useAsync(() => api.getOpportunity(id), [id])
  const report = useAsync(() => api.opportunityReport(id).catch(() => null), [id])
  const [status, setStatus] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const record = opp.data as
    | { title?: string; status?: string; evaluation?: { verdict?: string } | null }
    | null
  const verdict = record?.evaluation?.verdict ?? null
  const currentStatus = status ?? record?.status ?? 'new'

  async function save(next: string) {
    setSaving(true)
    try {
      await api.setOpportunityStatus(id, next)
      setStatus(next)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack">
      <div className="row-between">
        <h1>{record?.title ?? 'Opportunity'}</h1>
        <Link to="/">← Library</Link>
      </div>

      {opp.loading && <Loading what="opportunity" />}
      {opp.error && <ErrorBox message={opp.error} />}

      {record && (
        <Section title="Decision">
          <p>
            {verdict ? (
              <Chip text={verdict} tone={verdictTone(verdict)} />
            ) : (
              <span className="muted">no verdict</span>
            )}
          </p>
          <div className="field">
            <label htmlFor="status">Status</label>
            <select
              id="status"
              value={currentStatus}
              disabled={saving}
              onChange={(e) => save(e.target.value)}
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <p>
            <Link to={`/opportunities/${id}/evidence`}>Explore the evidence →</Link>
          </p>
        </Section>
      )}

      <Section title="Brief &amp; dossier">
        {report.loading && <Loading what="report" />}
        {report.data === null && !report.loading && (
          <p className="muted">No report for this opportunity yet.</p>
        )}
        {report.data && (
          <>
            {report.data.incomplete && (
              <p className="error">INCOMPLETE — {report.data.halted_at_stage ?? 'halted early'}</p>
            )}
            <pre className="report">{report.data.markdown}</pre>
          </>
        )}
      </Section>
    </div>
  )
}
