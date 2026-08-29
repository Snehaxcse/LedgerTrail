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
