'use client';

import {Bot, Cloud, Sparkles} from 'lucide-react';
import {motion} from 'motion/react';

import {usePlatform, type Platform} from '@/lib/platform-context';
import {cn} from '@/lib/utils';

/**
 * 平台切换（WorkBuddy / Qoder / Trae）。WorkBuddy 排第一：它是当前主力平台。
 *
 * 与右侧的版本切换（国内 / 国际）**并存**，两者是不同维度：
 * 平台决定连哪个上游网关，版本决定该平台下的账号属于国内站还是国际站。
 *
 * 样式与 RealmToggle 保持一致（同一套 pill + layoutId 弹簧滑动）。
 */
export function PlatformToggle({compact = false}: {compact?: boolean}) {
  const {platform, setPlatform} = usePlatform();
  const options: {id: Platform; label: string; title: string; icon: typeof Bot}[] = [
    {
      id: 'workbuddy',
      label: 'WorkBuddy',
      title: 'WorkBuddy 平台（只读合并：账号 / 模型 / 用量 / 日志来自 workbuddy2api）',
      icon: Cloud,
    },
    {id: 'qoder', label: 'Qoder', title: 'Qoder 平台（账号池 / 模型 / 用量）', icon: Sparkles},
    {id: 'trae', label: 'Trae', title: 'Trae 平台（账号池 / 模型 / 用量）', icon: Bot},
  ];

  return (
    <div
      className="inline-flex items-center gap-0.5 rounded-full border border-border/60 bg-muted/60 p-0.5"
      role="tablist"
      aria-label="平台切换"
    >
      {options.map(({id, label, title, icon: Icon}) => {
        const active = platform === id;
        return (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={active}
            title={title}
            onClick={() => setPlatform(id)}
            className={cn(
              'relative flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors',
              active ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {active && (
              <motion.span
                layoutId="platform-pill"
                className="absolute inset-0 rounded-full bg-background shadow-sm"
                transition={{type: 'spring', stiffness: 400, damping: 32}}
              />
            )}
            <Icon className="relative h-3 w-3 shrink-0" />
            {!compact && <span className="relative">{label}</span>}
            {compact && <span className="sr-only">{label}</span>}
          </button>
        );
      })}
    </div>
  );
}
