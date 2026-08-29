/**
 * Visual status is driven by is_reconciled from GET /batches.
 * Bank match / variance only change the supporting copy and the
 * NOT RECONCILED tint (amber vs rust) — they never turn the banner green.
 */
export function getReconStatus(batch) {
  if (batch.is_reconciled) {
    return {
      kind: 'reconciled',
      label: 'Reconciled',
      summary: 'Bank credit agrees with net, and nothing is blocking close.',
    }
  }

  if (batch.variance == null) {
    return {
      kind: 'unmatched',
      label: 'Not reconciled',
      summary: 'No bank transaction is linked to this batch.',
    }
  }

  if (Number(batch.variance) === 0) {
    return {
      kind: 'blocked',
      label: 'Not reconciled',
      summary:
        'The bank amount matches. This batch is still not reconciled — a matching credit is not enough.',
    }
  }

  return {
    kind: 'variance',
    label: 'Not reconciled',
    summary: 'Declared net and the matched bank credit do not agree.',
  }
}
