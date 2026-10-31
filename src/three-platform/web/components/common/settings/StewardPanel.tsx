'use client';

import {useEffect, useRef, useState, type FormEvent} from 'react';
import {ArrowDown, ArrowUpRight, Check, RefreshCw, Send, ShieldCheck, Sparkles} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {errText} from '@/lib/api';
import {evidenceAccess} from '@/lib/operational-evidence';
import {stewardApi, type ConversationMessage} from '@/lib/steward';
import {conversationHistory} from '@/lib/steward-ui';
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
  const transcript = useRef<HTMLDivElement | null>(null);
  const followLatest = useRef(true);
  const [atLatest, setAtLatest] = useState(true);
  const denied = access === 'forbidden' || access === 'unavailable';
  const visibleMessages = isAdmin && !denied ? messages : [];
  const canAsk = isAdmin && !access && status?.can_chat && status.capabilities.model_configured && status.available && !status.stale;
  useHeartbeat(data.refresh, 30000);
  useEffect(() => () => pending.current?.abort(), []);
  useEffect(() => {
    if (followLatest.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [messages, busy]);
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
    if (!canAsk || busy || pending.current || !question.trim()) return;
    const controller = new AbortController();
    pending.current = controller;
    const prompt = question.trim();
    const before = conversationHistory(messages);
    followLatest.current = true;
    setAtLatest(true);
    setMessages([...messages, {role: 'user' as const, content: prompt}].slice(-16));
    setQuestion('');
    setBusy(true);
    setError('');
    try {
      const response = await stewardApi.chat(prompt, before, controller.signal);
      if (!controller.signal.aborted) {
        setMessages(previous => [...previous, {role: 'assistant' as const, content: response.answer,
          evidence: {model: response.model, tools: response.tools, observed_at: response.observed_at}}].slice(-16));
      }
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(errText(failure));
        setQuestion(prompt);
        setMessages(messages);
      }
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

    <details className="group rounded-[24px] border border-border bg-background p-5 sm:p-6" open>
      <summary className="cursor-pointer text-sm font-medium">{t('steward.abilitiesTitle')}</summary>
      <p className="mt-2 text-xs leading-6 text-muted-foreground">{t('steward.abilitiesIntro')}</p>
      <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {(status?.allowed_tools ?? []).map(name => <div key={name} className="rounded-2xl border border-border/70 bg-muted/30 px-4 py-3 text-xs">
          <p className="font-medium">{t(`steward.tools.${name}`)}</p>
        </div>)}
      </div>
      <p className="mt-4 text-[11px] leading-6 text-muted-foreground">{t('steward.abilitiesBoundary')}</p>
    </details>

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

    <div className="min-w-0 rounded-[24px] border border-border bg-background p-5 sm:p-6">
      <div className="mb-5 flex items-center gap-2 text-sm font-medium"><Sparkles size={16} className="text-[#6e8bbd]" />{t('steward.chatTitle')}</div>
      <div ref={transcript} role="log" tabIndex={0} aria-label={t('steward.conversation')} aria-live="polite"
        aria-busy={busy} onScroll={() => {
          const box = transcript.current;
          const near = !!box && box.scrollHeight - box.scrollTop - box.clientHeight < 60;
          followLatest.current = near;
          setAtLatest(near);
        }} className="h-[clamp(240px,42dvh,440px)] overflow-y-auto overscroll-contain rounded-2xl border border-border/60 bg-muted/10 p-3 [overflow-wrap:anywhere] [scrollbar-gutter:stable] sm:p-4">
      {visibleMessages.length ? <div className="space-y-3">
        {visibleMessages.map((message, index) => <div key={index} className={`max-w-full rounded-2xl p-4 text-sm leading-7 ${message.role === 'user' ? 'ml-4 border border-border bg-muted sm:ml-16' : 'mr-4 bg-[#f7fafb] text-foreground sm:mr-12'}`}>
          <p className="mb-2 text-[10px] text-muted-foreground">{message.role === 'assistant' ? t('steward.title') : t('steward.you')}</p>
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
          {message.role === 'assistant' && message.evidence ? <div className="mt-3 border-t border-border/60 pt-3 text-[10px] leading-6 text-muted-foreground">
            <p>{message.evidence.model} · {t('steward.evidenceTime', {time: new Date(message.evidence.observed_at * 1000).toLocaleTimeString()})}</p>
            {message.evidence.tools.length ? <p>{message.evidence.tools.map(name => t(`steward.tools.${name}`)).join(' · ')}</p> : null}
          </div> : null}
        </div>)}
      </div> : <div className="flex min-h-full flex-col justify-center gap-3">
        <p className="text-center text-sm font-medium">{t('steward.chatWelcome')}</p>
        <p className="text-center text-xs text-muted-foreground">{t('steward.chatWelcomeHint')}</p>
        <div className="grid gap-2 sm:grid-cols-3">
        {(['questionHealth', 'questionBudget', 'questionSpeed'] as const).map(key => <button key={key} type="button" disabled={!canAsk} onClick={() => setQuestion(t(`steward.${key}`))} className="flex items-center justify-between rounded-2xl border border-border p-4 text-left text-xs text-muted-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60">
          {t(`steward.${key}`)}<ArrowUpRight size={13} className="ml-2 shrink-0" />
        </button>)}
        </div>
      </div>}
      {busy ? <p role="status" className="mt-4 flex items-center gap-2 text-xs text-muted-foreground"><Sparkles size={13} className="animate-pulse motion-reduce:animate-none" />{t('steward.thinking')}</p> : null}
      </div>
      <div className="my-3 flex min-h-6 justify-end">{!atLatest ? <Button type="button" size="sm" variant="ghost" onClick={() => {
        followLatest.current = true; setAtLatest(true);
        if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
      }}><ArrowDown size={13} />{t('steward.latest')}</Button> : null}</div>
      {!isAdmin ? <p className="mb-4 text-xs text-muted-foreground">{t('steward.adminOnly')}</p> : !status?.capabilities.model_configured ? <p className="mb-4 text-xs text-muted-foreground">{t('steward.notConfigured')}</p> : null}
      <form onSubmit={ask} className="space-y-3">
        <label htmlFor="steward-question" className="sr-only">{t('steward.questionLabel')}</label>
        <Textarea id="steward-question" value={isAdmin && !denied ? question : ''} onChange={event => setQuestion(event.target.value)} placeholder={t('steward.placeholder')} maxLength={2000} disabled={!canAsk || busy} rows={3} className="h-24 max-h-40 min-h-24 resize-none overflow-y-auto rounded-2xl bg-background" />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-[10px] text-muted-foreground">{t('steward.ephemeral')}</p>
          <div className="flex gap-2"><Button type="button" variant="ghost" size="sm" disabled={busy || !messages.length} onClick={() => {setMessages([]); setError(''); followLatest.current = true; setAtLatest(true);}}>{t('steward.clear')}</Button>
            <Button type="submit" size="sm" className="rounded-full" disabled={!canAsk || busy || !question.trim()}><Send size={13} />{busy ? t('steward.thinking') : t('steward.send')}</Button></div>
        </div>
      </form>
      {error && isAdmin && !denied ? <div role="alert" className="mt-3 rounded-2xl border border-amber-200/60 bg-amber-50/30 p-3 text-xs leading-6"><p>{error}</p><p className="text-muted-foreground">{t('steward.chatFailureHint')}</p></div> : null}
    </div>
  </section>;
}
