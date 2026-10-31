'use client';

import {useEffect, useRef, useState, type FormEvent} from 'react';
import {ArrowUpRight, Check, RefreshCw, Send, ShieldCheck, Sparkles} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {errText} from '@/lib/api';
import {evidenceAccess} from '@/lib/operational-evidence';
import {stewardApi, type ConversationMessage} from '@/lib/steward';
import {Button} from '@/components/ui/button';
import {Textarea} from '@/components/ui/textarea';

export function StewardPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const data = useAsyncAll({status: stewardApi.status}, []);
  const access = evidenceAccess(data.errors.status);
  const status = access === 'forbidden' || access === 'unavailable' ? undefined : data.values.status;
  const current = status?.snapshot;
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);
  const denied = access === 'forbidden' || access === 'unavailable';
  const visibleMessages = isAdmin && !denied ? messages : [];
  const canAsk = isAdmin && !access && status?.can_chat && status.capabilities.model_configured && status.available && !status.stale;
  useHeartbeat(data.refresh, 30000);
  useEffect(() => () => pending.current?.abort(), []);
  useEffect(() => {
    if (isAdmin && !denied) return;
    pending.current?.abort();
    setMessages([]);
    setQuestion('');
    setError('');
    setBusy(false);
  }, [isAdmin, denied]);

  async function ask(event: FormEvent) {
    event.preventDefault();
    if (!canAsk || busy || !question.trim()) return;
    const controller = new AbortController();
    pending.current = controller;
    const prompt = question.trim();
    const before = messages.slice(-8);
    setMessages([...messages, {role: 'user' as const, content: prompt}].slice(-16));
    setQuestion('');
    setBusy(true);
    setError('');
    try {
      const response = await stewardApi.chat(prompt, before, controller.signal);
      if (!controller.signal.aborted) {
        setMessages(previous => [...previous, {role: 'assistant' as const, content: response.answer}].slice(-16));
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(errText(failure));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      if (pending.current === controller) pending.current = null;
    }
  }

  return <section className="space-y-4" aria-label={t('steward.title')}>
    <div className="relative overflow-hidden rounded-[24px] border border-border bg-background p-5 sm:p-7">
      <div aria-hidden="true" className="pointer-events-none absolute -right-10 -top-14 size-64 rounded-full bg-[radial-gradient(circle,rgba(128,160,214,.12),transparent_70%)]" />
      <div className="relative flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <div aria-hidden="true" className="flex size-12 items-center justify-center rounded-2xl border border-border bg-muted text-2xl font-bold text-[#7b96d1]">Z</div>
          <div><p className="text-[10px] tracking-[.2em] text-muted-foreground">ZHIHUI / AT YOUR SERVICE</p>
            <h2 className="mt-1 text-xl font-semibold">{t('steward.title')}</h2></div>
        </div>
        <Button variant="outline" size="sm" className="rounded-full" disabled={data.isRefreshing} onClick={() => void data.reload()}>
          <RefreshCw className={data.isRefreshing ? 'animate-spin' : ''} />{t('common.refresh')}
        </Button>
      </div>
      <p className="relative mt-5 max-w-xl text-sm leading-7 text-muted-foreground">{t('steward.intro')}</p>
      <div className="mt-4 flex flex-wrap gap-3 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5"><ShieldCheck size={13} />{t('steward.readOnly')}</span>
        <span className="inline-flex items-center gap-1.5"><Check size={13} />{t('steward.metered')}</span>
        <span>{t('steward.noSecrets')}</span>
      </div>
    </div>

    {'status' in data.errors ? <p role="alert" className="rounded-2xl border border-amber-200/60 bg-amber-50/30 p-4 text-xs">{t('steward.loadFailed')}</p> : null}
    {status?.stale ? <p role="status" className="text-xs text-amber-700">{t('steward.stale')}</p> : null}
    <div className="grid gap-3 sm:grid-cols-3">
      {(['workbuddy', 'qoder', 'trae'] as const).map(name => {
        const item = current?.platforms.find(row => row.name === name);
        const state = item?.state ?? 'unknown';
        return <div key={name} className="rounded-[20px] border border-border bg-background p-4">
          <div className="flex items-center justify-between text-xs"><strong>{name === 'workbuddy' ? 'WorkBuddy' : name === 'qoder' ? 'Qoder' : 'Trae'}</strong>
            <span className={state === 'up' ? 'text-emerald-600' : state === 'down' ? 'text-orange-600' : 'text-muted-foreground'}>{t(`steward.state.${state}`)}</span></div>
          <p className="mt-4 text-2xl tabular-nums">{item?.pool?.total ?? '—'}<span className="ml-2 text-[11px] text-muted-foreground">{t('steward.accounts')}</span></p>
          <p className="mt-2 text-[10px] text-muted-foreground">{t('steward.probeBoundary')}</p>
        </div>;
      })}
    </div>

    <div className="rounded-[24px] border border-border bg-background p-5 sm:p-6">
      <div className="mb-5 flex items-center gap-2 text-sm font-medium"><Sparkles size={16} className="text-[#6e8bbd]" />{t('steward.chatTitle')}</div>
      {visibleMessages.length ? <div role="log" aria-label={t('steward.conversation')} aria-live="polite" className="mb-5 space-y-3">
        {visibleMessages.map((message, index) => <div key={index} className={`max-w-full rounded-2xl p-4 text-sm leading-7 ${message.role === 'user' ? 'ml-4 border border-border bg-muted sm:ml-16' : 'mr-4 bg-[#f7fafb] text-foreground sm:mr-12'}`}>
          <p className="mb-2 text-[10px] text-muted-foreground">{message.role === 'assistant' ? t('steward.title') : t('steward.you')}</p>
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        </div>)}
      </div> : <div className="mb-5 grid gap-2 sm:grid-cols-3">
        {(['questionHealth', 'questionBudget', 'questionSpeed'] as const).map(key => <button key={key} type="button" disabled={!canAsk} onClick={() => setQuestion(t(`steward.${key}`))} className="flex items-center justify-between rounded-2xl border border-border p-4 text-left text-xs text-muted-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60">
          {t(`steward.${key}`)}<ArrowUpRight size={13} className="ml-2 shrink-0" />
        </button>)}
      </div>}
      {!isAdmin ? <p className="mb-4 text-xs text-muted-foreground">{t('steward.adminOnly')}</p> : !status?.capabilities.model_configured ? <p className="mb-4 text-xs text-muted-foreground">{t('steward.notConfigured')}</p> : null}
      <form onSubmit={ask} className="space-y-3">
        <label htmlFor="steward-question" className="sr-only">{t('steward.questionLabel')}</label>
        <Textarea id="steward-question" value={question} onChange={event => setQuestion(event.target.value)} placeholder={t('steward.placeholder')} maxLength={2000} disabled={!canAsk || busy} className="min-h-24 resize-y rounded-2xl bg-background" />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-[10px] text-muted-foreground">{t('steward.ephemeral')}</p>
          <div className="flex gap-2"><Button type="button" variant="ghost" size="sm" disabled={busy || !messages.length} onClick={() => setMessages([])}>{t('steward.clear')}</Button>
            <Button type="submit" size="sm" className="rounded-full" disabled={!canAsk || busy || !question.trim()}><Send size={13} />{busy ? t('steward.thinking') : t('steward.send')}</Button></div>
        </div>
      </form>
      {error ? <p role="alert" className="mt-3 text-xs text-red-600">{error}</p> : null}
    </div>
  </section>;
}
