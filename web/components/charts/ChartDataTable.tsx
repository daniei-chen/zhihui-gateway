'use client';

import {useT} from '@/lib/i18n/provider';

export function ChartDataTable({title, columns, rows}: {
  title: string;
  columns: readonly string[];
  rows: readonly (readonly (string | number)[])[];
}) {
  const t = useT();
  return (
    <details className="mt-3 rounded-xl border border-border px-3 py-2 text-xs">
      <summary className="cursor-pointer rounded-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">
        {t('a11y.chartData')}
      </summary>
      <div className="mt-2 max-h-72 overflow-auto" tabIndex={0} role="region" aria-label={title}>
        <table className="w-full border-collapse text-left tabular-nums">
          <caption className="sr-only">{title}</caption>
          <thead><tr>{columns.map((column) => <th key={column} scope="col" className="border-b border-border px-2 py-2 font-medium">{column}</th>)}</tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, column) => column === 0
            ? <th key={column} scope="row" className="whitespace-nowrap border-b border-border/50 px-2 py-1.5 font-normal">{cell}</th>
            : <td key={column} className="border-b border-border/50 px-2 py-1.5">{cell}</td>)}</tr>)}</tbody>
        </table>
      </div>
    </details>
  );
}
