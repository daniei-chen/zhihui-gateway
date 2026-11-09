'use client';

import type {ReactNode} from 'react';
import {useT} from '@/lib/i18n/provider';
import {evidenceAccess} from '@/lib/operational-evidence';
import {Button} from '@/components/ui/button';

export function EvidenceFrame({title, source, loading, error, reload, children}: {
  title: string; source: string; loading?: boolean; error?: unknown;
  reload: () => unknown; children: ReactNode;
}) {
  const t = useT();
  const access = evidenceAccess(error);
  return <section className="min-w-0 space-y-3 rounded-[20px] bg-muted p-4" aria-busy={loading}>
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0"><h2 className="text-sm font-medium">{title}</h2><p className="mt-1 break-words text-xs text-muted-foreground">{source}</p></div>
      <Button size="sm" variant="outline" onClick={() => { void reload(); }}>{t('operations.refresh')}</Button>
    </div>
    {loading && <p role="status" className="text-xs">{t('operations.loading')}</p>}
    {access && <p role="alert" className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs">{t(`operations.${access}`)}</p>}
    {access !== 'forbidden' && access !== 'unavailable' && children}
  </section>;
}
