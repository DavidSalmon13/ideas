import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { CreateRunRequest } from '../api/types'
import { ErrorBox, Loading, Section } from '../components/ui'

// Configure and start a run (specs.md §2.2-§2.3). Everything defaults from the profile;
// the operator narrows discovery and picks the checkpoint mode.
export function NewRun() {
  const config = useAsync(() => api.config(), [])
  const navigate = useNavigate()

  const [profile, setProfile] = useState('bootstrap')
  const [budget, setBudget] = useState(20)
  const [industry, setIndustry] = useState('')
  const [geography, setGeography] = useState('')
  const [targetCustomer, setTargetCustomer] = useState('')
  const [autonomous, setAutonomous] = useState(false)
  const [signals, setSignals] = useState<Set<string>>(new Set())
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Default the profile/budget once config arrives (only if the operator hasn't changed them).
  if (config.data && profile === 'bootstrap' && config.data.default_profile !== 'bootstrap') {
    setProfile(config.data.default_profile)
  }

  function toggleSignal(source: string) {
    setSignals((prev) => {
      const next = new Set(prev)
      if (next.has(source)) next.delete(source)
      else next.add(source)
      return next
    })
  }

  async function submit() {
    if (!config.data) return
    setSubmitting(true)
    setError(null)
    const body: CreateRunRequest = {
      profile_ref: profile,
      mode: industry || geography || targetCustomer ? 'constrained' : 'open_ended',
      checkpoint_mode: autonomous ? 'autonomous' : 'mandatory',
      budget_ceiling_usd: budget,
      discovery_scope: {
        industry: industry || null,
        geography: geography || null,
        target_customer: targetCustomer || null,
        signal_sources: signals.size ? [...signals] : config.data.signal_sources,
      },
      lead_targets: { discovery_n: 30, shortlist_n: 5 },
      soft_preferences: [],
    }
    try {
      const run = await api.createRun(body)
      navigate(`/runs/${run.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSubmitting(false)
    }
  }

  if (config.loading) return <Loading what="options" />
  if (config.error) return <ErrorBox message={config.error} />
  if (!config.data) return null

  return (
    <div className="stack">
      <h1>New run</h1>
      <Section title="Configuration">
        <div className="field">
          <label htmlFor="profile">Profile</label>
          <select id="profile" value={profile} onChange={(e) => setProfile(e.target.value)}>
            {config.data.profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} (≤ ${p.max_capital_usd.toFixed(0)}, ≤{' '}
                {p.max_time_to_revenue_days} days)
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="budget">Budget ceiling (USD)</label>
          <input
            id="budget"
            type="number"
            min={1}
            value={budget}
            onChange={(e) => setBudget(Number(e.target.value))}
          />
        </div>

        <div className="field">
          <label htmlFor="industry">Industry</label>
          <input id="industry" value={industry} onChange={(e) => setIndustry(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="geography">Geography</label>
          <input id="geography" value={geography} onChange={(e) => setGeography(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="customer">Target customer</label>
          <input
            id="customer"
            value={targetCustomer}
            onChange={(e) => setTargetCustomer(e.target.value)}
          />
        </div>

        <fieldset className="field">
          <legend>Signal sources (default: all)</legend>
          {config.data.signal_sources.map((source) => (
            <label key={source} className="check">
              <input
                type="checkbox"
                checked={signals.has(source)}
                onChange={() => toggleSignal(source)}
              />
              {source}
            </label>
          ))}
        </fieldset>

        <label className="check">
          <input
            type="checkbox"
            checked={autonomous}
            onChange={(e) => setAutonomous(e.target.checked)}
          />
          Autonomous (skip the checkpoint and run to a verdict)
        </label>

        {error && <ErrorBox message={error} />}
        <button className="btn primary" onClick={submit} disabled={submitting}>
          {submitting ? 'Starting…' : 'Start run'}
        </button>
      </Section>
    </div>
  )
}
