import { Route, Routes } from 'react-router-dom'
import { Library } from './pages/Library'
import { NewRun } from './pages/NewRun'
import { RunProgress } from './pages/RunProgress'
import { RunLeads } from './pages/RunLeads'
import { Checkpoint } from './pages/Checkpoint'
import { OpportunityDetail } from './pages/OpportunityDetail'
import { EvidenceExplorer } from './pages/EvidenceExplorer'
import { Compare } from './pages/Compare'

export function App() {
  return (
    <main className="app">
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
