import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { subscribeToRun } from '../api/sse'
import type { RunDetail, RunEvent } from '../api/types'
import { Chip, CostMeter, ErrorBox, Loading, Section } from '../components/ui'

const STAGES = [
  'discovering',
  'screening',
  'shallow_validation',
  'awaiting_checkpoint',
  'deep_research',
  'synthesizing',
  'building_plan',
  'red_team',
  'evaluating',
  'completed',
]

// Live funnel view (plan.md §13): streams progress over SSE, shows the cost meter,
// lead/opportunity counts, and the next action (checkpoint or the finished report).
export function RunProgress() {
  const { id = '' } = useParams()
  const [event, setEvent] = useState<RunEvent | null>(null)
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [streamError, setStreamError] = useState(false)

  useEffect(() => {
    let alive = true
    api.getRun(id).then((d) => alive && setDetail(d)).catch(() => {})
    const unsubscribe = subscribeToRun(id, {
      onState: (e) => alive && setEvent(e),
      onDone: () => {
        // Refresh the detail once for the final selection / report links.
        api.getRun(id).then((d) => alive && setDetail(d)).catch(() => {})
      },
      onError: () => alive && setStreamError(true),
    })
    return () => {
      alive = false
      unsubscribe()
    }
  }, [id])

  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)

  const status = event?.status ?? detail?.status
  const isTerminal = event?.is_terminal ?? detail?.is_terminal ?? false
  const reachedIndex = status ? STAGES.indexOf(status) : -1

  async function cancel() {
    setCancelling(true)
    setCancelError(null)
    try {
      await api.cancelRun(id)
    } catch (err) {
      setCancelError(err instanceof Error ? err.message : String(err))
      setCancelling(false)
    }
  }

  return (
    <div className="stack">
      <div className="row-between">
        <h1>Run progress</h1>
        <Link to="/">← Library</Link>
      </div>

      {!event && !detail && <Loading what="run" />}
      {streamError && !event?.is_terminal && (
        <ErrorBox message="Progress stream disconnected — the run may have finished; reload to refresh." />
      )}

      {(event || detail) && (
        <Section title="Status">
          <p className="row-between">
            <span>
              <Chip text={status ?? 'unknown'} />{' '}
              {event?.running ? <span className="muted">running…</span> : null}
            </span>
            {!isTerminal && (
              <button className="btn" onClick={cancel} disabled={cancelling}>
                {cancelling ? 'Cancelling…' : 'Cancel run'}
              </button>
            )}
          </p>
          {cancelError && <ErrorBox message={cancelError} />}
          {detail && (
            <p>
              Profile: <strong>{detail.profile_ref}</strong>
              {(detail.hard_gates_override.max_capital_usd != null ||
                detail.hard_gates_override.max_time_to_revenue_days != null) && (
                <>
                  {' '}
                  <Chip text="gates overridden for this run" tone="warn" />
                  <span className="muted">
                    {' '}
                    {detail.hard_gates_override.max_capital_usd != null &&
                      `capital ≤ $${detail.hard_gates_override.max_capital_usd.toFixed(0)}`}
                    {detail.hard_gates_override.max_capital_usd != null &&
                      detail.hard_gates_override.max_time_to_revenue_days != null &&
                      ', '}
                    {detail.hard_gates_override.max_time_to_revenue_days != null &&
                      `time-to-revenue ≤ ${detail.hard_gates_override.max_time_to_revenue_days} days`}
                  </span>
                </>
              )}
            </p>
          )}
          {event && (
            <CostMeter spent={event.cost_to_date_usd} ceiling={event.budget_ceiling_usd} />
          )}
          <ol className="funnel">
            {STAGES.map((stage, i) => (
              <li
                key={stage}
                className={
                  i < reachedIndex ? 'done' : i === reachedIndex ? 'current' : 'pending'
                }
              >
                {stage}
              </li>
            ))}
          </ol>
        </Section>
      )}

      {event && (
        <Section title="Findings so far">
          <div className="counts">
            <div>
              <strong>{event.leads}</strong> leads
            </div>
            <div>
              <strong>{event.kept_leads}</strong> kept
            </div>
            <div>
              <strong>{event.killed_leads}</strong> killed
            </div>
            <div>
              <strong>{event.shortlist}</strong> shortlisted
            </div>
            <div>
              <strong>{event.opportunities}</strong> opportunities
            </div>
          </div>
          <p>
            <Link to={`/runs/${id}/leads`}>Inspect leads &amp; kills</Link>
          </p>
        </Section>
      )}

      <Section title="Next">
        {status === 'awaiting_checkpoint' && (
          <Link className="btn primary" to={`/runs/${id}/checkpoint`}>
            Review the checkpoint
          </Link>
        )}
        {status === 'completed' && detail?.selected_opportunity_id && (
          <Link className="btn primary" to={`/opportunities/${detail.selected_opportunity_id}`}>
            Read the decision brief
          </Link>
        )}
        {(status === 'completed_partial' ||
          status === 'halted_budget' ||
          status === 'halted_error' ||
          status === 'cancelled') && (
          <p className="muted">
            The run stopped early. {detail?.has_report && 'An incomplete report was produced.'}
          </p>
        )}
        {status && !event?.is_terminal && status !== 'awaiting_checkpoint' && (
          <p className="muted">Work in progress — this page updates live.</p>
        )}
      </Section>
    </div>
  )
}
