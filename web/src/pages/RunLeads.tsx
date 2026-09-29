import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { Chip, ErrorBox, Loading, Section, confidenceTone } from '../components/ui'

// Transparency (specs.md §2.6, §22): the run's leads with their three specificity claims,
// and the kill list with reasons.
export function RunLeads() {
  const { id = '' } = useParams()
  const leads = useAsync(() => api.leads(id), [id])

  return (
    <div className="stack">
      <div className="row-between">
        <h1>Leads &amp; kills</h1>
        <Link to={`/runs/${id}`}>← Run</Link>
      </div>

      {leads.loading && <Loading what="leads" />}
      {leads.error && <ErrorBox message={leads.error} />}

      {leads.data && (
        <>
          <Section title={`Leads (${leads.data.leads.length})`}>
            {leads.data.leads.map((lead) => (
              <div key={lead.id} className="lead">
                <div className="row-between">
                  <strong>{lead.title}</strong>
                  <span>
                    <Chip text={lead.signal_source} />{' '}
                    <Chip
                      text={lead.status}
                      tone={lead.status === 'kept' ? 'good' : 'bad'}
                    />
                  </span>
                </div>
                <dl className="claims">
                  <dt>customer</dt>
                  <dd>
                    {lead.named_customer.statement}{' '}
                    <Chip
                      text={lead.named_customer.confidence}
                      tone={confidenceTone(lead.named_customer.confidence)}
                    />
                  </dd>
                  <dt>pain</dt>
                  <dd>{lead.specific_pain.statement}</dd>
                  <dt>why now</dt>
                  <dd>{lead.why_now.statement}</dd>
                </dl>
                <p className="muted">{lead.source_ids.length} source(s)</p>
              </div>
            ))}
          </Section>

          <Section title={`Kills (${leads.data.kills.length})`}>
            {leads.data.kills.length === 0 && <p className="muted">Nothing killed.</p>}
            {leads.data.kills.map((kill) => (
              <p key={kill.id}>
                <Chip text={kill.failed_gate ?? kill.stage} tone="bad" /> {kill.reason}
              </p>
            ))}
          </Section>
        </>
      )}
    </div>
  )
}
