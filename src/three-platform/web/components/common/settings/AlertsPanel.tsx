'use client';

import {useEffect, useRef, useState, type FormEvent} from 'react';
import {Bell, Mail, Plus, RefreshCw, ShieldCheck, X} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {errText} from '@/lib/api';
import {evidenceAccess} from '@/lib/operational-evidence';
import {stewardApi, type StewardPolicy, type StewardSettings} from '@/lib/steward';
import {normalizeRecipient, recipientProblem} from '@/lib/steward-ui';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';

function AlertSettings({settings, reload, unavailable}: {settings: StewardSettings; reload: () => Promise<unknown>; unavailable: boolean}) {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [policy, setPolicy] = useState(settings.policy);
  const [recipients, setRecipients] = useState(settings.policy.recipients);
  const [adding, setAdding] = useState(false);
  const [address, setAddress] = useState('');
  const [addressError, setAddressError] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (isAdmin && settings.can_edit) return;
    setRecipients([]);
    setAdding(false); setAddress(''); setAddressError('');
    setNotice('');
    setError('');
  }, [isAdmin, settings.can_edit]);
  const editable = isAdmin && settings.can_edit && !unavailable;

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!editable || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await stewardApi.save({...policy, recipients});
      if (mounted.current) {
        setPolicy(result.policy); setRecipients(result.policy.recipients);
        setNotice(t('steward.saved'));
        try {
          if (await reload() === false && mounted.current) setNotice(t('steward.savedRefreshFailed'));
        } catch {
          if (mounted.current) setNotice(t('steward.savedRefreshFailed'));
        }
      }
    } catch (failure) {
      if (mounted.current) setError(errText(failure));
    } finally { if (mounted.current) setBusy(false); }
  }

  function addRecipient(event: FormEvent) {
    event.preventDefault();
    if (!editable || busy) return;
    const problem = recipientProblem(address, recipients);
    if (problem) { setAddressError(t(`steward.recipientError.${problem}`)); return; }
    setRecipients(previous => [...previous, normalizeRecipient(address)]);
    setAdding(false); setAddress(''); setAddressError('');
  }
  const dirty = JSON.stringify({...policy, recipients}) !== JSON.stringify(settings.policy);

  async function test() {
    if (!editable || busy || dirty) return;
    setBusy(true); setNotice(''); setError('');
    try {
      const result = await stewardApi.test();
      if (mounted.current) setNotice(t('steward.testQueued', {n: result.queued.length}));
    } catch (failure) { if (mounted.current) setError(errText(failure)); }
    finally { if (mounted.current) setBusy(false); }
  }

  const fields: {key: keyof StewardPolicy; label: string; min: number; max: number}[] = [
    {key: 'interval_seconds', label: 'interval', min: 30, max: 300},
    {key: 'consecutive_failures', label: 'consecutive', min: 1, max: 10},
    {key: 'disk_warning_percent', label: 'diskWarning', min: 60, max: 95},
    {key: 'disk_critical_percent', label: 'diskCritical', min: 70, max: 99},
    {key: 'memory_warning_percent', label: 'memoryWarning', min: 60, max: 99},
    {key: 'failure_warning_percent', label: 'failureWarning', min: 1, max: 50},
    {key: 'min_requests', label: 'minRequests', min: 5, max: 1000},
    {key: 'latency_warning_ms', label: 'latencyWarning', min: 1000, max: 300000},
    {key: 'budget_unknown_warning', label: 'unknownWarning', min: 1, max: 10000},
  ];
  return <><form onSubmit={save} className="space-y-5 rounded-[24px] border border-border bg-background p-5 sm:p-6">
    <div className="flex items-center justify-between gap-4"><div><h3 className="text-sm font-medium">{t('steward.alertSettings')}</h3><p className="mt-1 text-xs text-muted-foreground">{t('steward.dedup')}</p></div>
      <Switch aria-label={t('steward.enableAlerts')} checked={policy.enabled} disabled={!editable || busy} onCheckedChange={enabled => setPolicy(previous => ({...previous, enabled}))} /></div>
    <div className="grid gap-4 md:grid-cols-2">
      <div className="space-y-3" aria-labelledby="steward-recipients"><div className="flex items-center justify-between gap-2"><Label id="steward-recipients">{t('steward.recipients')}</Label>
        {editable ? <Button type="button" size="sm" variant="outline" className="rounded-full" disabled={busy || recipients.length >= 10} onClick={() => {setAdding(true); setAddress(''); setAddressError('');}}><Plus size={13} />{t('steward.addRecipient')}</Button> : null}</div>
        {isAdmin && settings.can_edit ? <ul className="space-y-2">
          {recipients.map(email => <li key={email} className="flex items-center gap-2 rounded-2xl border border-border bg-muted/20 px-3 py-2 text-xs"><Mail size={14} className="shrink-0 text-muted-foreground" /><span className="min-w-0 flex-1 break-all">{email}</span>
            <Button type="button" size="icon" variant="ghost" className="size-8 shrink-0 rounded-full" aria-label={t('steward.removeRecipient', {email})} disabled={!editable || busy} onClick={() => setRecipients(previous => previous.filter(item => item !== email))}><X size={13} /></Button></li>)}
          {!recipients.length ? <li className="rounded-2xl border border-dashed border-border p-5 text-center text-xs text-muted-foreground">{t('steward.noRecipients')}</li> : null}
        </ul> : null}
        <p className="text-[10px] text-muted-foreground">{editable ? t('steward.recipientsHint') : t('steward.recipientCount', {n: settings.recipient_count})}</p></div>
      <div className="space-y-2"><Label htmlFor="steward-model">{t('steward.model')}</Label>
        <Input id="steward-model" value={policy.model} onChange={event => setPolicy(previous => ({...previous, model: event.target.value}))} disabled={!editable || busy} maxLength={96} />
        <p className="text-[10px] leading-6 text-muted-foreground">{t('steward.modelHint')}</p>
        <p className="text-[10px] text-muted-foreground">SMTP · {t(settings.capabilities.smtp_configured ? 'steward.configured' : 'steward.unconfigured')}</p></div>
    </div>
    <div className="flex flex-wrap gap-6 text-xs">
      {(['send_recovery', 'ai_analysis'] as const).map(key => <div key={key} className="flex items-center gap-2"><Switch id={`steward-${key}`} aria-label={t(key === 'send_recovery' ? 'steward.recoveryMail' : 'steward.aiAnalysis')} checked={policy[key]} disabled={!editable || busy} onCheckedChange={value => setPolicy(previous => ({...previous, [key]: value}))} /><Label htmlFor={`steward-${key}`} className="text-xs">{t(key === 'send_recovery' ? 'steward.recoveryMail' : 'steward.aiAnalysis')}</Label></div>)}
    </div>
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
      {fields.map(field => <div key={field.key} className="space-y-2"><Label htmlFor={`alert-${field.key}`} className="text-[11px]">{t(`steward.${field.label}`)}</Label>
        <Input id={`alert-${field.key}`} type="number" min={field.min} max={field.max} step={1} required value={String(policy[field.key])} disabled={!editable || busy} onChange={event => setPolicy(previous => ({...previous, [field.key]: event.target.value === '' ? '' : Number(event.target.value)}))} /></div>)}
    </div>
    <p className="text-[11px] leading-6 text-muted-foreground">{t('steward.thresholdHint')}</p>
    {editable ? <div className="flex flex-wrap gap-2"><Button type="submit" size="sm" className="rounded-full" disabled={busy}>{t('steward.save')}</Button>
      <Button type="button" variant="outline" size="sm" className="rounded-full" disabled={busy || dirty || !settings.capabilities.smtp_configured || !settings.recipient_count} onClick={() => void test()}><Mail />{t('steward.testMail')}</Button>
      {dirty ? <p role="status" className="w-full text-[11px] text-muted-foreground">{t('steward.unsaved')}</p> : null}</div> : <p className="text-xs text-muted-foreground">{t('steward.settingsReadOnly')}</p>}
    {notice && isAdmin && settings.can_edit ? <p role="status" className="text-xs text-emerald-700">{notice}</p> : null}
    {error && isAdmin && settings.can_edit ? <p role="alert" className="text-xs text-red-600">{error}</p> : null}
  </form>
  <Dialog open={adding && editable} onOpenChange={open => {setAdding(open); if (!open) {setAddress(''); setAddressError('');}}}>
    <DialogContent className="max-w-md">
      <DialogHeader><DialogTitle>{t('steward.addRecipient')}</DialogTitle><DialogDescription>{t('steward.addRecipientHint')}</DialogDescription></DialogHeader>
      <form onSubmit={addRecipient} className="space-y-4 px-6 pb-6">
        <div className="space-y-2"><Label htmlFor="steward-new-email">{t('steward.recipientAddress')}</Label>
          <Input id="steward-new-email" type="email" inputMode="email" autoComplete="email" autoFocus required maxLength={254} value={editable ? address : ''} disabled={busy || !editable}
            onChange={event => {setAddress(event.target.value); setAddressError('');}} placeholder="name@example.com" aria-invalid={!!addressError} aria-describedby={addressError ? 'steward-email-error' : undefined} />
          {addressError ? <p id="steward-email-error" role="alert" className="text-xs text-red-600">{addressError}</p> : null}</div>
        <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={() => setAdding(false)}>{t('common.cancel')}</Button><Button type="submit" disabled={busy || !editable || !address.trim()}>{t('steward.confirmRecipient')}</Button></div>
      </form>
    </DialogContent>
  </Dialog></>;
}

export function AlertsPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const data = useAsyncAll({settings: stewardApi.settings, alerts: stewardApi.alerts, status: stewardApi.status}, []);
  const alertAccess = evidenceAccess(data.errors.alerts);
  const settingsAccess = evidenceAccess(data.errors.settings);
  const rows = alertAccess === 'forbidden' || alertAccess === 'unavailable' ? undefined : data.values.alerts;
  const selected = settingsAccess === 'forbidden' || settingsAccess === 'unavailable' ? undefined : data.values.settings;
  const [eventFilter, setEventFilter] = useState<'open' | 'resolved' | 'all'>('open');
  const visibleIncidents = rows?.incidents.filter(row => eventFilter === 'all' || row.state === eventFilter) ?? [];
  const stateAccess = evidenceAccess(data.errors.status);
  const currentStatus = stateAccess === 'forbidden' || stateAccess === 'unavailable' ? undefined : data.values.status;
  useHeartbeat(data.refresh, 30000);
  const formatTime = (stamp: number) => new Date(stamp * 1000).toLocaleString();
  return <section className="space-y-4" aria-label={t('steward.alertTitle')}>
    <div className="flex flex-wrap items-center justify-between gap-4 rounded-[24px] border border-border bg-background p-5 sm:p-6">
      <div><p className="mb-2 text-[10px] tracking-[.18em] text-muted-foreground">ZHIHUI / QUIETLY WATCHFUL</p>
        <h2 className="flex items-center gap-2 text-lg font-semibold"><Bell size={18} />{t('steward.alertTitle')}</h2>
        <p className="mt-3 text-xs leading-6 text-muted-foreground">{t('steward.alertIntro')}</p></div>
      <Button size="sm" variant="outline" className="rounded-full" disabled={data.isRefreshing} onClick={() => void data.reload()}><RefreshCw />{t('common.refresh')}</Button>
    </div>
    {Object.keys(data.errors).length ? <p role="alert" className="text-xs text-amber-700">{t('steward.loadFailed')}</p> : null}
    {currentStatus?.stale ? <p role="status" className="text-xs text-amber-700">{t('steward.stale')}</p> : null}
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {[
        ['openCount', rows ? rows.incidents.filter(row => row.state === 'open').length : '—'],
        ['pendingCount', rows ? (rows.queue?.counts.pending ?? 0) + (rows.queue?.counts.sending ?? 0) : '—'],
        ['unknownCount', rows ? rows.queue?.counts.unknown ?? 0 : '—'],
        ['monitorState', t(currentStatus?.available && !currentStatus.stale && !stateAccess ? 'steward.monitorFresh' : 'steward.monitorUnknown')],
      ].map(([label, value]) => <div key={label} className="rounded-[20px] border border-border bg-background p-4"><p className="text-[11px] text-muted-foreground">{t(`steward.${label}`)}</p><p className="mt-3 text-lg font-semibold tabular-nums">{value}</p></div>)}
    </div>
    <div className="rounded-[20px] border border-border bg-muted/20 px-5 py-4"><h3 className="text-xs font-medium">{t('steward.coverageTitle')}</h3><p className="mt-2 text-xs leading-6 text-muted-foreground">{t('steward.coverageHint')}</p></div>
    {selected ? <AlertSettings key={selected.policy.revision} settings={selected} reload={data.reload} unavailable={!!settingsAccess} /> : <p className="p-4 text-xs text-muted-foreground">{t('steward.loading')}</p>}
    <div className="rounded-[24px] border border-border bg-background p-5 sm:p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><h3 className="text-sm font-medium">{t('steward.incidents')}</h3><div className="flex gap-1 rounded-full border border-border p-1">
        {(['open', 'resolved', 'all'] as const).map(value => <Button key={value} size="sm" variant={eventFilter === value ? 'secondary' : 'ghost'} className="h-7 rounded-full text-xs" aria-pressed={eventFilter === value} onClick={() => setEventFilter(value)}>{t(`steward.${value === 'all' ? 'allEvents' : value}`)}</Button>)}
      </div></div>
      {rows ? visibleIncidents.length ? <div tabIndex={0} aria-label={t('steward.incidents')} className="max-h-[420px] space-y-3 overflow-y-auto overscroll-contain pr-1">{visibleIncidents.map(row => <article key={row.id} className="rounded-2xl border border-border p-4">
        <div className="flex flex-wrap justify-between gap-2 text-xs"><strong>{row.title}</strong><span className={row.state === 'resolved' ? 'text-emerald-600' : row.severity === 'critical' ? 'text-orange-600' : 'text-muted-foreground'}>{t(row.state === 'resolved' ? 'steward.resolved' : 'steward.open')}</span></div>
        <p className="mt-2 break-words text-xs leading-6 text-muted-foreground">{row.detail}</p><p className="mt-2 text-[10px] text-muted-foreground">{formatTime(row.last_seen)}</p>
      </article>)}</div> : <p className="text-xs text-muted-foreground">{t(rows.incidents.length ? 'steward.noMatchingEvents' : 'steward.noIncidents')}</p> : <p className="text-xs text-muted-foreground">{t('steward.loading')}</p>}
    </div>
    <div className="rounded-[24px] border border-border bg-background p-5 sm:p-6">
      <h3 className="mb-2 flex items-center gap-2 text-sm font-medium"><ShieldCheck size={15} />{t('steward.deliveries')}</h3>
      <p className="mb-4 text-[11px] leading-6 text-muted-foreground">{t('steward.acceptanceBoundary')}</p>
      <div tabIndex={0} aria-label={t('steward.deliveries')} className="max-h-80 space-y-2 overflow-y-auto overscroll-contain pr-1">{rows?.deliveries.map(row => <div key={row.id} className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 py-3 text-xs"><span className="min-w-0 break-all text-muted-foreground">#{row.id} · {formatTime(row.created_at)}{isAdmin && row.recipient ? ` · ${row.recipient}` : ''}</span><span>{t(`steward.delivery.${row.status}`)}</span></div>)}</div>
      {rows?.analyses.length ? <div className="mt-5 border-t border-border pt-4"><h4 className="mb-2 text-xs font-medium">{t('steward.analysisRecords')}</h4>
        <p className="mb-3 text-[11px] leading-6 text-muted-foreground">{t('steward.analysisBoundary')}</p>
        <div tabIndex={0} aria-label={t('steward.analysisRecords')} className="max-h-64 overflow-y-auto overscroll-contain">{rows.analyses.map(row => <p key={row.id} className="flex flex-wrap justify-between gap-2 py-2 text-xs"><span className="text-muted-foreground">#{row.incident_id} · {formatTime(row.created_at)}</span><span>{t(`steward.analysis.${row.status}`)}</span></p>)}</div></div> : null}
    </div>
  </section>;
}
