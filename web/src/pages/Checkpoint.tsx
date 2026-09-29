import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { ErrorBox, Loading, Section } from '../components/ui'

// The mandatory checkpoint (specs.md §2.7): the ranked shortlist, and approve / re-pick /
// veto. A re-pick or veto needs a one-line reason (feeds preference learning later).
export function Checkpoint() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const shortlist = useAsync(() => api.shortlist(id), [id])
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function decide(action: string, opportunityId?: string) {
    setBusy(true)
    setError(null)
    try {
      await api.checkpoint(id, action, opportunityId, reason || undefined)
      navigate(`/runs/${id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="stack">
      <div className="row-between">
        <h1>Checkpoint</h1>
        <Link to={`/runs/${id}`}>← Run</Link>
      </div>
      <p className="muted">
        Approve the top candidate for deep research, re-pick a runner-up, or veto the whole
        shortlist. The composite ranks the shortlist — it is not a verdict.
      </p>

      {shortlist.loading && <Loading what="shortlist" />}
      {shortlist.error && <ErrorBox message={shortlist.error} />}
      {shortlist.data && shortlist.data.length === 0 && (
        <p className="muted">No shortlist — nothing survived screening.</p>
      )}

      {shortlist.data && shortlist.data.length > 0 && (
        <>
          <Section title="Shortlist">
            <ol className="shortlist">
              {shortlist.data.map((opp) => (
                <li key={opp.id}>
                  <div className="row-between">
                    <div>
                      <strong>#{opp.shallow?.rank ?? '?'}</strong> {opp.title}{' '}
                      <span className="muted">
                        composite {opp.composite?.toFixed(2)} [{opp.shallow?.weights_ref}]
                      </span>
                    </div>
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() => decide('pick', opp.id)}
                    >
                      Pick this
                    </button>
                  </div>
                </li>
              ))}
            </ol>
          </Section>

          <Section title="Decide">
            <div className="field">
              <label htmlFor="reason">Reason (required to re-pick or veto)</label>
              <input
                id="reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="e.g. loved the automation angle"
              />
            </div>
            {error && <ErrorBox message={error} />}
            <div className="row">
              <button className="btn primary" disabled={busy} onClick={() => decide('approve')}>
                Approve top candidate
              </button>
              <button className="btn danger" disabled={busy} onClick={() => decide('veto')}>
                Veto all
              </button>
            </div>
          </Section>
        </>
      )}
    </div>
  )
}
