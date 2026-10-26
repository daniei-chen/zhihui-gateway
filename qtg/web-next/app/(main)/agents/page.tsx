'use client';

/**
 * 智能体配置。
 *
 * 作用是把本机已安装的 AI 客户端（Claude Code / Codex / Crush / DSH …）一键指向本网关，
 * 并可原样还原。能力由 **qoder2 引擎**提供（它有本机客户端探测 + 写入器）；
 * trae 引擎（aiwork-server）没有等价能力，页面上会如实说明。
 *
 * 注意：写入的是**服务器本机**的客户端配置。如果这些 CLI 装在你自己电脑上，
 * 应该用「API 密钥」页导出配置片段，而不是在这里点应用。
 */
import {useState} from 'react';
import {Bot, Check, RotateCcw, Terminal, Zap} from 'lucide-react';

import {agentsApi, errText} from '@/lib/api';
import type {AgentClient} from '@/lib/api';
import {useAsyncAll} from '@/lib/use-async-data';
import {usePlatform} from '@/lib/platform-context';
import {notify} from '@/lib/toast';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {Skeleton} from '@/components/ui/skeleton';
import {Button} from '@/components/ui/button';
import {cn} from '@/lib/utils';

function ProtocolBadge({p}: {p: string}) {
  const map: Record<string, {label: string; cls: string}> = {
    anthropic: {label: 'Anthropic', cls: 'bg-orange-500/15 text-orange-600 dark:text-orange-400'},
    openai: {label: 'OpenAI', cls: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'},
  };
  const m = map[p] || {label: p || '未知', cls: 'bg-muted text-muted-foreground'};
  return <span className={cn('rounded px-1.5 py-0.5 text-[11px] font-medium', m.cls)}>{m.label}</span>;
}

function ClientCard({
  c,
  busy,
  onApply,
  onRestore,
}: {
  c: AgentClient;
  busy: boolean;
  onApply: () => void;
  onRestore: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Terminal className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="truncate text-sm font-medium">{c.label}</span>
          <ProtocolBadge p={c.protocol} />
        </div>
        <span
          className={cn(
            'shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium',
            c.configured
              ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
              : 'bg-muted text-muted-foreground',
          )}
        >
          {c.configured ? '已接入' : '未接入'}
        </span>
      </div>

      <p className="text-xs text-muted-foreground">{c.desc}</p>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>
          安装：<strong className={c.installed ? 'text-emerald-600' : ''}>{c.installed ? '是' : '否'}</strong>
        </span>
        {c.base_url ? <span className="truncate">地址：{c.base_url}</span> : null}
        {c.model ? <span>模型：{c.model}</span> : null}
        {c.applied_at ? <span>接入于 {c.applied_at}</span> : null}
      </div>

      <div className="flex gap-2">
        <Button size="sm" disabled={busy || !c.installed} onClick={onApply}>
          <Zap className="mr-1.5 h-3.5 w-3.5" />
          一键接入
        </Button>
        <Button size="sm" variant="outline" disabled={busy || !c.configured} onClick={onRestore}>
          <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
          还原
        </Button>
      </div>
      {!c.installed ? (
        <p className="text-[11px] text-muted-foreground">
          本机没装这个客户端，所以不能一键写入（装了之后刷新即可操作）。
        </p>
      ) : null}
    </div>
  );
}

export default function AgentsPage() {
  const {platform} = usePlatform();
  const [busy, setBusy] = useState<string>('');
  const {values, errors, isInitialLoading, isInitialFailed, reload} = useAsyncAll(
    {agents: () => agentsApi.list()},
    [platform],
  );

  const data = values.agents;
  const clients: AgentClient[] = data?.clients ?? [];

  async function run(kind: 'apply' | 'restore', c: AgentClient) {
    setBusy(c.id);
    try {
      const res = kind === 'apply' ? await agentsApi.apply(c.id) : await agentsApi.restore(c.id);
      if (res.ok) {
        notify.ok(kind === 'apply' ? `${c.label} 已接入` : `${c.label} 已还原`);
        await reload();
      } else {
        notify.err(res.detail || `${c.label} 操作失败`);
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  const header = (
    <PageHeader
      title="智能体配置"
      description="把服务器本机的 AI 客户端一键指向本网关（可原样还原）；数据由 qoder2 引擎探测"
    />
  );

  if (isInitialLoading || isInitialFailed) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? (
          <LoadError variant="page" onRetry={reload} />
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-40 w-full rounded-xl" />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      {header}

      {errors.agents ? <LoadError message={String(errors.agents)} onRetry={reload} /> : null}

      {!data?.available ? (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed py-14 text-center">
          <Bot className="h-8 w-8 text-muted-foreground/50" />
          <p className="text-sm font-medium">当前平台不支持智能体一键配置</p>
          <p className="max-w-lg text-xs text-muted-foreground">
            {data?.note || '这个能力由 qoder2 引擎提供；trae 引擎没有等价的写入器。'}
          </p>
        </div>
      ) : clients.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed py-14 text-center">
          <Bot className="h-8 w-8 text-muted-foreground/50" />
          <p className="text-sm font-medium">没有检测到可配置的客户端</p>
          <p className="max-w-lg text-xs text-muted-foreground">
            在服务器上装好 Claude Code / Codex 等 CLI 后刷新，这里会出现对应卡片。
            如果客户端在你自己的电脑上，请到「API 密钥」页导出配置片段。
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {clients.map((c) => (
            <ClientCard
              key={c.id}
              c={c}
              busy={busy === c.id}
              onApply={() => void run('apply', c)}
              onRestore={() => void run('restore', c)}
            />
          ))}
        </div>
      )}

      <div className="rounded-xl border bg-muted/30 p-3.5 text-xs text-muted-foreground">
        <div className="mb-1 flex items-center gap-1.5 font-medium text-foreground">
          <Check className="h-3.5 w-3.5" />
          写入范围说明
        </div>
        这里改的是「服务器本机」的客户端配置文件（写入前 qoder2 会自动备份，可随时还原）。
        如果客户端装在你自己电脑上，应该用「API 密钥」页导出配置片段，而不是在这里点应用。
      </div>
    </div>
  );
}
