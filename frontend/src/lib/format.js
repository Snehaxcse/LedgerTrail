const inr = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function formatInr(value) {
  if (value === null || value === undefined) return '—'
  return inr.format(value)
}

export function formatDate(isoDate) {
  if (!isoDate) return '—'
  const [year, month, day] = String(isoDate).split('-').map(Number)
  return new Date(year, month - 1, day).toLocaleDateString('en-IN', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

export function formatDateShort(isoDate) {
  if (!isoDate) return '—'
  const [year, month, day] = String(isoDate).split('-').map(Number)
  return new Date(year, month - 1, day).toLocaleDateString('en-IN', {
    day: 'numeric',
    month: 'short',
  })
}

export function formatScore(value) {
  if (value === null || value === undefined) return '—'
  return Number(value).toFixed(2)
}

export function formatPercent(value) {
  if (value === null || value === undefined) return null
  return new Intl.NumberFormat('en-IN', {
    style: 'percent',
    maximumFractionDigits: 0,
  }).format(Number(value))
}

export function formatRatePercent(value) {
  if (value === null || value === undefined) return '—'
  return new Intl.NumberFormat('en-IN', {
    style: 'percent',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value))
}

export function formatPercentPoints(value) {
  if (value === null || value === undefined) return '—'
  return `${Number(value).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}%`
}

export function formatDateTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-IN', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatMatchLine(matchType, confidenceScore, matchBasis) {
  const basis = matchBasis || null
  const type = matchType || null
  const percent = formatPercent(confidenceScore)
  if (!basis && !type && !percent) return null
  const label = basis || (type ? `Matched: ${type}` : null)
  if (label && percent) return `${label} (${percent})`
  return label || percent
}

export function formatClassification(code) {
  if (!code) return '—'
  return code
    .split('_')
    .map((word) => word.charAt(0) + word.slice(1).toLowerCase())
    .join(' ')
}
