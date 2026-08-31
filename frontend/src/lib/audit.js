import { formatClassification, formatInr, formatPercent } from './format'

function parseState(raw) {
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function titleAction(action) {
  return String(action || '')
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

export function formatBatchLabel(batchId) {
  if (batchId == null) return null
  return `Batch ${String(batchId).padStart(2, '0')}`
}

function subjectLabel(classification, batchId) {
  const name = classification ? formatClassification(classification) : null
  const batch = formatBatchLabel(batchId)
  if (name && batch) return `${name} (${batch})`
  if (name) return name
  if (batch) return batch
  return 'Exception'
}

export function resolveAuditContext(event, index = { byId: {}, byClassification: {} }) {
  const after = parseState(event.after_state) || {}
  if (after.exception_id != null && index.byId[after.exception_id]) {
    return index.byId[after.exception_id]
  }
  if (after.classification) {
    const matches = index.byClassification[after.classification] || []
    if (matches.length === 1) return matches[0]
  }
  if (after.batch_id != null) {
    return { batch_id: after.batch_id, classification: after.classification || null }
  }
  return null
}

export function describeAuditEvent(event, index) {
  const after = parseState(event.after_state)
  const before = parseState(event.before_state)
  const context = resolveAuditContext(event, index)
  const batchId = context?.batch_id ?? after?.batch_id
  const classification = context?.classification ?? after?.classification

  if (event.action === 'match_created') {
    // match_basis is written server-side at match-creation time (see app/matching.py's
    // match_basis()) -- older AuditEvent rows predate that field and won't have it, so
    // this falls back to the bare match_type label rather than crashing or omitting detail.
    const label = after?.match_basis || after?.match_type
    const percent = formatPercent(after?.confidence_score)
    const batch = formatBatchLabel(batchId)
    if (batch && label && percent) return `Match created for ${batch} (${label}, ${percent})`
    if (batch) return `Match created for ${batch}`
    return 'Match created'
  }

  if (event.action === 'exception_created') {
    const amount = after?.unexplained_amount
    const amountText =
      amount === null || amount === undefined ? '' : ` · unexplained ${formatInr(amount)}`
    return `Exception created: ${subjectLabel(classification, batchId)}${amountText}`
  }

  if (event.action === 'exception_reviewed') {
    const who = after?.approver || 'unknown'
    const decision = after?.decision || after?.status
    const reason = after?.reason
    const subject = subjectLabel(classification, batchId)
    if (decision === 'rejected') {
      return reason ? `${subject} rejected by ${who}: ${reason}` : `${subject} rejected by ${who}`
    }
    if (decision === 'approved') {
      return `${subject} approved by ${who}`
    }
    if (before?.status && after?.status) {
      return `${subject} ${before.status} → ${after.status} by ${who}`
    }
    return `${subject} reviewed by ${who}`
  }

  return titleAction(event.action)
}

// Structured version of describeAuditEvent for AuditTrail.jsx's card layout --
// same underlying event/index data, plus batchesById (from GET /batches, for
// match_created's bank amount/variance -- not present in the event's own
// after_state, and app/matching.py stays untouched to get it) and
// approversByName (from GET /approvers, for exception_reviewed's role label).
// Falls back to describeAuditEvent's plain text for any action type not
// explicitly handled here, so a future action never renders blank.
export function auditEventDetails(event, index, batchesById = {}, approversByName = {}) {
  const after = parseState(event.after_state) || {}
  const before = parseState(event.before_state) || {}
  const context = resolveAuditContext(event, index)
  const batchId = context?.batch_id ?? after?.batch_id ?? null
  const classification = context?.classification ?? after?.classification ?? null
  const batch = batchId != null ? batchesById[batchId] : null
  const subject = subjectLabel(classification, batchId)

  if (event.action === 'match_created') {
    return {
      kind: 'matched',
      badgeLabel: 'MATCHED',
      badgeTone: 'forest',
      subject: formatBatchLabel(batchId) || subject,
      matchBasis: after.match_basis || after.match_type || null,
      confidencePercent: formatPercent(after.confidence_score),
      bankAmount: batch?.matched_bank_amount ?? null,
      variance: batch?.variance ?? null,
    }
  }

  if (event.action === 'exception_created') {
    return {
      kind: 'created',
      badgeLabel: 'EXCEPTION CREATED',
      badgeTone: 'amber',
      subject,
      amount: after.unexplained_amount ?? null,
      requiresApproval: after.requires_approval ?? null,
    }
  }

  if (event.action === 'exception_reviewed') {
    const decision = after.decision || after.status
    const fromStatus = String(before.status || 'open').toUpperCase()
    const toStatus = String(after.status || decision || '').toUpperCase()
    const actor = after.approver || 'unknown'
    return {
      kind: 'reviewed',
      badgeLabel: `${fromStatus} → ${toStatus}`,
      badgeTone: decision === 'rejected' ? 'rust' : 'forest',
      subject,
      actor,
      role: approversByName[actor] || null,
      reason: after.reason || null,
    }
  }

  return {
    kind: 'other',
    badgeLabel: titleAction(event.action),
    badgeTone: 'brass',
    subject: describeAuditEvent(event, index),
  }
}
