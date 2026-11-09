'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {Loader2, CheckCircle2, AlertTriangle, ExternalLink} from 'lucide-react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountApi, errText, type AccountAuthResult, type AccountAuthReviewItem} from '@/lib/api';
import {usePlatform} from '@/lib/platform-context';
import {useRealm} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';
import {openAuthorizationWindow} from '@/lib/authorization-window';
import {Button} from '@/components/ui/button';
import {Textarea} from '@/components/ui/textarea';
import {CopyButton} from '@/components/ui/copy-button';
import {Select, SelectContent, SelectItem, SelectTrigger, SelectValue} from '@/components/ui/select';
import {Dialog, DialogContent, DialogTitle, DialogDescription, DialogHeader} from '@/components/animate-ui/radix/dialog';

type Phase = 'loading' | 'waiting' | 'submitting' | 'success' | 'error';
const POLL_FAIL_LIMIT = 3;
const INTERNATIONAL_REGIONS = ['HK', 'MO', 'SG', 'TH', 'PH', 'MY', 'ID'] as const;

function wakeEvents(): Array<[EventTarget, string]> {
  if (typeof window === 'undefined') return [];
  return [[document, 'visibilitychange'], [window, 'focus'], [document, 'pointerdown'], [document, 'keydown']];
}

export function AddAccountDialog({open, onOpenChange, onSuccess, upstreamId}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  onSuccess?: () => void;
  upstreamId?: number | null;
}) {
  const t = useT();
  const {platform, label: platformName} = usePlatform();
  const {realm, label: realmName} = useRealm();
  const {isAdmin: canManage} = useAuth();
  const workbuddy = platform === 'workbuddy';
  const unsupported = platform === 'trae' && realm === 'global';
  const [phase, setPhase] = useState<Phase>('loading');
  const [region, setRegion] = useState('');
  const [proxy, setProxy] = useState<string | undefined>(undefined);
  const [proxyRoutes, setProxyRoutes] = useState<string[]>([]);
  const [routesReady, setRoutesReady] = useState(false);
  const [routeError, setRouteError] = useState('');
  const [authUrl, setAuthUrl] = useState('');
  const [message, setMessage] = useState('');
  const [mode, setMode] = useState<'poll' | 'callback'>('poll');
  const [callbackUrl, setCallbackUrl] = useState('');
  const [callbackError, setCallbackError] = useState('');
  const [unconfirmed, setUnconfirmed] = useState(false);
  const [reviewItems, setReviewItems] = useState<AccountAuthReviewItem[]>([]);
  const [reviewing, setReviewing] = useState(false);
  const reviewBusy = useRef(false);
  const [retry, setRetry] = useState(0);
  const stateRef = useRef('');
  const generation = useRef(0);
  const callbacks = useRef({onSuccess, onOpenChange});
  const closeTimer = useRef<number | null>(null);
  const submitted = useRef(false);
  const presentation = useRef({t, platformName, realmName});

  useEffect(() => { callbacks.current = {onSuccess, onOpenChange}; }, [onSuccess, onOpenChange]);
  useEffect(() => { presentation.current = {t, platformName, realmName}; }, [t, platformName, realmName]);

  useEffect(() => {
    if (!open || !workbuddy) { setRoutesReady(false); return; }
    if (!canManage) { setProxy(''); setProxyRoutes([]); setRoutesReady(true); return; }
    let active = true;
    setRoutesReady(false);
    setRouteError('');
    accountApi.proxies().then(data => {
      if (!active) return;
      setProxyRoutes(data.routes);
      setProxy(data.default || '');
      setRoutesReady(true);
    }).catch(error => {
      if (!active) return;
      setRouteError(errText(error));
      setProxy('');
      setRoutesReady(true);
    });
    return () => { active = false; };
  }, [open, workbuddy, canManage]);

  const completed = useCallback((result: AccountAuthResult, epoch: number) => {
    if (generation.current !== epoch) return;
    const {t, platformName, realmName} = presentation.current;
    setPhase('success');
    setAuthUrl('');
    setCallbackUrl('');
    setMessage(t('addAccount.connected', {platform: platformName, name: result.nickname || result.uid || ''}));
    notify.ok(t('addAccount.connectedTitle', {platform: platformName}),
              t('addAccount.connectedDetail', {realm: realmName}));
    window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
    callbacks.current.onSuccess?.();
    closeTimer.current = window.setTimeout(() => {
      if (generation.current === epoch) callbacks.current.onOpenChange(false);
    }, 1600);
  }, []);

  useEffect(() => {
    const {t, platformName} = presentation.current;
    const epoch = ++generation.current;
    let timer: number | undefined;
    let tick: (() => void) | undefined;
    let polling = false;
    let failures = 0;
    const current = () => generation.current === epoch;
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
      if (tick) wakeEvents().forEach(([target, event]) => target.removeEventListener(event, tick!));
      tick = undefined;
    };
    stateRef.current = '';
    submitted.current = false;
    reviewBusy.current = false;
    setReviewing(false);
    setReviewItems([]);
    setUnconfirmed(false);
    setAuthUrl('');
    setCallbackUrl('');
    setCallbackError('');
    setPhase('loading');
    setMode('poll');
    setMessage(t('addAccount.requestingPlatform', {platform: platformName}));
    if (open && unsupported) {
      setPhase('error');
      setMessage(t('addAccount.traeGlobalUnsupported'));
    } else if (open && (!workbuddy || routesReady)) {
      void (async () => {
        try {
          if (!workbuddy) {
            const review = await accountApi.review(undefined, platform, realm);
            if (!current()) return;
            if (review.platform !== platform || review.realm !== realm) throw new Error(t('addAccount.scopeMismatch'));
            setReviewItems(review.sessions);
            if (review.sessions.some(item => item.requires_reconciliation || item.status === 'processing' ||
                item.status === 'engine_changed' || (item.status === 'waiting' && item.engine_binding !== 'current'))) {
              setPhase('error');
              setUnconfirmed(true);
              setMessage(t('addAccount.reviewBeforeNew'));
              return;
            }
          }
          const data = await accountApi.start(realm, workbuddy ? upstreamId : undefined,
            workbuddy ? region || undefined : undefined, workbuddy ? proxy : undefined, platform);
          if (!current()) return;
          if (data.platform !== platform || data.realm !== realm ||
              !data.state || !data.authUrl || !['poll', 'callback'].includes(data.mode)) {
            throw new Error(t('addAccount.scopeMismatch'));
          }
          const link = new URL(data.authUrl);
          const expectedHost = platform === 'qoder' ? (realm === 'global' ? 'qoder.com' : 'qoder.com.cn') :
            platform === 'trae' ? 'www.trae.cn' : undefined;
          if (link.protocol !== 'https:' || link.username || link.password ||
              (expectedHost && link.hostname !== expectedHost)) throw new Error(t('addAccount.scopeMismatch'));
          stateRef.current = data.state;
          setAuthUrl(data.authUrl);
          setMode(data.mode);
          setPhase('waiting');
          setMessage(t(data.mode === 'callback' ? 'addAccount.waitingCallback' : 'addAccount.waitingPlatform',
                       {platform: platformName}));
          if (data.mode === 'callback') return;
          tick = async () => {
            if (!current() || document.hidden || polling) return;
            polling = true;
            try {
              const result = await accountApi.poll(data.state, realm,
                workbuddy ? region || undefined : undefined, workbuddy ? upstreamId : undefined, platform);
              if (!current()) return;
              failures = 0;
              if ((result.platform && result.platform !== platform) || (result.realm && result.realm !== realm)) {
                stop(); setPhase('error'); setMessage(t('addAccount.scopeMismatch')); return;
              }
              if (result.status === 'success') { stop(); completed(result, epoch); }
              else if (result.status === 'unconfirmed') {
                stop(); setPhase('error'); setUnconfirmed(true); setAuthUrl('');
                setMessage(t('addAccount.resultUnconfirmed'));
              }
              else if (result.status !== 'waiting') {
                stop(); setPhase('error');
                setMessage(t(result.status === 'expired' ? 'addAccount.qrExpired' : 'addAccount.stateLost'));
              }
            } catch (error) {
              if (!current()) return;
              failures += 1;
              if (failures >= POLL_FAIL_LIMIT) {
                stop(); setPhase('error'); setMessage(errText(error));
              }
            } finally { polling = false; }
          };
          timer = window.setInterval(tick, 2000);
          wakeEvents().forEach(([target, event]) => target.addEventListener(event, tick!));
        } catch (error) {
          if (current()) { setPhase('error'); setMessage(errText(error)); }
        }
      })();
    }
    return () => {
      generation.current += 1;
      stop();
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    };
  }, [open, platform, realm, upstreamId, region, proxy, routesReady,
      workbuddy, unsupported, retry, completed]);

  async function reviewAuthorization() {
    if (workbuddy || unsupported || reviewBusy.current || !open) return;
    const epoch = generation.current;
    const state = stateRef.current;
    reviewBusy.current = true;
    setReviewing(true);
    setCallbackError('');
    try {
      const review = await accountApi.review(state || undefined, platform, realm);
      if (generation.current !== epoch) return;
      if (review.platform !== platform || review.realm !== realm) throw new Error(t('addAccount.scopeMismatch'));
      setReviewItems(review.sessions);
      // A previous session's successful record must never complete this dialog.
      const own = state && review.sessions.find(item => item.reference === state.slice(-8));
      if (own && own.status === 'success') completed({...own, status: 'success'}, epoch);
    } catch (error) {
      if (generation.current === epoch) setCallbackError(errText(error));
    } finally {
      if (generation.current === epoch) { reviewBusy.current = false; setReviewing(false); }
    }
  }

  async function finish() {
    if (!callbackUrl.trim() || submitted.current || phase !== 'waiting') return;
    const epoch = generation.current;
    submitted.current = true;
    setPhase('submitting');
    setCallbackError('');
    try {
      const result = await accountApi.finish(stateRef.current, callbackUrl.trim(), platform, realm);
      if (generation.current !== epoch) return;
      if (result.status !== 'success' || result.platform !== platform || result.realm !== realm) {
        throw new Error(t('addAccount.scopeMismatch'));
      }
      completed(result, epoch);
    } catch (error) {
      if (generation.current !== epoch) return;
      const status = (error as {response?: {status?: number}})?.response?.status;
      setCallbackError(errText(error));
      if (status === 400) {
        submitted.current = false;
        setPhase('waiting');
      } else {
        // A transport timeout can follow a remote account write. Do not repeat it.
        setPhase('error');
        setUnconfirmed(true);
        setAuthUrl('');
        setCallbackUrl('');
        setMessage(t('addAccount.resultUnconfirmed'));
      }
    }
  }

  async function cancelTrae() {
    if (submitted.current || !stateRef.current) { onOpenChange(false); return; }
    try {
      await accountApi.cancel(stateRef.current, platform, realm);
      onOpenChange(false);
    } catch (error) { setCallbackError(errText(error)); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[440px] overflow-y-auto" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('addAccount.titlePlatform', {platform: platformName, realm: realmName})}</DialogTitle>
          <DialogDescription>
            {t(unsupported ? 'addAccount.traeGlobalUnsupported' : platform === 'qoder' ? 'addAccount.descQoder' :
              platform === 'trae' ? 'addAccount.descTrae' : realm === 'global' ? 'addAccount.descWbGlobal' : 'addAccount.descWbCn')}
          </DialogDescription>
        </DialogHeader>
        <div className="flex w-full min-w-0 flex-col items-center gap-4 px-6 pb-6">
          {workbuddy && routeError && <p role="alert" className="text-xs text-destructive">{routeError}</p>}
          {workbuddy && proxyRoutes.length > 0 && (
            <div className="w-full space-y-1.5">
              <div className="text-[11px] font-medium">{t('accounts.proxyLine')}</div>
              <Select value={proxy ? `route:${proxy}` : 'default'} disabled={!routesReady}
                onValueChange={value => setProxy(value === 'default' ? '' : value.slice(6))}>
                <SelectTrigger className="h-9 w-full rounded-full text-xs" aria-label={t('accounts.proxyLine')}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">{t('accounts.proxyDirect')}</SelectItem>
                  {proxy && !proxyRoutes.includes(proxy) && <SelectItem value={`route:${proxy}`}>{proxy} ({t('accounts.proxyMissing')})</SelectItem>}
                  {proxyRoutes.map(route => <SelectItem key={route} value={`route:${route}`}>{route}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          )}
          {workbuddy && realm === 'global' && (
            <div className="w-full space-y-1.5">
              <div className="text-[11px] font-medium">{t('addAccount.regionLabel')}</div>
              <Select value={region} onValueChange={setRegion}>
                <SelectTrigger className="h-9 w-full rounded-full text-xs" aria-label={t('addAccount.regionLabel')}>
                  <SelectValue placeholder={t('addAccount.regionPlaceholder')} />
                </SelectTrigger>
                <SelectContent>{INTERNATIONAL_REGIONS.map(code => <SelectItem key={code} value={code} className="text-xs">
                  {t('addAccount.regionOption', {label: t(`region.${code}`), code})}
                </SelectItem>)}</SelectContent>
              </Select>
              {!region && <p className="text-[11px] leading-4 text-muted-foreground">{t('addAccount.regionHint')}</p>}
            </div>
          )}
          {!unsupported && (
            <div className="grid h-[212px] w-[212px] shrink-0 place-items-center overflow-hidden rounded-2xl bg-white p-3 ring-1 ring-black/5">
              {(phase === 'loading' || phase === 'submitting') && <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />}
              {phase === 'error' && <AlertTriangle className="h-7 w-7 text-amber-500" />}
              {phase === 'success' && <CheckCircle2 className="h-9 w-9 text-emerald-500" />}
              {phase === 'waiting' && authUrl && <QRCodeSVG value={authUrl} size={188} level="M" />}
            </div>
          )}
          {phase === 'waiting' && authUrl && (
            <div className="w-full space-y-2">
              <Button asChild className="w-full rounded-full"><a href={authUrl} target="_blank" rel="noopener noreferrer"
                referrerPolicy="no-referrer" onClick={event => {
                  if (openAuthorizationWindow(authUrl)) event.preventDefault();
                }}>
                {t('addAccount.openPlatform', {platform: platformName})}<ExternalLink className="h-3.5 w-3.5" />
              </a></Button>
              <div className="flex items-center gap-2 rounded-xl border border-border/70 bg-background px-3 py-2">
                <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">https://{new URL(authUrl).hostname}</span>
                <CopyButton value={authUrl} size="sm" showLabel label={t(platform === 'trae' ? 'addAccount.copyOfficialLink' : 'addAccount.copyLink')} variant="outline" className="rounded-full" />
              </div>
              <p className="text-center text-[11px] leading-4 text-muted-foreground">{t('addAccount.privateLink')}</p>
            </div>
          )}
          {mode === 'callback' && (phase === 'waiting' || phase === 'submitting') && (
            <div className="w-full space-y-2">
              <label htmlFor="trae-auth-callback" className="text-xs font-medium">{t('addAccount.callbackLabel')}</label>
              <Textarea id="trae-auth-callback" autoComplete="off" spellCheck={false} value={callbackUrl}
                disabled={phase === 'submitting'} onChange={event => setCallbackUrl(event.target.value)}
                placeholder={t('addAccount.callbackPlaceholder')} className="min-h-24 break-all text-xs" />
              <p className="text-[11px] leading-4 text-muted-foreground">{t('addAccount.callbackHint')}</p>
              <Button className="w-full rounded-full" disabled={!callbackUrl.trim() || phase === 'submitting'} onClick={finish}>
                {phase === 'submitting' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}{t('addAccount.confirmCallback')}
              </Button>
            </div>
          )}
          <p role="status" aria-live="polite" className={'text-center text-xs leading-5 ' +
            (phase === 'error' ? 'text-destructive' : phase === 'success' ? 'text-emerald-600' : 'text-muted-foreground')}>
            {message}
          </p>
          {callbackError && <p role="alert" className="w-full break-words text-xs text-destructive">{callbackError}</p>}
          {!workbuddy && !unsupported && phase === 'error' && (
            <section aria-label={t('addAccount.reviewTitle')} className="w-full space-y-3 rounded-2xl border border-border/70 bg-muted/30 p-4">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-xs font-medium">{t('addAccount.reviewTitle')}</h3>
                <Button size="sm" variant="outline" className="h-8 rounded-full text-xs" disabled={reviewing} onClick={reviewAuthorization}>
                  {reviewing && <Loader2 className="h-3 w-3 animate-spin" />}{t('addAccount.reviewRefresh')}
                </Button>
              </div>
              {reviewItems.length > 0 && <ul className="max-h-40 space-y-2 overflow-y-auto">
                {reviewItems.map(item => <li key={item.reference} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px]">
                  <code className="text-muted-foreground">{item.reference}</code>
                  <span>{t(`addAccount.reviewState.${item.status}`)}</span>
                  {item.engine_binding !== 'current' && <span className="w-full text-muted-foreground">{t(`addAccount.reviewEngine.${item.engine_binding}`)}</span>}
                </li>)}
              </ul>}
              <p className="text-[11px] leading-4 text-muted-foreground">{t('addAccount.reviewHint')}</p>
            </section>
          )}
          <div className="flex w-full gap-2">
            <Button variant="outline" className="flex-1 rounded-full" onClick={() => onOpenChange(false)}>{t('common.close')}</Button>
            {platform === 'trae' && phase === 'waiting' && <Button variant="outline" className="flex-1 rounded-full" onClick={cancelTrae}>{t('addAccount.cancelAuthorization')}</Button>}
            {phase === 'error' && !unsupported && !unconfirmed && <Button className="flex-1 rounded-full" onClick={() => setRetry(value => value + 1)}>{t('addAccount.retry')}</Button>}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
