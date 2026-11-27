'use client';

/**
 * 调用轨迹（抓包）。
 *
 * 这一页要回答的问题只有一个：**用户说「模型答得不对」时，当时到底发生了什么。**
 * 所以详情按「客户端发了什么 / 网关改成了什么 / 差异 / 上游回了什么」四段排，
 * 而不是把三个 JSON 平铺在那儿让人自己比对。
 *
 * 关于隐私：正文属于用户内容，抓包默认关闭；开启后也只存本机、按保留期清理。
 * 页面顶部常驻这条说明 —— 开关就在旁边，不写清楚等于诱导别人打开它。
 */
import {useCallback, useEffect, useRef, useState} from 'react';
import {Activity, Download, RefreshCw, X} from 'lucide-react';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Drawer, DrawerContent, DrawerHeader, DrawerTitle,
} from '@/components/ui/drawer';
import {trafficApi, type TrafficRecord} from '@/lib/api';
import {fmtDateTime, fmtLatency, fmtNumber} from '@/lib/format';
import {useT} from '@/lib/i18n/provider';
import {useAsyncAll} from '@/lib/use-async-data';
import {useAuth} from '@/lib/auth-context';

const STATUS_TONE = (status: number | null) => {
  if (status === null || status === undefined) return 'secondary' as const;
  if (status >= 500) return 'destructive' as const;
  if (status >= 400) return 'outline' as const;
  return 'default' as const;
};

const pretty = (value: unknown) => {
  try {
    return JSON.stringify(value ?? null, null, 2);
  } catch {
    return String(value);
  }
};

/** 只读的 JSON 块。等宽、可滚动，不解析成表单 —— 这里要的是「原样」。 */
function JsonBlock({value}: {value: unknown}) {
  return (
    <pre className="max-h-[420px] overflow-auto rounded-md bg-muted/40 p-3 text-[12px] leading-relaxed">
      {pretty(value)}
    </pre>
  );
}

export default function TrafficPage() {
  const t = useT();
  const {me} = useAuth();
  const [detail, setDetail] = useState<TrafficRecord | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // 筛选只读服务端索引，切换时不必重新拉状态；`refresh` 依赖它，所以要进 useCallback 的依赖表。
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [sinceFilter, setSinceFilter] = useState<string>('');
  const [detailError, setDetailError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toggleFlight = useRef<symbol | null>(null);
  const detailFlight = useRef<AbortController | null>(null);
  const statusRead = useAsyncAll({status: () => trafficApi.status()}, [me?.username, me?.role]);
  const listingRead = useAsyncAll({listing: () => trafficApi.list(100, {
    ...(statusFilter ? {status: statusFilter} : {}),
    ...(sinceFilter ? {since: sinceFilter} : {}),
  })}, [me?.username, me?.role, statusFilter, sinceFilter]);
  const status = statusRead.values.status ?? null;
  const records = listingRead.values.listing?.records ?? null;
  const error = listingRead.errors.listing;
  const statusError = mutationError ?? statusRead.errors.status;
  const refreshStatus = statusRead.refresh;
  const refreshListing = listingRead.refresh;
  const reloadStatus = statusRead.reload;
  const reloadListing = listingRead.reload;
  const refresh = useCallback(() => Promise.all([refreshStatus(), refreshListing()]),
    [refreshStatus, refreshListing]);
  const retryStatus = useCallback(() => {
    setMutationError(null);
    return reloadStatus();
  }, [reloadStatus]);
  const reload = useCallback(() => Promise.all([retryStatus(), reloadListing()]),
    [retryStatus, reloadListing]);
  const closeDetail = useCallback(() => {
    detailFlight.current?.abort();
    detailFlight.current = null;
    setSelectedId(null);
    setDetail(null);
    setDetailError(null);
  }, []);
  useEffect(() => {
    closeDetail();
    setBusy(false);
    setMutationError(null);
    return () => {
      detailFlight.current?.abort();
      detailFlight.current = null;
      toggleFlight.current = null;
    };
  }, [me?.username, me?.role, closeDetail]);

  // 自动刷新：这一页的典型用法是「我刚发了一次请求，看看它怎么了」。
  // 只加载一次的话，新记录永远不会出现，第一反应是「抓包没生效」。
  // 打开详情时暂停轮询，免得正在读的内容被刷新打断。
  const [autoRefresh, setAutoRefresh] = useState(true);
  useEffect(() => {
    if (!autoRefresh || selectedId !== null) return;
    // 标签页在后台时不要拉：开着流量页放一晚上，会白跑几千次请求，
    // 每次都要服务端扫一遍索引。切回前台立刻补一次，不让人盯着空屏等 10 秒。
    const tick = () => {
      if (document.hidden || toggleFlight.current) return;
      void refresh();
    };
    const timer = setInterval(tick, 10000);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [autoRefresh, selectedId, refresh]);

  const toggle = async () => {
    if (!status || toggleFlight.current) return;
    const flight = Symbol();
    toggleFlight.current = flight;
    setBusy(true);
    setMutationError(null);
    try {
      await trafficApi.setEnabled(!(status.desired ?? status.setting ?? status.enabled));
      if (toggleFlight.current === flight) await reloadStatus();
    } catch (exc) {
      if (toggleFlight.current === flight) {
        setMutationError(exc instanceof Error ? exc.message : String(exc));
      }
    } finally {
      if (toggleFlight.current === flight) {
        toggleFlight.current = null;
        setBusy(false);
      }
    }
  };

  const openDetail = async (id: string) => {
    detailFlight.current?.abort();
    const controller = new AbortController();
    detailFlight.current = controller;
    setSelectedId(id);
    setDetail(null);
    setDetailError(null);
    try {
      const next = await trafficApi.read(id, controller.signal);
      if (detailFlight.current !== controller || controller.signal.aborted) return;
      if (next.request_id !== id) throw new Error(t('state.partialFailed'));
      setDetail(next);
    } catch (exc) {
      if (detailFlight.current === controller && !controller.signal.aborted) {
        setDetailError(exc instanceof Error ? exc.message : String(exc));
      }
    }
  };

  const enabled = status?.effective ?? status?.enabled ?? false;
  const desired = status?.desired ?? status?.setting ?? enabled;

  return (
    <div className="space-y-6">
      <PageHeader title={t('traffic.title')} description={t('traffic.subtitle')} />

      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border/60 bg-muted/30 px-4 py-3 text-sm">
        <Badge variant={enabled ? 'default' : 'secondary'}>
          {enabled ? t('traffic.on') : t('traffic.off')}
        </Badge>
        <span className="text-muted-foreground">
          {t('traffic.retention', {hours: status?.retention_hours ?? 24})}
        </span>
        {status ? (
          <span className="text-muted-foreground">
            {t('traffic.records', {count: fmtNumber(records?.length ?? 0)})}
          </span>
        ) : null}
        {status && !enabled && status.setting ? (
          <span className="text-destructive">{t('traffic.blockedByEnv')}</span>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void reload()}>
            <RefreshCw className="mr-1 h-4 w-4" />{t('traffic.refresh')}
          </Button>
          <Button size="sm" variant={autoRefresh ? 'secondary' : 'outline'}
                  onClick={() => setAutoRefresh(!autoRefresh)}>
            {autoRefresh ? t('traffic.autoOn') : t('traffic.autoOff')}
          </Button>
          <Button size="sm" variant={desired ? 'outline' : 'default'} disabled={busy || !status} onClick={() => void toggle()}>
            {desired ? t('traffic.disable') : t('traffic.enable')}
          </Button>
        </div>
      </div>

      {statusError ? <LoadError message={String(statusError)} onRetry={() => void retryStatus()} /> : null}
      {error ? <LoadError message={String(error)} onRetry={() => void reloadListing()} /> : null}

      {/*
        筛选：记录上限 5000 条 / 24 小时，不能筛的时候「刚才那次失败」只能一页页
        翻。只放最有用的两个维度 —— 状态与时间窗，够定位到那一条。
      */}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">{t('traffic.filterStatus')}</span>
        {(['', 'error', 'ok'] as const).map((value) => (
          <Button key={value || 'all'} size="sm"
                  variant={statusFilter === value ? 'default' : 'outline'}
                  onClick={() => setStatusFilter(value)}>
            {value === '' ? t('traffic.filterAll') : value === 'error'
              ? t('traffic.filterError') : t('traffic.filterOk')}
          </Button>
        ))}
        <span className="ml-3 text-muted-foreground">{t('traffic.filterSince')}</span>
        {(['', '15m', '1h', '6h'] as const).map((value) => (
          <Button key={value || 'all-time'} size="sm"
                  variant={sinceFilter === value ? 'default' : 'outline'}
                  onClick={() => setSinceFilter(value)}>
            {value === '' ? t('traffic.filterAll') : value}
          </Button>
        ))}
        {statusFilter || sinceFilter ? (
          <Button size="sm" variant="ghost"
                  onClick={() => { setStatusFilter(''); setSinceFilter(''); }}>
            {t('traffic.filterClear')}
          </Button>
        ) : null}
      </div>

      {records === null ? error ? null : (
        <div className="space-y-2">
          <SkeletonBar /><SkeletonBar /><SkeletonBar />
        </div>
      ) : records.length === 0 ? (
        <EmptyState icon={Activity}
                    title={t('traffic.empty')}
                    description={enabled ? t('traffic.emptyHint') : t('traffic.disabledHint')} />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('traffic.colTime')}</TableHead>
              <TableHead>{t('traffic.colModel')}</TableHead>
              <TableHead>{t('traffic.colStatus')}</TableHead>
              <TableHead>{t('traffic.colLatency')}</TableHead>
              <TableHead>{t('traffic.colKey')}</TableHead>
              <TableHead className="text-right">{t('traffic.colActions')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {records.map((row) => (
              <TableRow key={row.request_id} className="cursor-pointer"
                        onClick={() => void openDetail(row.request_id)}>
                <TableCell className="whitespace-nowrap">{fmtDateTime(row.ts)}</TableCell>
                <TableCell>
                  <span className="font-mono text-[12px]">{row.model ?? '—'}</span>
                  {row.mapped && row.mapped !== row.model ? (
                    <span className="ml-1 text-muted-foreground">→ {row.mapped}</span>
                  ) : null}
                </TableCell>
                <TableCell>
                  <Badge variant={STATUS_TONE(row.status)}>{row.status ?? '—'}</Badge>
                  {row.stream ? <span className="ml-1 text-[11px] text-muted-foreground">stream</span> : null}
                </TableCell>
                <TableCell>{row.latency_ms != null ? fmtLatency(row.latency_ms) : '—'}</TableCell>
                <TableCell className="text-muted-foreground">{row.key ?? '—'}</TableCell>
                <TableCell className="text-right">
                  {row.truncated ? (
                    <span className="mr-2 text-[11px] text-muted-foreground">{t('traffic.truncated')}</span>
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Drawer open={selectedId !== null}
              onOpenChange={(open) => { if (!open) closeDetail(); }}>
        <DrawerContent className="max-w-3xl">
          <DrawerHeader className="flex flex-row items-start justify-between gap-4">
            <div>
              <DrawerTitle>{t('traffic.detail')}</DrawerTitle>
              {detail ? (
                <p className="mt-1 font-mono text-[12px] text-muted-foreground">
                  {detail.request_id} ｜ {detail.path} ｜ {detail.status} ｜ {detail.latency_ms != null ? fmtLatency(detail.latency_ms) : '—'}
                </p>
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              {detail ? (
                <Button size="sm" variant="outline" asChild>
                  <a href={trafficApi.exportUrl(detail.request_id)} target="_blank" rel="noreferrer">
                    <Download className="mr-1 h-4 w-4" />{t('traffic.export')}
                  </a>
                </Button>
              ) : null}
              <Button size="sm" variant="ghost" aria-label={t('common.close')} onClick={closeDetail}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          </DrawerHeader>

          <div className="space-y-5 overflow-y-auto px-4 pb-6">
            {selectedId !== null && !detail && !detailError ? <SkeletonBar /> : null}
            {detailError ? <LoadError message={detailError} /> : null}
            {detail?.truncated_fields?.length ? (
              // 正文太长时网关只保留头尾，中间那段是真的没存下来。
              // 不说明的话，用户会以为请求本来就这么短 —— 那样比不记录更误导。
              <div className="rounded-md border border-amber-300/60 bg-amber-50/60 px-3 py-2 text-[12px] leading-relaxed text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
                <p className="font-medium">{t('traffic.truncatedHint')}</p>
                <p className="mt-1 font-mono text-[11px] break-all">
                  {detail.truncated_fields.join('、')}
                </p>
              </div>
            ) : null}
            {detail ? (
              <>
                <section>
                  <h3 className="mb-2 text-sm font-medium">{t('traffic.client')}</h3>
                  <JsonBlock value={detail.client} />
                </section>
                <section>
                  <h3 className="mb-2 text-sm font-medium">{t('traffic.gateway')}</h3>
                  <JsonBlock value={detail.gateway} />
                </section>
                <section>
                  <h3 className="mb-2 text-sm font-medium">{t('traffic.diff')}</h3>
                  {(detail.diff ?? []).length === 0 ? (
                    <p className="text-sm text-muted-foreground">{t('traffic.noDiff')}</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>{t('traffic.diffPath')}</TableHead>
                          <TableHead>{t('traffic.diffBefore')}</TableHead>
                          <TableHead>{t('traffic.diffAfter')}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {(detail.diff ?? []).map((item, index) => (
                          <TableRow key={`${item.path}-${index}`}>
                            <TableCell className="font-mono text-[12px]">{item.path}</TableCell>
                            <TableCell className="font-mono text-[12px]">{pretty(item.before)}</TableCell>
                            <TableCell className="font-mono text-[12px]">{pretty(item.after)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </section>
                <section>
                  <h3 className="mb-2 text-sm font-medium">{t('traffic.response')}</h3>
                  {/*
                    模型实际生成的内容单独渲染：它是「为什么这么答」唯一能直接读的
                    证据，塞进 JSON 里看不出来。其余字段（终止原因、用量、错误）仍然
                    走 JSON 视图。
                  */}
                  {typeof (detail.response as {text?: unknown})?.text === 'string' ? (
                    <div className="mb-3">
                      <p className="mb-1 text-[12px] text-muted-foreground">{t('traffic.modelText')}</p>
                      <pre className="max-h-[320px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 text-[12px] leading-relaxed">
                        {(detail.response as {text: string}).text}
                      </pre>
                    </div>
                  ) : null}
                  {typeof (detail.response as {reasoning?: unknown})?.reasoning === 'string' ? (
                    <div className="mb-3">
                      <p className="mb-1 text-[12px] text-muted-foreground">{t('traffic.modelReasoning')}</p>
                      <pre className="max-h-[240px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/20 p-3 text-[12px] leading-relaxed text-muted-foreground">
                        {(detail.response as {reasoning: string}).reasoning}
                      </pre>
                    </div>
                  ) : null}
                  <JsonBlock value={detail.response} />
                </section>
              </>
            ) : null}
          </div>
        </DrawerContent>
      </Drawer>
    </div>
  );
}
