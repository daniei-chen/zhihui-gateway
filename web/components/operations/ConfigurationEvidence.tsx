'use client';

import {useState} from 'react';
import {useAuth} from '@/lib/auth-context';
import {operationalApi} from '@/lib/operational-api';
import {useAsyncAll} from '@/lib/use-async-data';
import {useT} from '@/lib/i18n/provider';
import {safeConfigurationValue, safeNamedConfigurationValue} from '@/lib/operational-evidence';
import {Input} from '@/components/ui/input';
import {EvidenceFrame} from './EvidenceFrame';

export function ConfigurationEvidence() {
  const {isAdmin} = useAuth();
  const t = useT();
  if (!isAdmin) return <section className="rounded-[20px] bg-muted p-4 text-xs"><h2 className="mb-2 text-sm font-medium">{t('operations.configurationTitle')}</h2>{t('operations.adminOnly')}</section>;
  return <ConfigurationBody />;
}

function ConfigurationBody() {
  const t = useT();
  const [filter, setFilter] = useState('');
  const state = useAsyncAll({snapshot: operationalApi.configuration, drift: operationalApi.drift}, []);
  const snapshot = state.values.snapshot;
  const drift = state.values.drift;
  return <div className="space-y-4">
    <EvidenceFrame title={t('operations.configurationTitle')} source={t('operations.configurationScope')}
      loading={state.isInitialLoading || state.isRefreshing} error={state.errors.snapshot} reload={state.reload}>
      {snapshot && <div className="space-y-3 text-xs">
        <p className="text-muted-foreground">{snapshot.note}</p>
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">{(['upstream_timeout_s', 'drain_timeout_s', 'http_pool_connections', 'http_pool_keepalive'] as const).map(key => <div key={key} className="rounded-xl border border-border p-3"><dt className="break-all text-muted-foreground">{key}</dt><dd>{safeConfigurationValue(snapshot.active[key])}</dd></div>)}</dl>
        <details><summary className="cursor-pointer">{t('operations.startupSnapshot')}</summary>
          <div className="mt-2 max-h-72 overflow-auto" tabIndex={0} role="region" aria-label={t('operations.startupSnapshot')}><table className="w-full text-left"><thead><tr><th scope="col">{t('operations.setting')}</th><th scope="col">{t('operations.effective')}</th><th scope="col">{t('operations.source')}</th></tr></thead><tbody>{Object.entries(snapshot.configuration.settings).map(([name, row]) => <tr key={name} className="border-t border-border"><th scope="row" className="break-all p-2 font-normal">{name}</th><td className="max-w-xs break-all p-2">{safeNamedConfigurationValue(name, row.value)}</td><td className="p-2">{row.source}</td></tr>)}</tbody></table></div>
        </details>
      </div>}
    </EvidenceFrame>
    <EvidenceFrame title={t('operations.driftTitle')} source={t('operations.driftScope')}
      loading={state.isInitialLoading || state.isRefreshing} error={state.errors.drift} reload={state.reload}>
      {drift && <div className="space-y-3 text-xs">
        <p>{drift.release} · high {drift.summary.high} / medium {drift.summary.medium} / ok {drift.summary.ok}</p>
        {!drift.sources.declared.available && <p role="status">{t('operations.declaredUnavailable')} · {drift.sources.declared.reason}</p>}
        <Input className="max-w-sm" aria-label={t('operations.filterSetting')} placeholder={t('operations.filterSetting')} value={filter} onChange={event => setFilter(event.target.value)} />
        <div className="max-h-96 overflow-auto" tabIndex={0} role="region" aria-label={t('operations.driftTitle')}><table className="w-full text-left"><thead><tr>{['setting', 'schema', 'sourceDefault', 'declared', 'process', 'flags'].map(key => <th key={key} scope="col" className="p-2">{t(`operations.${key}`)}</th>)}</tr></thead><tbody>{drift.settings.filter(row => `${row.name} ${row.flags.join(' ')}`.toLowerCase().includes(filter.toLowerCase())).map(row => <tr key={row.name} className="border-t border-border">
          <th scope="row" className="min-w-36 break-all p-2 font-normal">{row.name}<div className={row.risk === 'high' ? 'text-destructive' : 'text-muted-foreground'}>{row.risk}</div></th>
          {[row.schema_default, row.source_default ?? row.source_expression, row.declared, row.process].map((value, index) => <td key={index} className="max-w-56 break-all p-2">{safeNamedConfigurationValue(row.name, value, row.sensitive, row.registered)}</td>)}
          <td className="min-w-32 break-words p-2">{row.flags.join(', ') || '—'}</td>
        </tr>)}</tbody></table></div>
      </div>}
    </EvidenceFrame>
  </div>;
}
