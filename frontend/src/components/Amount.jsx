import { formatInr } from '../lib/format'

export default function Amount({ value, className = '' }) {
  return <span className={`font-mono tabular-nums ${className}`}>{formatInr(value)}</span>
}
