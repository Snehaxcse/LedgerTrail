async function api(path, options) {
  const response = await fetch(`/api${path}`, options)
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `Request failed (${response.status})`)
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
