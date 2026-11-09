'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {Archive, ExternalLink, RefreshCw, ShieldCheck} from 'lucide-react';
import {errText, systemApi} from '@/lib/api';
import type {GithubMirror, UpdateCheck, UpdateStatus, Versions} from '@/lib/types';
import {PRODUCT_NAME, PRODUCT_REPOSITORY, PRODUCT_REPOSITORY_URL} from '@/lib/product';
import {notify} from '@/lib/toast';
import {fmtDateTime} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {useT} from '@/lib/i18n/provider';

const RELEASES_URL = `${PRODUCT_REPOSITORY_URL}/releases`;
const PHASES = new Set(['candidate_verified', 'backup_verified', 'drained', 'activated',
  'ready_verified', 'rolled_back', 'failed']);

function ReleaseAccessSettings() {
  const t = useT();
  const [token, setToken] = useState<{configured: boolean; masked: string} | null>(null);
  const [tokenInput, setTokenInput] = useState('');
  const [mirror, setMirror] = useState<GithubMirror | null>(null);
  const [mirrorInput, setMirrorInput] = useState('');
  const [busy, setBusy] = useState(false);
  const active = useRef(true);

  useEffect(() => {
    active.current = true;
    void systemApi.githubToken().then(value => {
      if (active.current) setToken(value);
    }).catch(() => {});
    void systemApi.githubMirror().then(value => {
      if (active.current) {
        setMirror(value);
        setMirrorInput(value.stored);
      }
    }).catch(() => {});
    return () => { active.current = false; };
  }, []);

  async function save(kind: 'token' | 'mirror', value: string) {
    setBusy(true);
    try {
      if (kind === 'token') {
        await systemApi.saveGithubToken(value);
        const fresh = await systemApi.githubToken();
        if (active.current) {
          setToken(fresh);
          setTokenInput('');
        }
      } else {
        const fresh = await systemApi.saveGithubMirror(value);
        if (active.current) {
          setMirror(fresh);
          setMirrorInput(fresh.stored);
        }
      }
      if (active.current) notify.ok(t('releaseCenter.saved'));
    } catch (error) {
      if (active.current) notify.err(errText(error));
    } finally {
      if (active.current) setBusy(false);
    }
  }

  return (
    <details className="rounded-[20px] border p-4">
      <summary className="cursor-pointer text-sm font-medium">{t('releaseCenter.accessTitle')}</summary>
      <p className="mt-3 text-xs leading-5 text-muted-foreground">{t('releaseCenter.accessHint')}</p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="zhihui-release-token">{t('releaseCenter.tokenLabel')}</Label>
          <p className="text-xs text-muted-foreground">
            {token === null ? t('releaseCenter.unknown') : token.configured ? token.masked : t('releaseCenter.notConfigured')}
          </p>
          <Input id="zhihui-release-token" type="password" autoComplete="off" value={tokenInput}
            onChange={event => setTokenInput(event.target.value)} disabled={busy || token === null} />
          <div className="flex gap-2">
            <Button size="sm" onClick={() => void save('token', tokenInput)} disabled={busy || !tokenInput || token === null}>{t('common.save')}</Button>
            <Button size="sm" variant="outline" onClick={() => void save('token', '')} disabled={busy || !token?.configured}>{t('releaseCenter.clearToken')}</Button>
          </div>
        </div>
        <div className="space-y-2">
          <Label htmlFor="zhihui-release-mirror">{t('releaseCenter.mirrorLabel')}</Label>
          <p className="text-xs text-muted-foreground">{mirror === null ? t('releaseCenter.unknown') : mirror.mirror || t('releaseCenter.directConnection')}</p>
          <Input id="zhihui-release-mirror" value={mirrorInput} onChange={event => setMirrorInput(event.target.value)}
            disabled={busy || mirror === null} placeholder="https://mirror.example/" />
          <Button size="sm" onClick={() => void save('mirror', mirrorInput)} disabled={busy || mirror === null}>{t('common.save')}</Button>
        </div>
      </div>
    </details>
  );
}

export function UpdatePanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [snapshot, setSnapshot] = useState<{versions: Versions | null; status: UpdateStatus | null}>({versions: null, status: null});
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [check, setCheck] = useState<UpdateCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const generation = useRef(0);
  const checkGeneration = useRef(0);
  const active = useRef(true);

  const load = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    const responses = await Promise.allSettled([systemApi.versions(), systemApi.updateStatus()]);
    if (!active.current || request !== generation.current) return;
    const [versions, status] = responses;
    setSnapshot({versions: versions.status === 'fulfilled' ? versions.value : null,
      status: status.status === 'fulfilled' ? status.value : null});
    setError(responses.some(response => response.status === 'rejected') ? t('releaseCenter.loadFailed') : '');
    setLoading(false);
  }, [t]);

  const checkLatest = useCallback(async (force = false) => {
    const request = ++checkGeneration.current;
    setChecking(true);
    try {
      const value = await systemApi.checkUpdate(force);
      if (active.current && request === checkGeneration.current) setCheck(value);
    } catch {
      if (active.current && request === checkGeneration.current) setCheck(null);
    } finally {
      if (active.current && request === checkGeneration.current) setChecking(false);
    }
  }, []);

  useEffect(() => {
    active.current = true;
    void load();
    void checkLatest();
    return () => {
      active.current = false;
      generation.current++;
      checkGeneration.current++;
    };
  }, [load, checkLatest]);

  const {versions, status} = snapshot;
  const identity = versions?.product ?? status?.product;
  const identityValid = identity?.repository === PRODUCT_REPOSITORY;
  const reported = identityValid && status?.record_state === 'reported'
    && status.product?.repository === PRODUCT_REPOSITORY && status.product.version === identity.version
    && (!versions?.release_id || versions.release_id === status.release_id);
  const events = reported && isAdmin ? (status?.logs ?? []).filter(entry => PHASES.has(entry.text)) : [];
  const latest = check?.manager;
  const checkValid = latest?.repo === PRODUCT_REPOSITORY && !latest.error && !latest.stale;

  async function archiveHistory() {
    setArchiving(true);
    try {
      await systemApi.clearUpdateStatus();
      if (active.current) {
        notify.ok(t('releaseCenter.historyArchived'));
        await load();
      }
    } catch (failure) {
      if (active.current) notify.err(errText(failure));
    } finally {
      if (active.current) setArchiving(false);
    }
  }

  return (
    <div className="space-y-4" data-slot="zhihui-release-center">
      {error && <LoadError message={error} onRetry={load} />}
      <section className="rounded-[20px] border bg-muted/40 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2"><ShieldCheck className="h-5 w-5 text-primary" /><h2 className="font-semibold">{t('releaseCenter.title')}</h2></div>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">{t('releaseCenter.description')}</p>
          </div>
          <Button size="sm" variant="outline" disabled={loading} onClick={() => void load()}><RefreshCw className={loading ? 'animate-spin' : ''} />{t('common.refresh')}</Button>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <div className="rounded-2xl bg-background p-3"><p className="text-xs text-muted-foreground">{t('releaseCenter.product')}</p><p className="mt-1 font-medium">{PRODUCT_NAME}</p></div>
          <div className="rounded-2xl bg-background p-3"><p className="text-xs text-muted-foreground">{t('releaseCenter.currentVersion')}</p><p className="mt-1 font-medium">{identityValid ? identity.version : '—'}</p></div>
          <div className="rounded-2xl bg-background p-3"><p className="text-xs text-muted-foreground">{t('releaseCenter.policy')}</p><Badge variant="secondary" className="mt-1">{t('releaseCenter.publisherOnly')}</Badge></div>
        </div>
        <p className="mt-3 break-all font-mono text-xs text-muted-foreground">{identityValid ? versions?.release_id || status?.release_id || '—' : t('releaseCenter.identityUnknown')}</p>
        <div className="mt-3 flex flex-wrap gap-4 text-xs">
          <a href={PRODUCT_REPOSITORY_URL} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">{PRODUCT_REPOSITORY}<ExternalLink size={12} /></a>
          <a href={RELEASES_URL} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">{t('releaseCenter.releases')}</a>
        </div>
      </section>

      <section className="rounded-[20px] border p-5">
        <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="text-sm font-medium">{t('releaseCenter.releaseCheck')}</h3><Button size="sm" variant="outline" disabled={checking} onClick={() => void checkLatest(isAdmin)}><RefreshCw className={checking ? 'animate-spin' : ''} />{t('releaseCenter.check')}</Button></div>
        <p role="status" className="mt-3 text-sm">{checking ? t('releaseCenter.checking') : !checkValid ? t('releaseCenter.checkUnknown') : latest?.has_update ? t('releaseCenter.available', {version: latest.latest}) : t('releaseCenter.noNewRelease')}</p>
        {checkValid && latest?.has_update && <p className="mt-2 text-xs text-muted-foreground">{latest.assets_ready === false ? t('releaseCenter.assetsPending') : t('releaseCenter.notVerified')}</p>}
        {checkValid && latest?.release_notes && <details className="mt-3"><summary className="cursor-pointer text-xs text-primary">{t('releaseCenter.releaseNotes')}</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-muted p-3 font-sans text-xs leading-5">{latest.release_notes.slice(0, 8000)}</pre></details>}
      </section>

      <section className="rounded-[20px] border p-5" data-slot="trusted-release-policy">
        <h3 className="text-sm font-medium">{t('releaseCenter.processTitle')}</h3>
        <ol className="mt-3 list-inside list-decimal space-y-2 text-xs leading-5 text-muted-foreground">
          <li>{t('releaseCenter.processSource')}</li><li>{t('releaseCenter.processVerify')}</li>
          <li>{t('releaseCenter.processSwitch')}</li><li>{t('releaseCenter.processRollback')}</li>
        </ol>
        <p className="mt-4 rounded-xl bg-muted p-3 text-xs leading-5">{t('releaseCenter.noWebInstaller')}</p>
      </section>

      <section className="rounded-[20px] border p-5" data-slot="publisher-receipt">
        <h3 className="text-sm font-medium">{t('releaseCenter.receiptTitle')}</h3>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{reported ? t('releaseCenter.receiptReported') : t('releaseCenter.receiptUnknown')}</p>
        {reported && <p className="mt-2 text-xs">{status?.step && PHASES.has(status.step) ? t(`releaseCenter.phase.${status.step}`) : t('releaseCenter.unknown')}</p>}
        {events.length > 0 && <ul className="mt-3 space-y-2 text-xs">{events.map((entry, index) => <li key={`${entry.ts}-${index}`} className="flex flex-wrap gap-3"><span className="text-muted-foreground">{entry.ts ? fmtDateTime(entry.ts) : '—'}</span><span>{t(`releaseCenter.phase.${entry.text}`)}</span></li>)}</ul>}
      </section>

      {isAdmin && status?.legacy_history_available && <section className="rounded-[20px] border p-5" data-slot="isolated-legacy-history">
        <h3 className="text-sm font-medium">{t('releaseCenter.historyTitle')}</h3>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{t('releaseCenter.historyDescription')}</p>
        {status.legacy_history_blocked && <p className="mt-2 text-xs text-amber-600">{t('releaseCenter.historyBlocked')}</p>}
        <ConfirmDialog title={t('releaseCenter.archiveHistory')} description={t('releaseCenter.archiveDescription')}
          confirmText={t('releaseCenter.archiveHistory')} onConfirm={archiveHistory}
          trigger={<Button size="sm" variant="outline" className="mt-3" disabled={archiving || status.legacy_history_blocked || status.running}><Archive size={14} />{t('releaseCenter.archiveHistory')}</Button>} />
      </section>}
      {isAdmin && <ReleaseAccessSettings />}
    </div>
  );
}
