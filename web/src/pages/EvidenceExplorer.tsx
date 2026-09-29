import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import type { Source } from '../api/types'
import { Chip, ErrorBox, Loading, Section, confidenceTone } from '../components/ui'

// Evidence explorer (specs.md §22): claims filterable by type/confidence, each linking its
// sources; contradictions highlighted; the judge's dimension scores with justifications.
export function EvidenceExplorer() {
  const { id = '' } = useParams()
  const evidence = useAsync(() => api.evidence(id), [id])
  const [type, setType] = useState('')
  const [confidence, setConfidence] = useState('')

  const sourcesById = useMemo(() => {
    const map = new Map<string, Source>()
    evidence.data?.sources.forEach((s) => map.set(s.id, s))
    return map
  }, [evidence.data])

  const claims = (evidence.data?.claims ?? []).filter(
    (c) => (!type || c.type === type) && (!confidence || c.confidence === confidence),
  )

  return (
    <div className="stack">
      <div className="row-between">
        <h1>Evidence</h1>
        <Link to={`/opportunities/${id}`}>← Opportunity</Link>
      </div>

      {evidence.loading && <Loading what="evidence" />}
      {evidence.error && <ErrorBox message={evidence.error} />}

      {evidence.data && (
        <>
          <Section title="Filters">
            <div className="row">
              <select value={type} onChange={(e) => setType(e.target.value)} aria-label="type">
                <option value="">any type</option>
                <option value="fact">fact</option>
                <option value="estimate">estimate</option>
                <option value="assumption">assumption</option>
                <option value="unknown">unknown</option>
              </select>
              <select
                value={confidence}
                onChange={(e) => setConfidence(e.target.value)}
                aria-label="confidence"
              >
                <option value="">any confidence</option>
                <option value="high">high</option>
                <option value="medium">medium</option>
                <option value="low">low</option>
              </select>
              <span className="muted">
                {claims.length} of {evidence.data.claims.length} claims
              </span>
            </div>
          </Section>

          <Section title="Claims">
            {claims.map((claim) => (
              <div key={claim.id} className={`claim${claim.conflict_ids.length ? ' conflict' : ''}`}>
                <div className="row-between">
                  <span>{claim.statement}</span>
                  <span>
                    <Chip text={claim.type} />{' '}
                    <Chip text={claim.confidence} tone={confidenceTone(claim.confidence)} />
                    {claim.conflict_ids.length > 0 && <Chip text="conflict" tone="bad" />}
                  </span>
                </div>
                <p className="muted">{claim.confidence_reason}</p>
                <ul className="sources">
                  {claim.source_ids.map((sid) => {
                    const src = sourcesById.get(sid)
                    return src ? (
                      <li key={sid}>
                        <a href={src.url} target="_blank" rel="noreferrer">
                          {src.title || src.url}
                        </a>{' '}
                        <Chip text={src.credibility_tier} />
                      </li>
                    ) : null
                  })}
                </ul>
              </div>
            ))}
          </Section>

          {evidence.data.evaluation && (
            <Section title="Judge scores">
              <table>
                <thead>
                  <tr>
                    <th>Dimension</th>
                    <th>Score</th>
                    <th>Justification</th>
                  </tr>
                </thead>
                <tbody>
                  {evidence.data.evaluation.dimension_scores.map((d) => (
                    <tr key={d.dimension}>
                      <td>{d.dimension}</td>
                      <td>{d.score}</td>
                      <td>{d.justification}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
          )}
        </>
      )}
    </div>
  )
}
