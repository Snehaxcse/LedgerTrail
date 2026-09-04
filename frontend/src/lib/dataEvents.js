// Minimal pub-sub so a mutation in one page (e.g. the Ingestion Demo's real
// database write) can tell already-mounted pages elsewhere to refetch
// authoritative backend state, instead of relying only on a fresh mount's
// own useEffect. No caching layer to invalidate -- this just closes the gap
// where a mutation happens while a batch-list/stats view is already mounted
// (or gets revisited via a path that doesn't force a remount).
const target = new EventTarget()
const BATCHES_INVALIDATED = 'batches-invalidated'

export function invalidateBatches() {
  target.dispatchEvent(new Event(BATCHES_INVALIDATED))
}

export function onBatchesInvalidated(handler) {
  target.addEventListener(BATCHES_INVALIDATED, handler)
  return () => target.removeEventListener(BATCHES_INVALIDATED, handler)
}
