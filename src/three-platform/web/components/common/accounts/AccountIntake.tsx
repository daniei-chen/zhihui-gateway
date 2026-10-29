'use client';

import {useState} from 'react';
import {ArrowUpRight, Check, Plus} from 'lucide-react';
import {usePlatform} from '@/lib/platform-context';
import {useRealm} from '@/lib/realm-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {AddAccountDialog} from './AddAccountDialog';

export function AccountIntake() {
  const t = useT();
  const {label: platform} = usePlatform();
  const {label: realm} = useRealm();
  const [open, setOpen] = useState(false);
  const [completed, setCompleted] = useState(0);
  return <section className="relative mx-auto flex w-full max-w-3xl flex-1 flex-col justify-center py-10 sm:py-16" data-slot="account-intake">
    <div className="rounded-[32px] border border-border/70 bg-background/80 p-7 shadow-[0_24px_80px_-40px_#628cff45] sm:p-12">
      <span className="inline-flex items-center gap-2 rounded-full bg-primary/5 px-3 py-1 text-[11px] text-muted-foreground">
        <span className="size-1.5 rounded-full bg-primary" />{t('intake.eyebrow')}
      </span>
      <h1 className="mt-6 text-2xl font-semibold tracking-tight sm:text-3xl">{t('intake.title')}</h1>
      <p className="mt-3 max-w-md text-sm leading-6 text-muted-foreground">{t('intake.description')}</p>
      <div className="my-8 flex items-center gap-3 rounded-2xl border border-border/60 bg-muted/30 px-4 py-4 text-xs">
        <span className="font-medium">{platform}</span><ArrowUpRight aria-hidden="true" className="size-3.5 text-primary" />
        <span className="text-muted-foreground">{realm}</span>
      </div>
      <Button className="h-11 rounded-full px-6" onClick={() => setOpen(true)}><Plus className="size-4" />{t('accounts.addAccount')}</Button>
      {completed > 0 && <p role="status" className="mt-5 flex items-center gap-2 text-xs text-muted-foreground"><Check aria-hidden="true" className="size-3.5 text-emerald-600" />{t('intake.completed', {count: completed})}</p>}
      <p className="mt-8 border-t border-border/60 pt-4 text-[11px] leading-5 text-muted-foreground">{t('intake.permission')}</p>
    </div>
    <AddAccountDialog open={open} onOpenChange={setOpen} onSuccess={() => setCompleted(value => value + 1)} />
  </section>;
}
