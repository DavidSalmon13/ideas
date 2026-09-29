import type { ReactNode } from 'react'

export function Loading({ what }: { what?: string }) {
  return <p className="muted">Loading{what ? ` ${what}` : ''}…</p>
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <p className="error" role="alert">
      {message}
    </p>
  )
}

export function Chip({ text, tone }: { text: string; tone?: string }) {
  return <span className={`chip${tone ? ` chip-${tone}` : ''}`}>{text}</span>
}

export function Money({ usd }: { usd: number }) {
  return <span>${usd.toFixed(2)}</span>
}

// Colour verdicts and confidences consistently across the app.
export function verdictTone(verdict: string | null | undefined): string {
  switch (verdict) {
    case 'pursue':
      return 'good'
    case 'promising_but_unproven':
      return 'warn'
    case 'kill':
      return 'bad'
    default:
      return 'neutral'
  }
}

export function confidenceTone(confidence: string): string {
  return confidence === 'high' ? 'good' : confidence === 'low' ? 'bad' : 'warn'
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card">
      <h2>{title}</h2>
      {children}
    </section>
  )
}

// A budget meter: spend against the ceiling.
export function CostMeter({ spent, ceiling }: { spent: number; ceiling: number }) {
  const pct = ceiling > 0 ? Math.min(100, (spent / ceiling) * 100) : 0
  return (
    <div className="meter" title={`$${spent.toFixed(4)} of $${ceiling.toFixed(2)}`}>
      <div className="meter-fill" style={{ width: `${pct}%` }} />
      <span className="meter-label">
        ${spent.toFixed(2)} / ${ceiling.toFixed(2)}
      </span>
    </div>
  )
}
