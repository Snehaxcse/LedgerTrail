export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const API_BASE = String(import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/$/, '') || '/api'

async function api(path, options) {
  const response = await fetch(`${API_BASE}${path}`, options)
  if (!response.ok) {
    const detail = await response.text()
    throw new ApiError(detail || `Request failed (${response.status})`, response.status)
  }
  return response.json()
}

export function getBatches() {
  return api('/batches')
}

export function getBatch(id) {
  return api(`/batches/${id}`)
}

export function getBatchExceptions(id) {
  return api(`/batches/${id}/exceptions`)
}

export function getExceptionExplanation(batchId, exceptionId) {
  return api(`/batches/${batchId}/exceptions/${exceptionId}/explain`)
}

export function getExceptionEvidence(batchId, exceptionId) {
  return api(`/batches/${batchId}/exceptions/${exceptionId}/evidence`)
}

export function reviewException(id, { approver, decision, reason }) {
  const body = { approver, decision }
  if (reason != null && reason !== '') {
    body.reason = reason
  }
  return api(`/exceptions/${id}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getAuditTrail({ limit = 50, offset = 0 } = {}) {
  return api(`/audit-trail?limit=${limit}&offset=${offset}`)
}

export function getTransparency() {
  return api('/transparency')
}

export function getHeldOutEvaluation() {
  return api('/evaluation/held-out')
}

export function getTrend() {
  return api('/trend')
}

export function getStats() {
  return api('/stats')
}

export function getDataSources() {
  return api('/data-sources')
}

export function getBankTransactions() {
  return api('/bank-transactions')
}

export function verifyBankNarration(bankTransactionId) {
  return api(`/bank-transactions/${bankTransactionId}/verify-narration`)
}

export function getApprovers() {
  return api('/approvers')
}

export function investigateException(batchId, exceptionId) {
  return api(`/batches/${batchId}/exceptions/${exceptionId}/investigate`)
}

export function getHeroCase() {
  return api('/demo/hero-case')
}

export function investigateHeroCase() {
  return api('/demo/hero-case/investigate')
}

export function runHoldoutIdempotencyCheck() {
  return api('/demo/holdout-sandbox/idempotency-check', { method: 'POST' })
}

export function startHoldoutReconciliationSandbox() {
  return api('/demo/holdout-sandbox/reconciliation/start', { method: 'POST' })
}

export function runHoldoutSandboxMatch(sandboxId) {
  return api(`/demo/holdout-sandbox/reconciliation/${sandboxId}/match`, { method: 'POST' })
}

export function runHoldoutSandboxBridge(sandboxId) {
  return api(`/demo/holdout-sandbox/reconciliation/${sandboxId}/bridge`, { method: 'POST' })
}

export function runHoldoutSandboxClassify(sandboxId) {
  return api(`/demo/holdout-sandbox/reconciliation/${sandboxId}/classify`, { method: 'POST' })
}

export function startHoldoutApprovalSandbox() {
  return api('/demo/holdout-sandbox/approval/start', { method: 'POST' })
}

export function approveHoldoutSandboxException(sandboxId, { exceptionId, approver, decision, reason }) {
  const body = { exception_id: exceptionId, approver, decision }
  if (reason != null && reason !== '') body.reason = reason
  return api(`/demo/holdout-sandbox/approval/${sandboxId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function getExceptionIndex() {
  const batches = await getBatches()
  const lists = await Promise.all(batches.map((batch) => getBatchExceptions(batch.id)))
  const byId = {}
  const byClassification = {}
  for (const list of lists) {
    for (const row of list) {
      byId[row.id] = row
      if (!byClassification[row.classification]) byClassification[row.classification] = []
      byClassification[row.classification].push(row)
    }
  }
  return { byId, byClassification }
}
