import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { AuthProvider, useAuth } from './lib/AuthContext'
import AgentDemo from './pages/AgentDemo'
import AuditTrail from './pages/AuditTrail'
import BankStatement from './pages/BankStatement'
import BatchBridge from './pages/BatchBridge'
import Dashboard from './pages/Dashboard'
import IngestionDemo from './pages/IngestionDemo'
import Login from './pages/Login'
import Trend from './pages/Trend'
import Transparency from './pages/Transparency'

function AuthGate() {
  const { user, loading } = useAuth()

  if (loading) return null
  if (!user) return <Login />

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/batches/:id" element={<BatchBridge />} />
        <Route path="/bank-statement" element={<BankStatement />} />
        <Route path="/trend" element={<Trend />} />
        <Route path="/audit" element={<AuditTrail />} />
        <Route path="/transparency" element={<Transparency />} />
        <Route path="/agent-demo" element={<AgentDemo />} />
        <Route path="/ingestion-demo" element={<IngestionDemo />} />
      </Routes>
    </Layout>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuthGate />
      </AuthProvider>
    </BrowserRouter>
  )
}
