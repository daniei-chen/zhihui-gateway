import {fmtNumber} from '@/lib/format';

export function CreditFlowTile({label, value, tone, title}: {
  label: string;
  value: number | null;
  tone: 'income' | 'expense' | 'net';
  title: string;
}) {
  const known = typeof value === 'number' && Number.isFinite(value);
  const color = !known || value === 0
    ? 'text-muted-foreground'
    : tone === 'expense'
      ? 'text-amber-700 dark:text-amber-400'
      : value < 0
        ? 'text-red-600 dark:text-red-400'
        : 'text-emerald-600 dark:text-emerald-400';
  const sign = known && (tone === 'expense' || value < 0) ? '−' : '+';
  return (
    <div className="rounded-2xl bg-background/60 px-3 py-2.5" title={title}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={'mt-1 text-lg font-semibold tabular-nums ' + color}>
        {known ? sign + fmtNumber(Math.abs(value)) : '—'}
      </div>
    </div>
  );
}
