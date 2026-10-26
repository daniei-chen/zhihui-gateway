'use client';

/**
 * 告警中心。
 *
 * 数据来自后端 `/api/alerts`，它是**实时派生**的（不存库）：
 *   1. 上游风控（qoder2 的 /health.risk：排队 / 限流 / 拒绝 / 版本门）
 *   2. 上游不可达（readyz 的 health）
 *   3. 账号类：3 天内到期 / 余额 < 50 / 冷却·禁用·过期
 *   4. 密钥 7 天内过期
 *
 * 因为派生自实时状态，所以没有「标记已读」——告警消失就等于问题恢复了。
 * 版本（国内版/国际版）只影响账号类告警；风控与上游类是全局信号。
 */
import {useMemo} from 'react';
import {AlertTriangle, BellOff, CircleAlert, Info, RefreshCw} from 'lucide-react';

import {alertsApi} from '@/lib/api';
import type {AlertItem} from '@/lib/api';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {usePlatform} from '@/lib/platform-context';
import {useRealm} from '@/lib/realm-context';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {Skeleton} from '@/components/ui/skeleton';
import {Button} from '@/components/ui/button';
import {cn} from '@/lib/utils';

const LEVEL_META: Record<
  string,
  {ring: string; badge: string; icon: typeof AlertTriangle; label: string}
> = {
  error: {
    ring: 'border-red-500/40 bg-red-500/5',
    badge: 'bg-red-500/15 text-red-600 dark:text-red-400',
    icon: CircleAlert,
    label: '严重',
  },
  warn: {
    ring: 'border-amber-500/40 bg-amber-500/5',
    badge: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
    icon: AlertTriangle,
    label: '警告',
  },
  info: {
    ring: 'border-sky-500/40 bg-sky-500/5',
    badge: 'bg-sky-500/15 text-sky-600 dark:text-sky-400',
    icon: Info,
    label: '提示',
  },
};

const KIND_LABEL: Record<string, string> = {
  risk: '上游风控',
  upstream: '上游可用性',
  account: '账号',
  key: '密钥',
};

function AlertCard({a}: {a: AlertItem}) {
  const meta = LEVEL_META[a.level] || LEVEL_META.info;
  const Icon = meta.icon;
  return (
    <div className={cn('flex gap-3 rounded-xl border p-3.5 md:p-4', meta.ring)}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{a.title}</span>
          <span className={cn('rounded px-1.5 py-0.5 text-[11px] font-medium', meta.badge)}>
            {meta.label}
          </span>
          {a.kind && (
            <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
              {KIND_LABEL[a.kind] || a.kind}
            </span>
          )}
        </div>
        {a.message ? (
          <p className="mt-1 break-words text-xs text-muted-foreground">{a.message}</p>
        ) : null}
      </div>
    </div>
  );
}

export default function AlertsPage() {
  const {platform, label: platformLabel} = usePlatform();
  const {realm, label: realmLabel} = useRealm();
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll(
    {alerts: () => alertsApi.list(realm)},
    [platform, realm],
  );
  useHeartbeat(() => void reload(), 30000);

  const alerts: AlertItem[] = values.alerts?.alerts ?? [];
  const counts = useMemo(() => {
    const c = {error: 0, warn: 0, info: 0};
    for (const a of alerts) {
      if (a.level === 'error') c.error += 1;
      else if (a.level === 'warn') c.warn += 1;
      else c.info += 1;
    }
    return c;
  }, [alerts]);

  const platformName = platformLabel;
  const header = (
    <PageHeader
      title="告警中心"
      description={`${platformName} · ${realmLabel} —— 上游风控、账号到期与余额、密钥有效期，实时派生（每 30 秒刷新）`}
      actions={
        <Button variant="outline" size="sm" onClick={() => void reload()} disabled={isRefreshing}>
          <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', isRefreshing && 'animate-spin')} />
          刷新
        </Button>
      }
    />
  );

  if (isInitialLoading || isInitialFailed) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? (
          <LoadError variant="page" onRetry={reload} />
        ) : (
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-16 w-full rounded-xl" />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      {header}

      {errors.alerts ? <LoadError message={String(errors.alerts)} onRetry={reload} /> : null}

      <div className="grid grid-cols-3 gap-2 md:gap-3">
        {(['error', 'warn', 'info'] as const).map((lv) => {
          const meta = LEVEL_META[lv];
          const Icon = meta.icon;
          return (
            <div key={lv} className={cn('rounded-xl border p-3', meta.ring)}>
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Icon className="h-3.5 w-3.5" />
                {meta.label}
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums">{counts[lv]}</div>
            </div>
          );
        })}
      </div>

      {alerts.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed py-16 text-center">
          <BellOff className="h-8 w-8 text-muted-foreground/50" />
          <p className="text-sm font-medium">没有需要处理的告警</p>
          <p className="max-w-md text-xs text-muted-foreground">
            这里展示的是实时状态：上游风控信号、上游可达性、账号到期/低余额/异常状态、密钥 7 天内过期。
            没有任何条目说明当前这些都正常。
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {alerts.map((a) => (
            <AlertCard key={a.id} a={a} />
          ))}
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        说明：告警由实时状态派生，没有「已读」状态——条目消失即代表对应问题已恢复。
        版本切换（国内版 / 国际版）只影响账号类告警，风控与上游可用性是全局的。
      </p>
    </div>
  );
}
