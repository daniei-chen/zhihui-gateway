'use client';

import {operationalApi} from '@/lib/operational-api';
import {evidenceValue} from '@/lib/operational-evidence';
import {useAsyncAll} from '@/lib/use-async-data';
import {useT} from '@/lib/i18n/provider';
import {EvidenceFrame} from './EvidenceFrame';

export function AttemptEvidence({requestId}: {requestId?: string | null}) {
  const t = useT();
  if (!requestId) return <p className="text-xs text-muted-foreground">{t('operations.noExecutionId')}</p>;
  return <AttemptBody key={requestId} requestId={requestId} />;
}

function AttemptBody({requestId}: {requestId: string}) {
  const t = useT();
  const state = useAsyncAll({evidence: () => operationalApi.explain(requestId)}, [requestId]);
  const data = state.values.evidence;
  const value = (v: unknown) => evidenceValue(v, t('operations.unknown'));
  return <EvidenceFrame title={t('operations.attemptTitle')} source={`${t('operations.executionId')}: ${requestId}`}
    loading={state.isInitialLoading || state.isRefreshing} error={state.errors.evidence} reload={state.reload}>
    {data && <div className="space-y-3 text-xs">
      <p className="rounded-xl border border-border p-3">{t('operations.selectionBoundary')}<br /><code className="break-all">{data.reason}</code></p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div><dt className="text-muted-foreground">{t('operations.terminal')}</dt><dd>{value(data.request.terminal_state)}</dd></div>
        <div><dt className="text-muted-foreground">{t('operations.errorCode')}</dt><dd>{value(data.request.error_code)}</dd></div>
        <div><dt className="text-muted-foreground">HTTP</dt><dd>{value(data.request.http_status)}</dd></div>
        <div><dt className="text-muted-foreground">{t('operations.recordedAttempts')}</dt><dd>{data.attempt_count_source === 'absent' ? t('operations.unknown') : data.attempt_count}</dd></div>
      </dl>
      {data.attempts.length === 0 && <p>{t('operations.noAttempts')}</p>}
      {data.attempts.map(attempt => <details key={attempt.attempt} className="rounded-xl border border-border p-3">
        <summary className="cursor-pointer font-medium">Attempt {attempt.attempt} · {value(attempt.last_phase)} · {value(attempt.duration_ms)} ms</summary>
        <dl className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          <div><dt className="text-muted-foreground">{t('operations.account')}</dt><dd className="break-all">{value(attempt.account_uid)}</dd></div>
          <div><dt className="text-muted-foreground">{t('operations.model')}</dt><dd className="break-all">{value(attempt.resolved_model)}</dd></div>
          <div><dt className="text-muted-foreground">{t('operations.billing')}</dt><dd>{attempt.billing ? `${value(attempt.billing.billing_fact)} / ${value(attempt.billing.billing_source)}` : t('operations.unknown')}</dd></div>
          <div><dt className="text-muted-foreground">{t('operations.phaseSequence')}</dt><dd className="break-words">{attempt.phase_sequence.join(' → ')}</dd></div>
        </dl>
      </details>)}
      {data.timeline.length > 0 && <details><summary className="cursor-pointer">{t('operations.timeline')}</summary><div className="mt-2 overflow-x-auto" tabIndex={0} role="region" aria-label={t('operations.timeline')}><table className="w-full text-left"><thead><tr><th scope="col">Attempt</th><th scope="col">{t('operations.phaseSequence')}</th><th scope="col">HTTP</th><th scope="col">{t('operations.elapsed')}</th></tr></thead><tbody>{data.timeline.map((event, index) => <tr key={index} className="border-t border-border"><td className="p-2">{event.attempt}</td><td className="p-2">{event.phase}</td><td className="p-2">{value(event.status)}</td><td className="p-2">{value(event.elapsed_ms)} ms</td></tr>)}</tbody></table></div></details>}
      <p className="text-muted-foreground">{t('operations.billingBoundary')}</p>
      <p className="break-words text-muted-foreground">{data.evidence.sources.join(' / ')} · {data.evidence.note}</p>
    </div>}
  </EvidenceFrame>;
}
