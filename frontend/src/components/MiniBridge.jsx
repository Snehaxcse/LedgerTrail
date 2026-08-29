import Amount from './Amount'

const STEPS = [
  { key: 'total_gross', label: 'Gross' },
  { key: 'total_refunds', label: 'Refunds' },
  { key: 'total_fees', label: 'Fees' },
  { key: 'total_tax', label: 'Tax' },
  { key: 'total_net', label: 'Net' },
]

export default function MiniBridge({ batch }) {
  return (
    <ol className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-5 sm:gap-4">
      {STEPS.map((step, index) => (
        <li key={step.key} className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
            {index > 0 && index < 4 ? '− ' : index === 4 ? '= ' : ''}
            {step.label}
          </p>
          <Amount value={batch[step.key]} className="mt-0.5 block text-sm text-ink" />
        </li>
      ))}
    </ol>
  )
}
