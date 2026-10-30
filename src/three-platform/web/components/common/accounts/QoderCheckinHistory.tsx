'use client';

import {useEffect} from 'react';
import {accountApi} from '@/lib/api';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useRealm} from '@/lib/realm-context';
import {useT} from '@/lib/i18n/provider';
import {evidenceAccess} from '@/lib/operational-evidence';
import {LoadError} from '@/components/common/states/LoadError';
import {Button} from '@/components/ui/button';

/** Local engine evidence is not a provider receipt or an invented balance delta. */
export function QoderCheckinHistory() {
  const t = useT();
  const {realm} = useRealm();
  const {values, errors, refresh, reload, isInitialLoading, isRefreshing} = useAsyncAll({
    history: () => accountApi.checkinLogs(20, 0, undefined, 30, realm),
  }, [realm]);
  useHeartbeat(refresh, 30000);
  useEffect(() => {
    const changed = () => { void refresh(); };
    window.addEventListener('workbuddy-manager:accounts-changed', changed);
    return () => window.removeEventListener('workbuddy-manager:accounts-changed', changed);
  }, [refresh]);
  const access = evidenceAccess(errors.history);
  const history = access === 'forbidden' || access === 'unavailable' ? undefined : values.history;
  const rows = history?.items ?? [];
  return (
    <section className="overflow-hidden rounded-[24px] border border-blue-100/80 bg-white/70 shadow-[0_12px_48px_-32px_#416db8] dark:border-border dark:bg-muted" aria-busy={isRefreshing}>
      <div className="flex items-center justify-between gap-3 px-4 py-4 md:px-5">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">{t('tasks.rewardHistoryTitle')}</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{t('tasks.rewardEvidenceNote')}</p>
        </div>
        <Button size="sm" variant="outline" className="shrink-0 rounded-full" disabled={isRefreshing} onClick={() => { void reload(); }}>{t('common.refresh')}</Button>
      </div>
      {history?.scheduler?.state === 'uncertain' && <p className="mx-4 mb-3 rounded-xl bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">{t('tasks.rewardSchedulerUncertain')}</p>}
      {history?.scheduler?.automation_owner === 'engine' && <p className="px-4 pb-3 text-xs leading-relaxed text-muted-foreground">{t('tasks.rewardEngineScheduler')}</p>}
      {history?.scheduler?.automation_owner === 'unverified' && <p className="px-4 pb-3 text-xs leading-relaxed text-muted-foreground">{t('tasks.rewardSchedulerOwnershipUnknown')}</p>}
      {'history' in errors && <div className="px-4 pb-3"><LoadError message={t('tasks.checkinLoadFailed')} onRetry={reload} /></div>}
      {access && <p className="px-4 pb-3 text-xs text-muted-foreground">{t(`operations.${access}`)}</p>}
      {isInitialLoading ? <p className="px-4 pb-4 text-xs text-muted-foreground">{t('common.loading')}</p> : rows.length ? (
        <div className="divide-y divide-blue-100/60 dark:divide-border">
          {rows.map(row => {
            const status = row.reward_status ?? 'unknown';
            const rewarded = status === 'rewarded' && typeof row.earned_credit === 'number' && Number.isFinite(row.earned_credit) && row.earned_credit > 0;
            const failed = status === 'failed';
            return <div key={row.id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-3 md:px-5">
              <div className="min-w-0 flex-1"><p className="truncate text-xs font-medium">{row.nickname || row.uid || '—'}</p><time className="mt-1 block text-[11px] text-muted-foreground">{row.ts ? fmtDateTime(row.ts) : '—'}</time></div>
              <div className="text-right"><p className={'text-xs font-medium ' + (rewarded ? 'text-emerald-700 dark:text-emerald-400' : failed ? 'text-red-700 dark:text-red-400' : 'text-muted-foreground')}>
                {rewarded ? t('tasks.rewardGranted', {amount: fmtNumber(row.earned_credit!)}) : t(`tasks.rewardStatus_${status}`)}
              </p>{status === 'no_reward' && <p className="mt-1 text-[11px] text-muted-foreground">{t('tasks.rewardZeroNote')}</p>}</div>
            </div>;
          })}
        </div>
      ) : !('history' in errors) && <p className="px-4 pb-4 text-xs text-muted-foreground">{t('tasks.rewardHistoryEmpty')}</p>}
      {history && <p className="border-t border-blue-100/60 px-4 py-3 text-[11px] text-muted-foreground dark:border-border">{t('tasks.rewardHistoryWindow', {shown: rows.length})}</p>}
    </section>
  );
}
