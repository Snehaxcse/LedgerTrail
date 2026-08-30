import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import AuditTrail from './pages/AuditTrail'
import BankStatement from './pages/BankStatement'
import BatchBridge from './pages/BatchBridge'
import Dashboard from './pages/Dashboard'
import Trend from './pages/Trend'
import Transparency from './pages/Transparency'

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/batches/:id" element={<BatchBridge />} />
          <Route path="/bank-statement" element={<BankStatement />} />
          <Route path="/trend" element={<Trend />} />
          <Route path="/audit" element={<AuditTrail />} />
          <Route path="/transparency" element={<Transparency />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
