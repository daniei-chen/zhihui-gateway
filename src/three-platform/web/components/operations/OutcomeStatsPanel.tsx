'use client';

import {operationalApi} from '@/lib/operational-api';
import {usePlatform} from '@/lib/platform-context';
import {useRealm} from '@/lib/realm-context';
import {useAsyncAll} from '@/lib/use-async-data';
import {useT} from '@/lib/i18n/provider';
import {useHydrated} from '@/lib/use-hydrated';
import {evidenceValue} from '@/lib/operational-evidence';
import {EvidenceFrame} from './EvidenceFrame';

export function OutcomeStatsPanel({days}: {days: number}) {
  const {platform} = usePlatform();
  const hydrated = useHydrated();
  const t = useT();
  if (!hydrated) return null;
  if (platform !== 'workbuddy') return <section className="rounded-[20px] bg-muted p-4 text-xs"><h2 className="mb-2 text-sm font-medium">{t('operations.statsTitle')}</h2>{t('operations.statsUnsupported')}</section>;
  return <StatsBody days={days} />;
}

function StatsBody({days}: {days: number}) {
  const {realm} = useRealm();
  const t = useT();
  const state = useAsyncAll({stats: () => operationalApi.stats(days, realm)}, [days, realm]);
  const data = state.values.stats;
  const value = (v: unknown) => evidenceValue(v, t('operations.unknown'));
  return <EvidenceFrame title={t('operations.statsTitle')} source={t('operations.statsScope', {realm})}
    loading={state.isInitialLoading || state.isRefreshing} error={state.errors.stats} reload={state.reload}>
    {data && <div className="space-y-3 text-xs">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">{Object.entries(data.classes).map(([key, count]) => <div key={key} className="rounded-xl border border-border p-3"><dt className="text-muted-foreground">{key}</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{count}</dd></div>)}</dl>
      <p>{t('operations.coverage')}: {data.error_code_coverage.ratio == null ? t('operations.unknown') : `${Math.round(data.error_code_coverage.ratio * 100)}%`} · {data.error_code_coverage.note}</p>
      <div className="flex flex-wrap gap-2">{Object.entries(data.by_traffic_class).map(([key, count]) => <span key={key} className="rounded-full border border-border px-2 py-1">{key}: {count}</span>)}</div>
      <ul className="space-y-1">{data.error_code_top.map((row, i) => <li key={`${row.error_code}-${i}`}><code>{row.error_code}</code> · {value(row.error_source)} · {row.count}</li>)}</ul>
      {data.error_code_top.length === 0 && <p>{t('operations.noClassifiedErrors')}</p>}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-xl border border-border p-3"><h3 className="font-medium">{t('operations.backup')}</h3><p>{data.backup.status} · {value(data.backup.age_seconds)} s</p>{data.backup.reason && <p className="text-muted-foreground">{data.backup.reason}</p>}</div>
        <div className="rounded-xl border border-border p-3"><h3 className="font-medium">{t('operations.certificate')}</h3><p>{data.certificate.status} · {value(data.certificate.days_left)} d</p>{data.certificate.reason && <p className="text-muted-foreground">{data.certificate.reason}</p>}</div>
      </div>
      <details><summary className="cursor-pointer">{t('operations.boundary')}</summary><ul className="mt-2 list-inside list-disc text-muted-foreground">{data.notes.map(note => <li key={note}>{note}</li>)}</ul></details>
    </div>}
  </EvidenceFrame>;
}
