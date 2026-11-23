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
import {useCallback, useEffect, useState} from 'react';
import {Activity, Download, X} from 'lucide-react';
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
import {trafficApi, type TrafficRecord, type TrafficRecordSummary, type TrafficStatus} from '@/lib/api';
import {fmtDateTime, fmtLatency, fmtNumber} from '@/lib/format';
import {useT} from '@/lib/i18n/provider';

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
  const [status, setStatus] = useState<TrafficStatus | null>(null);
  const [records, setRecords] = useState<TrafficRecordSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<TrafficRecord | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, listing] = await Promise.all([trafficApi.status(), trafficApi.list(100)]);
      setStatus(nextStatus);
      setRecords(listing.records);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const toggle = async () => {
    if (!status) return;
    setBusy(true);
    try {
      await trafficApi.setEnabled(!status.enabled);
      // 开关有 5 秒缓存（热路径不查库），稍等再读状态，否则显示的还是旧值。
      await new Promise((resolve) => setTimeout(resolve, 300));
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const openDetail = async (id: string) => {
    setDetailError(null);
    try {
      setDetail(await trafficApi.read(id));
    } catch (exc) {
      setDetailError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  if (error && !records) {
    return <LoadError message={error} onRetry={() => void refresh()} />;
  }

  const enabled = status?.enabled ?? false;

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
        <div className="ml-auto">
          <Button size="sm" variant={enabled ? 'outline' : 'default'} disabled={busy} onClick={() => void toggle()}>
            {enabled ? t('traffic.disable') : t('traffic.enable')}
          </Button>
        </div>
      </div>

      {records === null ? (
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

      <Drawer open={detail !== null || detailError !== null}
              onOpenChange={(open) => { if (!open) { setDetail(null); setDetailError(null); } }}>
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
              <Button size="sm" variant="ghost" onClick={() => { setDetail(null); setDetailError(null); }}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          </DrawerHeader>

          <div className="space-y-5 overflow-y-auto px-4 pb-6">
            {detailError ? <LoadError message={detailError} /> : null}
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
