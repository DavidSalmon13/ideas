import { useState } from 'react'
import { Route, Routes } from 'react-router-dom'
import { getApiToken, setApiToken } from './api/client'
import { Library } from './pages/Library'
import { NewRun } from './pages/NewRun'
import { RunProgress } from './pages/RunProgress'
import { RunLeads } from './pages/RunLeads'
import { Checkpoint } from './pages/Checkpoint'
import { OpportunityDetail } from './pages/OpportunityDetail'
import { EvidenceExplorer } from './pages/EvidenceExplorer'
import { Compare } from './pages/Compare'

// Only relevant when the API is deployed with OPP_API_AUTH_TOKEN set (plan.md §11, §18,
// §29); harmless and easy to ignore for the default local/no-auth setup.
function ApiTokenField() {
  const [token, setToken] = useState(getApiToken())

  function update(value: string) {
    setToken(value)
    setApiToken(value)
  }

  return (
    <label className="api-token">
      <span className="muted">API token</span>
      <input
        type="password"
        placeholder="only needed for a token-protected deployment"
        value={token}
        onChange={(e) => update(e.target.value)}
      />
    </label>
  )
}

export function App() {
  return (
    <main className="app">
      <div className="row-between api-token-bar">
        <span />
        <ApiTokenField />
      </div>
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/runs/new" element={<NewRun />} />
        <Route path="/runs/:id" element={<RunProgress />} />
        <Route path="/runs/:id/leads" element={<RunLeads />} />
        <Route path="/runs/:id/checkpoint" element={<Checkpoint />} />
        <Route path="/opportunities/:id" element={<OpportunityDetail />} />
        <Route path="/opportunities/:id/evidence" element={<EvidenceExplorer />} />
        <Route path="/compare" element={<Compare />} />
      </Routes>
    </main>
  )
}
