'use client';

import {useState} from 'react';
import {operationalApi} from '@/lib/operational-api';
import {usePlatform} from '@/lib/platform-context';
import {useAsyncAll} from '@/lib/use-async-data';
import {useT} from '@/lib/i18n/provider';
import {evidenceValue} from '@/lib/operational-evidence';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {EvidenceFrame} from './EvidenceFrame';
import {AttemptEvidence} from './AttemptEvidence';

export function OutcomeExplorer() {
  const t = useT();
  const {platform} = usePlatform();
  const [page, setPage] = useState(1);
  const [errorCode, setErrorCode] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const state = useAsyncAll({list: () => operationalApi.outcomes(platform, page, query, 7)}, [platform], [page, query]);
  const data = state.values.list;
  const value = (v: unknown) => evidenceValue(v, t('operations.unknown'));
  return <EvidenceFrame title={t('operations.outcomesTitle')} source={t('operations.outcomesScope', {platform})}
    loading={state.isInitialLoading || state.isRefreshing} error={state.errors.list} reload={state.reload}>
    <form className="flex flex-wrap gap-2" onSubmit={event => {event.preventDefault(); setPage(1); setQuery(errorCode.trim()); setSelected(null);}}>
      <Input className="max-w-xs" aria-label={t('operations.errorCode')} value={errorCode} maxLength={64} placeholder={t('operations.errorCode')} onChange={event => setErrorCode(event.target.value)} />
      <Button type="submit" variant="outline">{t('operations.filter')}</Button>
    </form>
    {data && <>
      {!data.error_code_available && <p role="status" className="text-xs">{t('operations.codeUnavailable')}</p>}
      <div className="overflow-x-auto" tabIndex={0} role="region" aria-label={t('operations.outcomesTitle')}>
        <table className="w-full text-left text-xs"><thead><tr>{['executionId', 'terminal', 'errorCode', 'recordedAttempts'].map(key => <th key={key} scope="col" className="p-2">{t(`operations.${key}`)}</th>)}</tr></thead>
          <tbody>{data.items.map(row => <tr key={row.request_id} className="border-t border-border">
            <th scope="row" className="max-w-56 break-all p-2 text-left font-normal"><button className="text-primary underline-offset-4 hover:underline" onClick={() => setSelected(row.request_id)}>{row.request_id}</button></th>
            <td className="p-2">{value(row.terminal_state)}</td><td className="p-2">{value(row.error_code)}</td><td className="p-2">{value(row.attempt_count)}</td>
          </tr>)}</tbody></table>
      </div>
      {data.items.length === 0 && <p className="text-xs text-muted-foreground">{t('operations.noOutcomes')}</p>}
      <div className="flex items-center gap-2 text-xs"><span>{data.total} · {page}</span>
        <Button size="sm" variant="outline" disabled={page <= 1} onClick={() => {setPage(page - 1); setSelected(null);}}>{t('common.previousPage')}</Button>
        <Button size="sm" variant="outline" disabled={page * data.size >= data.total} onClick={() => {setPage(page + 1); setSelected(null);}}>{t('common.nextPage')}</Button>
      </div>
      {selected && <AttemptEvidence requestId={selected} />}
    </>}
  </EvidenceFrame>;
}
