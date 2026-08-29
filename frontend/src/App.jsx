import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import BatchBridge from './pages/BatchBridge'
import Dashboard from './pages/Dashboard'

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/batches/:id" element={<BatchBridge />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
